# =============================================================================
# Geocodificacion de direcciones chilenas (OpenStreetMap: Nominatim + Photon)
# =============================================================================
#
# Resolucion por OT, de mejor a peor precision:
#   original   -> la OT ya trae coordenadas validas
#   numero     -> OSM tiene la direccion con su numero
#   calle      -> se encontro la calle dentro de la comuna (sin el numero exacto)
#   comuna     -> centroide de la comuna (direccion no encontrada)
#   aproximada -> ni la comuna se reconoce: Santiago Centro
#
# Cada resultado de los geocodificadores se valida contra la comuna y la calle
# pedidas: OSM devuelve con frecuencia una calle homonima en otra comuna
# (ej: "Pedro Montt, Valparaiso" -> Pedro Montt en Vina del Mar).
# =============================================================================

import json
import math
import os
import re
import threading
import time
import unicodedata
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parent
COMUNAS_JSON_PATH = BASE_DIR / "Latitud - Longitud Chile.json"
GEOCODING_CACHE_PATH = BASE_DIR / "geocoding_cache.json"

NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
PHOTON_URL = os.environ.get("PHOTON_URL", "https://photon.komoot.io/api/")
GEOCODING_EMAIL = os.environ.get("GEOCODING_EMAIL")  # Recomendado por la politica de uso de Nominatim
USER_AGENT = "optimizador-rutas-chile/4.1"

# Bounding box de Chile
LAT_MIN, LAT_MAX = -56.5, -17.5
LON_MIN, LON_MAX = -75.6, -66.5
COORD_DEFAULT = (-33.4372, -70.6572)  # Santiago Centro, ultimo recurso

CACHE_VERSION = 2
DIAS_REINTENTO_NO_ENCONTRADA = 30
INTERVALO_MIN_POR_PROVEEDOR_S = 1.1   # Nominatim y Photon publicos: maximo ~1 req/s
RADIO_MAX_SIN_COMUNA_KM = 10.0        # Si el resultado no informa comuna, debe caer cerca del centroide
RADIO_COMUNA_LIMITROFE_KM = 3.0       # Numero exacto en comuna vecina: aceptado si esta a esta distancia

