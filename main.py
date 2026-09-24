# backend/main.py
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup
import pandas as pd
import os
import re
import time
import zipfile
import glob
import unicodedata
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

app = FastAPI(title="Combustíveis ANP API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PAGINA_ANP = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/serie-historica-de-precos-de-combustiveis"
CSV_DATA_DIR = "data"
OCM_API_KEY = "d75c2b4f-371d-4514-9f57-fbe8330538fa"
OCM_BASE_URL = "https://api.openchargemap.io/v3"
PETROBRAS_CACHE = {}
PETROBRAS_CACHE_TTL = 3600
INTERVALO_VERIFICACAO_HORAS = 6
DIAS_PARA_CONSIDERAR_ANTIGO = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

PRODUTO_PALAVRAS = {
    "gasolina": ["gasolina"],
    "etanol": ["etanol"],
    "diesel": ["diesel"],
    "gnv": ["gnv"],
    "glp": ["glp"],
}


def normalizar_texto(texto):
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.upper().strip()


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
def get_precos(municipio: str, uf: str, produto: str = None):
    municipio_norm = normalizar_texto(municipio)
    uf_norm = normalizar_texto(uf)

    # ⚠️ ALTERAÇÃO AQUI: adicionar GLP à lista de CSVs
    csvs_disponiveis = {
        "gasolina": obter_csv_mais_recente("gasolina"),
        "diesel": obter_csv_mais_recente("diesel"),
        "glp": obter_csv_mais_recente("glp"),   # ← NOVO
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
                continue

            df["_municipio_norm"] = df[col_municipio].astype(str).apply(normalizar_texto)
            df["_uf_norm"] = df[col_uf].astype(str).apply(normalizar_texto)
            df["_produto_norm"] = df[col_produto].astype(str).apply(normalizar_texto)

            df_filtrado = df[
                (df["_municipio_norm"] == municipio_norm) & (df["_uf_norm"] == uf_norm)
            ]

            if df_filtrado.empty:
                df_filtrado = df[
                    (df["_municipio_norm"].str.contains(municipio_norm, na=False)) & (df["_uf_norm"] == uf_norm)
                ]

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

                if prod_chave == "GASOLINA":
                    df_prod = df_filtrado[df_filtrado["_produto_norm"] == "GASOLINA"]
                elif prod_chave == "GASOLINA ADITIVADA":
                    df_prod = df_filtrado[df_filtrado["_produto_norm"] == "GASOLINA ADITIVADA"]
                else:
                    df_prod = df_filtrado[df_filtrado["_produto_norm"].str.contains(prod_chave, na=False)]

                if df_prod.empty:
                    continue

                valores = pd.to_numeric(
                    df_prod[col_valor].astype(str).str.replace(",", "."),
                    errors="coerce"
                ).dropna()

                if valores.empty:
                    continue

                postos_lista = []
                for _, row in df_prod.iterrows():
                    try:
                        preco = float(str(row[col_valor]).replace(",", "."))
                    except (ValueError, TypeError):
                        continue
                    postos_lista.append({
                        "revenda": str(row[col_revenda]) if col_revenda else "",
                        "cnpj": str(row[col_cnpj]) if col_cnpj else "",
                        "endereco": f"{row[col_endereco]}" if col_endereco else "",
                        "bairro": str(row[col_bairro]) if col_bairro else "",
                        "bandeira": str(row[col_bandeira]) if col_bandeira else "",
                        "produto": str(row[col_produto]) if col_produto else "",
                        "preco": round(preco, 2),
                    })

                if prod_label not in resultados or len(postos_lista) > resultados[prod_label]["total_postos"]:
                    resultados[prod_label] = {
                        "media": round(float(valores.mean()), 2),
                        "minimo": round(float(valores.min()), 2),
                        "maximo": round(float(valores.max()), 2),
                        "total_postos": len(postos_lista),
                        "postos": postos_lista,
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
        # Fallback estático para cada produto
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
                # GNV vendido em m³ — composição baseada em gás natural
                "parcela_petrobras": 2.40, "impostos_federais": 0.18,
                "icms": 1.20, "biocombustivel": 0.00,
                "margem_distribuicao_revenda": 0.90, "preco_medio_final": 4.68,
                "periodo": "Fallback estático",
            },
            "glp": {
                # GLP P13 (botijão 13kg) — composição baseada em propano/butano
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