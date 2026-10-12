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
import subprocess
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

# ======================== CONFIGURAÇÕES ========================
PAGINA_ANP = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/serie-historica-de-precos-de-combustiveis"
CSV_DATA_DIR = "data"
CACHE_API_DIR = os.path.join(CSV_DATA_DIR, "cache_api_anp")
CACHE_CEP_DIR = os.path.join(CSV_DATA_DIR, "cache_cep")
CACHE_PETROBRAS_DIR = os.path.join(CSV_DATA_DIR, "cache_petrobras")
CACHE_ELETRO_DIR = os.path.join(CSV_DATA_DIR, "cache_eletropostos")

OCM_API_KEY = "d75c2b4f-371d-4514-9f57-fbe8330538fa"
OCM_BASE_URL = "https://api.openchargemap.io/v3"

CARREGADOS_CLI = "npx -y @carregados/cli"
CARREGADOS_TIMEOUT = 45

ELETRO_RAIO_CIDADE_KM = 25
ELETRO_RAIO_VIZINHAS_KM = 75
ELETRO_RAIO_EMERGENCIA_KM = 150
ELETRO_MAX_RESULTADOS = 500
ELETRO_CACHE_TTL_HORAS = 24

PETROBRAS_CACHE = {}
PETROBRAS_CACHE_TTL = 3600
INTERVALO_VERIFICACAO_HORAS = 6
DIAS_PARA_CONSIDERAR_ANTIGO = 30
IDADE_MAXIMA_CSV_DIAS = 1

API_ANP_REVENDEDORES = "https://revendedoresapi.anp.gov.br/v1/combustivel"
API_CACHE_TTL_HORAS = 24

BRASILAPI_CEP_URL = "https://brasilapi.com.br/api/cep/v2/{cep}"
CEP_CACHE_TTL_DIAS = 90

PRODUTOS = ["gasolina", "etanol", "diesel", "gnv", "glp"]

FAIXAS_PRECO = {
    "gasolina":           (2.50, 15.00),
    "gasolina_aditivada": (2.50, 16.00),
    "etanol":             (1.50, 12.00),
    "diesel":             (2.50, 15.00),
    "diesel_s10":         (2.50, 15.00),
    "diesel_s500":        (2.50, 15.00),
    "gnv":                (1.50, 10.00),
    "glp":               (30.00, 200.00),
}

CSV_URLS_ANP = {
    "gasolina": ["https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-gasolina-etanol.csv"],
    "etanol":   ["https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-gasolina-etanol.csv"],
    "diesel":   ["https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-diesel-gnv.csv"],
    "gnv":      ["https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-diesel-gnv.csv"],
    "glp":      ["https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-glp.csv"],
}

os.makedirs(CSV_DATA_DIR, exist_ok=True)
os.makedirs(CACHE_API_DIR, exist_ok=True)
os.makedirs(CACHE_CEP_DIR, exist_ok=True)
os.makedirs(CACHE_PETROBRAS_DIR, exist_ok=True)
os.makedirs(CACHE_ELETRO_DIR, exist_ok=True)

HEADERS_NAVEGADOR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

HEADERS_CSV = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/csv,application/csv,application/octet-stream,*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/serie-historica-de-precos-de-combustiveis",
}

HEADERS = HEADERS_NAVEGADOR

_sessao = requests.Session()
_sessao.headers.update(HEADERS_NAVEGADOR)

# ======================== UTILITÁRIOS ========================
def normalizar_texto(texto):
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.upper().strip()

def normalizar_cnpj(cnpj) -> str:
    if not cnpj:
        return ""
    return re.sub(r"\D", "", str(cnpj))

def _api_cache_path(municipio: str, uf: str) -> str:
    chave = f"{normalizar_texto(municipio)}_{normalizar_texto(uf)}"
    h = hashlib.md5(chave.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_API_DIR, f"{h}.json")

def _cep_cache_path(cep: str) -> str:
    h = hashlib.md5(cep.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_CEP_DIR, f"{h}.json")

def _petrobras_cache_key(produto: str, uf: str) -> str:
    return f"{produto.lower()}_{(uf or 'BR').upper()}"

def _petrobras_cache_path(produto: str, uf: str) -> str:
    chave = _petrobras_cache_key(produto, uf)
    h = hashlib.md5(chave.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_PETROBRAS_DIR, f"{h}.json")

def _ler_petrobras_cache_disco(produto: str, uf: str):
    caminho = _petrobras_cache_path(produto, uf)
    if not os.path.exists(caminho):
        return None
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            cached = json.load(f)
        idade = time.time() - cached.get("_ts", 0)
        if idade < PETROBRAS_CACHE_TTL:
            dados = cached.get("dados")
            if dados:
                PETROBRAS_CACHE[_petrobras_cache_key(produto, uf)] = (dados, cached["_ts"])
                dados["cache_origem"] = "disco"
                print(f"💾 [Petrobras] cache disco válido para {produto}/{uf} (idade {idade:.0f}s)")
                return dados
        print(f"💾 [Petrobras] cache disco expirado para {produto}/{uf} (idade {idade:.0f}s)")
        return None
    except Exception as e:
        print(f"⚠️ [Petrobras] erro lendo cache disco {produto}/{uf}: {e}")
        return None

def _gravar_petrobras_cache_disco(produto: str, uf: str, dados: dict):
    caminho = _petrobras_cache_path(produto, uf)
    try:
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump({"_ts": time.time(), "produto": produto, "uf": uf, "dados": dados},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ [Petrobras] erro gravando cache disco {produto}/{uf}: {e}")

def validar_preco(produto_label: str, preco: float):
    faixa = FAIXAS_PRECO.get(produto_label.lower())
    if not faixa:
        return True
    minimo, maximo = faixa
    return minimo <= preco <= maximo

def calcular_distancia_km(lat1, lon1, lat2, lon2):
    try:
        from math import radians, sin, cos, sqrt, atan2
        R = 6371.0
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
        c = 2 * atan2(sqrt(a), sqrt(1-a))
        return round(R * c, 2)
    except Exception:
        return None

# ======================== GEOCÓDIGO POR CEP ========================
def geocodificar_por_cep(cep: str) -> dict:
    cep_limpo = re.sub(r"\D", "", str(cep or ""))
    if len(cep_limpo) != 8:
        return None
    cache_file = _cep_cache_path(cep_limpo)
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("_ts", 0) < CEP_CACHE_TTL_DIAS * 86400:
                if cached.get("latitude") and cached.get("longitude"):
                    return {"latitude": cached["latitude"], "longitude": cached["longitude"], "fonte": "brasilapi_cep"}
                return None
        except Exception:
            pass
    try:
        url = BRASILAPI_CEP_URL.format(cep=cep_limpo)
        r = _sessao.get(url, timeout=10)
        if r.status_code == 404:
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump({"_ts": time.time(), "latitude": None, "longitude": None}, f)
            except Exception:
                pass
            return None
        r.raise_for_status()
        dados = r.json()
        coords = (dados.get("location") or {}).get("coordinates") or {}
        lat = coords.get("latitude")
        lon = coords.get("longitude")
        if lat is not None and lon is not None:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                if -90 <= lat_f <= 90 and -180 <= lon_f <= 180 and (lat_f != 0 or lon_f != 0):
                    resultado = {
                        "latitude": lat_f, "longitude": lon_f, "fonte": "brasilapi_cep",
                        "endereco_cep": dados.get("street"),
                        "bairro_cep": dados.get("neighborhood"),
                        "cidade_cep": dados.get("city"),
                        "uf_cep": dados.get("state"),
                    }
                    try:
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump({"_ts": time.time(), **resultado}, f, ensure_ascii=False)
                    except Exception:
                        pass
                    return resultado
            except (ValueError, TypeError):
                pass
        return None
    except Exception as e:
        print(f"⚠️ [CEP] erro geocodificando {cep_limpo}: {e}")
        return None

def enriquecer_postos_sem_coordenada(postos: list, limite: int = 40) -> list:
    sem_coord = [p for p in postos if not (p.get("latitude") and p.get("longitude")) and p.get("cep")]
    if not sem_coord:
        return postos
    print(f"🌎 [CEP] tentando geocodificar {min(len(sem_coord), limite)}/{len(sem_coord)} postos sem lat/lon")
    geocodificados = 0
    for p in sem_coord[:limite]:
        geo = geocodificar_por_cep(p.get("cep"))
        if geo:
            p["latitude"] = geo["latitude"]
            p["longitude"] = geo["longitude"]
            p["geo_fonte"] = "brasilapi_cep"
            p["geo_precisao"] = "aproximado_cep"
            geocodificados += 1
    print(f"🌎 [CEP] {geocodificados} postos geocodificados por CEP")
    return postos

# ======================== API ANP (cadastro) ========================
def buscar_postos_anp_api(municipio: str, uf: str) -> list:
    cache_file = _api_cache_path(municipio, uf)
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("_ts", 0) < API_CACHE_TTL_HORAS * 3600:
                postos = cached.get("postos", [])
                print(f"📦 [API ANP] cache: {len(postos)} postos em {municipio}/{uf}")
                return postos
        except Exception:
            pass
    postos = []
    try:
        params = {"municipio": municipio.upper().strip(), "uf": uf.upper().strip()}
        print(f"📡 [API ANP] consultando {API_ANP_REVENDEDORES} params={params}...")
        r = _sessao.get(API_ANP_REVENDEDORES, params=params, timeout=30)
        print(f"📡 [API ANP] status: {r.status_code}")
        r.raise_for_status()
        dados = r.json()
        itens = []
        if isinstance(dados, dict):
            itens = dados.get("data") or dados.get("items") or []
            if not isinstance(itens, list):
                itens = []
        elif isinstance(dados, list):
            itens = dados
        print(f"📡 [API ANP] {len(itens)} registros brutos recebidos")
        for item in itens:
            posto = _extrair_posto_api(item, municipio, uf)
            if posto:
                postos.append(posto)
        unicos = {}
        for p in postos:
            unicos[p["cnpj"]] = p
        postos = list(unicos.values())
        print(f"✅ [API ANP] {len(postos)} postos únicos em {municipio}/{uf}")
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"_ts": time.time(), "postos": postos}, f, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ [API ANP] erro salvando cache: {e}")
        return postos
    except Exception as e:
        print(f"❌ [API ANP] erro: {e}")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                print(f"⚠️ [API ANP] usando cache expirado ({len(cached.get('postos', []))} postos)")
                return cached.get("postos", [])
            except Exception:
                pass
        return []