# =============================================================================
# TEXTO Y COORDENADAS
# =============================================================================
def normalizar_texto(texto: Any) -> str:
    """Minusculas, sin tildes ni espacios sobrantes (para cruces exactos)."""
    if not texto:
        return ""
    texto = str(texto).strip().lower()
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def coordenada_valida(lat: Any, lon: Any) -> bool:
    """True si (lat, lon) son numeros dentro del territorio chileno."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def _distancia_km(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*p1, *p2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))

# =============================================================================
# COMUNAS
# =============================================================================
@lru_cache(maxsize=1)
def cargar_coordenadas_comunas() -> Dict[str, Tuple[float, float, str]]:
    """Carga (una sola vez) las comunas de Chile: nombre normalizado -> (lat, lon, nombre original)."""
    comunas: Dict[str, Tuple[float, float, str]] = {}
    if not COMUNAS_JSON_PATH.exists():
        print(f"   [WARNING] Archivo '{COMUNAS_JSON_PATH.name}' no encontrado.")
        return comunas
    try:
        with open(COMUNAS_JSON_PATH, "r", encoding="utf-8") as f:
            datos = json.load(f)
        for item in datos:
            nombre = (item.get("Comuna") or "").strip()
            lat = item.get("Latitud (Decimal)")
            lon = item.get("Longitud (decimal)") or item.get("Longitud (Decimal)")
            if nombre and lat is not None and lon is not None:
                comunas[normalizar_texto(nombre)] = (float(lat), float(lon), nombre)
    except (OSError, ValueError) as e:
        print(f"   [ERROR] Fallo al procesar '{COMUNAS_JSON_PATH.name}': {e}")
        return comunas

    alias = {
        "santiago centro": "santiago",
        "stgo centro": "santiago",
        "la calera": "calera",
        "marchigue": "marchihue",
        "llay llay": "llaillay",
    }
    for a, original in alias.items():
        if original in comunas:
            comunas[a] = comunas[original]
    return comunas


@lru_cache(maxsize=1)
def _comunas_por_longitud() -> Tuple[str, ...]:
    return tuple(sorted(cargar_coordenadas_comunas(), key=len, reverse=True))


def extraer_comuna(texto: str) -> Optional[str]:
    """
    Detecta la comuna (normalizada) presente en un texto.
    Primero por segmentos separados por coma; luego por palabra completa,
    probando las comunas de nombre mas largo primero.
    """
    if not texto:
        return None
    comunas = cargar_coordenadas_comunas()
    texto_norm = normalizar_texto(texto)
    for parte in (p.strip() for p in texto_norm.split(",")):
        if parte in comunas:
            return parte
    for c in _comunas_por_longitud():
        if len(c) >= 4 and re.search(rf"\b{re.escape(c)}\b", texto_norm):
            return c
    return None


def resolver_comuna_ot(ot: Dict[str, Any]) -> Optional[str]:
    """Comuna normalizada de una OT: campo 'comuna' si es reconocible, si no la detectada en la direccion."""
    comunas = cargar_coordenadas_comunas()
    comuna = normalizar_texto(ot.get("comuna"))
    if comuna in comunas:
        return comuna
    return extraer_comuna(ot.get("comuna") or "") or extraer_comuna(ot.get("direccion_instalacion") or "")

# =============================================================================
# NORMALIZACION DE DIRECCIONES
# =============================================================================
_PATRON_INTERIOR = re.compile(
    r"(?i)\b(?:depto|dpto|departamento|piso|of|oficina|block|bloque|local|sitio|bodega|casa|"
    r"edificio|torre|habitacion|habitación)\.?\s*#?\s*[a-z0-9\-]+")
_ABREVIATURAS = [
    (re.compile(r"(?i)\bavda?\.?(?=\s)|\bav\.?(?=\s)"), "Avenida"),
    (re.compile(r"(?i)\bp(?:s)?je\.?(?=\s)"), "Pasaje"),
    (re.compile(r"(?i)\bgral\.?(?=\s)"), "General"),
    (re.compile(r"(?i)\bpdte\.?(?=\s)"), "Presidente"),
    (re.compile(r"(?i)\bsta\.?(?=\s)"), "Santa"),
    (re.compile(r"(?i)\bsto\.?(?=\s)"), "Santo"),
]
_PATRON_MARCA_NUMERO = re.compile(r"(?i)(?:#|\bn[°º]\.?|\bnro\.?|\bnum\.?|\bn[uú]mero)\s*(?=\d)")
_PATRON_CALLE_NUMERO = re.compile(r"^(?P<calle>.*?[^\d\s,])[\s,]+(?P<numero>\d{1,6})\s*[a-zA-Z]?$")
_PREFIJOS_CALLE = {"avenida", "calle", "pasaje", "camino", "av", "avda"}


def limpiar_direccion(direccion: str) -> str:
    """Quita complementos interiores (depto, oficina...) y marcas de numero (#, N°), expande abreviaturas."""
    if not direccion:
        return ""
    limpia = _PATRON_INTERIOR.sub("", direccion)
    limpia = _PATRON_MARCA_NUMERO.sub("", limpia)
    limpia = re.sub(r"(?i)\bs/n\b", "", limpia)
    for patron, reemplazo in _ABREVIATURAS:
        limpia = patron.sub(reemplazo, limpia)
    limpia = re.sub(r"(\s*,\s*)+", ", ", limpia)
    return re.sub(r"\s+", " ", limpia).strip(" ,")


def separar_calle_numero(direccion: str) -> Tuple[str, Optional[str]]:
    """'Av. Pedro Montt #2855, Depto 4, Valparaiso' -> ('Avenida Pedro Montt', '2855')."""
    primera_parte = limpiar_direccion(direccion).split(",")[0].strip()
    m = _PATRON_CALLE_NUMERO.match(primera_parte)
    if m:
        return m.group("calle").strip(), m.group("numero")
    return primera_parte, None


def variantes_calle(calle: str) -> List[str]:
    """OSM es inconsistente con el prefijo 'Avenida' (Valparaiso lo exige, Quilpue no): se prueban ambos."""
    variantes = [calle]
    if normalizar_texto(calle).startswith("avenida "):
        variantes.append(calle.split(" ", 1)[1])
    else:
        variantes.append(f"Avenida {calle}")
    return variantes


def _tokens_calle(nombre: str) -> set:
    texto = re.sub(r"[^a-z0-9 ]", "", normalizar_texto(nombre).replace("'", ""))
    # Los numeros se conservan: "5 Norte" no debe coincidir con "6 Norte" (comun en Vina del Mar)
    return {t for t in texto.split() if (len(t) >= 3 or t.isdigit()) and t not in _PREFIJOS_CALLE}


def calle_coincide(pedida: str, encontrada: Optional[str]) -> bool:
    """True si la calle encontrada corresponde a la pedida (tolerante a prefijos, tildes y sufijos)."""
    if not encontrada:
        return False
    a, b = _tokens_calle(pedida), _tokens_calle(encontrada)
    if not a or not b:
        return False
    menor, mayor = (a, b) if len(a) <= len(b) else (b, a)
    return len(menor & mayor) / len(menor) >= 0.75

# =============================================================================
# PROVEEDORES (Nominatim y Photon)
# =============================================================================
_ultimo_request: Dict[str, float] = {}
_lock_proveedores = threading.Lock()


def _esperar_turno(proveedor: str) -> None:
    """Respeta el limite de ~1 req/s por proveedor, esperando solo lo necesario."""
    with _lock_proveedores:
        espera = _ultimo_request.get(proveedor, 0) + INTERVALO_MIN_POR_PROVEEDOR_S - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        _ultimo_request[proveedor] = time.monotonic()


def _get(proveedor: str, url: str, params: Dict[str, Any], session: Optional[requests.Session]) -> Optional[Any]:
    client = session or requests
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "es"}
    for intento in range(1, 3):
        _esperar_turno(proveedor)
        try:
            res = client.get(url, params=params, headers=headers, timeout=8)
            if res.status_code == 429:
                time.sleep(2.0 * intento)
                continue
            res.raise_for_status()
            return res.json()
        except requests.RequestException as e:
            print(f"   [WARNING] {proveedor}: {e}")
        except ValueError:
            return None
    return None


