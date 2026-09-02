import os
import re
import json
import math
import time
import copy
import unicodedata
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

import requests
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# =============================================================================
# CONFIGURACION CENTRALIZADA Y DINAMICA VRP
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent
COMUNAS_JSON_PATH = BASE_DIR / "Latitud - Longitud Chile.json"
GEOCODING_CACHE_PATH = BASE_DIR / "geocoding_cache.json"

API_BASE_URL = os.environ.get("API_BASE_URL", "https://api-dummy-yurf.onrender.com/api")
APLICAR_CAMBIOS = os.environ.get("APLICAR_CAMBIOS", "False").lower() in ('true', '1', 't')

# Bounding Box de Chile
LAT_MIN, LAT_MAX = -56.5, -17.5
LON_MIN, LON_MAX = -75.6, -66.5
USER_AGENT = "optimizador-rutas-chile/3.4"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_TABLE_URL = os.environ.get("OSRM_TABLE_URL", "http://router.project-osrm.org/table/v1/driving")

DEFAULT_CONFIG_VRP: Dict[str, Any] = {
    # Tiempos de servicio por tipo de OT (en minutos)
    "tiempos_servicio_por_tipo": {
        "instalacion_simple": 45,
        "instalacion_con_corte": 90,
        "mantencion": 40,
        "retiro": 25,
    },
    "tiempo_servicio_default": 30,

    # Parametros de Jornada y Horarios
    "inicio_jornada_horas": 8,          # 08:00 AM es el minuto 0
    "fin_jornada_minutos": 600,         # 10 horas laborales (hasta 18:00)

    # Capacidades maximas por defecto
    "capacidad_max_externo": 8,
    "capacidad_max_interno": 12,

    # Parametros Geometricos y Viales
    "factor_sinuosidad_vial": 1.30,      # Correccion vial urbana (1.30x)
    "velocidad_promedio_kmh": 30.0,     # Velocidad promedio en ciudad
    "max_radio_operacional_km": 80.0,   # Umbral de alerta para puntos distantes

    # Costos y Penalizaciones Algoritmicas
    "penalty_drop_node": 500_000,       # Penalizacion por descarte de OT
    "penalty_mix_sector": 5_000_000,    # Penalizacion para evitar mezcla de sector
    "span_cost_coefficient": 50,        # Balanceo de tiempo entre tecnicos
    "solver_time_limit_seconds": 10,    # Tiempo limite de busqueda en segundos

    # Servicios Externos
    "usar_osrm": True,
    "usar_geocoding": True,
}

CONFIG_VRP: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG_VRP)

def obtener_configuracion() -> Dict[str, Any]:
    """Retorna una copia de la configuracion actual del optimizador."""
    return copy.deepcopy(CONFIG_VRP)

def actualizar_configuracion(nuevos_valores: Dict[str, Any]) -> Dict[str, Any]:
    """Actualiza dinamicamente los parametros del optimizador."""
    global CONFIG_VRP
    for k, v in nuevos_valores.items():
        if k in CONFIG_VRP and v is not None:
            if isinstance(CONFIG_VRP[k], dict) and isinstance(v, dict):
                CONFIG_VRP[k].update(v)
            else:
                CONFIG_VRP[k] = v
    print(f"   [CONFIG] Parametros VRP actualizados en memoria.")
    return obtener_configuracion()

def restaurar_configuracion() -> Dict[str, Any]:
    """Restaura los parametros del optimizador a sus valores por defecto."""
    global CONFIG_VRP
    CONFIG_VRP = copy.deepcopy(DEFAULT_CONFIG_VRP)
    print(f"   [CONFIG] Parametros VRP restaurados a valores iniciales.")
    return obtener_configuracion()

# =============================================================================
# FUNCIONES AUXILIARES DE TIEMPO
# =============================================================================
def minutos_desde_inicio(hora_str: Optional[str], inicio_horas: Optional[int] = None) -> Optional[int]:
    """Convierte 'HH:MM' a minutos transcurridos desde el inicio de la jornada laboral."""
    if not hora_str:
        return None
    try:
        partes = hora_str.strip().split(':')
        h, m = int(partes[0]), int(partes[1])
        base_h = inicio_horas if inicio_horas is not None else CONFIG_VRP.get("inicio_jornada_horas", 8)
        minutos_totales = (h * 60 + m) - (base_h * 60)
        return max(0, minutos_totales)
    except (ValueError, IndexError):
        return None

