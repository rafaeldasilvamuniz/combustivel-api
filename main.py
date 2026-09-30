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

# ⚠️ IMPORTANTE: gere uma nova chave em https://openchargemap.org/site/develop/api
OCM_API_KEY = "d75c2b4f-371d-4514-9f57-fbe8330538fa"
OCM_BASE_URL = "https://api.openchargemap.io/v3"

# ✅ Configurações de busca de eletropostos
ELETRO_RAIO_CIDADE_KM = 25        # raio inicial para achar a própria cidade
ELETRO_RAIO_VIZINHAS_KM = 75      # raio estendido para cidades vizinhas
ELETRO_RAIO_EMERGENCIA_KM = 150   # raio de emergência (último recurso)
ELETRO_MAX_RESULTADOS = 500       # máximo por chamada OCM
ELETRO_CACHE_TTL_HORAS = 24       # eletropostos mudam pouco

PETROBRAS_CACHE = {}
PETROBRAS_CACHE_TTL = 3600
INTERVALO_VERIFICACAO_HORAS = 6
DIAS_PARA_CONSIDERAR_ANTIGO = 30
IDADE_MAXIMA_CSV_DIAS = 1

API_ANP_REVENDEDORES = "https://revendedoresapi.anp.gov.br/v1/combustivel"
API_CACHE_TTL_HORAS = 24

BRASILAPI_CEP_URL = "https://brasilapi.com.br/api/cep/v2/{cep}"
CEP_CACHE_TTL_DIAS = 90

# ✅ Todos os 5 produtos
PRODUTOS = ["gasolina", "etanol", "diesel", "gnv", "glp"]

# ✅ Faixas plausíveis de preço
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

# ✅ URLs de fallback (caso a descoberta dinâmica falhe)
CSV_URLS_ANP = {
    "gasolina": [
        "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-gasolina-etanol.csv",
    ],
    "etanol": [
        "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-gasolina-etanol.csv",
    ],
    "diesel": [
        "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-diesel-gnv.csv",
    ],
    "gnv": [
        "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-diesel-gnv.csv",
    ],
    "glp": [
        "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/shpc/qus/ultimas-4-semanas-glp.csv",
    ],
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

PRODUTO_PALAVRAS = {
    "gasolina": ["gasolina"],
    "etanol": ["etanol"],
    "diesel": ["diesel"],
    "gnv": ["gnv"],
    "glp": ["glp"],
}

# ======================== SESSÃO PERSISTENTE ========================
_sessao = requests.Session()
_sessao.headers.update(HEADERS_NAVEGADOR)

def _obter_session():
    return _sessao

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

def _eletro_cache_path(lat: float, lon: float, raio_km: float) -> str:
    chave = f"{lat:.4f}_{lon:.4f}_{raio_km:.0f}"
    h = hashlib.md5(chave.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_ELETRO_DIR, f"{h}.json")

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
    """Fórmula de Haversine — distância entre dois pontos em km."""
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
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"_ts": time.time(), "latitude": None, "longitude": None}, f)
        except Exception:
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

        if not urls_encontradas:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not href.lower().endswith(".csv"):
                    continue
                href_lower = href.lower()
                if "semana" not in href_lower and "qus" not in href_lower:
                    continue
                if href.startswith("/"):
                    href = "https://www.gov.br" + href
                elif not href.startswith("http"):
                    href = "https://www.gov.br/anp/" + href.lstrip("/")

                if "gasolina" in href_lower or "etanol" in href_lower:
                    urls_encontradas.setdefault("gasolina-etanol", href)
                elif "diesel" in href_lower or "gnv" in href_lower:
                    urls_encontradas.setdefault("diesel-gnv", href)
                elif "glp" in href_lower:
                    urls_encontradas.setdefault("glp", href)

        if urls_encontradas:
            print(f"✅ [ANP] descobertos {len(urls_encontradas)} links via scraping")
        else:
            print(f"⚠️ [ANP] nenhum link .csv encontrado na página")

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

    print(f"❌ [ANP] não foi possível descobrir URL para {produto_lower}")
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
                print(f"⚠️ [CSV] URL retornou vazio: {url}")
        else:
            print(f"⚠️ [CSV] URL retornou {status} (content-type: {ct}): {url}")
            r.close()
    except Exception as e:
        print(f"⚠️ [CSV] erro testando {url}: {e}")

    for url_alt in CSV_URLS_ANP.get(produto.lower(), []):
        if url_alt == url:
            continue
        try:
            print(f"🔗 [CSV] tentando fallback: {url_alt}")
            r = _sessao.get(url_alt, headers=HEADERS_CSV, timeout=20, allow_redirects=True, stream=True)
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code == 200 and "text/html" not in ct:
                chunk = next(r.iter_content(chunk_size=1), b"")
                r.close()
                if chunk:
                    print(f"✅ [CSV] fallback válido: {url_alt}")
                    return url_alt
        except Exception as e:
            print(f"⚠️ [CSV] fallback falhou: {e}")

    print(f"❌ [CSV] nenhuma URL funcionou para {produto}")
    return None