def _comuna_de_campos(valores: List[Optional[str]]) -> Optional[str]:
    """Primer valor (del mas especifico al mas general) que sea una comuna conocida."""
    comunas = cargar_coordenadas_comunas()
    for v in valores:
        n = normalizar_texto(v)
        if n in comunas:
            return n
    return None


def buscar_nominatim(consulta: str, session: Optional[requests.Session] = None) -> List[Dict[str, Any]]:
    params = {"q": consulta, "format": "jsonv2", "addressdetails": 1, "limit": 5, "countrycodes": "cl"}
    if GEOCODING_EMAIL:
        params["email"] = GEOCODING_EMAIL
    candidatos = []
    for r in _get("nominatim", NOMINATIM_URL, params, session) or []:
        a = r.get("address", {})
        candidatos.append({
            "lat": float(r["lat"]), "lon": float(r["lon"]),
            "calle": a.get("road") or (r.get("name") if r.get("category") == "highway" else None),
            "numero": a.get("house_number"),
            # En Santiago 'city' es "Santiago" para toda la ciudad; la comuna viene en 'suburb'
            "comuna": _comuna_de_campos([a.get(k) for k in
                                         ("city_district", "suburb", "municipality", "town", "village", "city")]),
            "fuente": "nominatim",
        })
    return candidatos