def _extrair_posto_api(item: dict, municipio: str, uf: str) -> dict:
    if not isinstance(item, dict):
        return None
    cnpj = normalizar_cnpj(item.get("cnpj") or item.get("CNPJ") or "")
    if not cnpj:
        return None
    lat_str = item.get("latitude") or item.get("Latitude")
    lon_str = item.get("longitude") or item.get("Longitude")
    lat_f = lon_f = None
    try:
        if lat_str not in (None, ""):
            a = float(str(lat_str).replace(",", "."))
            if -90 <= a <= 90 and a != 0:
                lat_f = a
    except (ValueError, TypeError):
        pass
    try:
        if lon_str not in (None, ""):
            b = float(str(lon_str).replace(",", "."))
            if -180 <= b <= 180 and b != 0:
                lon_f = b
    except (ValueError, TypeError):
        pass
    endereco = item.get("endereco") or item.get("Endereço") or ""
    endereco = re.sub(r"\s+", " ", str(endereco)).strip().strip(",")
    if not endereco:
        endereco = "Endereço não disponível na base ANP"
    return {
        "cnpj": cnpj,
        "revenda": item.get("razaoSocial") or item.get("razao_social") or item.get("Razão Social") or "",
        "nome_fantasia": item.get("nomeFantasia") or item.get("nome_fantasia") or "",
        "endereco": endereco,
        "bairro": item.get("bairro") or item.get("Bairro") or "",
        "municipio": item.get("municipio") or item.get("Município") or municipio,
        "uf": item.get("uf") or item.get("UF") or uf,
        "cep": str(item.get("cep") or item.get("CEP") or "").strip(),
        "distribuidora": item.get("distribuidora") or item.get("Distribuidora") or "",
        "bandeira": item.get("distribuidora") or item.get("Distribuidora") or "",
        "latitude": lat_f,
        "longitude": lon_f,
    }

# ======================== CSV DE PREÇOS ========================
def gerar_nome_csv(produto="gasolina"):
    agora = datetime.now()
    data_str = agora.strftime("%d%m%Y_%H%M")
    return os.path.join(CSV_DATA_DIR, f"precos_anp_{produto.lower()}_{data_str}.csv")

def obter_csv_mais_recente(produto=None):
    if not os.path.exists(CSV_DATA_DIR):
        return None
    padrao = os.path.join(CSV_DATA_DIR, f"precos_anp_{produto.lower()}_*.csv") if produto else os.path.join(CSV_DATA_DIR, "precos_anp_*.csv")
    arquivos = glob.glob(padrao)
    if not arquivos:
        return None
    arquivos.sort(key=os.path.getmtime, reverse=True)
    return arquivos[0]