# ======================== HEALTH ========================
@app.get("/")
def health():
    return {"status": "ok", "service": "combustivel"}

# ======================== TESTES ========================
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

@app.get("/api/testar-csvs")
def testar_csvs():
    resultado = {}
    for produto, urls in CSV_URLS_ANP.items():
        resultado[produto] = []
        for url in urls:
            try:
                r = _sessao.get(url, headers=HEADERS_CSV, timeout=20, allow_redirects=True, stream=True)
                chunk = next(r.iter_content(chunk_size=200), b"")
                preview = chunk[:200].decode("latin1", errors="ignore") if chunk else ""
                resultado[produto].append({
                    "url": url,
                    "status": r.status_code,
                    "content_type": r.headers.get("content-type"),
                    "preview": preview.replace("\n", " ")[:150],
                })
                r.close()
            except Exception as e:
                resultado[produto].append({"url": url, "erro": str(e)})
    return resultado

@app.get("/api/descobrir-links-anp")
def descobrir_links_anp():
    urls = descobrir_urls_csv_anp()
    return {
        "total": len(urls),
        "links_encontrados": urls,
        "pagina_raspada": PAGINA_ANP,
    }

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
                print(f"⚠️ [baixar-csv] {tipo}: nenhuma URL válida. Usando CSV local ({idade:.1f} dias).")
                return {
                    "status": "ok_offline",
                    "arquivo": csv_local,
                    "url_usada": None,
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
                    "status": "ok_offline",
                    "arquivo": csv_local,
                    "url_usada": csv_url,
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
                "status": "ok_offline",
                "arquivo": csv_local,
                "url_usada": None,
                "tamanho_bytes": os.path.getsize(csv_local),
                "aviso": f"Erro no download: {str(e)}. Usando CSV local.",
            }
        return {"erro": str(e)}

# ======================== GARANTIR TODOS OS CSVs ========================
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
                "acao": "ok",
                "ok": True,
                "arquivo": caminho,
                "idade_dias": round(idade, 2) if idade is not None else None,
            }
    return status_por_produto