def buscar_photon(consulta: str, cerca_de: Optional[Tuple[float, float]] = None,
                  session: Optional[requests.Session] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"q": consulta, "limit": 5, "bbox": f"{LON_MIN},{LAT_MIN},{LON_MAX},{LAT_MAX}"}
    if cerca_de:
        params["lat"], params["lon"] = cerca_de
    data = _get("photon", PHOTON_URL, params, session) or {}
    candidatos = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        lon, lat = f["geometry"]["coordinates"]
        candidatos.append({
            "lat": float(lat), "lon": float(lon),
            "calle": p.get("street") or (p.get("name") if p.get("type") == "street" else None),
            "numero": p.get("housenumber"),
            "comuna": _comuna_de_campos([p.get("district"), p.get("locality"), p.get("city")]),
            "fuente": "photon",
        })
    return candidatos


def _validar(candidatos: List[Dict[str, Any]], calle: str, numero: Optional[str],
             comuna: Optional[str]) -> List[Dict[str, Any]]:
    """Filtra candidatos por calle y comuna, y les asigna precision 'numero' o 'calle'."""
    comunas = cargar_coordenadas_comunas()
    validos = []
    for c in candidatos:
        if not coordenada_valida(c["lat"], c["lon"]) or not calle_coincide(calle, c["calle"]):
            continue
        numero_exacto = bool(numero) and re.sub(r"\D", "", str(c["numero"] or "")) == numero
        if comuna:
            distancia = _distancia_km((c["lat"], c["lon"]), comunas[comuna][:2])
            if c["comuna"]:
                misma = c["comuna"] == comuna or comunas.get(c["comuna"]) == comunas.get(comuna)
                # Avenidas limitrofes (ej: Santos Dumont, Independencia/Recoleta): OSM puede asignarlas a la
                # comuna vecina. Se aceptan solo con calle y numero exactos, y cerca de la comuna pedida.
                if not misma and not (numero_exacto and distancia <= RADIO_COMUNA_LIMITROFE_KM):
                    continue
            elif distancia > RADIO_MAX_SIN_COMUNA_KM:
                continue
        c["precision"] = "numero" if numero_exacto else "calle"
        validos.append(c)
    return validos


