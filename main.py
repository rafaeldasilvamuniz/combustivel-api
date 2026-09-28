# backend/main.py
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup
import pandas as pd
import os
import re
import time
import glob
import unicodedata
import json
import hashlib
from datetime import datetime
from typing import Optional
from threading import Lock
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

app = FastAPI(title="Combustíveis ANP API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================== CONFIGURAÇÕES ========================
PAGINA_ANP = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/serie-historica-de-precos-de-combustiveis"
CSV_DATA_DIR = "data"
OCM_API_KEY = "d75c2b4f-371d-4514-9f57-fbe8330538fa"
OCM_BASE_URL = "https://api.openchargemap.io/v3"
PETROBRAS_CACHE = {}
PETROBRAS_CACHE_TTL = 3600
INTERVALO_VERIFICACAO_HORAS = 6
DIAS_PARA_CONSIDERAR_ANTIGO = 30

# APIs de Dados Abertos
BRASILAPI_CNPJ = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
BRASILAPI_CEP = "https://brasilapi.com.br/api/cep/v2/{cep}"

# Cache local de geolocalização
GEO_CACHE_DIR = os.path.join(CSV_DATA_DIR, "geo_cache")
GEO_CACHE_TTL_DIAS = 90

# Controle de rate limit para BrasilAPI
_rate_lock = Lock()
_ultimo_request_brasilapi = [0.0]
BRASILAPI_DELAY = 0.5

HEADERS = {
    "User-Agent": "CombustiveisANP/1.0 (contato@exemplo.com)"
}

PRODUTO_PALAVRAS = {
    "gasolina": ["gasolina"],
    "etanol": ["etanol"],
    "diesel": ["diesel"],
    "gnv": ["gnv"],
    "glp": ["glp"],
}

os.makedirs(GEO_CACHE_DIR, exist_ok=True)


# ======================== UTILITÁRIOS ========================
def normalizar_texto(texto):
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.upper().strip()


def normalizar_cnpj(cnpj) -> str:
    """Remove tudo que não é dígito."""
    if not cnpj:
        return ""
    return re.sub(r"\D", "", str(cnpj))


def _geo_cache_path(chave: str) -> str:
    h = hashlib.md5(chave.encode("utf-8")).hexdigest()
    return os.path.join(GEO_CACHE_DIR, f"{h}.json")


def _ler_geo_cache(chave: str) -> Optional[dict]:
    caminho = _geo_cache_path(chave)
    if not os.path.exists(caminho):
        return None
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados = json.load(f)
        if time.time() - dados.get("_ts", 0) > GEO_CACHE_TTL_DIAS * 86400:
            return None
        return dados
    except Exception:
        return None


def _salvar_geo_cache(chave: str, dados: Optional[dict]):
    caminho = _geo_cache_path(chave)
    payload = dict(dados) if dados else {}
    payload["_ts"] = time.time()
    try:
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Erro ao salvar geo cache: {e}")


def _respeitar_rate_limit():
    with _rate_lock:
        agora = time.time()
        delta = agora - _ultimo_request_brasilapi[0]
        if delta < BRASILAPI_DELAY:
            time.sleep(BRASILAPI_DELAY - delta)
        _ultimo_request_brasilapi[0] = time.time()


# ======================== DEDUPLICAÇÃO POR CNPJ ========================
def deduplicar_por_cnpj(postos: list) -> list:
    """
    Remove duplicatas mantendo UM registro por CNPJ.
    Se CNPJ estiver vazio, usa (revenda + endereco + bairro) como chave.
    Em caso de preços diferentes para o mesmo CNPJ (coletas de semanas
    diferentes no CSV da ANP), mantém o MENOR preço.
    """
    if not postos:
        return []

    mapa = {}
    removidos = 0

    for p in postos:
        if not isinstance(p, dict):
            continue

        cnpj = normalizar_cnpj(p.get("cnpj", ""))
        if cnpj:
            chave = f"cnpj:{cnpj}"
        else:
            chave = (
                "nm:"
                f"{normalizar_texto(p.get('revenda', ''))}|"
                f"{normalizar_texto(p.get('endereco', ''))}|"
                f"{normalizar_texto(p.get('bairro', ''))}"
            )

        preco = p.get("preco")

        if chave not in mapa:
            mapa[chave] = p
        else:
            removidos += 1
            atual = mapa[chave]
            preco_atual = atual.get("preco")
            if isinstance(preco, (int, float)) and (
                not isinstance(preco_atual, (int, float)) or preco < preco_atual
            ):
                mapa[chave] = p

    resultado = list(mapa.values())
    print(f"🧹 Deduplicação: {len(postos)} → {len(resultado)} (removidos: {removidos})")
    return resultado


# ======================== GEOLOCALIZAÇÃO (Opção 7) ========================
def obter_endereco_por_cnpj(cnpj: str) -> Optional[dict]:
    cnpj_limpo = normalizar_cnpj(cnpj)
    if len(cnpj_limpo) != 14:
        return None

    chave_cache = f"cnpj_{cnpj_limpo}"
    cached = _ler_geo_cache(chave_cache)
    if cached is not None:
        return cached if cached.get("cep") else None

    try:
        _respeitar_rate_limit()
        url = BRASILAPI_CNPJ.format(cnpj=cnpj_limpo)
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 404:
            _salvar_geo_cache(chave_cache, None)
            return None
        r.raise_for_status()
        dados = r.json()

        resultado = {
            "razao_social": dados.get("razao_social"),
            "nome_fantasia": dados.get("nome_fantasia"),
            "logradouro": dados.get("logradouro"),
            "numero": dados.get("numero"),
            "complemento": dados.get("complemento"),
            "bairro": dados.get("bairro"),
            "municipio": dados.get("municipio"),
            "uf": dados.get("uf"),
            "cep": dados.get("cep"),
        }

        _salvar_geo_cache(chave_cache, resultado)
        return resultado

    except Exception as e:
        print(f"⚠️ Erro BrasilAPI CNPJ ({cnpj_limpo}): {e}")
        return None


def obter_coordenadas_por_cep(cep: str) -> Optional[dict]:
    cep_limpo = re.sub(r"\D", "", str(cep or ""))
    if len(cep_limpo) != 8:
        return None

    chave_cache = f"cep_{cep_limpo}"
    cached = _ler_geo_cache(chave_cache)
    if cached is not None:
        return cached if cached.get("latitude") else None

    try:
        _respeitar_rate_limit()
        url = BRASILAPI_CEP.format(cep=cep_limpo)
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 404:
            _salvar_geo_cache(chave_cache, None)
            return None
        r.raise_for_status()
        dados = r.json()

        coords = dados.get("location", {}).get("coordinates", {})
        lat = coords.get("latitude")
        lon = coords.get("longitude")

        if lat is not None and lon is not None:
            resultado = {
                "latitude": float(lat),
                "longitude": float(lon),
                "endereco_cep": dados.get("street"),
                "bairro_cep": dados.get("neighborhood"),
                "cidade_cep": dados.get("city"),
                "uf_cep": dados.get("state"),
            }
            _salvar_geo_cache(chave_cache, resultado)
            return resultado

        _salvar_geo_cache(chave_cache, None)
        return None

    except Exception as e:
        print(f"⚠️ Erro BrasilAPI CEP ({cep_limpo}): {e}")
        return None


def geolocalizar_posto_por_cnpj(cnpj: str, endereco_csv: str = "", bairro_csv: str = "",
                                 municipio_csv: str = "", uf_csv: str = "") -> Optional[dict]:
    dados_receita = obter_endereco_por_cnpj(cnpj)

    cep = None
    fonte_endereco = None

    if dados_receita and dados_receita.get("cep"):
        cep = dados_receita["cep"]
        fonte_endereco = "receita_federal"
    elif endereco_csv:
        match = re.search(r"\d{5}-?\d{3}", endereco_csv)
        if match:
            cep = match.group()
            fonte_endereco = "csv_anp"

    if cep:
        geo = obter_coordenadas_por_cep(cep)
        if geo:
            geo["nivel_precisao"] = "cnpj_receita_cep"
            geo["fonte_endereco"] = fonte_endereco
            geo["cep_encontrado"] = cep
            return geo

    return None


def enriquecer_postos_com_geo(postos: list, limite: int = None) -> list:
    """
    Enriquece postos com lat/lon SEM REMOVER nenhum.
    'limite' controla apenas quantos recebem geolocalização
    (os demais ficam com latitude/longitude = None, mas PERMANECEM na lista).
    """
    if not postos:
        return []

    total = len(postos)
    n_geo = min(limite, total) if limite else total
    print(f"📍 Geolocalização: {n_geo}/{total} postos serão geocodificados")

    for i, posto in enumerate(postos):
        if i < n_geo:
            geo = geolocalizar_posto_por_cnpj(
                cnpj=posto.get("cnpj", ""),
                endereco_csv=posto.get("endereco", ""),
                bairro_csv=posto.get("bairro", ""),
                municipio_csv=posto.get("_municipio_original", ""),
                uf_csv=posto.get("_uf_original", ""),
            )
            if geo:
                posto["latitude"] = geo["latitude"]
                posto["longitude"] = geo["longitude"]
                posto["geo_fonte"] = geo.get("fonte_endereco", "desconhecida")
                posto["geo_precisao"] = geo.get("nivel_precisao")
                posto["geo_cep"] = geo.get("cep_encontrado")
            else:
                posto["latitude"] = None
                posto["longitude"] = None
                posto["geo_fonte"] = None
                posto["geo_precisao"] = None
                posto["geo_cep"] = None
        else:
            posto["latitude"] = None
            posto["longitude"] = None
            posto["geo_fonte"] = None
            posto["geo_precisao"] = None
            posto["geo_cep"] = None

    return postos


# ======================== FUNÇÕES ANP (CSV) ========================
def gerar_nome_csv(produto="gasolina"):
    agora = datetime.now()
    data_str = agora.strftime("%d%m%Y_%H%M")
    return os.path.join(CSV_DATA_DIR, f"precos_anp_{produto.lower()}_{data_str}.csv")


def obter_csv_mais_recente(produto=None):
    if not os.path.exists(CSV_DATA_DIR):
        return None
    if produto:
        padrao = os.path.join(CSV_DATA_DIR, f"precos_anp_{produto.lower()}_*.csv")
    else:
        padrao = os.path.join(CSV_DATA_DIR, "precos_anp_*.csv")
    arquivos = glob.glob(padrao)
    if not arquivos:
        return None
    arquivos.sort(key=os.path.getmtime, reverse=True)
    return arquivos[0]


def listar_csvs_antigos(dias=DIAS_PARA_CONSIDERAR_ANTIGO):
    if not os.path.exists(CSV_DATA_DIR):
        return []
    arquivos = glob.glob(os.path.join(CSV_DATA_DIR, "precos_anp_*.csv"))
    agora = time.time()
    antigos = []
    por_produto = {}
    for caminho in arquivos:
        nome = os.path.basename(caminho)
        partes = nome.replace("precos_anp_", "").replace(".csv", "").split("_")
        produto = partes[0] if partes else "desconhecido"
        if produto not in por_produto:
            por_produto[produto] = []
        por_produto[produto].append(caminho)
    for produto, lista in por_produto.items():
        lista.sort(key=os.path.getmtime, reverse=True)
        for caminho in lista[1:]:
            idade_dias = (agora - os.path.getmtime(caminho)) / 86400
            if idade_dias >= dias:
                antigos.append({
                    "arquivo": caminho,
                    "nome": os.path.basename(caminho),
                    "produto": produto,
                    "idade_dias": round(idade_dias, 1),
                    "tamanho_bytes": os.path.getsize(caminho),
                    "modificado_em": datetime.fromtimestamp(os.path.getmtime(caminho)).isoformat(),
                })
    return antigos


def apagar_csv(caminho):
    try:
        if os.path.exists(caminho):
            os.remove(caminho)
            return True
        return False
    except Exception as e:
        print(f"❌ Erro: {e}")
        return False


@app.get("/")
def health():
    return {"status": "ok", "service": "combustivel"}


def encontrar_link_csv_anp(produto="gasolina"):
    try:
        resposta = requests.get(PAGINA_ANP, headers=HEADERS, timeout=30)
        resposta.raise_for_status()
        soup = BeautifulSoup(resposta.content, "html.parser")
        todos_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            texto = a.get_text(strip=True).lower()
            if href.lower().endswith((".csv", ".zip")):
                if href.startswith("/"):
                    href = "https://www.gov.br" + href
                elif not href.startswith("http"):
                    href = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/" + href
                todos_links.append({"url": href, "texto": texto})
        if not todos_links:
            raise Exception("Nenhum arquivo.")
        palavras = PRODUTO_PALAVRAS.get(produto.lower(), [produto.lower()])
        def score(link):
            url_lower = link["url"].lower()
            texto_lower = link["texto"]
            p = 0
            for palavra in palavras:
                if palavra in url_lower: p += 20
                if palavra in texto_lower: p += 20
            if "ultimas-4-semanas" in url_lower: p += 10
            return p
        links_ordenados = sorted(todos_links, key=score, reverse=True)
        return links_ordenados[0]["url"]
    except Exception as e:
        print(f"❌ Erro: {e}")
        return None


@app.get("/api/baixar-csv")
def baixar_csv(tipo: str = "gasolina"):
    try:
        os.makedirs(CSV_DATA_DIR, exist_ok=True)
        csv_url = encontrar_link_csv_anp(tipo)
        if not csv_url:
            raise Exception(f"CSV não encontrado para {tipo}.")
        r = requests.get(csv_url, headers=HEADERS, timeout=180, stream=True)
        r.raise_for_status()
        caminho_destino = gerar_nome_csv(tipo)
        with open(caminho_destino, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        tamanho = os.path.getsize(caminho_destino)
        return {"status": "ok", "arquivo": caminho_destino, "url_usada": csv_url, "tamanho_bytes": tamanho}
    except Exception as e:
        return {"erro": str(e)}


@app.get("/api/precos")
def get_precos(
    municipio: str,
    uf: str,
    produto: str = None,
    com_geo: bool = Query(False, description="Inclui geolocalização (mais lento)"),
    limite_geo: int = Query(30, description="Máximo de postos a geocodificar (os demais continuam na lista, sem lat/lon)"),
):
    municipio_norm = normalizar_texto(municipio)
    uf_norm = normalizar_texto(uf)

    csvs_disponiveis = {
        "gasolina": obter_csv_mais_recente("gasolina"),
        "diesel": obter_csv_mais_recente("diesel"),
        "glp": obter_csv_mais_recente("glp"),
    }

    for tipo, caminho in csvs_disponiveis.items():
        if not caminho:
            print(f"📥 Baixando CSV de {tipo}...")
            baixar_csv(tipo=tipo)
            csvs_disponiveis[tipo] = obter_csv_mais_recente(tipo)

    if not any(csvs_disponiveis.values()):
        raise HTTPException(status_code=500, detail="Nenhum CSV disponível.")

    try:
        resultados = {}

        for tipo_csv, caminho in csvs_disponiveis.items():
            if not caminho:
                continue

            print(f"📂 Lendo: {caminho}")
            df = pd.read_csv(caminho, sep=";", encoding="latin1", decimal=",")
            df.columns = [c.replace("ï»¿", "").strip() for c in df.columns]

            col_municipio = col_uf = col_produto = col_valor = col_revenda = None
            col_endereco = col_bairro = col_cnpj = col_bandeira = None

            for col in df.columns:
                cl = col.lower()
                if "municipio" in cl: col_municipio = col
                if "estado" in cl or cl == "uf": col_uf = col
                if "produto" in cl: col_produto = col
                if "venda" in cl: col_valor = col
                if "revenda" in cl: col_revenda = col
                if "rua" in cl or "endereco" in cl: col_endereco = col
                if "bairro" in cl: col_bairro = col
                if "cnpj" in cl: col_cnpj = col
                if "bandeira" in cl: col_bandeira = col

            if not all([col_municipio, col_uf, col_produto, col_valor]):
                print(f"⚠️ Colunas essenciais faltando em {caminho}")
                continue

            df["_municipio_norm"] = df[col_municipio].astype(str).apply(normalizar_texto)
            df["_uf_norm"] = df[col_uf].astype(str).apply(normalizar_texto)
            df["_produto_norm"] = df[col_produto].astype(str).apply(normalizar_texto)

            # Filtro por município e UF (exato primeiro)
            df_filtrado = df[
                (df["_municipio_norm"] == municipio_norm) & (df["_uf_norm"] == uf_norm)
            ]

            if df_filtrado.empty:
                df_filtrado = df[
                    (df["_municipio_norm"].str.contains(municipio_norm, na=False)) & (df["_uf_norm"] == uf_norm)
                ]

            print(f"🔎 {tipo_csv} - {municipio}/{uf}: {len(df_filtrado)} linhas no CSV")

            if df_filtrado.empty:
                continue

            for prod_chave, prod_label in [
                ("GASOLINA ADITIVADA", "gasolina_aditivada"),
                ("GASOLINA", "gasolina"),
                ("ETANOL HIDRATADO", "etanol"),
                ("ETANOL", "etanol"),
                ("DIESEL S10", "diesel_s10"),
                ("DIESEL S-10", "diesel_s10"),
                ("DIESEL S500", "diesel_s500"),
                ("DIESEL S-500", "diesel_s500"),
                ("DIESEL", "diesel"),
                ("GNV", "gnv"),
                ("GLP", "glp"),
            ]:
                if produto and prod_label != produto.lower():
                    continue

                # ⚠️ Filtro rigoroso por produto
                if prod_chave == "GASOLINA":
                    df_prod = df_filtrado[df_filtrado["_produto_norm"] == "GASOLINA"]
                elif prod_chave == "GASOLINA ADITIVADA":
                    df_prod = df_filtrado[df_filtrado["_produto_norm"] == "GASOLINA ADITIVADA"]
                elif prod_chave == "ETANOL":
                    df_prod = df_filtrado[df_filtrado["_produto_norm"] == "ETANOL"]
                elif prod_chave == "ETANOL HIDRATADO":
                    df_prod = df_filtrado[df_filtrado["_produto_norm"].str.contains("ETANOL HIDRATADO", na=False)]
                elif prod_chave == "DIESEL":
                    df_prod = df_filtrado[df_filtrado["_produto_norm"] == "DIESEL"]
                else:
                    df_prod = df_filtrado[df_filtrado["_produto_norm"].str.contains(prod_chave, na=False)]

                if df_prod.empty:
                    continue

                print(f"  → {prod_label}: {len(df_prod)} linhas brutas")

                postos_lista = []
                for _, row in df_prod.iterrows():
                    try:
                        preco = float(str(row[col_valor]).replace(",", "."))
                    except (ValueError, TypeError):
                        continue
                    posto = {
                        "revenda": str(row[col_revenda]) if col_revenda else "",
                        "cnpj": normalizar_cnpj(row[col_cnpj]) if col_cnpj else "",
                        "endereco": f"{row[col_endereco]}" if col_endereco else "",
                        "bairro": str(row[col_bairro]) if col_bairro else "",
                        "bandeira": str(row[col_bandeira]) if col_bandeira else "",
                        "produto": str(row[col_produto]) if col_produto else "",
                        "preco": round(preco, 2),
                        "_municipio_original": municipio,
                        "_uf_original": uf,
                    }
                    postos_lista.append(posto)

                # ✅ DEDUPLICAÇÃO NO BACKEND (um registro por CNPJ)
                postos_unicos = deduplicar_por_cnpj(postos_lista)

                if not postos_unicos:
                    continue

                # ✅ Recalcula estatísticas com a lista deduplicada
                precos_dedup = [
                    p["preco"] for p in postos_unicos
                    if isinstance(p.get("preco"), (int, float))
                ]
                if not precos_dedup:
                    continue

                media = round(sum(precos_dedup) / len(precos_dedup), 2)
                minimo = round(min(precos_dedup), 2)
                maximo = round(max(precos_dedup), 2)

                # ✅ Geolocalização opcional (NÃO remove nenhum posto)
                if com_geo and postos_unicos:
                    postos_unicos = enriquecer_postos_com_geo(postos_unicos, limite=limite_geo)

                # Mantém o resultado com mais postos (caso apareça em 2 CSVs)
                if prod_label not in resultados or len(postos_unicos) > resultados[prod_label]["total_postos"]:
                    resultados[prod_label] = {
                        "media": media,
                        "minimo": minimo,
                        "maximo": maximo,
                        "total_postos": len(postos_unicos),
                        "postos": postos_unicos,
                    }

        if not resultados:
            return {"erro": "Nenhum preço encontrado", "municipio": municipio, "uf": uf, "produtos": {}}

        return {"municipio": municipio, "uf": uf, "produtos": resultados}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Erro: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/csvs-baixados")
def listar_csvs_baixados():
    if not os.path.exists(CSV_DATA_DIR):
        return {"total": 0, "arquivos": []}
    arquivos = glob.glob(os.path.join(CSV_DATA_DIR, "precos_anp_*.csv"))
    arquivos.sort(key=os.path.getmtime, reverse=True)
    lista = []
    for caminho in arquivos:
        lista.append({
            "arquivo": caminho,
            "nome": os.path.basename(caminho),
            "tamanho_bytes": os.path.getsize(caminho),
            "modificado_em": datetime.fromtimestamp(os.path.getmtime(caminho)).isoformat(),
        })
    return {"total": len(lista), "arquivos": lista}


@app.get("/api/status-atualizacao")
def status_atualizacao():
    status = {"csvs_atuais": [], "csvs_antigos": [], "precisa_atualizar": False, "mensagem": ""}
    for produto in PRODUTO_PALAVRAS.keys():
        csv_recente = obter_csv_mais_recente(produto)
        if csv_recente:
            idade_dias = (time.time() - os.path.getmtime(csv_recente)) / 86400
            status["csvs_atuais"].append({"produto": produto, "arquivo": csv_recente, "idade_dias": round(idade_dias, 1)})
        else:
            status["csvs_atuais"].append({"produto": produto, "arquivo": None, "idade_dias": None})
            status["precisa_atualizar"] = True
    status["csvs_antigos"] = listar_csvs_antigos()
    if status["precisa_atualizar"]:
        status["mensagem"] = "Há produtos sem CSV."
    elif status["csvs_antigos"]:
        status["mensagem"] = f"{len(status['csvs_antigos'])} arquivo(s) antigo(s)."
    else:
        status["mensagem"] = "Todos atualizados."
    return status


@app.get("/api/csvs-antigos")
def listar_antigos(dias: int = DIAS_PARA_CONSIDERAR_ANTIGO):
    antigos = listar_csvs_antigos(dias=dias)
    return {"total": len(antigos), "dias_limite": dias, "arquivos": antigos}


@app.post("/api/apagar-csv")
def apagar_csv_endpoint(caminho: str = Query(...)):
    caminho_abs = os.path.abspath(caminho)
    data_abs = os.path.abspath(CSV_DATA_DIR)
    if not caminho_abs.startswith(data_abs):
        raise HTTPException(status_code=400, detail="Caminho inválido.")
    if not os.path.exists(caminho_abs):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    if apagar_csv(caminho_abs):
        return {"status": "ok", "arquivo_apagado": caminho_abs}
    raise HTTPException(status_code=500, detail="Não foi possível apagar.")


@app.post("/api/apagar-todos-antigos")
def apagar_todos_antigos(dias: int = DIAS_PARA_CONSIDERAR_ANTIGO):
    antigos = listar_csvs_antigos(dias=dias)
    apagados = []
    for item in antigos:
        if apagar_csv(item["arquivo"]):
            apagados.append(item["arquivo"])
    return {"status": "ok", "total_apagados": len(apagados), "apagados": apagados}


@app.post("/api/atualizar-agora")
def atualizar_agora():
    verificar_e_baixar_novos_csvs()
    return {"status": "ok", "mensagem": "Verificação concluída."}


# ======================== COMPOSIÇÃO PETROBRAS ========================
def raspar_composicao_petrobras(produto="gasolina"):
    agora = time.time()
    if produto in PETROBRAS_CACHE:
        dados_cache, timestamp = PETROBRAS_CACHE[produto]
        if agora - timestamp < PETROBRAS_CACHE_TTL:
            return dados_cache

    urls = {
        "gasolina": "https://precos.petrobras.com.br/precos-gasolina",
        "diesel": "https://precos.petrobras.com.br/precos-diesel",
        "glp": "https://precos.petrobras.com.br/precos-glp",
        "gnv": "https://precos.petrobras.com.br/precos-gnv",
    }
    url = urls.get(produto.lower())
    if not url:
        return None

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        texto = soup.get_text(separator="\n")
        linhas = [l.strip() for l in texto.split("\n") if l.strip()]

        dados = {}
        for i, linha in enumerate(linhas):
            if "Preço Médio do Brasil" in linha or ("Preço Médio" in linha and "R$" in linha):
                for j in range(i, min(len(linhas), i + 5)):
                    if "R$" in linhas[j]:
                        match = re.search(r"R\$\s*(\d+[,.]\d+)", linhas[j])
                        if match:
                            dados["preco_medio_final"] = float(match.group(1).replace(",", "."))
                            break
                break

        componentes = {
            "Parcela Petrobras": "parcela_petrobras",
            "Impostos Federais": "impostos_federais",
            "Imposto Estadual": "icms",
            "ICMS": "icms",
            "Custo Etanol Anidro": "biocombustivel",
            "Biodiesel": "biocombustivel",
            "Distribuição e Revenda": "margem_distribuicao_revenda",
        }

        for i, linha in enumerate(linhas):
            for chave, campo in componentes.items():
                if chave.lower() in linha.lower():
                    for j in range(max(0, i - 3), min(len(linhas), i + 4)):
                        if "R$" in linhas[j]:
                            match = re.search(r"R\$\s*(\d+[,.]\d+)", linhas[j])
                            if match:
                                try:
                                    dados[campo] = float(match.group(1).replace(",", "."))
                                    break
                                except ValueError:
                                    pass

        for linha in linhas:
            match = re.search(r"(\d{2}/\d{2}/\d{4}\s*a\s*\d{2}/\d{2}/\d{4})", linha)
            if match:
                dados["periodo"] = match.group(1)
                break

        obrigatorios = ["parcela_petrobras", "impostos_federais", "icms",
                        "biocombustivel", "margem_distribuicao_revenda", "preco_medio_final"]
        if not all(c in dados for c in obrigatorios):
            return None

        PETROBRAS_CACHE[produto] = (dados, agora)
        return dados
    except Exception as e:
        print(f"❌ Erro scraping: {e}")
        return None


@app.get("/api/composicao")
def get_composicao(uf: str = "BR", produto: str = "gasolina"):
    dados = raspar_composicao_petrobras(produto)
    if not dados:
        fallback = {
            "gasolina": {
                "parcela_petrobras": 2.08, "impostos_federais": 0.24,
                "icms": 1.57, "biocombustivel": 0.93,
                "margem_distribuicao_revenda": 1.72, "preco_medio_final": 6.54,
                "periodo": "Fallback estático",
            },
            "diesel": {
                "parcela_petrobras": 2.76, "impostos_federais": 0.32,
                "icms": 1.12, "biocombustivel": 0.85,
                "margem_distribuicao_revenda": 1.08, "preco_medio_final": 6.14,
                "periodo": "Fallback estático",
            },
            "gnv": {
                "parcela_petrobras": 2.40, "impostos_federais": 0.18,
                "icms": 1.20, "biocombustivel": 0.00,
                "margem_distribuicao_revenda": 0.90, "preco_medio_final": 4.68,
                "periodo": "Fallback estático",
            },
            "glp": {
                "parcela_petrobras": 45.00, "impostos_federais": 3.50,
                "icms": 12.00, "biocombustivel": 0.00,
                "margem_distribuicao_revenda": 22.00, "preco_medio_final": 82.50,
                "periodo": "Fallback estático",
            },
        }
        dados = fallback.get(produto.lower(), fallback["gasolina"])
        dados["fonte"] = "fallback_estatico"
    else:
        dados["fonte"] = "scraping_petrobras"

    total = dados.get("preco_medio_final", 0)
    if total > 0:
        dados["percentuais"] = {
            "parcela_petrobras": round(dados.get("parcela_petrobras", 0) / total * 100, 1),
            "impostos_federais": round(dados.get("impostos_federais", 0) / total * 100, 1),
            "icms": round(dados.get("icms", 0) / total * 100, 1),
            "biocombustivel": round(dados.get("biocombustivel", 0) / total * 100, 1),
            "margem_distribuicao_revenda": round(dados.get("margem_distribuicao_revenda", 0) / total * 100, 1),
        }

    return {"uf": uf, "produto": produto, **dados}


@app.get("/api/eletropostos")
def get_eletropostos(
    latitude: float = Query(...),
    longitude: float = Query(...),
    raio_km: float = Query(10),
    max_resultados: int = Query(50),
):
    if not OCM_API_KEY or OCM_API_KEY == "SUA_CHAVE_AQUI":
        raise HTTPException(status_code=500, detail="Configure a OCM_API_KEY.")

    try:
        params = {
            "key": OCM_API_KEY,
            "output": "json",
            "latitude": latitude,
            "longitude": longitude,
            "distance": raio_km,
            "distanceunit": "KM",
            "maxresults": max_resultados,
            "compact": True,
            "verbose": True,
        }

        r = requests.get(f"{OCM_BASE_URL}/poi/", params=params, timeout=30)
        r.raise_for_status()
        dados = r.json()

        eletropostos = []
        for item in dados:
            endereco = item.get("AddressInfo", {})
            conexoes = item.get("Connections", [])

            conexoes_formatadas = []
            custo_estimado_total = 0
            potencia_total = 0

            for c in conexoes[:5]:
                pot = c.get("PowerKW") or 0
                potencia_total += pot
                if pot >= 22:
                    preco_estimado_kwh = 2.50
                else:
                    preco_estimado_kwh = 1.50

                custo_sessao = round(preco_estimado_kwh * 30, 2)
                custo_estimado_total += custo_sessao

                conexoes_formatadas.append({
                    "tipo": c.get("ConnectionType", {}).get("Title") if c.get("ConnectionType") else "N/A",
                    "potencia_kw": pot,
                    "quantidade": c.get("Quantity", 1),
                    "preco_estimado_kwh": preco_estimado_kwh,
                    "custo_estimado_30kwh": custo_sessao,
                })

            eletropostos.append({
                "id": item.get("ID"),
                "nome": endereco.get("Title"),
                "endereco": endereco.get("AddressLine1"),
                "cidade": endereco.get("Town"),
                "uf": endereco.get("StateOrProvince"),
                "latitude": endereco.get("Latitude"),
                "longitude": endereco.get("Longitude"),
                "operador": item.get("OperatorInfo", {}).get("Title") if item.get("OperatorInfo") else "Não informado",
                "conexoes": conexoes_formatadas,
                "potencia_total_kw": potencia_total,
                "custo_estimado_sessao": round(custo_estimado_total, 2) if conexoes_formatadas else 0,
                "observacao_custo": "Estimativa baseada em tarifas médias: R$1,50/kWh (AC) e R$2,50/kWh (DC), sessão média de 30 kWh",
                "status": item.get("StatusType", {}).get("Title") if item.get("StatusType") else "Disponível",
            })

        return {"total": len(eletropostos), "eletropostos": eletropostos}
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Erro ao consultar OCM: {str(e)}")


# ======================== AGENDADOR ========================
def verificar_e_baixar_novos_csvs():
    print(f"\n🔍 [AGENDADOR] Verificando...")
    for produto in PRODUTO_PALAVRAS.keys():
        try:
            csv_atual = obter_csv_mais_recente(produto)
            if not csv_atual:
                baixar_csv(tipo=produto)
                continue
            idade_dias = (time.time() - os.path.getmtime(csv_atual)) / 86400
            if idade_dias > 1:
                baixar_csv(tipo=produto)
        except Exception as e:
            print(f"❌ [AGENDADOR] Erro: {e}")
    print(f"✅ [AGENDADOR] Concluído.\n")


scheduler = BackgroundScheduler()
scheduler.add_job(
    func=verificar_e_baixar_novos_csvs,
    trigger=IntervalTrigger(hours=INTERVALO_VERIFICACAO_HORAS),
    id="verificar_csvs",
    replace_existing=True,
)


@app.on_event("startup")
def iniciar_agendador():
    scheduler.start()
    print(f"⏰ Agendador iniciado.")


@app.on_event("shutdown")
def parar_agendador():
    scheduler.shutdown()
    print("⏰ Agendador parado.")