def idade_csv_dias(caminho):
    if not caminho or not os.path.exists(caminho):
        return None
    return (time.time() - os.path.getmtime(caminho)) / 86400

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
        por_produto.setdefault(produto, []).append(caminho)
    for produto, lista in por_produto.items():
        lista.sort(key=os.path.getmtime, reverse=True)
        for caminho in lista[1:]:
            idade_dias = (agora - os.path.getmtime(caminho)) / 86400
            if idade_dias >= dias:
                antigos.append({
                    "arquivo": caminho, "nome": os.path.basename(caminho),
                    "produto": produto, "idade_dias": round(idade_dias, 1),
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

# ======================== DESCOBERTA DINÂMICA DE URLs DA ANP ========================
def descobrir_urls_csv_anp():
    urls_encontradas = {}
    try:
        print(f"🌐 [ANP] raspando página para descobrir URLs dos CSVs...")
        r = _sessao.get(PAGINA_ANP, headers=HEADERS_NAVEGADOR, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.lower().endswith(".csv"):
                continue
            href_lower = href.lower()
            if "ultimas-4-semanas" not in href_lower and "/qus/" not in href_lower:
                continue
            if href.startswith("/"):
                href = "https://www.gov.br" + href
            elif not href.startswith("http"):
                href = "https://www.gov.br/anp/" + href.lstrip("/")
            if "gasolina" in href_lower or "etanol" in href_lower:
                urls_encontradas.setdefault("gasolina-etanol", href)
            elif "diesel" in href_lower:
                urls_encontradas.setdefault("diesel-gnv", href)
            elif "glp" in href_lower:
                urls_encontradas.setdefault("glp", href)
            elif "gnv" in href_lower:
                urls_encontradas.setdefault("diesel-gnv", href)
        if urls_encontradas:
            print(f"✅ [ANP] descobertos {len(urls_encontradas)} links via scraping")
        return urls_encontradas
    except Exception as e:
        print(f"❌ [ANP] erro raspando página: {e}")
        return {}

def _mapear_produto_para_grupo(produto: str):
    p = produto.lower()
    if p in ("gasolina", "etanol", "gasolina_aditivada"):
        return "gasolina-etanol"
    if p in ("diesel", "diesel_s10", "diesel_s500", "gnv"):
        return "diesel-gnv"
    if p == "glp":
        return "glp"
    return None

def obter_url_csv(produto: str) -> str:
    produto_lower = produto.lower()
    cache_link_file = os.path.join(CACHE_API_DIR, "ultimo_link_anp.json")
    grupo = _mapear_produto_para_grupo(produto_lower)
    urls_descobertas = descobrir_urls_csv_anp()
    if urls_descobertas and grupo and grupo in urls_descobertas:
        url = urls_descobertas[grupo]
        try:
            cache_atual = {}
            if os.path.exists(cache_link_file):
                with open(cache_link_file, "r", encoding="utf-8") as f:
                    cache_atual = json.load(f)
            cache_atual[grupo] = {"url": url, "_ts": time.time()}
            with open(cache_link_file, "w", encoding="utf-8") as f:
                json.dump(cache_atual, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return url
    if produto_lower in CSV_URLS_ANP and CSV_URLS_ANP[produto_lower]:
        print(f"ℹ️ [ANP] usando URL hard-coded para {produto_lower}")
        return CSV_URLS_ANP[produto_lower][0]
    if os.path.exists(cache_link_file):
        try:
            with open(cache_link_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if grupo and grupo in cache:
                print(f"ℹ️ [ANP] usando URL do cache local para {produto_lower}")
                return cache[grupo]["url"]
        except Exception:
            pass
    return None

def encontrar_link_csv_anp(produto="gasolina"):
    url = obter_url_csv(produto)
    if not url:
        return None
    try:
        print(f"🔗 [CSV] testando URL: {url}")
        r = _sessao.get(url, headers=HEADERS_CSV, timeout=20, allow_redirects=True, stream=True)
        ct = (r.headers.get("content-type") or "").lower()
        status = r.status_code
        if status == 200 and ("csv" in ct or "octet-stream" in ct or "text/plain" in ct or "text/html" not in ct):
            chunk = next(r.iter_content(chunk_size=1), b"")
            r.close()
            if chunk:
                print(f"✅ [CSV] URL válida: {url}")
                return url
        else:
            r.close()
    except Exception as e:
        print(f"⚠️ [CSV] erro testando {url}: {e}")
    for url_alt in CSV_URLS_ANP.get(produto.lower(), []):
        if url_alt == url:
            continue
        try:
            r = _sessao.get(url_alt, headers=HEADERS_CSV, timeout=20, allow_redirects=True, stream=True)
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code == 200 and "text/html" not in ct:
                chunk = next(r.iter_content(chunk_size=1), b"")
                r.close()
                if chunk:
                    return url_alt
        except Exception:
            pass
    return None

# ======================== HEALTH E TESTES ========================
@app.get("/")
def health():
    return {"status": "ok", "service": "combustivel"}

@app.get("/api/testar-api-anp")
def testar_api_anp(municipio: str = "VITORIA", uf: str = "ES"):
    params = {"municipio": municipio.upper(), "uf": uf.upper()}
    url = API_ANP_REVENDEDORES
    resultado = {
        "municipio": municipio, "uf": uf,
        "url_testada": f"{url}?municipio={params['municipio']}&uf={params['uf']}",
        "status_code": None, "content_type": None, "tipo_resposta": None,
        "chaves": None, "total_items": 0, "total_registro_anp": None,
        "primeiro_item": None, "primeiro_item_extraido": None, "erro": None,
    }
    try:
        r = _sessao.get(url, params=params, timeout=30)
        resultado["status_code"] = r.status_code
        resultado["content_type"] = r.headers.get("content-type")
        dados = r.json()
        if isinstance(dados, dict):
            resultado["tipo_resposta"] = "dict"
            resultado["chaves"] = list(dados.keys())
            itens = dados.get("data") or dados.get("items") or []
            resultado["total_items"] = len(itens)
            filtro = dados.get("searchPageFilter") or {}
            resultado["total_registro_anp"] = filtro.get("totalRegistro")
            if itens:
                resultado["primeiro_item"] = itens[0]
                resultado["primeiro_item_extraido"] = _extrair_posto_api(itens[0], municipio, uf)
        elif isinstance(dados, list):
            resultado["tipo_resposta"] = "list"
            resultado["total_items"] = len(dados)
            if dados:
                resultado["primeiro_item"] = dados[0]
                resultado["primeiro_item_extraido"] = _extrair_posto_api(dados[0], municipio, uf)
    except Exception as e:
        resultado["erro"] = str(e)
    return resultado

@app.get("/api/descobrir-links-anp")
def descobrir_links_anp():
    urls = descobrir_urls_csv_anp()
    return {"total": len(urls), "links_encontrados": urls, "pagina_raspada": PAGINA_ANP}

@app.get("/api/baixar-csv")
def baixar_csv(tipo: str = "gasolina"):
    try:
        if tipo.lower() not in CSV_URLS_ANP:
            raise Exception(f"Produto desconhecido: {tipo}")
        os.makedirs(CSV_DATA_DIR, exist_ok=True)
        csv_url = encontrar_link_csv_anp(tipo)
        if not csv_url:
            csv_local = obter_csv_mais_recente(tipo)
            if csv_local:
                idade = idade_csv_dias(csv_local)
                return {
                    "status": "ok_offline", "arquivo": csv_local, "url_usada": None,
                    "tamanho_bytes": os.path.getsize(csv_local),
                    "aviso": f"Não foi possível baixar CSV novo. Usando arquivo local com {idade:.1f} dias.",
                }
            raise Exception(f"CSV não encontrado para {tipo} e não há arquivo local.")
        r = _sessao.get(csv_url, headers=HEADERS_CSV, timeout=180, stream=True, allow_redirects=True)
        r.raise_for_status()
        ct = (r.headers.get("content-type") or "").lower()
        if "text/html" in ct and "csv" not in ct:
            raise Exception(f"Servidor retornou HTML em vez de CSV")
        caminho_destino = gerar_nome_csv(tipo)
        with open(caminho_destino, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        tamanho = os.path.getsize(caminho_destino)
        if tamanho < 1000:
            os.remove(caminho_destino)
            csv_local = obter_csv_mais_recente(tipo)
            if csv_local:
                return {
                    "status": "ok_offline", "arquivo": csv_local, "url_usada": csv_url,
                    "tamanho_bytes": os.path.getsize(csv_local),
                    "aviso": "Download falhou (arquivo muito pequeno). Usando CSV local.",
                }
            raise Exception(f"Arquivo muito pequeno ({tamanho} bytes)")
        print(f"✅ [baixar-csv] {tipo}: {tamanho} bytes salvos em {caminho_destino}")
        return {"status": "ok", "arquivo": caminho_destino, "url_usada": csv_url, "tamanho_bytes": tamanho}
    except Exception as e:
        print(f"❌ [baixar-csv] {tipo}: {e}")
        csv_local = obter_csv_mais_recente(tipo)
        if csv_local:
            return {
                "status": "ok_offline", "arquivo": csv_local, "url_usada": None,
                "tamanho_bytes": os.path.getsize(csv_local),
                "aviso": f"Erro no download: {str(e)}. Usando CSV local.",
            }
        return {"erro": str(e)}

# ======================== GARANTIR CSVs ========================
def garantir_todos_csvs(force_rebaixar_antigos=True):
    status_por_produto = {}
    for produto in PRODUTOS:
        caminho = obter_csv_mais_recente(produto)
        idade = idade_csv_dias(caminho)
        if caminho is None:
            print(f"📥 [garantir] {produto}: sem CSV local, baixando...")
            resultado = baixar_csv(tipo=produto)
            status_por_produto[produto] = {
                "acao": "baixado",
                "ok": resultado.get("status") in ("ok", "ok_offline"),
                "arquivo": resultado.get("arquivo"),
                "erro": resultado.get("erro"),
            }
            continue
        if force_rebaixar_antigos and idade is not None and idade > IDADE_MAXIMA_CSV_DIAS:
            print(f"📥 [garantir] {produto}: CSV com {idade:.1f} dias, rebaixando...")
            resultado = baixar_csv(tipo=produto)
            novo_caminho = obter_csv_mais_recente(produto)
            status_por_produto[produto] = {
                "acao": "rebaixado",
                "ok": resultado.get("status") == "ok",
                "arquivo_anterior": caminho,
                "arquivo_novo": novo_caminho,
                "erro": resultado.get("erro"),
            }
        else:
            status_por_produto[produto] = {
                "acao": "ok", "ok": True, "arquivo": caminho,
                "idade_dias": round(idade, 2) if idade is not None else None,
            }
    return status_por_produto

# ======================== PARSING CSV ========================
def _extrair_data_referencia(df: pd.DataFrame):
    col_ini = col_fim = col_unica = None
    for col in df.columns:
        cl = col.lower()
        if "data" in cl and "inicial" in cl:
            col_ini = col
        elif "data" in cl and "final" in cl:
            col_fim = col
        elif "data" in cl and ("coleta" in cl or "pesquisa" in cl):
            col_unica = col

    try:
        if col_ini or col_fim:
            ini = str(df[col_ini].dropna().iloc[0]) if col_ini else None
            fim = str(df[col_fim].dropna().iloc[0]) if col_fim else None
            if ini or fim:
                return {"data_inicial": ini, "data_final": fim}
        if col_unica:
            valores = df[col_unica].dropna().astype(str).unique().tolist()
            if valores:
                def _parse_data(s):
                    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
                        try:
                            return datetime.strptime(s.strip()[:10], fmt)
                        except Exception:
                            continue
                    return None
                valores_parsed = [(v, _parse_data(v)) for v in valores]
                valores_validos = [v for v, d in valores_parsed if d is not None]
                if valores_validos:
                    valores_validos.sort(key=lambda s: _parse_data(s) or datetime.min)
                    maior = valores_validos[-1]
                    return {"data_inicial": maior, "data_final": maior}
                return {"data_inicial": valores[0], "data_final": valores[0]}
    except Exception:
        pass
    return None

def _detectar_colunas(df: pd.DataFrame):
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
    return {
        "municipio": col_municipio, "uf": col_uf, "produto": col_produto,
        "valor": col_valor, "revenda": col_revenda, "endereco": col_endereco,
        "bairro": col_bairro, "cnpj": col_cnpj, "bandeira": col_bandeira,
    }

# ======================== ENDPOINT PRINCIPAL ========================
@app.get("/api/precos")
def get_precos(
    municipio: str, uf: str, produto: str = None,
    incluir_sem_preco: bool = Query(True),
    geocodificar_cep: bool = Query(True),
):
    """
    Retorna TODOS os postos autorizados pela ANP no município.
    A fonte primária é o CSV de preços (que contém todos os CNPJs pesquisados).
    A API de revendedores é usada para enriquecer com dados cadastrais.
    Postos sem preço também são retornados.
    """
    municipio_norm = normalizar_texto(municipio)
    uf_norm = normalizar_texto(uf)
    print(f"\n{'='*60}\n🔍 {municipio}/{uf} - produto: {produto or 'todos'}\n{'='*60}")

    # 1. Busca postos da API (enriquecimento cadastral)
    postos_api = buscar_postos_anp_api(municipio, uf)
    total_autorizados_api = len(postos_api)
    print(f"📋 Base ANP (API): {total_autorizados_api} postos")

    # 2. Garante CSVs atualizados
    garantir_todos_csvs(force_rebaixar_antigos=True)
    csvs_disponiveis = {p: obter_csv_mais_recente(p) for p in PRODUTOS}

    # 3. Processa cada CSV
    try:
        resultados = {}
        # Mapa global de CNPJs encontrados no CSV (para contar total de autorizados)
        cnpjs_csv_global = set()

        for tipo_csv, caminho in csvs_disponiveis.items():
            if not caminho:
                continue
            idade = idade_csv_dias(caminho)
            fonte_dado = "csv_anp_atual" if (idade is not None and idade <= IDADE_MAXIMA_CSV_DIAS) else "csv_anp_antigo"
            try:
                df = pd.read_csv(caminho, sep=";", encoding="latin1", decimal=",")
            except Exception as e:
                print(f"⚠️ Erro lendo {caminho}: {e}")
                continue
            df.columns = [c.replace("ï»¿", "").strip() for c in df.columns]
            data_ref = _extrair_data_referencia(df)
            cols = _detectar_colunas(df)
            if not all([cols["municipio"], cols["uf"], cols["produto"], cols["valor"]]):
                continue

            df["_municipio_norm"] = df[cols["municipio"]].astype(str).apply(normalizar_texto)
            df["_uf_norm"] = df[cols["uf"]].astype(str).apply(normalizar_texto)
            df["_produto_norm"] = df[cols["produto"]].astype(str).apply(normalizar_texto)

            df_filtrado = df[(df["_municipio_norm"] == municipio_norm) & (df["_uf_norm"] == uf_norm)]
            if df_filtrado.empty:
                df_filtrado = df[(df["_municipio_norm"].str.contains(municipio_norm, na=False)) & (df["_uf_norm"] == uf_norm)]
            if df_filtrado.empty:
                continue

            # Registra CNPJs do CSV para contagem global
            if cols["cnpj"]:
                for c in df_filtrado[cols["cnpj"]].dropna().tolist():
                    cnpj_limpo = normalizar_cnpj(c)
                    if len(cnpj_limpo) == 14:
                        cnpjs_csv_global.add(cnpj_limpo)

            mapeamento_produtos = [
                ("GASOLINA ADITIVADA", "gasolina_aditivada"), ("GASOLINA", "gasolina"),
                ("ETANOL HIDRATADO", "etanol"), ("ETANOL", "etanol"),
                ("DIESEL S10", "diesel_s10"), ("DIESEL S-10", "diesel_s10"),
                ("DIESEL S500", "diesel_s500"), ("DIESEL S-500", "diesel_s500"),
                ("DIESEL", "diesel"), ("GNV", "gnv"), ("GLP", "glp"),
            ]
            for prod_chave, prod_label in mapeamento_produtos:
                if produto and prod_label != produto.lower():
                    continue

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

                # Mapa de preços por CNPJ (do CSV)
                mapa_precos = {}
                if not df_prod.empty:
                    for _, row in df_prod.iterrows():
                        try:
                            preco = float(str(row[cols["valor"]]).replace(",", "."))
                        except (ValueError, TypeError):
                            continue
                        if not validar_preco(prod_label, preco):
                            continue
                        cnpj_limpo = normalizar_cnpj(row[cols["cnpj"]]) if cols["cnpj"] else ""
                        if not cnpj_limpo:
                            continue
                        # Extrai dados cadastrais do CSV como fallback
                        revenda_csv = str(row[cols["revenda"]]).strip() if cols["revenda"] else ""
                        endereco_csv = str(row[cols["endereco"]]).strip() if cols["endereco"] else ""
                        bairro_csv = str(row[cols["bairro"]]).strip() if cols["bairro"] else ""
                        bandeira_csv = str(row[cols["bandeira"]]).strip() if cols["bandeira"] else ""
                        mapa_precos[cnpj_limpo] = {
                            "preco": round(preco, 2),
                            "bandeira": bandeira_csv,
                            "revenda": revenda_csv,
                            "endereco": endereco_csv,
                            "bairro": bairro_csv,
                        }

                # Monta a lista COM TODOS os autorizados (API + CSV)
                postos_merge = {}

                # 3.1 Insere todos os postos da API
                for p in postos_api:
                    cnpj_key = normalizar_cnpj(p["cnpj"])
                    postos_merge[cnpj_key] = {
                        **p,
                        "cnpj": cnpj_key,
                        "preco": 0.0,
                        "tem_preco": False,
                        "produto": prod_label.upper(),
                        "cnpj_orfao": False,
                        "fonte_dado": "sem_preco_anp",
                        "autorizado_anp": True,
                    }

                # 3.2 Aplica preços do CSV nos autorizados; insere os que faltam
                for cnpj_limpo, dados_preco in mapa_precos.items():
                    if cnpj_limpo in postos_merge:
                        postos_merge[cnpj_limpo]["preco"] = dados_preco["preco"]
                        postos_merge[cnpj_limpo]["tem_preco"] = True
                        postos_merge[cnpj_limpo]["fonte_dado"] = fonte_dado
                        if dados_preco["bandeira"]:
                            postos_merge[cnpj_limpo]["bandeira"] = dados_preco["bandeira"]
                    else:
                        # Posto do CSV que não está na API → insere com dados do CSV
                        postos_merge[cnpj_limpo] = {
                            "cnpj": cnpj_limpo,
                            "revenda": dados_preco.get("revenda") or f"Posto {cnpj_limpo}",
                            "nome_fantasia": dados_preco.get("revenda") or "",
                            "endereco": dados_preco.get("endereco") or "Endereço não disponível",
                            "bairro": dados_preco.get("bairro") or "",
                            "municipio": municipio,
                            "uf": uf,
                            "cep": "",
                            "bandeira": dados_preco.get("bandeira") or "",
                            "preco": dados_preco["preco"],
                            "tem_preco": True,
                            "produto": prod_label.upper(),
                            "latitude": None,
                            "longitude": None,
                            "cnpj_orfao": True,
                            "fonte_dado": fonte_dado,
                            "autorizado_anp": True,  # Consideramos autorizado (está no CSV)
                        }

                postos_lista = list(postos_merge.values())

                if not incluir_sem_preco:
                    postos_lista = [p for p in postos_lista if p["preco"] > 0]

                if not postos_lista:
                    continue

                # Geocodifica postos sem coordenada
                if geocodificar_cep:
                    postos_lista = enriquecer_postos_sem_coordenada(postos_lista, limite=60)

                # Estatísticas
                precos_validos = [p["preco"] for p in postos_lista if p["preco"] > 0]
                if precos_validos:
                    media = round(sum(precos_validos) / len(precos_validos), 2)
                    minimo = round(min(precos_validos), 2)
                    maximo = round(max(precos_validos), 2)
                else:
                    media = minimo = maximo = None

                # Ordena: com preço primeiro (menor para maior), depois sem preço
                postos_lista.sort(key=lambda p: (
                    0 if p["preco"] > 0 else 1,
                    p["preco"] if p["preco"] > 0 else 9999,
                    normalizar_texto(p.get("revenda") or p.get("nome_fantasia") or ""),
                ))

                com_preco = sum(1 for p in postos_lista if p["preco"] > 0)
                sem_preco = len(postos_lista) - com_preco
                autorizados = sum(1 for p in postos_lista if p.get("autorizado_anp"))

                if prod_label not in resultados or len(postos_lista) > resultados[prod_label]["total_postos"]:
                    resultados[prod_label] = {
                        "media": media, "minimo": minimo, "maximo": maximo,
                        "total_postos": len(postos_lista),
                        "total_com_preco": com_preco,
                        "total_sem_preco": sem_preco,
                        "total_autorizados_anp": autorizados,
                        "fonte_dado": fonte_dado,
                        "data_referencia": data_ref,
                        "postos": postos_lista,
                    }

        if not resultados:
            return {
                "erro": "Nenhum posto encontrado",
                "municipio": municipio,
                "uf": uf,
                "produtos": {},
                "fonte_api_anp": total_autorizados_api,
                "total_autorizados_anp": 0,
            }

        # Total de autorizados = união dos CNPJs da API + CSV
        cnpjs_api = set(normalizar_cnpj(p["cnpj"]) for p in postos_api if normalizar_cnpj(p["cnpj"]))
        cnpjs_todos = cnpjs_api | cnpjs_csv_global
        total_autorizados_final = len(cnpjs_todos)

        return {
            "municipio": municipio,
            "uf": uf,
            "fonte_api_anp": total_autorizados_api,
            "total_autorizados_anp": total_autorizados_final,
            "produtos": resultados,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Erro: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# ======================== DEMAIS ENDPOINTS ========================
@app.get("/api/csvs-baixados")
def listar_csvs_baixados():
    if not os.path.exists(CSV_DATA_DIR):
        return {"total": 0, "arquivos": []}
    arquivos = glob.glob(os.path.join(CSV_DATA_DIR, "precos_anp_*.csv"))
    arquivos.sort(key=os.path.getmtime, reverse=True)
    return {"total": len(arquivos), "arquivos": [
        {"arquivo": c, "nome": os.path.basename(c), "tamanho_bytes": os.path.getsize(c),
         "idade_dias": round(idade_csv_dias(c), 2) if idade_csv_dias(c) is not None else None,
         "modificado_em": datetime.fromtimestamp(os.path.getmtime(c)).isoformat()}
        for c in arquivos
    ]}

@app.get("/api/status-atualizacao")
def status_atualizacao():
    status = {"csvs_atuais": [], "csvs_antigos": [], "precisa_atualizar": False, "mensagem": ""}
    for produto in PRODUTOS:
        csv_recente = obter_csv_mais_recente(produto)
        if csv_recente:
            idade_dias = idade_csv_dias(csv_recente)
            status["csvs_atuais"].append({"produto": produto, "arquivo": csv_recente, "idade_dias": round(idade_dias, 1) if idade_dias is not None else None})
            if idade_dias is not None and idade_dias > IDADE_MAXIMA_CSV_DIAS:
                status["precisa_atualizar"] = True
        else:
            status["csvs_atuais"].append({"produto": produto, "arquivo": None, "idade_dias": None})
            status["precisa_atualizar"] = True
    status["csvs_antigos"] = listar_csvs_antigos()
    if status["precisa_atualizar"]:
        status["mensagem"] = "Há produtos sem CSV ou com CSV desatualizado."
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
    apagados = [item["arquivo"] for item in antigos if apagar_csv(item["arquivo"])]
    return {"status": "ok", "total_apagados": len(apagados), "apagados": apagados}

@app.post("/api/atualizar-agora")
def atualizar_agora():
    resultado = garantir_todos_csvs(force_rebaixar_antigos=True)
    return {"status": "ok", "mensagem": "Verificação concluída.", "detalhes": resultado}

@app.get("/api/status-dados")
def status_dados():
    resultado = {
        "csvs": {}, "api_anp": None, "petrobras": {},
        "agendador": {
            "intervalo_horas": INTERVALO_VERIFICACAO_HORAS,
            "rodando": scheduler.running if 'scheduler' in globals() else False,
        },
        "carregados_configurado": True,
    }
    for produto in PRODUTOS:
        caminho = obter_csv_mais_recente(produto)
        idade = idade_csv_dias(caminho)
        resultado["csvs"][produto] = {
            "arquivo": caminho, "existe": caminho is not None,
            "idade_dias": round(idade, 2) if idade is not None else None,
            "fonte_dado": "csv_anp_atual" if (idade is not None and idade <= IDADE_MAXIMA_CSV_DIAS) else ("csv_anp_antigo" if caminho else "sem_csv"),
        }
    try:
        test = testar_api_anp()
        resultado["api_anp"] = {
            "status": "ok" if test.get("status_code") == 200 and test.get("total_items", 0) > 0 else "degradado",
            "status_code": test.get("status_code"),
            "total_items": test.get("total_items"),
            "erro": test.get("erro"),
        }
    except Exception as e:
        resultado["api_anp"] = {"status": "erro", "erro": str(e)}
    for prod in ["gasolina", "diesel", "glp", "gnv"]:
        dados = raspar_composicao_petrobras(prod)
        resultado["petrobras"][prod] = {
            "status": "ok" if dados else "fallback",
            "fonte": "scraping_petrobras" if dados else "fallback_estatico",
            "periodo": dados.get("periodo") if dados else None,
            "cache_origem": dados.get("cache_origem") if dados else None,
        }
    return resultado

# ======================== COMPOSIÇÃO PETROBRAS ========================
PETROBRAS_SLUG = {
    "gasolina": "gasolina",
    "diesel":   "diesel",
    "glp":      "gas-de-cozinha",
    "gnv":      "gnv",
}

COMPONENTES_POR_PRODUTO = {
    "gasolina": ["parcela_petrobras", "impostos_federais", "icms",
                 "biocombustivel", "margem_distribuicao_revenda"],
    "diesel":   ["parcela_petrobras", "impostos_federais", "icms",
                 "biocombustivel", "margem_distribuicao_revenda"],
    "glp":      ["parcela_petrobras", "impostos_federais", "icms",
                 "margem_distribuicao_revenda"],
    "gnv":      ["parcela_petrobras", "impostos_federais", "icms",
                 "margem_distribuicao_revenda"],
}

def _componentes_obrigatorios(produto: str) -> list:
    return COMPONENTES_POR_PRODUTO.get(produto.lower(), COMPONENTES_POR_PRODUTO["gasolina"])

COMPONENTES_OBRIGATORIOS = COMPONENTES_POR_PRODUTO["gasolina"]

def _parse_valor_brl(txt: str):
    if not txt:
        return None
    m = re.search(r"R\$\s*([\d.]+,\d{2}|\d+,\d{2}|\d+\.\d{2})", txt)
    if not m:
        return None
    s = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None

def _parse_numero_simples(txt: str):
    if not txt:
        return None
    txt = str(txt).strip()
    m = re.match(r"^(\d{1,3}(?:\.\d{3})*,\d{1,2}|\d+,\d{1,2})$", txt)
    if m:
        try:
            return float(txt.replace(".", "").replace(",", "."))
        except ValueError:
            pass
    try:
        return float(txt.replace(",", "."))
    except ValueError:
        return None

def _scrape_petrobras_pagina(url: str):
    try:
        r = _sessao.get(url, timeout=25, allow_redirects=True)
        if r.status_code != 200:
            print(f"⚠️ [Petrobras] HTTP {r.status_code} em {url}")
            return None

        soup = BeautifulSoup(r.content, "html.parser")
        texto = soup.get_text(separator="\n")
        linhas = [l.strip() for l in texto.split("\n") if l.strip()]
        dados = {}

        for l in linhas:
            m = re.search(r"(\d{2}/\d{2}/\d{4})\s*a\s*(\d{2}/\d{2}/\d{4})", l)
            if m:
                dados["periodo"] = f"{m.group(1)} a {m.group(2)}"
                break

        for i, l in enumerate(linhas):
            if "preço médio" in l.lower() and "brasil" not in l.lower():
                for j in range(i, min(len(linhas), i + 5)):
                    v = _parse_numero_simples(linhas[j])
                    if v is not None and 0.5 < v < 500:
                        dados["preco_medio_final"] = round(v, 2)
                        break
                if "preco_medio_final" in dados:
                    break

        for i, l in enumerate(linhas):
            if "preço médio do brasil" in l.lower():
                for j in range(i, min(len(linhas), i + 4)):
                    v = _parse_numero_simples(linhas[j])
                    if v is not None and 0.5 < v < 500:
                        dados["preco_medio_brasil"] = round(v, 2)
                        break
                if "preco_medio_brasil" not in dados:
                    v = _parse_valor_brl(l)
                    if v is not None and 0.5 < v < 500:
                        dados["preco_medio_brasil"] = round(v, 2)
                break

        rotulos = {
            "parcela petrobras": "parcela_petrobras",
            "parcela da petrobras": "parcela_petrobras",
            "impostos federais": "impostos_federais",
            "imposto federal": "impostos_federais",
            "imposto estadual": "icms",
            "icms": "icms",
            "custo etanol anidro": "biocombustivel",
            "custo do etanol anidro": "biocombustivel",
            "etanol anidro": "biocombustivel",
            "biodiesel": "biocombustivel",
            "custo biodiesel": "biocombustivel",
            "distribuicao e revenda": "margem_distribuicao_revenda",
            "distribuição e revenda": "margem_distribuicao_revenda",
            "margem de distribuicao": "margem_distribuicao_revenda",
            "margem de distribuição": "margem_distribuicao_revenda",
        }

        idx_inicio_visual = 0
        for i, l in enumerate(linhas):
            if "preço médio" in l.lower() and (">" in l or ":" in l):
                idx_inicio_visual = i
                break

        valores_com_pos = []
        for i, l in enumerate(linhas):
            if l.strip() in ("R$", "R$:"):
                if i + 1 < len(linhas):
                    v = _parse_numero_simples(linhas[i + 1])
                    if v is not None and 0 <= v < 500:
                        valores_com_pos.append((i, v))
            else:
                v = _parse_valor_brl(l)
                if v is not None and 0 <= v < 500:
                    valores_com_pos.append((i, v))

        for i, l in enumerate(linhas):
            if i < idx_inicio_visual:
                continue
            ll = normalizar_texto(l).lower()
            for chave, campo in rotulos.items():
                if campo in dados:
                    continue
                chave_norm = normalizar_texto(chave).lower()
                if chave_norm == ll or chave_norm in ll:
                    melhor_valor = None
                    melhor_dist = 999
                    for idx_valor, valor in valores_com_pos:
                        if idx_valor < i and idx_valor >= i - 6:
                            dist = i - idx_valor
                            if dist < melhor_dist:
                                melhor_dist = dist
                                melhor_valor = valor
                    if melhor_valor is not None:
                        dados[campo] = round(melhor_valor, 2)
                    break

        if len(dados) < len(COMPONENTES_OBRIGATORIOS) + 1:
            html_text = r.text
            regexes = {
                "parcela_petrobras": [
                    r'"?parcela[_ ]?petrobras"?\s*[:=]\s*"?([\d.,]+)',
                    r'"?parcelaPetrobras"?\s*[:=]\s*"?([\d.,]+)',
                ],
                "impostos_federais": [
                    r'"?impostos?[_ ]?federais?"?\s*[:=]\s*"?([\d.,]+)',
                    r'"?impostoFederal"?\s*[:=]\s*"?([\d.,]+)',
                ],
                "icms": [
                    r'"?icms"?\s*[:=]\s*"?([\d.,]+)',
                    r'"?imposto[_ ]?estadual"?\s*[:=]\s*"?([\d.,]+)',
                ],
                "biocombustivel": [
                    r'"?biocombustivel"?\s*[:=]\s*"?([\d.,]+)',
                    r'"?custo[_ ]?etanol[_ ]?anidro"?\s*[:=]\s*"?([\d.,]+)',
                    r'"?biodiesel"?\s*[:=]\s*"?([\d.,]+)',
                ],
                "margem_distribuicao_revenda": [
                    r'"?margem[_ ]?distribuicao[_ ]?revenda"?\s*[:=]\s*"?([\d.,]+)',
                    r'"?distribuicao[_ ]?revenda"?\s*[:=]\s*"?([\d.,]+)',
                ],
            }
            for campo, patterns in regexes.items():
                if campo in dados:
                    continue
                for pat in patterns:
                    m = re.search(pat, html_text, re.IGNORECASE)
                    if m:
                        try:
                            val = _parse_numero_simples(m.group(1))
                            if val is not None and 0 <= val < 500:
                                dados[campo] = round(val, 2)
                                break
                        except (ValueError, TypeError):
                            continue

        return dados if dados else None
    except Exception as e:
        print(f"⚠️ [Petrobras] erro em {url}: {e}")
        return None

@app.get("/api/debug-petrobras")
def debug_petrobras(uf: str = "SP", produto: str = "gasolina"):
    slug = PETROBRAS_SLUG.get(produto.lower())
    if not slug:
        raise HTTPException(status_code=400, detail="Produto inválido")
    url = f"https://precos.petrobras.com.br/w/preco-{slug}-{uf.lower()}"
    try:
        r = _sessao.get(url, timeout=25, allow_redirects=True)
        if r.status_code != 200:
            return {"erro": f"HTTP {r.status_code}", "url": url}
        soup = BeautifulSoup(r.content, "html.parser")
        texto = soup.get_text(separator="\n")
        linhas = [l.strip() for l in texto.split("\n") if l.strip()]
        dados_parser = _scrape_petrobras_pagina(url)
        return {
            "url": url,
            "status": r.status_code,
            "dados_extraidos": dados_parser,
            "total_linhas": len(linhas),
            "linhas_relevantes": [
                {"idx": i, "linha": l}
                for i, l in enumerate(linhas)
                if any(k in normalizar_texto(l).lower() for k in [
                    "parcela", "imposto", "icms", "etanol", "biodiesel",
                    "distribu", "revenda", "preço médio", "preco medio",
                ]) or l.strip() == "R$"
            ][:80],
        }
    except Exception as e:
        return {"erro": str(e), "url": url}

def _urls_petrobras(produto: str, uf: str):
    slug = PETROBRAS_SLUG.get(produto)
    if not slug:
        return []
    uf_l = (uf or "br").lower()
    return [f"https://precos.petrobras.com.br/w/preco-{slug}-{uf_l}"]

def _scrape_petrobras_completo(produto: str, uf: str):
    candidatas = _urls_petrobras(produto, uf)
    obrigatorios = _componentes_obrigatorios(produto)
    melhor = None
    melhor_url = None
    melhor_score = -1
    for url in candidatas:
        print(f"🌐 [Petrobras] tentando: {url}")
        d = _scrape_petrobras_pagina(url)
        if not d:
            continue
        score = sum(1 for c in obrigatorios if c in d)
        if score > melhor_score:
            melhor, melhor_url, melhor_score = d, url, score
        if score == len(obrigatorios):
            print(f"✅ [Petrobras] URL completa encontrada: {url}")
            return d, url
    if melhor:
        print(f"⚠️ [Petrobras] melhor resultado teve {melhor_score}/{len(obrigatorios)} componentes: {melhor_url}")
    return melhor, melhor_url

def raspar_composicao_petrobras(produto="gasolina", uf="BR"):
    uf_norm = (uf or "BR").upper().strip()
    produto_norm = produto.lower().strip()
    if produto_norm not in PETROBRAS_SLUG:
        return None

    obrigatorios = _componentes_obrigatorios(produto_norm)

    cache_key = _petrobras_cache_key(produto_norm, uf_norm)
    agora = time.time()
    if cache_key in PETROBRAS_CACHE:
        dados_cache, ts = PETROBRAS_CACHE[cache_key]
        if agora - ts < PETROBRAS_CACHE_TTL:
            dados_cache["cache_origem"] = "memoria"
            print(f"⚡ [Petrobras] cache memória para {produto_norm}/{uf_norm}")
            return dados_cache

    dados_disco = _ler_petrobras_cache_disco(produto_norm, uf_norm)
    if dados_disco:
        if all(c in dados_disco for c in obrigatorios):
            return dados_disco
        print(f"🗑️ [Petrobras] cache disco incompleto descartado para {produto_norm}/{uf_norm}")
        try:
            os.remove(_petrobras_cache_path(produto_norm, uf_norm))
        except Exception:
            pass

    dados_estado, url_estado_usada = (None, None)
    if uf_norm != "BR":
        print(f"🌐 [Petrobras] raspando estado {uf_norm}...")
        dados_estado, url_estado_usada = _scrape_petrobras_completo(produto_norm, uf_norm)

    print(f"🌐 [Petrobras] raspando Brasil...")
    dados_brasil, url_brasil_usada = _scrape_petrobras_completo(produto_norm, "BR")

    dados = None
    origem = None
    if dados_estado and all(c in dados_estado for c in obrigatorios):
        dados = dict(dados_estado); origem = "estado"
    elif dados_brasil and all(c in dados_brasil for c in obrigatorios):
        dados = dict(dados_brasil); origem = "brasil_fallback"
    elif dados_estado:
        dados = dict(dados_estado); origem = "estado_incompleto"
    elif dados_brasil:
        dados = dict(dados_brasil); origem = "brasil_incompleto"
    else:
        print(f"❌ [Petrobras] nada raspado para {produto_norm}/{uf_norm}")
        return None

    dados["uf_consultada"] = uf_norm
    dados["origem_uf"] = origem
    if url_estado_usada:
        dados["url_estado"] = url_estado_usada
    if url_brasil_usada:
        dados["url_brasil"] = url_brasil_usada

    if dados_brasil and "preco_medio_final" in dados_brasil and "preco_medio_brasil" not in dados:
        dados["preco_medio_brasil"] = dados_brasil["preco_medio_final"]
    if dados_brasil and "periodo" in dados_brasil and "periodo" not in dados:
        dados["periodo"] = dados_brasil["periodo"]

    if produto_norm in ("glp", "gnv"):
        dados.setdefault("biocombustivel", 0.0)

    if all(c in dados for c in obrigatorios) and "preco_medio_final" in dados:
        soma = sum(dados.get(c, 0) for c in obrigatorios)
        final = dados["preco_medio_final"]
        if final > 0:
            desvio = abs(soma - final) / final
            dados["validacao_soma"] = {
                "soma_componentes": round(soma, 2),
                "preco_final": round(final, 2),
                "desvio_percentual": round(desvio * 100, 2),
                "consistente": desvio <= 0.05,
            }

    dados["cache_origem"] = "scraping"

    if all(c in dados for c in obrigatorios):
        PETROBRAS_CACHE[cache_key] = (dados, agora)
        _gravar_petrobras_cache_disco(produto_norm, uf_norm, dados)
        print(f"✅ [Petrobras] scraping completo e cache gravado para {produto_norm}/{uf_norm}")
    else:
        faltando = [c for c in obrigatorios if c not in dados]
        print(f"⚠️ [Petrobras] scraping incompleto (faltam {faltando}) — não gravando cache")

    return dados

@app.get("/api/composicao")
def get_composicao(uf: str = "BR", produto: str = "gasolina"):
    produto_lower = produto.lower()
    dados = raspar_composicao_petrobras(produto, uf=uf)

    obrigatorios = _componentes_obrigatorios(produto_lower)
    incompleto = not dados or any(c not in dados for c in obrigatorios)

    if incompleto:
        fallback = {
            "gasolina": {"parcela_petrobras": 2.06, "impostos_federais": 0.24, "icms": 1.57, "biocombustivel": 0.98, "margem_distribuicao_revenda": 1.51, "preco_medio_final": 6.36, "preco_medio_brasil": 6.55, "periodo": None},
            "diesel":   {"parcela_petrobras": 2.81, "impostos_federais": 0.32, "icms": 1.17, "biocombustivel": 0.81, "margem_distribuicao_revenda": 2.17, "preco_medio_final": 7.29, "preco_medio_brasil": 7.35, "periodo": None},
            "gnv":      {"parcela_petrobras": 2.40, "impostos_federais": 0.18, "icms": 1.20, "biocombustivel": 0.00, "margem_distribuicao_revenda": 0.90, "preco_medio_final": 4.68, "periodo": None},
            "glp":      {"parcela_petrobras": 34.74, "impostos_federais": 0.00, "icms": 19.11, "biocombustivel": 0.00, "margem_distribuicao_revenda": 62.68, "preco_medio_final": 116.53, "preco_medio_brasil": 114.88, "periodo": None},
        }
        parcial = dados or {}
        dados = dict(fallback.get(produto_lower, fallback["gasolina"]))
        for k, v in parcial.items():
            if k not in dados or not dados.get(k):
                dados[k] = v
        dados["fonte"] = "fallback_estatico"
        dados["aviso"] = "Não foi possível obter todos os componentes do scraping da Petrobras. Exibindo valores de referência."
    else:
        dados["fonte"] = "scraping_petrobras"
        if produto_lower in ("glp", "gnv"):
            dados.setdefault("biocombustivel", 0.0)

    if dados.get("periodo") in ("Fallback estático", "", None):
        dados["periodo"] = None

    total = dados.get("preco_medio_final", 0)
    if total and total > 0:
        dados["percentuais"] = {
            "parcela_petrobras": round(dados.get("parcela_petrobras", 0) / total * 100, 1),
            "impostos_federais": round(dados.get("impostos_federais", 0) / total * 100, 1),
            "icms": round(dados.get("icms", 0) / total * 100, 1),
            "biocombustivel": round(dados.get("biocombustivel", 0) / total * 100, 1),
            "margem_distribuicao_revenda": round(dados.get("margem_distribuicao_revenda", 0) / total * 100, 1),
        }
    return {"uf": uf, "produto": produto, **dados}

# ======================== ANP RESUMO ========================
@app.get("/api/anp-resumo")
def anp_resumo(uf: str, produto: str = "gasolina"):
    uf_norm = normalizar_texto(uf)
    produto_norm = produto.lower().strip()

    caminho = obter_csv_mais_recente(produto_norm)
    if not caminho:
        return {"uf": uf, "produto": produto, "escopo": "estado", "erro": "CSV indisponível"}

    try:
        df = pd.read_csv(caminho, sep=";", encoding="latin1", decimal=",")
        df.columns = [c.replace("ï»¿", "").strip() for c in df.columns]
        cols = _detectar_colunas(df)
        if not all([cols["municipio"], cols["uf"], cols["produto"], cols["valor"]]):
            return {"uf": uf, "produto": produto, "escopo": "estado", "erro": "Colunas não detectadas"}

        df["_uf_norm"] = df[cols["uf"]].astype(str).apply(normalizar_texto)
        df["_produto_norm"] = df[cols["produto"]].astype(str).apply(normalizar_texto)

        prod_alvo = produto_norm.upper()
        if produto_norm == "gasolina":
            mask = df["_produto_norm"] == "GASOLINA"
        elif produto_norm == "etanol":
            mask = df["_produto_norm"].str.contains("ETANOL", na=False)
        elif produto_norm == "diesel":
            mask = df["_produto_norm"].str.contains("DIESEL", na=False)
        elif produto_norm == "gnv":
            mask = df["_produto_norm"].str.contains("GNV", na=False)
        elif produto_norm == "glp":
            mask = df["_produto_norm"].str.contains("GLP", na=False)
        else:
            mask = df["_produto_norm"].str.contains(prod_alvo, na=False)

        df_f = df[(df["_uf_norm"] == uf_norm) & mask]

        precos = []
        for v in df_f[cols["valor"]].tolist():
            try:
                p = float(str(v).replace(",", "."))
                if validar_preco(produto_norm, p):
                    precos.append(p)
            except (ValueError, TypeError):
                continue

        if not precos:
            return {"uf": uf, "produto": produto, "escopo": "estado", "erro": "Sem preços válidos"}

        periodo = _extrair_data_referencia(df) or {}
        if not periodo:
            m = re.search(r"(\d{2})(\d{2})(\d{4})_(\d{2})(\d{2})", os.path.basename(caminho))
            if m:
                d, mo, y, h, mi = m.groups()
                periodo = {"data_inicial": f"{d}/{mo}/{y}", "data_final": f"{d}/{mo}/{y}"}

        return {
            "uf": uf,
            "produto": produto,
            "escopo": "estado",
            "minimo": round(min(precos), 2),
            "media":  round(sum(precos) / len(precos), 2),
            "maximo": round(max(precos), 2),
            "total_postos": len(precos),
            "periodo": periodo,
            "fonte": "anp_csv",
        }
    except Exception as e:
        print(f"❌ [ANP resumo] erro: {e}")
        return {"uf": uf, "produto": produto, "escopo": "estado", "erro": str(e)}

# ======================== CACHE PETROBRAS ========================
@app.get("/api/petrobras-cache-info")
def petrobras_cache_info():
    if not os.path.exists(CACHE_PETROBRAS_DIR):
        return {"total": 0, "arquivos": []}
    arquivos = glob.glob(os.path.join(CACHE_PETROBRAS_DIR, "*.json"))
    itens = []
    for c in arquivos:
        try:
            with open(c, "r", encoding="utf-8") as f:
                cached = json.load(f)
            idade = time.time() - cached.get("_ts", 0)
            itens.append({
                "arquivo": os.path.basename(c),
                "produto": cached.get("produto"),
                "uf": cached.get("uf"),
                "idade_segundos": round(idade),
                "valido": idade < PETROBRAS_CACHE_TTL,
                "periodo_dado": (cached.get("dados") or {}).get("periodo"),
                "preco_final": (cached.get("dados") or {}).get("preco_medio_final"),
            })
        except Exception:
            pass
    itens.sort(key=lambda x: x["idade_segundos"])
    return {"total": len(itens), "ttl_segundos": PETROBRAS_CACHE_TTL, "arquivos": itens}

@app.post("/api/petrobras-cache-limpar")
def petrobras_cache_limpar():
    if not os.path.exists(CACHE_PETROBRAS_DIR):
        return {"status": "ok", "total_apagados": 0}
    arquivos = glob.glob(os.path.join(CACHE_PETROBRAS_DIR, "*.json"))
    apagados = 0
    for c in arquivos:
        try:
            os.remove(c)
            apagados += 1
        except Exception:
            pass
    PETROBRAS_CACHE.clear()
    return {"status": "ok", "total_apagados": apagados}

# ======================== ELETROPOSTOS (OCM + Carregados) ========================
def _buscar_eletropostos_ocm(lat: float, lon: float, raio_km: float, max_results: int = 500):
    """OCM — API pública, gratuita, sem token obrigatório. Fonte principal."""
    try:
        params = {
            "key": OCM_API_KEY, "output": "json",
            "latitude": lat, "longitude": lon,
            "distance": raio_km, "distanceunit": "KM",
            "maxresults": max_results, "compact": True, "verbose": True,
        }
        r = _sessao.get(f"{OCM_BASE_URL}/poi/", params=params, timeout=45)
        if r.status_code != 200:
            return []
        try:
            return r.json()
        except ValueError:
            return []
    except Exception as e:
        print(f"⚠️ [OCM] erro de rede: {e}")
        return []

def _buscar_eletropostos_carregados(lat: float, lon: float, raio_km: float):
    """Carregados CLI — consulta por coordenada não suportada. Retorna []."""
    print("ℹ️ [Carregados] consulta por coordenada não suportada. Pulando.")
    return []

def _buscar_eletropostos_carregados_por_cidade(municipio: str, uf: str):
    """Busca eletropostos do Carregados CLI filtrando por cidade e estado."""
    if not municipio or not uf:
        return []
    try:
        cmd = f'{CARREGADOS_CLI} stations search --estado "{uf}" --cidade "{municipio}" --json'
        print(f"🌐 [Carregados] executando: {cmd}")
        resultado = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=CARREGADOS_TIMEOUT
        )
        if resultado.returncode != 0:
            print(f"⚠️ [Carregados] erro na execução: {resultado.stderr[:300]}")
            return []
        saida = resultado.stdout.strip()
        if not saida:
            return []
        dados = json.loads(saida)
        itens = dados if isinstance(dados, list) else dados.get("stations") or dados.get("data") or []
        print(f"✅ [Carregados] {len(itens)} estações recebidas")
        return itens
    except subprocess.TimeoutExpired:
        print("❌ [Carregados] timeout na execução do CLI")
        return []
    except json.JSONDecodeError as e:
        print(f"❌ [Carregados] erro ao parsear JSON: {e}")
        return []
    except Exception as e:
        print(f"❌ [Carregados] erro: {e}")
        return []

def _formatar_eletroposto_ocm(item, lat_origem=None, lon_origem=None):
    endereco = item.get("AddressInfo") or {}
    conexoes = item.get("Connections") or []
    conexoes_formatadas = []
    custo_estimado_total = 0
    potencia_total = 0
    for c in conexoes[:5]:
        pot = c.get("PowerKW") or 0
        potencia_total += pot
        preco_estimado_kwh = 2.50 if pot >= 22 else 1.50
        custo_sessao = round(preco_estimado_kwh * 30, 2)
        custo_estimado_total += custo_sessao
        conexoes_formatadas.append({
            "tipo": (c.get("ConnectionType") or {}).get("Title") if c.get("ConnectionType") else "N/A",
            "potencia_kw": pot,
            "quantidade": c.get("Quantity", 1),
            "preco_estimado_kwh": preco_estimado_kwh,
            "custo_estimado_30kwh": custo_sessao,
        })
    lat = endereco.get("Latitude")
    lon = endereco.get("Longitude")
    distancia = None
    if lat is not None and lon is not None and lat_origem is not None and lon_origem is not None:
        distancia = calcular_distancia_km(lat_origem, lon_origem, lat, lon)
    return {
        "id": item.get("ID"),
        "nome": endereco.get("Title"),
        "endereco": endereco.get("AddressLine1"),
        "cidade": endereco.get("Town"),
        "uf": endereco.get("StateOrProvince"),
        "latitude": lat,
        "longitude": lon,
        "distancia_km": distancia,
        "operador": (item.get("OperatorInfo") or {}).get("Title") if item.get("OperatorInfo") else "Não informado",
        "conexoes": conexoes_formatadas,
        "potencia_total_kw": potencia_total,
        "custo_estimado_sessao": round(custo_estimado_total, 2) if conexoes_formatadas else 0,
        "observacao_custo": "Estimativa baseada em tarifas médias: R$1,50/kWh (AC) e R$2,50/kWh (DC), sessão média de 30 kWh",
        "status": (item.get("StatusType") or {}).get("Title") if item.get("StatusType") else "Disponível",
        "fonte": "openchargemap"
    }

def _formatar_eletroposto_carregados(item, lat_origem=None, lon_origem=None):
    lat = item.get("latitude") or item.get("lat")
    lon = item.get("longitude") or item.get("lng") or item.get("lon")
    distancia = None
    if lat is not None and lon is not None and lat_origem is not None and lon_origem is not None:
        distancia = calcular_distancia_km(lat_origem, lon_origem, lat, lon)
    conexoes = []
    for c in (item.get("chargers") or item.get("connectors") or []):
        pot = c.get("power_kw") or c.get("power") or 0
        conexoes.append({
            "tipo": c.get("type") or c.get("connector_type") or "N/A",
            "potencia_kw": pot,
            "quantidade": c.get("quantity", 1),
            "preco_estimado_kwh": 2.50 if pot >= 22 else 1.50,
            "custo_estimado_30kwh": round((2.50 if pot >= 22 else 1.50) * 30, 2),
        })
    if not conexoes:
        conexoes.append({
            "tipo": "N/A", "potencia_kw": 7, "quantidade": 1,
            "preco_estimado_kwh": 1.50, "custo_estimado_30kwh": 45.00,
        })
    return {
        "id": item.get("id"),
        "nome": item.get("name"),
        "endereco": item.get("address"),
        "cidade": item.get("city"),
        "uf": item.get("state"),
        "latitude": lat,
        "longitude": lon,
        "distancia_km": distancia,
        "operador": item.get("operator") or "Não informado",
        "conexoes": conexoes,
        "potencia_total_kw": max([c["potencia_kw"] for c in conexoes], default=0),
        "custo_estimado_sessao": sum(c["custo_estimado_30kwh"] for c in conexoes),
        "observacao_custo": "Dados do Carregados (estimativa de custo baseada em tarifas médias)",
        "status": "Disponível",
        "fonte": "carregados"
    }

def _fundir_eletropostos(ocm_lista: list, carregados_lista: list) -> list:
    """Funde OCM + Carregados, removendo duplicatas por coordenada (~100m). Carregados tem prioridade."""
    unicos = {}
    for est in carregados_lista:
        if est.get("latitude") and est.get("longitude"):
            chave = (round(est["latitude"], 3), round(est["longitude"], 3))
            if chave not in unicos:
                unicos[chave] = est
    for est in ocm_lista:
        if est.get("latitude") and est.get("longitude"):
            chave = (round(est["latitude"], 3), round(est["longitude"], 3))
            if chave not in unicos:
                unicos[chave] = est
    resultado = list(unicos.values())
    resultado.sort(key=lambda x: x.get("distancia_km") if x.get("distancia_km") is not None else 999999)
    return resultado

@app.get("/api/eletropostos")
def get_eletropostos(
    latitude: float = Query(None), longitude: float = Query(None),
    municipio: str = Query(None), uf: str = Query(None),
    raio_km: float = Query(ELETRO_RAIO_CIDADE_KM),
    max_resultados: int = Query(ELETRO_MAX_RESULTADOS),
):
    if latitude is None or longitude is None:
        raise HTTPException(status_code=400, detail="Informe latitude e longitude.")
    municipio_norm = normalizar_texto(municipio) if municipio else None

    ocm_itens = _buscar_eletropostos_ocm(latitude, longitude, raio_km, max_resultados)
    ocm_formatados = [_formatar_eletroposto_ocm(i, latitude, longitude) for i in ocm_itens]

    carregados_itens = []
    if municipio and uf:
        carregados_itens = _buscar_eletropostos_carregados_por_cidade(municipio, uf)
    carregados_formatados = [_formatar_eletroposto_carregados(i, latitude, longitude) for i in carregados_itens]

    todos = _fundir_eletropostos(ocm_formatados, carregados_formatados)

    if municipio_norm and raio_km < 50:
        filtrados = []
        for est in todos:
            cidade_est = normalizar_texto(est.get("cidade") or "")
            if cidade_est == municipio_norm or est.get("fonte") == "carregados":
                filtrados.append(est)
        if filtrados:
            todos = filtrados

    if not todos:
        return {"total": 0, "modo_busca": "vazio", "raio_usado_km": raio_km,
                "municipio_filtrado": municipio, "eletropostos": []}

    total_ocm = sum(1 for e in todos if e.get("fonte") == "openchargemap")
    total_carregados = sum(1 for e in todos if e.get("fonte") == "carregados")

    return {
        "total": len(todos),
        "total_ocm": total_ocm,
        "total_carregados": total_carregados,
        "modo_busca": "fusao",
        "raio_usado_km": raio_km,
        "municipio_filtrado": municipio,
        "mensagem": f"{len(todos)} eletropostos (OCM: {total_ocm}, Carregados: {total_carregados})",
        "eletropostos": todos,
    }

@app.get("/api/eletropostos-cache-info")
def eletropostos_cache_info():
    if not os.path.exists(CACHE_ELETRO_DIR):
        return {"total": 0, "arquivos": []}
    arquivos = glob.glob(os.path.join(CACHE_ELETRO_DIR, "*.json"))
    itens = []
    for c in arquivos:
        try:
            with open(c, "r", encoding="utf-8") as f:
                cached = json.load(f)
            idade = time.time() - cached.get("_ts", 0)
            itens.append({
                "arquivo": os.path.basename(c),
                "raio_km": cached.get("raio_km"),
                "total_resultados": cached.get("total_resultados"),
                "idade_segundos": round(idade),
                "valido": idade < ELETRO_CACHE_TTL_HORAS * 3600,
            })
        except Exception:
            pass
    itens.sort(key=lambda x: x["idade_segundos"])
    return {"total": len(itens), "ttl_horas": ELETRO_CACHE_TTL_HORAS, "arquivos": itens}

# ======================== AGENDADOR ========================
def verificar_e_baixar_novos_csvs():
    print(f"\n🔍 [AGENDADOR] Verificando CSVs...")
    resultado = garantir_todos_csvs(force_rebaixar_antigos=True)
    for produto, info in resultado.items():
        print(f"  {produto}: {info.get('acao')} (ok={info.get('ok')})")
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
    print(f"⏰ Agendador iniciado (intervalo: {INTERVALO_VERIFICACAO_HORAS}h).")
    print("✅ [Eletropostos] Fontes: Open Charge Map (principal) + Carregados CLI (complementar)")
    try:
        garantir_todos_csvs(force_rebaixar_antigos=True)
    except Exception as e:
        print(f"⚠️ [startup] erro ao garantir CSVs: {e}")

@app.on_event("shutdown")
def parar_agendador():
    scheduler.shutdown()
    print("⏰ Agendador parado.")