def _mas_central(candidatos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Tramo mas central (medoide) entre los tramos encontrados de una calle. Sin numeracion en OSM
    no se sabe en que tramo cae el numero; el central minimiza el error en avenidas largas.
    """
    puntos = [(c["lat"], c["lon"]) for c in candidatos]
    return min(candidatos, key=lambda c: sum(_distancia_km((c["lat"], c["lon"]), p) for p in puntos))

# =============================================================================
# CACHE PERSISTENTE (v2: con precision y resultados negativos)
# =============================================================================
_lock_cache = threading.Lock()


def cargar_cache() -> Dict[str, Dict[str, Any]]:
    """Entradas del cache v2. El formato anterior (sin validacion de comuna) se descarta."""
    if not GEOCODING_CACHE_PATH.exists():
        return {}
    try:
        with open(GEOCODING_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"   [WARNING] Error leyendo cache de geocodificacion: {e}")
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        print("   [INFO] Cache de geocodificacion en formato antiguo: se regenera con validacion de comuna.")
        return {}
    return data.get("entradas", {})


def guardar_cache(entradas: Dict[str, Dict[str, Any]]) -> None:
    """Escritura atomica (archivo temporal + reemplazo) para no corromper el cache."""
    tmp = GEOCODING_CACHE_PATH.with_suffix(".tmp")
    try:
        with _lock_cache:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": CACHE_VERSION, "entradas": entradas}, f, indent=1, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, GEOCODING_CACHE_PATH)
    except OSError as e:
        print(f"   [WARNING] No se pudo persistir cache de geocodificacion: {e}")


def _entrada_vigente(entrada: Optional[Dict[str, Any]]) -> bool:
    if not entrada:
        return False
    if entrada.get("precision") != "no_encontrada":
        return True
    try:
        fecha = date.fromisoformat(entrada.get("fecha", ""))
    except ValueError:
        return False
    return date.today() - fecha < timedelta(days=DIAS_REINTENTO_NO_ENCONTRADA)

def clave_cache(direccion: str, comuna: Optional[str]) -> str:
    calle, numero = separar_calle_numero(direccion)
    return normalizar_texto(f"{calle} {numero or ''}|{comuna or ''}")

# =============================================================================
# API PUBLICA
# =============================================================================
def geocodificar_direccion(
    direccion: str,
    comuna: Optional[str],
    session: Optional[requests.Session] = None,
    cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Geocodifica una direccion dentro de una comuna (normalizada). Retorna
    {lat, lon, precision: 'numero'|'calle', fuente} o None si no se encuentra.
    Estrategia (se detiene al lograr precision 'numero'):
      1. Nominatim con la calle tal como viene
      2. Photon (suele tener numeros que Nominatim no tiene)
      3. Nominatim con la variante de prefijo 'Avenida' (agregado o quitado)
    """
    calle, numero = separar_calle_numero(direccion)
    if not calle:
        return None
    comunas = cargar_coordenadas_comunas()
    nombre_comuna = comunas[comuna][2] if comuna in comunas else None

    clave = clave_cache(direccion, comuna)
    if cache is not None and _entrada_vigente(cache.get(clave)):
        e = cache[clave]
        return None if e["precision"] == "no_encontrada" else e

    def consulta(nombre_calle: str) -> str:
        return ", ".join(p for p in (f"{nombre_calle} {numero}" if numero else nombre_calle, nombre_comuna, "Chile") if p)

    variantes = variantes_calle(calle)
    pasos = [
        lambda: buscar_nominatim(consulta(variantes[0]), session),
        lambda: buscar_photon(consulta(calle), comunas[comuna][:2] if nombre_comuna else None, session),
        lambda: buscar_nominatim(consulta(variantes[1]), session),
    ]
    validos: List[Dict[str, Any]] = []
    for paso in pasos:
        validos += _validar(paso(), calle, numero, comuna if nombre_comuna else None)
        if any(c["precision"] == "numero" for c in validos):
            break

    mejor = next((c for c in validos if c["precision"] == "numero"), _mas_central(validos) if validos else None)
    resultado = ({"lat": mejor["lat"], "lon": mejor["lon"], "precision": mejor["precision"], "fuente": mejor["fuente"]}
                 if mejor else None)
    if cache is not None:
        cache[clave] = dict(resultado or {"precision": "no_encontrada"}, fecha=date.today().isoformat())
    return resultado


def resolver_coordenadas_ordenes(
    ordenes: List[Dict[str, Any]],
    usar_geocoding: bool = True,
    max_segundos: float = 60,
    session: Optional[requests.Session] = None,
) -> Tuple[List[str], int]:
    """
    Completa latitud/longitud de cada OT (las modifica). Retorna (precision por OT, cantidad de OTs
    que no alcanzaron a geocodificarse por el limite de tiempo; se resuelven en ejecuciones futuras).
    """
    comunas = cargar_coordenadas_comunas()
    cache = cargar_cache()
    cache_inicial = dict(cache)
    limite = time.monotonic() + max_segundos
    precisiones: List[str] = []
    sin_tiempo = 0

    for ot in ordenes:
        if coordenada_valida(ot.get("latitud"), ot.get("longitud")):
            ot["latitud"], ot["longitud"] = float(ot["latitud"]), float(ot["longitud"])
            precisiones.append("original")
            continue

        direccion = (ot.get("direccion_instalacion") or "").strip()
        comuna = resolver_comuna_ot(ot)

        if usar_geocoding and direccion:
            entrada = cache.get(clave_cache(direccion, comuna))
            if _entrada_vigente(entrada) or time.monotonic() < limite:
                r = geocodificar_direccion(direccion, comuna, session, cache)
                if r:
                    ot["latitud"], ot["longitud"] = r["lat"], r["lon"]
                    precisiones.append(r["precision"])
                    continue
            else:
                sin_tiempo += 1  # Tiempo agotado y no esta en cache: se resolvera en una proxima ejecucion

        if comuna:
            ot["latitud"], ot["longitud"] = comunas[comuna][:2]
            precisiones.append("comuna")
        else:
            ot["latitud"], ot["longitud"] = COORD_DEFAULT
            precisiones.append("aproximada")

    if cache != cache_inicial:
        guardar_cache(cache)
    return precisiones, sin_tiempo