# ======================== LEITURA E PARSING DO CSV ========================
def _extrair_data_referencia(df: pd.DataFrame):
    col_ini = col_fim = None
    for col in df.columns:
        cl = col.lower()
        if "data" in cl and "inicial" in cl:
            col_ini = col
        if "data" in cl and "final" in cl:
            col_fim = col
    try:
        ini = str(df[col_ini].dropna().iloc[0]) if col_ini else None
        fim = str(df[col_fim].dropna().iloc[0]) if col_fim else None
        if ini or fim:
            return {"data_inicial": ini, "data_final": fim}
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
    municipio: str,
    uf: str,
    produto: str = None,
    incluir_sem_preco: bool = Query(True),
    geocodificar_cep: bool = Query(True),
):
    municipio_norm = normalizar_texto(municipio)
    uf_norm = normalizar_texto(uf)

    print(f"\n{'='*60}")
    print(f"🔍 {municipio}/{uf} - produto: {produto or 'todos'}")
    print(f"{'='*60}")

    postos_api = buscar_postos_anp_api(municipio, uf)
    print(f"📋 Base ANP: {len(postos_api)} postos autorizados em {municipio}/{uf}")

    if geocodificar_cep and postos_api:
        postos_api = enriquecer_postos_sem_coordenada(postos_api, limite=40)

    garantir_todos_csvs(force_rebaixar_antigos=True)

    csvs_disponiveis = {p: obter_csv_mais_recente(p) for p in PRODUTOS}

    if not any(csvs_disponiveis.values()):
        print("⚠️ Nenhum CSV disponível. Retornando apenas cadastro ANP.")
        if postos_api:
            postos_sem_preco = []
            for p in postos_api:
                postos_sem_preco.append({
                    **p,
                    "cnpj": normalizar_cnpj(p["cnpj"]),
                    "preco": 0.0,
                    "tem_preco": False,
                    "fonte_dado": "sem_csv",
                })
            postos_sem_preco.sort(
                key=lambda p: normalizar_texto(p.get("revenda") or p.get("nome_fantasia") or "")
            )
            return {
                "municipio": municipio, "uf": uf,
                "fonte_api_anp": len(postos_api),
                "aviso": "CSVs de preços indisponíveis — mostrando apenas cadastro de postos",
                "produtos": {
                    (produto or "gasolina").lower(): {
                        "media": None, "minimo": None, "maximo": None,
                        "total_postos": len(postos_sem_preco),
                        "total_com_preco": 0,
                        "total_sem_preco": len(postos_sem_preco),
                        "fonte_dado": "sem_csv",
                        "postos": postos_sem_preco,
                    }
                },
            }
        raise HTTPException(status_code=500, detail="Nenhum CSV disponível e nenhum posto na base ANP.")

    try:
        resultados = {}

        for tipo_csv, caminho in csvs_disponiveis.items():
            if not caminho:
                continue

            idade = idade_csv_dias(caminho)
            fonte_dado = "csv_anp_atual" if (idade is not None and idade <= IDADE_MAXIMA_CSV_DIAS) else "csv_anp_antigo"

            print(f"📂 Lendo: {caminho} (idade: {idade:.1f} dias)" if idade is not None else f"📂 Lendo: {caminho}")
            try:
                df = pd.read_csv(caminho, sep=";", encoding="latin1", decimal=",")
            except Exception as e:
                print(f"⚠️ Erro lendo {caminho}: {e}")
                continue

            df.columns = [c.replace("ï»¿", "").strip() for c in df.columns]

            data_ref = _extrair_data_referencia(df)
            cols = _detectar_colunas(df)

            if not all([cols["municipio"], cols["uf"], cols["produto"], cols["valor"]]):
                print(f"⚠️ Colunas obrigatórias faltando em {caminho}: {list(df.columns)}")
                continue

            df["_municipio_norm"] = df[cols["municipio"]].astype(str).apply(normalizar_texto)
            df["_uf_norm"] = df[cols["uf"]].astype(str).apply(normalizar_texto)
            df["_produto_norm"] = df[cols["produto"]].astype(str).apply(normalizar_texto)

            df_filtrado = df[(df["_municipio_norm"] == municipio_norm) & (df["_uf_norm"] == uf_norm)]
            if df_filtrado.empty:
                df_filtrado = df[(df["_municipio_norm"].str.contains(municipio_norm, na=False)) & (df["_uf_norm"] == uf_norm)]
            if df_filtrado.empty:
                continue

            mapeamento_produtos = [
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

                mapa_precos = {}
                descartados_outlier = 0
                if not df_prod.empty:
                    for _, row in df_prod.iterrows():
                        try:
                            preco = float(str(row[cols["valor"]]).replace(",", "."))
                        except (ValueError, TypeError):
                            continue
                        if not validar_preco(prod_label, preco):
                            descartados_outlier += 1
                            continue
                        cnpj_limpo = normalizar_cnpj(row[cols["cnpj"]]) if cols["cnpj"] else ""
                        if not cnpj_limpo:
                            continue
                        mapa_precos[cnpj_limpo] = {
                            "preco": round(preco, 2),
                            "bandeira": str(row[cols["bandeira"]]) if cols["bandeira"] else "",
                        }

                if descartados_outlier:
                    print(f"  ⚠️ {prod_label}: {descartados_outlier} preços fora da faixa plausível descartados")

                postos_merge = {}
                for p in postos_api:
                    cnpj_key = normalizar_cnpj(p["cnpj"])
                    postos_merge[cnpj_key] = {
                        **p,
                        "cnpj": cnpj_key,
                        "preco": 0.0,
                        "tem_preco": False,
                        "produto": prod_label.upper(),
                        "cnpj_orfao": False,
                        "fonte_dado": fonte_dado,
                    }

                for cnpj_limpo, dados_preco in mapa_precos.items():
                    if cnpj_limpo in postos_merge:
                        postos_merge[cnpj_limpo]["preco"] = dados_preco["preco"]
                        postos_merge[cnpj_limpo]["tem_preco"] = True
                        if dados_preco["bandeira"]:
                            postos_merge[cnpj_limpo]["bandeira"] = dados_preco["bandeira"]
                    else:
                        postos_merge[cnpj_limpo] = {
                            "cnpj": cnpj_limpo,
                            "revenda": f"Posto {cnpj_limpo}",
                            "endereco": "Endereço não disponível na base ANP",
                            "bairro": "",
                            "municipio": municipio,
                            "uf": uf,
                            "cep": "",
                            "bandeira": dados_preco["bandeira"],
                            "preco": dados_preco["preco"],
                            "tem_preco": True,
                            "produto": prod_label.upper(),
                            "latitude": None,
                            "longitude": None,
                            "aviso": "Endereço não disponível na base ANP",
                            "cnpj_orfao": True,
                            "fonte_dado": fonte_dado,
                        }

                postos_lista = list(postos_merge.values())

                precos_validos = [
                    p["preco"] for p in postos_lista
                    if isinstance(p.get("preco"), (int, float)) and p["preco"] > 0
                ]

                if not incluir_sem_preco:
                    postos_lista = [
                        p for p in postos_lista
                        if isinstance(p.get("preco"), (int, float)) and p["preco"] > 0
                    ]

                if not postos_lista:
                    continue

                if precos_validos:
                    media = round(sum(precos_validos) / len(precos_validos), 2)
                    minimo = round(min(precos_validos), 2)
                    maximo = round(max(precos_validos), 2)
                else:
                    media = minimo = maximo = None

                postos_lista.sort(key=lambda p: (
                    0 if (isinstance(p.get("preco"), (int, float)) and p["preco"] > 0) else 1,
                    p["preco"] if (isinstance(p.get("preco"), (int, float)) and p["preco"] > 0) else 9999,
                    normalizar_texto(p.get("revenda") or p.get("nome_fantasia") or ""),
                ))

                com_preco = sum(1 for p in postos_lista if isinstance(p.get("preco"), (int, float)) and p["preco"] > 0)
                sem_preco = len(postos_lista) - com_preco
                orfaos = sum(1 for p in postos_lista if p.get("cnpj_orfao"))

                print(f"  ✅ {prod_label}: {len(postos_lista)} postos ({com_preco} c/ preço, {sem_preco} s/ preço, {orfaos} órfãos)")

                if prod_label not in resultados or len(postos_lista) > resultados[prod_label]["total_postos"]:
                    resultados[prod_label] = {
                        "media": media,
                        "minimo": minimo,
                        "maximo": maximo,
                        "total_postos": len(postos_lista),
                        "total_com_preco": com_preco,
                        "total_sem_preco": sem_preco,
                        "total_cnpj_orfao": orfaos,
                        "fonte_dado": fonte_dado,
                        "data_referencia": data_ref,
                        "postos": postos_lista,
                    }

        if not resultados:
            return {
                "erro": "Nenhum posto encontrado",
                "municipio": municipio, "uf": uf,
                "produtos": {}, "fonte_api_anp": len(postos_api),
            }

        return {
            "municipio": municipio, "uf": uf,
            "fonte_api_anp": len(postos_api),
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

# ======================== STATUS AGREGADO ========================
@app.get("/api/status-dados")
def status_dados():
    resultado = {
        "csvs": {},
        "api_anp": None,
        "petrobras": {},
        "agendador": {
            "intervalo_horas": INTERVALO_VERIFICACAO_HORAS,
            "rodando": scheduler.running if 'scheduler' in globals() else False,
        },
    }

    for produto in PRODUTOS:
        caminho = obter_csv_mais_recente(produto)
        idade = idade_csv_dias(caminho)
        resultado["csvs"][produto] = {
            "arquivo": caminho,
            "existe": caminho is not None,
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
def raspar_composicao_petrobras(produto="gasolina", uf="BR"):
    uf_norm = (uf or "BR").upper().strip()
    cache_key = _petrobras_cache_key(produto, uf_norm)
    agora = time.time()

    if cache_key in PETROBRAS_CACHE:
        dados_cache, timestamp = PETROBRAS_CACHE[cache_key]
        if agora - timestamp < PETROBRAS_CACHE_TTL:
            dados_cache["cache_origem"] = "memoria"
            print(f"⚡ [Petrobras] cache memória para {produto}/{uf_norm}")
            return dados_cache

    dados_disco = _ler_petrobras_cache_disco(produto, uf_norm)
    if dados_disco:
        return dados_disco

    urls = {
        "gasolina": "https://precos.petrobras.com.br/precos-gasolina",
        "diesel": "https://precos.petrobras.com.br/precos-diesel",
        "glp": "https://precos.petrobras.com.br/precos-glp",
    }
    url = urls.get(produto.lower())
    if not url:
        return None

    params = {}
    if uf_norm and uf_norm != "BR":
        params["estado"] = uf_norm

    try:
        r = _sessao.get(url, params=params, timeout=15)
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
            "Imposto Estadual": "icms", "ICMS": "icms",
            "Custo Etanol Anidro": "biocombustivel", "Biodiesel": "biocombustivel",
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
            print(f"⚠️ [Petrobras] scraping incompleto para {produto}/{uf_norm}, campos: {list(dados.keys())}")
            return None

        soma = sum(dados.get(c, 0) for c in [
            "parcela_petrobras", "impostos_federais", "icms",
            "biocombustivel", "margem_distribuicao_revenda"
        ])
        final = dados["preco_medio_final"]
        if final > 0:
            desvio = abs(soma - final) / final
            dados["validacao_soma"] = {
                "soma_componentes": round(soma, 2),
                "preco_final": round(final, 2),
                "desvio_percentual": round(desvio * 100, 2),
                "consistente": desvio <= 0.05,
            }

        dados["uf_consultada"] = uf_norm
        dados["cache_origem"] = "scraping"

        PETROBRAS_CACHE[cache_key] = (dados, agora)
        _gravar_petrobras_cache_disco(produto, uf_norm, dados)
        print(f"✅ [Petrobras] scraping OK para {produto}/{uf_norm} e cache gravado")

        return dados
    except Exception as e:
        print(f"❌ Erro scraping Petrobras {produto}/{uf_norm}: {e}")
        caminho = _petrobras_cache_path(produto, uf_norm)
        if os.path.exists(caminho):
            try:
                with open(caminho, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                dados = cached.get("dados")
                if dados:
                    dados["cache_origem"] = "disco_expirado"
                    dados["aviso"] = "scraping falhou; usando cache expirado do disco"
                    print(f"⚠️ [Petrobras] usando cache disco expirado para {produto}/{uf_norm}")
                    return dados
            except Exception:
                pass
        return None

@app.get("/api/composicao")
def get_composicao(uf: str = "BR", produto: str = "gasolina"):
    dados = raspar_composicao_petrobras(produto, uf=uf)
    if not dados:
        fallback = {
            "gasolina": {"parcela_petrobras": 2.08, "impostos_federais": 0.24, "icms": 1.57, "biocombustivel": 0.93, "margem_distribuicao_revenda": 1.72, "preco_medio_final": 6.54, "periodo": "Fallback estático"},
            "diesel": {"parcela_petrobras": 2.76, "impostos_federais": 0.32, "icms": 1.12, "biocombustivel": 0.85, "margem_distribuicao_revenda": 1.08, "preco_medio_final": 6.14, "periodo": "Fallback estático"},
            "gnv": {"parcela_petrobras": 2.40, "impostos_federais": 0.18, "icms": 1.20, "biocombustivel": 0.00, "margem_distribuicao_revenda": 0.90, "preco_medio_final": 4.68, "periodo": "Fallback estático"},
            "glp": {"parcela_petrobras": 45.00, "impostos_federais": 3.50, "icms": 12.00, "biocombustivel": 0.00, "margem_distribuicao_revenda": 22.00, "preco_medio_final": 82.50, "periodo": "Fallback estático"},
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

# ======================== CACHE PETROBRAS EM DISCO ========================
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

# ======================== ELETROPOSTOS ========================
def _buscar_eletropostos_ocm(lat: float, lon: float, raio_km: float, max_results: int = 500):
    """✅ Faz a chamada à OCM e retorna a lista crua (ou [] em caso de erro)."""
    try:
        params = {
            "key": OCM_API_KEY, "output": "json",
            "latitude": lat, "longitude": lon,
            "distance": raio_km, "distanceunit": "KM",
            "maxresults": max_results, "compact": True, "verbose": True,
        }
        r = _sessao.get(f"{OCM_BASE_URL}/poi/", params=params, timeout=45)
        if r.status_code != 200:
            print(f"⚠️ [OCM] status {r.status_code}: {r.text[:150]}")
            return []
        try:
            return r.json()
        except ValueError:
            print(f"⚠️ [OCM] resposta não-JSON: {r.text[:150]}")
            return []
    except Exception as e:
        print(f"⚠️ [OCM] erro de rede: {e}")
        return []


def _formatar_eletroposto(item: dict, lat_origem: float = None, lon_origem: float = None) -> dict:
    """✅ Converte um item cru da OCM no formato que o app espera."""
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
    }


@app.get("/api/eletropostos")
def get_eletropostos(
    latitude: float = Query(None, description="Latitude do centro da busca"),
    longitude: float = Query(None, description="Longitude do centro da busca"),
    municipio: str = Query(None, description="Nome do município (opcional, melhora o filtro)"),
    uf: str = Query(None, description="UF (opcional)"),
    raio_km: float = Query(ELETRO_RAIO_CIDADE_KM, description="Raio inicial em km"),
    max_resultados: int = Query(ELETRO_MAX_RESULTADOS),
):
    """
    ✅ Busca eletropostos com 3 camadas:
    1. Tenta achar TODOS os eletropostos da cidade (filtro por Town + raio inicial).
    2. Se não achar nenhum, expande o raio para ELETRO_RAIO_VIZINHAS_KM
       e retorna os mais próximos das cidades vizinhas.
    3. Se ainda não achar, usa ELETRO_RAIO_EMERGENCIA_KM (último recurso).
    """
    if latitude is None or longitude is None:
        raise HTTPException(
            status_code=400,
            detail="Informe latitude e longitude. Ex: /api/eletropostos?latitude=-20.3155&longitude=-40.3128"
        )

    if not OCM_API_KEY or OCM_API_KEY == "SUA_CHAVE_AQUI":
        raise HTTPException(status_code=500, detail="Configure a OCM_API_KEY.")

    municipio_norm = normalizar_texto(municipio) if municipio else None

    # ═══════ Camada 1: cidade (raio inicial, filtro por Town) ═══════
    print(f"\n⚡ [Eletropostos] camada 1: raio {raio_km} km em ({latitude}, {longitude})")
    itens = _buscar_eletropostos_ocm(latitude, longitude, raio_km, max_resultados)

    if municipio_norm:
        itens_cidade = [
            i for i in itens
            if normalizar_texto((i.get("AddressInfo") or {}).get("Town") or "") == municipio_norm
        ]
    else:
        itens_cidade = itens

    # Se achou na cidade, já formata e retorna
    if itens_cidade:
        formatados = [_formatar_eletroposto(i, latitude, longitude) for i in itens_cidade]
        formatados.sort(key=lambda x: x.get("distancia_km") if x.get("distancia_km") is not None else 999999)
        print(f"✅ [Eletropostos] camada 1: {len(formatados)} eletropostos em {municipio or 'região'}")
        return {
            "total": len(formatados),
            "modo_busca": "cidade",
            "raio_usado_km": raio_km,
            "municipio_filtrado": municipio,
            "mensagem": f"{len(formatados)} eletropostos encontrados em {municipio or 'sua região'}",
            "eletropostos": formatados,
        }

    # ═══════ Camada 2: cidades vizinhas (raio maior, sem filtro) ═══════
    print(f"⚠️ [Eletropostos] camada 1 vazia. Expandindo para {ELETRO_RAIO_VIZINHAS_KM} km...")
    itens_vizinhos = _buscar_eletropostos_ocm(latitude, longitude, ELETRO_RAIO_VIZINHAS_KM, max_resultados)

    if itens_vizinhos:
        formatados = [_formatar_eletroposto(i, latitude, longitude) for i in itens_vizinhos]
        formatados.sort(key=lambda x: x.get("distancia_km") if x.get("distancia_km") is not None else 999999)
        cidades_vizinhas = sorted(set(
            (e.get("cidade") or "Desconhecida")
            for e in formatados[:20]
        ))
        print(f"✅ [Eletropostos] camada 2: {len(formatados)} eletropostos em cidades vizinhas")
        return {
            "total": len(formatados),
            "modo_busca": "vizinhas",
            "raio_usado_km": ELETRO_RAIO_VIZINHAS_KM,
            "municipio_filtrado": municipio,
            "cidades_encontradas": cidades_vizinhas,
            "mensagem": f"Nenhum eletroposto em {municipio or 'sua cidade'}. "
                        f"Mostrando os mais próximos em {', '.join(cidades_vizinhas[:3])}",
            "eletropostos": formatados,
        }

    # ═══════ Camada 3: emergência (raio muito maior) ═══════
    print(f"⚠️ [Eletropostos] camada 2 vazia. Expandindo para {ELETRO_RAIO_EMERGENCIA_KM} km...")
    itens_emerg = _buscar_eletropostos_ocm(latitude, longitude, ELETRO_RAIO_EMERGENCIA_KM, max_resultados)

    if itens_emerg:
        formatados = [_formatar_eletroposto(i, latitude, longitude) for i in itens_emerg]
        formatados.sort(key=lambda x: x.get("distancia_km") if x.get("distancia_km") is not None else 999999)
        print(f"✅ [Eletropostos] camada 3: {len(formatados)} eletropostos em raio de emergência")
        return {
            "total": len(formatados),
            "modo_busca": "emergencia",
            "raio_usado_km": ELETRO_RAIO_EMERGENCIA_KM,
            "municipio_filtrado": municipio,
            "mensagem": f"Poucos eletropostos na região. Mostrando os mais próximos num raio de {ELETRO_RAIO_EMERGENCIA_KM} km",
            "eletropostos": formatados,
        }

    # ═══════ Nada encontrado ═══════
    print(f"❌ [Eletropostos] nenhum eletroposto num raio de {ELETRO_RAIO_EMERGENCIA_KM} km")
    return {
        "total": 0,
        "modo_busca": "vazio",
        "raio_usado_km": ELETRO_RAIO_EMERGENCIA_KM,
        "municipio_filtrado": municipio,
        "mensagem": f"Nenhum eletroposto num raio de {ELETRO_RAIO_EMERGENCIA_KM} km",
        "eletropostos": [],
    }


@app.get("/api/eletropostos-cache-info")
def eletropostos_cache_info():
    """✅ Mostra quantos caches de eletropostos existem em disco."""
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
    try:
        garantir_todos_csvs(force_rebaixar_antigos=True)
    except Exception as e:
        print(f"⚠️ [startup] erro ao garantir CSVs: {e}")

@app.on_event("shutdown")
def parar_agendador():
    scheduler.shutdown()
    print("⏰ Agendador parado.")