def minutos_a_hora_str(minutos: int, inicio_horas: Optional[int] = None) -> str:
    """Convierte minutos desde el inicio de la jornada a formato HH:MM."""
    base_h = inicio_horas if inicio_horas is not None else CONFIG_VRP.get("inicio_jornada_horas", 8)
    min_totales = (base_h * 60) + int(minutos)
    h = (min_totales // 60) % 24
    m = min_totales % 60
    return f"{h:02d}:{m:02d}"

def normalizar_texto(texto: Any) -> str:
    """Elimina tildes, convierte a minusculas y quita espacios extra para cruces exactos."""
    if not texto:
        return ""
    texto = str(texto).strip().lower()
    texto = ''.join((c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn'))
    return texto

# =============================================================================
# MODULO DE GEOMETRIA, GEOCODIFICACION Y MATRICES ESPACIALES
# =============================================================================
def cargar_geocoding_cache() -> Dict[str, Tuple[float, float]]:
    """Carga la cache local persistente de direcciones geocodificadas."""
    if GEOCODING_CACHE_PATH.exists():
        try:
            with open(GEOCODING_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {k: (float(v[0]), float(v[1])) for k, v in data.items() if isinstance(v, (list, tuple)) and len(v) == 2}
        except Exception as e:
            print(f"   [WARNING] Error leyendo cache de geocodificacion: {e}")
    return {}

def guardar_geocoding_cache(cache: Dict[str, Tuple[float, float]]) -> None:
    """Guarda en disco la cache de geocodificacion para acelerar futuras ejecuciones."""
    try:
        with open(GEOCODING_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"   [WARNING] No se pudo persistir cache de geocodificacion: {e}")

def cargar_coordenadas_comunas() -> Dict[str, Tuple[float, float]]:
    """Carga el JSON local de comunas de Chile de forma robusta con soporte de alias."""
    comunas_dict = {}
    if not COMUNAS_JSON_PATH.exists():
        print(f"   [WARNING] Archivo '{COMUNAS_JSON_PATH}' no encontrado.")
        return {}
    
    try:
        with open(COMUNAS_JSON_PATH, "r", encoding="utf-8") as f:
            datos = json.load(f)
            
        for item in datos:
            nombre_comuna = normalizar_texto(item.get("Comuna", ""))
            lat = item.get("Latitud (Decimal)")
            lon = item.get("Longitud (decimal)") or item.get("Longitud (Decimal)") 
            
            if nombre_comuna and lat is not None and lon is not None:
                comunas_dict[nombre_comuna] = (float(lat), float(lon))
                
        # Aliases comunes para comunas con nombres compuestos o abreviados
        if "santiago" in comunas_dict:
            comunas_dict["santiago centro"] = comunas_dict["santiago"]
            comunas_dict["stgo centro"] = comunas_dict["santiago"]
        if "calera" in comunas_dict:
            comunas_dict["la calera"] = comunas_dict["calera"]
        if "marchihue" in comunas_dict:
            comunas_dict["marchigue"] = comunas_dict["marchihue"]
        if "llaillay" in comunas_dict:
            comunas_dict["llay llay"] = comunas_dict["llaillay"]
            
        return comunas_dict
    except Exception as e:
        print(f"   [ERROR] Fallo al procesar '{COMUNAS_JSON_PATH.name}': {e}")
        return {}

def extraer_comuna_direccion(direccion: str, comunas_dict: Dict[str, Tuple[float, float]]) -> Optional[str]:
    """
    Identifica y extrae semánticamente la comuna presente en una direccion textual.
    Prioriza coincidencia por segmentos y luego por busqueda de subcadena ordenada por longitud.
    """
    if not direccion:
        return None
    dir_norm = normalizar_texto(direccion)
    
    partes = [p.strip() for p in dir_norm.split(',') if p.strip()]
    for p in partes:
        if p in comunas_dict:
            return p
            
    comunas_ordenadas = sorted(comunas_dict.keys(), key=len, reverse=True)
    for c in comunas_ordenadas:
        if len(c) >= 4 and c in dir_norm:
            return c
            
    return None

def calcular_distancia_haversine_metros(coord1: Tuple[float, float], coord2: Tuple[float, float], aplicar_sinuosidad: bool = True) -> int:
    """Calcula la distancia ortodromica entre dos coordenadas (lon, lat) aplicando sinuosidad vial."""
    lon1, lat1 = coord1
    lon2, lat2 = coord2
    
    if lon1 == lon2 and lat1 == lat2:
        return 0

    R = 6371000  # Radio medio de la Tierra en metros
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist_metros = R * c
    
    if aplicar_sinuosidad:
        factor = float(CONFIG_VRP.get("factor_sinuosidad_vial", 1.30))
        dist_metros *= factor
        
    return int(dist_metros)

def estimar_tiempo_viaje_minutos(distancia_metros: int, velocidad_kmh: Optional[float] = None) -> int:
    """Estima el tiempo de viaje en minutos a partir de la distancia vial estimada y velocidad media."""
    if distancia_metros <= 0:
        return 0
    vel = velocidad_kmh if velocidad_kmh is not None else float(CONFIG_VRP.get("velocidad_promedio_kmh", 30.0))
    velocidad_mpm = (vel * 1000.0) / 60.0  # metros por minuto
    minutos = distancia_metros / velocidad_mpm
    return max(1, int(round(minutos)))

def limpiar_direccion_para_geocoding(direccion: str) -> str:
    """Remueve complementos interiores para maximizar tasa de acierto en OSM/Nominatim."""
    if not direccion:
        return ""
    patron_interior = r'(?i)\b(?:depto|dpto|departamento|piso|of|oficina|block|bloque|local|sitio|bodega|casa|edificio|torre|habitacion|habitación)\.?\s*#?\s*[a-zA-Z0-9\-]+'
    limpia = re.sub(patron_interior, '', direccion)
    limpia = re.sub(r'(\s*,\s*)+', ', ', limpia)
    limpia = re.sub(r'\s+', ' ', limpia).strip(' ,')
    return limpia

def geocode_direccion(
    direccion: str, 
    session: Optional[requests.Session] = None,
    max_intentos: int = 2
) -> Tuple[Optional[float], Optional[float]]:
    """Geocodifica una direccion mediante Nominatim con sanitizacion y reintentos con backoff."""
    client = session or requests
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "es"}
    
    dir_limpia = limpiar_direccion_para_geocoding(direccion)
    consultas_a_probar = [dir_limpia]
    if dir_limpia != direccion:
        consultas_a_probar.append(direccion)
        
    for query in consultas_a_probar:
        if not query:
            continue
            
        params = {
            "q": query, 
            "format": "json", 
            "limit": 1, 
            "countrycodes": "cl", 
            "addressdetails": 0
        }
        
        for intento in range(1, max_intentos + 1):
            try:
                res = client.get(NOMINATIM_URL, params=params, headers=headers, timeout=6)
                if res.status_code == 429:
                    espera = 1.5 * intento
                    print(f"   [RATE LIMIT] Nominatim 429. Reintentando en {espera}s...")
                    time.sleep(espera)
                    continue
                    
                res.raise_for_status()
                data = res.json()
                
                if data:
                    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
                    if LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX:
                        return lat, lon
                break
            except requests.RequestException as e:
                time.sleep(1.0)
            except (KeyError, IndexError, ValueError):
                break
                
    return None, None

def asegurar_coordenadas_ordenes(
    ordenes: List[Dict[str, Any]], 
    comunas_dict: Dict[str, Tuple[float, float]],
    session: Optional[requests.Session] = None
) -> None:
    """Jerarquia de 4 niveles de resolucion geografica con cache local."""
    cache = cargar_geocoding_cache()
    cache_actualizado = False
    usar_geo = CONFIG_VRP.get("usar_geocoding", True)
    
    for ot in ordenes:
        lat, lon = ot.get("latitud"), ot.get("longitud")
        
        if lat is not None and lon is not None:
            if not (LAT_MIN <= float(lat) <= LAT_MAX and LON_MIN <= float(lon) <= LON_MAX):
                ot["latitud"], ot["longitud"] = None, None
        
        if ot.get("latitud") is not None and ot.get("longitud") is not None:
            continue
            
        direccion = ot.get("direccion_instalacion", "").strip()
        if not direccion:
            continue
            
        dir_canonica = normalizar_texto(limpiar_direccion_para_geocoding(direccion))
        
        if dir_canonica in cache:
            lat_c, lon_c = cache[dir_canonica]
            ot["latitud"], ot["longitud"] = lat_c, lon_c
            continue
            
        if usar_geo:
            lat_geo, lon_geo = geocode_direccion(direccion, session=session)
            if lat_geo is not None and lon_geo is not None:
                ot["latitud"], ot["longitud"] = lat_geo, lon_geo
                cache[dir_canonica] = (lat_geo, lon_geo)
                cache_actualizado = True
                time.sleep(1.0)
                continue
                
        comuna = extraer_comuna_direccion(direccion, comunas_dict)
        if comuna and comuna in comunas_dict:
            lat_com, lon_com = comunas_dict[comuna]
            ot["latitud"], ot["longitud"] = lat_com, lon_com
        else:
            ot["latitud"], ot["longitud"] = -33.4372, -70.6572

    if cache_actualizado:
        guardar_geocoding_cache(cache)

def generar_matrices_haversine(coords_nodos: List[Tuple[float, float]]) -> Tuple[List[List[int]], List[List[int]]]:
    """Genera matrices de distancia y tiempo utilizando formula Haversine con factor de sinuosidad vial."""
    num_nodos = len(coords_nodos)
    matriz_distancias = [[0] * num_nodos for _ in range(num_nodos)]
    matriz_tiempos = [[0] * num_nodos for _ in range(num_nodos)]
    
    for i in range(num_nodos):
        for j in range(num_nodos):
            if i != j:
                dist = calcular_distancia_haversine_metros(coords_nodos[i], coords_nodos[j], aplicar_sinuosidad=True)
                matriz_distancias[i][j] = dist
                matriz_tiempos[i][j] = estimar_tiempo_viaje_minutos(dist)
                
    return matriz_distancias, matriz_tiempos

def obtener_matrices_osrm(coords_nodos: List[Tuple[float, float]], session: Optional[requests.Session] = None) -> Tuple[List[List[int]], List[List[int]]]:
    """Obtiene la matriz de distancias y tiempos de OSRM con fallback determinista a Haversine sinuoso."""
    num_nodos = len(coords_nodos)
    coords_str = ";".join([f"{lon},{lat}" for lon, lat in coords_nodos])
    print(f"   [INFO] Solicitando matriz a OSRM para {num_nodos} puntos...")
    
    client = session or requests
    url = f"{OSRM_TABLE_URL}/{coords_str}?annotations=distance,duration"
    try:
        response = client.get(url, timeout=15).json()
        if response.get('code') == 'Ok':
            matriz_tiempos = [[max(1, int(round(val / 60.0))) if i != j else 0 for j, val in enumerate(row)] for i, row in enumerate(response['durations'])]
            matriz_distancias = [[int(round(val)) for val in row] for row in response['distances']]
            return matriz_distancias, matriz_tiempos
    except Exception:
        pass
        
    return generar_matrices_haversine(coords_nodos)

def validar_radio_operacional(coords_bases: List[Tuple[float, float]], coords_ots: List[Tuple[float, float]], ordenes: List[Dict[str, Any]]) -> None:
    """Verifica si existen OTs ubicadas a distancias desproporcionadas respecto a la base de la flota."""
    if not coords_bases or not coords_ots:
        return
        
    centroide_base = (
        sum(p[0] for p in coords_bases) / len(coords_bases),
        sum(p[1] for p in coords_bases) / len(coords_bases)
    )
    max_radio = float(CONFIG_VRP.get("max_radio_operacional_km", 80.0))
    
    for i, p_ot in enumerate(coords_ots):
        dist_km = calcular_distancia_haversine_metros(centroide_base, p_ot, aplicar_sinuosidad=False) / 1000.0
        if dist_km > max_radio:
            ot_id = ordenes[i].get('id', f'Nodo-{i}')
            print(f"   [GEOMETRY ALERT] OT {ot_id} esta a {dist_km:.1f} km del centroide de flota (supera radio de {max_radio} km).")

# =============================================================================
# 1. EXTRACCION (API) - SOLO LECTURA
# =============================================================================
def obtener_datos_operativos(
    session: Optional[requests.Session] = None,
    api_base_url: str = API_BASE_URL,
    fecha: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Descarga los datos operativos desde la API."""
    print("1. Consumiendo API externa (LECTURA SOLAMENTE)...")
    target_fecha = fecha or date.today().isoformat()
    client = session or requests
    try:
        res_tec = client.get(f"{api_base_url}/tecnicos", timeout=10)
        res_tec.raise_for_status()
        tecnicos_req = res_tec.json()
        
        res_disp = client.get(f"{api_base_url}/disponibilidad?fecha={target_fecha}", timeout=10)
        res_disp.raise_for_status()
        disp_req = res_disp.json()
        
        ids_disponibles = {d["tecnico_id"] for d in disp_req if d.get("disponible")}
        tecnicos_hoy = [t for t in tecnicos_req if t["id"] in ids_disponibles]
        
        res_ord = client.get(f"{api_base_url}/ordenes?estado=por_asignar", timeout=10)
        res_ord.raise_for_status()
        ordenes_pendientes = res_ord.json()
        
        print(f"   [RESUMEN] {len(tecnicos_hoy)} tecnicos disponibles | {len(ordenes_pendientes)} OTs pendientes")
        return tecnicos_hoy, ordenes_pendientes
    except Exception as e:
        print(f"   [ERROR FATAL] Conexion a API: {e}")
        return [], []

# =============================================================================
# 2. TRANSFORMACION (MODELO DE DATOS VRP)
# =============================================================================
def preparar_modelo_datos(tecnicos: List[Dict[str, Any]], ordenes: List[Dict[str, Any]], session: Optional[requests.Session] = None) -> Dict[str, Any]:
    """Construye el modelo de datos unificado para Google OR-Tools utilizando CONFIG_VRP."""
    print("\n2. Transformando datos para el modelo VRP...")
    data = {}
    data['num_vehicles'] = len(tecnicos)
    V = data['num_vehicles']

    coords_bases: List[Tuple[float, float]] = []
    coords_ots: List[Tuple[float, float]] = []
    
    diccionario_comunas = cargar_coordenadas_comunas()

    for t in tecnicos:
        zona_original = t.get('zona', '')
        zona_norm = normalizar_texto(zona_original)
        lat, lon = None, None
        
        if zona_norm in diccionario_comunas:
            lat, lon = diccionario_comunas[zona_norm]
        else:
            comuna_detectada = extraer_comuna_direccion(zona_original, diccionario_comunas)
            if comuna_detectada and comuna_detectada in diccionario_comunas:
                lat, lon = diccionario_comunas[comuna_detectada]
            else:
                lat, lon = -33.4372, -70.6572
            
        coords_bases.append((lon, lat))

    if CONFIG_VRP.get("usar_geocoding", True):
        asegurar_coordenadas_ordenes(ordenes, comunas_dict=diccionario_comunas, session=session)

    for ot in ordenes:
        lat, lon = ot.get('latitud'), ot.get('longitud')
        if lat is None or lon is None:
            lat, lon = -33.4372, -70.6572
        coords_ots.append((float(lon), float(lat)))

    validar_radio_operacional(coords_bases, coords_ots, ordenes)

    coords_nodos = coords_bases + coords_ots
    data['starts'] = list(range(V))
    data['ends'] = list(range(V))
    data['coords_bases'] = coords_bases
    data['coords_ots'] = coords_ots
    num_nodos = len(coords_nodos)

    # Matrices de Distancia y Tiempo
    usar_osrm = CONFIG_VRP.get("usar_osrm", True)
    if usar_osrm and num_nodos <= 100:
        data['distance_matrix'], data['time_matrix'] = obtener_matrices_osrm(coords_nodos, session=session)
    else:
        data['distance_matrix'], data['time_matrix'] = generar_matrices_haversine(coords_nodos)

    # Tiempos de Servicio
    tiempos_servicio_cfg = CONFIG_VRP.get("tiempos_servicio_por_tipo", {})
    st_default = CONFIG_VRP.get("tiempo_servicio_default", 30)
    service_times = [0] * V
    for ot in ordenes:
        tipo_ot = ot.get('tipo', '')
        st = tiempos_servicio_cfg.get(tipo_ot, st_default)
        service_times.append(st)
    data['service_times'] = service_times

    # Demandas y Capacidades
    data['demands'] = [0] * V + [1] * len(ordenes)
    
    cap_ext = int(CONFIG_VRP.get("capacidad_max_externo", 8))
    cap_int = int(CONFIG_VRP.get("capacidad_max_interno", 12))
    capacidades = []
    for t in tecnicos:
        cap_custom = t.get('cap_max') or t.get('capacidad')
        if cap_custom is not None:
            capacidades.append(int(cap_custom))
        elif t.get('tipo') == 'externo':
            capacidades.append(cap_ext)
        else:
            capacidades.append(cap_int)
    data['vehicle_capacities'] = capacidades
    data['tecnicos_info'] = [{'id': t.get('id'), 'nombre': t.get('nombre'), 'tipo': t.get('tipo'), 'zona': t.get('zona')} for t in tecnicos]

    # Deteccion Semantica de Sectores
    def sector_de_orden(ot: Dict[str, Any]) -> str:
        dir_inst = ot.get('direccion_instalacion', '')
        comuna = extraer_comuna_direccion(dir_inst, diccionario_comunas)
        if comuna:
            return comuna.title()
        partes = [p.strip() for p in dir_inst.split(',') if p.strip()]
        return partes[-2].title() if len(partes) >= 2 else f"Desconocido-{ot.get('id', 'x')}"

    data['orden_sectores'] = [sector_de_orden(ot) for ot in ordenes]
    data['sector_counts'] = {}
    for sec in data['orden_sectores']:
        data['sector_counts'][sec] = data['sector_counts'].get(sec, 0) + 1

    # Ventanas Temporales
    fin_jornada = int(CONFIG_VRP.get("fin_jornada_minutos", 600))
    inicio_h = int(CONFIG_VRP.get("inicio_jornada_horas", 8))
    data['time_windows'] = [(0, fin_jornada)] * V
    
    for i, ot in enumerate(ordenes):
        st = data['service_times'][V + i]
        minuto_prog = minutos_desde_inicio(ot.get('hora_programada'), inicio_horas=inicio_h)
        if minuto_prog is not None:
            inicio_v = max(0, minuto_prog - 30)
            fin_v = min(fin_jornada - st, minuto_prog + 30)
            fin_v = max(inicio_v, fin_v)
            data['time_windows'].append((inicio_v, fin_v))
        else:
            data['time_windows'].append((0, max(0, fin_jornada - st)))

    return data

# =============================================================================
# 3. MOTOR DE OPTIMIZACION VRP (OR-TOOLS)
# =============================================================================
def resolver_rutas(
    data: Dict[str, Any], 
    tecnicos: List[Dict[str, Any]],
    tiempo_limite_segundos: Optional[int] = None
) -> Tuple[pywrapcp.RoutingIndexManager, pywrapcp.RoutingModel, Any]:
    """Configura y resuelve el modelo VRP con penalizaciones y limites configurables."""
    print("\n3. Ejecutando motor de optimizacion OR-Tools...")
    
    manager = pywrapcp.RoutingIndexManager(
        len(data['distance_matrix']), 
        data['num_vehicles'], 
        data['starts'], 
        data['ends']
    )
    routing = pywrapcp.RoutingModel(manager)
    V = data['num_vehicles']

    penalty_mix = int(CONFIG_VRP.get("penalty_mix_sector", 5_000_000))
    penalty_drop = int(CONFIG_VRP.get("penalty_drop_node", 500_000))
    span_coeff = int(CONFIG_VRP.get("span_cost_coefficient", 50))
    fin_jornada = int(CONFIG_VRP.get("fin_jornada_minutos", 600))
    time_limit = tiempo_limite_segundos or int(CONFIG_VRP.get("solver_time_limit_seconds", 10))

    # Evaluador de Costos de Arco
    def crear_callback_distancia(vehicle_id: int):
        def callback(from_index: int, to_index: int) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            costo_base = data['distance_matrix'][from_node][to_node]
            tecnico = data['tecnicos_info'][vehicle_id]
            
            if from_node >= V and to_node >= V:
                if tecnico.get('tipo') == 'interno':
                    sector_from = data['orden_sectores'][from_node - V]
                    sector_to = data['orden_sectores'][to_node - V]
                    if sector_from != sector_to:
                        return costo_base + penalty_mix
            
            return costo_base
        return callback

    for vehicle_id in range(V):
        callback_idx = routing.RegisterTransitCallback(crear_callback_distancia(vehicle_id))
        routing.SetArcCostEvaluatorOfVehicle(callback_idx, vehicle_id)

    # Dimension de Tiempo
    def time_transit_callback(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        tiempo_viaje = data['time_matrix'][from_node][to_node]
        tiempo_servicio = data['service_times'][from_node]
        return tiempo_viaje + tiempo_servicio
    
    time_callback_idx = routing.RegisterTransitCallback(time_transit_callback)
    time_dimension_name = 'Time'
    routing.AddDimension(
        time_callback_idx,
        fin_jornada,
        fin_jornada,
        False,
        time_dimension_name
    )
    time_dimension = routing.GetDimensionOrDie(time_dimension_name)

    if span_coeff > 0:
        time_dimension.SetGlobalSpanCostCoefficient(span_coeff)

    for node_idx, time_window in enumerate(data['time_windows']):
        if node_idx in data['starts'] or node_idx in data['ends']:
            continue
        index = manager.NodeToIndex(node_idx)
        time_dimension.CumulVar(index).SetRange(time_window[0], time_window[1])
        routing.AddDisjunction([index], penalty_drop)

    # Dimension de Capacidad
    def demand_callback(from_index: int) -> int:
        return data['demands'][manager.IndexToNode(from_index)]
    
    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index, 0, data['vehicle_capacities'], True, 'Capacity'
    )

    # Restricciones de Sector (>= 10 OTs)
    internal_vehicles = [idx for idx, t in enumerate(tecnicos) if t.get('tipo') == 'interno']
    external_vehicles = [idx for idx, t in enumerate(tecnicos) if t.get('tipo') == 'externo']
    
    for i, sector in enumerate(data['orden_sectores']):
        node_idx = V + i
        if data['sector_counts'].get(sector, 0) >= 10:
            if internal_vehicles:
                index = manager.NodeToIndex(node_idx)
                for ext_v in external_vehicles:
                    routing.VehicleVar(index).RemoveValue(ext_v)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(time_limit)
    search_parameters.log_search = False

    print(f"   [PROCESS] Resolviendo modelo VRP (limite: {time_limit}s)...")
    solution = routing.SolveWithParameters(search_parameters)
    
    if solution:
        print(f"   [OK] Solucion ENCONTRADA con costo objetivo: {solution.ObjectiveValue()}")
    else:
        print(f"   [WARNING] No se encontro solucion factible")
    
    return manager, routing, solution

# =============================================================================
# 4. DIAGNOSTICO DE ORDENES NO ASIGNADAS
# =============================================================================
def diagnosticar_orden_pendiente(ot: Dict[str, Any], nodo_real: int, data: Dict[str, Any], tecnicos: List[Dict[str, Any]]) -> List[str]:
    """Explica las razones por las que una OT no fue asignada."""
    razones = []
    V = data['num_vehicles']
    idx_orden = nodo_real - V
    sector = data['orden_sectores'][idx_orden]
    time_window = data['time_windows'][nodo_real]
    service_time = data['service_times'][nodo_real]
    count_sector = data['sector_counts'].get(sector, 0)

    if ot.get('latitud') is None or ot.get('longitud') is None:
        razones.append("Coordenadas invalidas. No se pudo enrutar.")
        return razones

    internal_techs = [t for t in tecnicos if t.get('tipo') == 'interno']
    if count_sector >= 10:
        if not internal_techs:
            razones.append(f"SECTOR: '{sector}' exige solo internos ({count_sector} OTs) y no hay disponibles.")
        else:
            razones.append(f"SECTOR: '{sector}' tiene {count_sector} OTs. Priorizado para internos.")
        return razones

    travel_from_starts = [data['time_matrix'][v][nodo_real] for v in range(V)]
    min_travel = min(travel_from_starts) if travel_from_starts else 0
    if min_travel + service_time > time_window[1]:
        razones.append(f"TIEMPO: Viaje ({min_travel}m) + Servicio ({service_time}m) supera ventana maxima {time_window[1]}m.")

    if not razones:
        razones.append("OPTIMIZACION: La ubicacion o ventana colisiona con el recorrido mas eficiente.")

    return razones

# =============================================================================
# 5. ANALISIS, REPORTE Y ACTUALIZACION
# =============================================================================
def enviar_asignaciones(
    manager: pywrapcp.RoutingIndexManager,
    routing: pywrapcp.RoutingModel,
    solution: Any,
    tecnicos: List[Dict[str, Any]],
    ordenes: List[Dict[str, Any]],
    data: Dict[str, Any],
    session: Optional[requests.Session] = None,
    api_base_url: str = API_BASE_URL,
    aplicar_cambios: bool = APLICAR_CAMBIOS
) -> Dict[str, Any]:
    """Reporta los resultados y actualiza la API."""
    if not solution:
        return {
            "status": "infeasible",
            "mensaje": "No se encontro solucion matematica factible.",
            "kpis": {"total_ots": len(ordenes), "asignadas": 0, "pendientes": len(ordenes)},
            "diagnosticos": [],
            "rutas": []
        }

    V = data['num_vehicles']
    time_dimension = routing.GetDimensionOrDie('Time')
    dropped_nodes = []
    inicio_h = int(CONFIG_VRP.get("inicio_jornada_horas", 8))
    
    for node in range(routing.Size()):
        if routing.IsStart(node) or routing.IsEnd(node):
            continue
        if solution.Value(routing.NextVar(node)) == node:
            nodo_real = manager.IndexToNode(node)
            idx_orden = nodo_real - V
            dropped_nodes.append(ordenes[idx_orden]['id'])
    
    diagnosticos_list = []
    if dropped_nodes:
        for ot_id in dropped_nodes:
            ot = next((orden for orden in ordenes if orden['id'] == ot_id), None)
            if ot is None:
                continue
            idx_orden = next((i for i, orden in enumerate(ordenes) if orden['id'] == ot_id), None)
            nodo_real = V + idx_orden
            
            razones = diagnosticar_orden_pendiente(ot, nodo_real, data, tecnicos)
            diagnosticos_list.append({
                "ot_id": ot_id,
                "tipo": ot.get('tipo'),
                "hora_programada": ot.get('hora_programada', 'Libre'),
                "razones": razones
            })

    client = session or requests
    asignaciones_bulk: List[Dict[str, Any]] = []
    rutas_list: List[Dict[str, Any]] = []
    
    for vehicle_id, tecnico_actual in enumerate(tecnicos):
        index = routing.Start(vehicle_id)
        paradas_info = []
        
        tiempo_inicio_ruta = solution.Min(time_dimension.CumulVar(index))
        hora_salida_str = minutos_a_hora_str(tiempo_inicio_ruta, inicio_horas=inicio_h)
        
        secuencia = 1
        while not routing.IsEnd(index):
            nodo_real = manager.IndexToNode(index)
            if nodo_real >= V:
                idx_orden = nodo_real - V
                ot = ordenes[idx_orden]
                
                time_var = time_dimension.CumulVar(index)
                t_llegada_min = solution.Min(time_var)
                t_servicio = data['service_times'][nodo_real]
                t_salida_min = t_llegada_min + t_servicio
                
                info_parada = {
                    "secuencia": secuencia,
                    "ot_id": ot['id'],
                    "tipo": ot.get('tipo', 'N/A'),
                    "direccion": ot.get('direccion_instalacion', ''),
                    "latitud": ot.get('latitud'),
                    "longitud": ot.get('longitud'),
                    "hora_estimada_llegada": minutos_a_hora_str(t_llegada_min, inicio_horas=inicio_h),
                    "hora_estimada_salida": minutos_a_hora_str(t_salida_min, inicio_horas=inicio_h),
                    "duracion_servicio_min": t_servicio,
                    "sector": data['orden_sectores'][idx_orden]
                }
                paradas_info.append(info_parada)
                
                asignaciones_bulk.append({
                    "ot_id": ot['id'],
                    "tecnico_id": tecnico_actual['id'],
                    "secuencia": secuencia,
                    "hora_estimada_llegada": info_parada["hora_estimada_llegada"],
                    "hora_estimada_salida": info_parada["hora_estimada_salida"],
                    "duracion_servicio_min": t_servicio,
                    "sector": info_parada["sector"]
                })
                secuencia += 1
                
            index = solution.Value(routing.NextVar(index))
        
        tiempo_fin_ruta = solution.Min(time_dimension.CumulVar(index))
        hora_retorno_str = minutos_a_hora_str(tiempo_fin_ruta, inicio_horas=inicio_h)
        total_tiempo_ruta = tiempo_fin_ruta - tiempo_inicio_ruta
        
        capacidad = data['vehicle_capacities'][vehicle_id]
        uso = f"{len(paradas_info)}/{capacidad}"
        
        base_lon, base_lat = data.get('coords_bases', [])[vehicle_id] if vehicle_id < len(data.get('coords_bases', [])) else (None, None)
        
        ruta_dict = {
            "tecnico_id": tecnico_actual['id'],
            "nombre": tecnico_actual['nombre'],
            "tipo": tecnico_actual.get('tipo', 'N/A'),
            "zona_base": tecnico_actual.get('zona', 'N/A'),
            "base_latitud": base_lat,
            "base_longitud": base_lon,
            "capacidad_uso": uso,
            "hora_salida_base": hora_salida_str,
            "hora_retorno_base": hora_retorno_str,
            "duracion_total_min": total_tiempo_ruta,
            "total_ots": len(paradas_info),
            "paradas": paradas_info
        }
        rutas_list.append(ruta_dict)

    if aplicar_cambios and asignaciones_bulk:
        bulk_url = f"{api_base_url}/ordenes/asignaciones-masivas"
        bulk_exitoso = False
        try:
            res_bulk = client.patch(bulk_url, json={"asignaciones": asignaciones_bulk}, timeout=10)
            if res_bulk.status_code == 200:
                bulk_exitoso = True
        except Exception:
            pass

        if not bulk_exitoso:
            for asig in asignaciones_bulk:
                try:
                    client.patch(
                        f"{api_base_url}/ordenes/{asig['ot_id']}/tecnico",
                        json={"tecnico_id": asig["tecnico_id"]},
                        timeout=10
                    )
                except Exception:
                    pass

    return {
        "status": "success",
        "resumen": {
            "total_ots": len(ordenes),
            "ots_asignadas": len(ordenes) - len(dropped_nodes),
            "ots_pendientes": len(dropped_nodes),
            "total_tecnicos": len(tecnicos),
            "tecnicos_utilizados": len([r for r in rutas_list if r["total_ots"] > 0]),
            "costo_objetivo": solution.ObjectiveValue()
        },
        "diagnosticos": diagnosticos_list,
        "rutas": rutas_list
    }

# =============================================================================
# FUNCION PRINCIPAL PROGRAMATICA (EXPORTABLE)
# =============================================================================
def optimizar_jornada(
    fecha: Optional[str] = None,
    aplicar_cambios: bool = APLICAR_CAMBIOS,
    tiempo_limite_segundos: Optional[int] = None,
    api_base_url: str = API_BASE_URL,
    session: Optional[requests.Session] = None
) -> Dict[str, Any]:
    """Funcion modular para ejecutar el pipeline de optimizacion de rutas VRP."""
    client = session or requests.Session()
    tecnicos, ordenes = obtener_datos_operativos(session=client, api_base_url=api_base_url, fecha=fecha)
    
    if not tecnicos or not ordenes:
        return {
            "status": "no_data",
            "mensaje": "Faltan tecnicos disponibles u OTs por asignar para la fecha seleccionada.",
            "kpis": {"total_ots": len(ordenes), "asignadas": 0, "pendientes": len(ordenes)},
            "rutas": []
        }
        
    data_model = preparar_modelo_datos(tecnicos, ordenes, session=client)
    manager, routing, solution = resolver_rutas(data_model, tecnicos, tiempo_limite_segundos=tiempo_limite_segundos)
    resultado = enviar_asignaciones(
        manager, routing, solution, tecnicos, ordenes, data_model,
        session=client, api_base_url=api_base_url, aplicar_cambios=aplicar_cambios
    )
    return resultado

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Optimizador de Rutas VRP Avanzado")
    parser.add_argument("--reset", action="store_true", help="Llama al endpoint /api/reset antes de obtener datos")
    parser.add_argument("--aplicar", action="store_true", help="Aplica los cambios en la API (equivalente a APLICAR_CAMBIOS=true)")
    args = parser.parse_args()
    
    print("OPTIMIZADOR DE RUTAS VRP AVANZADO")
    with requests.Session() as session:
        if args.reset:
            print("-> Llamando a /api/reset para regenerar datos...")
            try:
                res = session.post(f"{API_BASE_URL}/reset")
                if res.status_code == 200:
                    print("   [OK] Datos regenerados en la API")
                else:
                    print(f"   [WARNING] /api/reset respondio con {res.status_code}")
            except Exception as e:
                print(f"   [ERROR] No se pudo hacer reset en la API: {e}")
                
        aplicar_final = args.aplicar or APLICAR_CAMBIOS
        optimizar_jornada(aplicar_cambios=aplicar_final, session=session)