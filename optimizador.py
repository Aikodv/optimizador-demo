# =============================================================================
# Optimizador de Rutas VRP (Google OR-Tools) - Despacho de tecnicos en Chile
# =============================================================================
#
# Pipeline:
#   1. Extraccion:      tecnicos disponibles + OTs por asignar (API o parametros)
#   2. Transformacion:  coordenadas, matrices distancia/tiempo, ventanas, sectores
#   3. Optimizacion:    VRP con ventanas horarias, capacidad y sectores (OR-Tools)
#   4. Resultado:       hojas de ruta, diagnostico de OTs pendientes y alertas
#   5. Aplicacion:      (opcional) asignacion de tecnicos en la API externa
#
# Convencion: todas las coordenadas internas se manejan como (lat, lon).
# =============================================================================

import copy
import json
import math
import os
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import requests
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from geocoding import (
    COORD_DEFAULT,
    cargar_coordenadas_comunas,
    coordenada_valida,
    extraer_comuna,
    normalizar_texto,
    resolver_comuna_ot,
    resolver_coordenadas_ordenes,
)

# =============================================================================
# CONFIGURACION
# =============================================================================
API_BASE_URL = os.environ.get("API_BASE_URL", "https://api-dummy-yurf.onrender.com/api").rstrip("/")
OSRM_TABLE_URL = os.environ.get("OSRM_TABLE_URL", "https://router.project-osrm.org/table/v1/driving")

def _p(clave: str, default: Any, tipo: str, grupo: str, ambito: str, origen: str, etiqueta: str,
       descripcion: str, unidad: Optional[str] = None, minimo: Optional[float] = None,
       maximo: Optional[float] = None) -> Dict[str, Any]:
    return {"clave": clave, "etiqueta": etiqueta, "descripcion": descripcion, "tipo": tipo, "unidad": unidad,
            "minimo": minimo, "maximo": maximo, "default": default, "grupo": grupo, "ambito": ambito, "origen": origen}


AMBITOS = {
    "negocio": "Regla de la operacion: la decide la planificadora y va en el panel de configuracion.",
    "solver": "Interno del modelo/solver: lo ajusta el equipo tecnico; no se expone a la planificadora.",
    "servicio": "Integracion con servicios externos (ruteo, geocodificacion): lo ajusta el equipo tecnico.",
}
ORIGENES = {
    "operacion_cliente": "Refleja como opera el cliente (jornada, duraciones, capacidades, reglas de sector).",
    "supuesto_equipo": "Supuesto o calibracion del equipo de desarrollo; no fue entregado por el cliente.",
}

# Catalogo unico de parametros: de aqui salen los valores por defecto, la validacion del
# endpoint de configuracion y la documentacion (GET /api/optimizador/configuracion/parametros).
# Las penalizaciones estan en unidades de costo = metros.
PARAMETROS: List[Dict[str, Any]] = [
    # --- Tiempos de servicio ---
    _p("tiempos_servicio_por_tipo",
       {"instalacion_simple": 45, "instalacion_con_corte": 90, "mantencion": 40, "retiro": 25},
       "dict_int", "tiempos_servicio", "negocio", "operacion_cliente", "Duracion del servicio por tipo de OT",
       "Minutos que el tecnico permanece en el domicilio segun el tipo de OT.", "min", 1, 600),
    _p("tiempo_servicio_default", 30, "int", "tiempos_servicio", "negocio", "supuesto_equipo",
       "Duracion para tipos de OT no listados", "Minutos de servicio cuando el tipo de OT no tiene duracion definida.",
       "min", 1, 600),

    # --- Jornada ---
    _p("inicio_jornada_horas", 8, "int", "jornada", "negocio", "operacion_cliente", "Inicio de jornada",
       "Hora del dia en que los tecnicos salen de su base (minuto 0 del modelo).", "hora del dia", 5, 12),
    _p("fin_jornada_minutos", 600, "int", "jornada", "negocio", "operacion_cliente", "Duracion de la jornada",
       "Minutos desde el inicio de jornada en que el tecnico debe estar de vuelta en su base (600 = 10 horas).",
       "min", 180, 1440),
    _p("ventana_tolerancia_min", 30, "int", "jornada", "negocio", "supuesto_equipo", "Tolerancia de la hora acordada",
       "Minutos antes/despues de la hora programada en que se permite iniciar el servicio.", "min", 0, 240),

    # --- Capacidad ---
    _p("capacidad_max_externo", 8, "int", "capacidad", "negocio", "operacion_cliente", "Maximo de OTs por tecnico externo",
       "Tope de OTs diarias de un externo; el cap_max individual del tecnico solo puede reducirlo.", "OTs", 1, 30),
    _p("capacidad_max_interno", 12, "int", "capacidad", "negocio", "operacion_cliente", "Maximo de OTs por tecnico interno",
       "Tope de OTs diarias de un interno; el cap_max individual del tecnico solo puede reducirlo.", "OTs", 1, 30),

    # --- Sectores ---
    _p("umbral_ots_sector_interno", 10, "int", "sectores", "negocio", "operacion_cliente",
       "OTs por sector para reservarlo a internos",
       "Los sectores (comunas) con MAS de este numero de OTs se reservan a tecnicos internos. 0 = desactivado.",
       "OTs", 0, 100),
    _p("sectores_internos_exclusivos", False, "bool", "sectores", "negocio", "supuesto_equipo",
       "Sectores reservados exclusivos de internos",
       "true: un externo nunca atiende OTs de un sector reservado (las que no alcancen los internos quedan sin "
       "asignar con causa sectorial). false: el externo puede apoyar pagando penalty_externo_sector_interno."),
    _p("costo_por_ot_externo", 0, "int", "sectores", "negocio", "supuesto_equipo", "Preferencia por tecnicos internos",
       "Costo extra por cada OT atendida por un externo (20000 = equivale a 20 km). 0 = sin preferencia.",
       "metros", 0, None),
    _p("max_radio_operacional_km", 80.0, "float", "sectores", "negocio", "supuesto_equipo", "Radio operacional",
       "Distancia desde el centroide de las bases sobre la cual una OT genera alerta (no impide asignarla).",
       "km", 10.0, 300.0),

    # --- Georreferencia ---
    _p("excluir_ots_sin_georreferencia", True, "bool", "georreferencia", "negocio", "supuesto_equipo",
       "No rutear OTs sin ubicacion",
       "true: una OT sin coordenadas, cuya direccion no se pudo geocodificar y sin comuna reconocible, queda sin "
       "asignar con causa de georreferencia. false: se rutea hacia Santiago Centro (ruta poco confiable)."),

    # --- Red vial (respaldo cuando no hay distancias de OSRM) ---
    _p("factor_sinuosidad_vial", 1.30, "float", "red_vial", "solver", "supuesto_equipo", "Factor de sinuosidad",
       "Multiplicador de la distancia en linea recta para estimar la distancia por calles en la matriz de respaldo.",
       None, 1.0, 3.0),
    _p("velocidad_promedio_kmh", 30.0, "float", "red_vial", "solver", "supuesto_equipo", "Velocidad promedio",
       "Velocidad usada para estimar tiempos de viaje en la matriz de respaldo.", "km/h", 10.0, 120.0),

    # --- Funcion objetivo ---
    # penalty_drop_node debe ser mayor que las demas penalizaciones: dejar una OT sin asignar
    # tiene que ser peor que mezclar sectores o que la atienda un externo.
    _p("penalty_drop_node", 10_000_000, "int", "objetivo", "solver", "supuesto_equipo", "Penalizacion por OT sin asignar",
       "Costo de dejar una OT fuera de las rutas. Debe ser mayor que las demas penalizaciones.", "metros", 0, None),
    _p("penalty_mix_sector", 100_000, "int", "objetivo", "solver", "supuesto_equipo",
       "Penalizacion por mezclar sectores (internos)",
       "Costo de que un interno pase entre OTs de sectores distintos. 0 = desactivado.", "metros", 0, None),
    _p("penalty_externo_sector_interno", 1_000_000, "int", "objetivo", "solver", "supuesto_equipo",
       "Penalizacion por externo en sector reservado",
       "Costo de cada OT de un sector reservado a internos atendida por un externo. Menor que penalty_drop_node: "
       "es preferible un externo a dejar la OT sin atender.", "metros", 0, None),
    _p("span_cost_coefficient", 50, "int", "objetivo", "solver", "supuesto_equipo", "Balance de carga entre tecnicos",
       "Coeficiente sobre la duracion de la ruta mas larga; mas alto reparte mejor la carga. 0 = desactivado.",
       None, 0, 500),

    # --- Solver ---
    _p("solver_time_limit_seconds", 10, "int", "solver", "solver", "supuesto_equipo", "Tiempo minimo de busqueda",
       "Segundos minimos de busqueda cuando la corrida no indica tiempo_limite_segundos.", "s", 1, 600),
    _p("solver_segundos_por_ot", 0.5, "float", "solver", "solver", "supuesto_equipo", "Segundos de busqueda por OT",
       "El tiempo automatico de busqueda crece con el problema: OTs x este valor (acotado por minimo y maximo).",
       "s/OT", 0.0, 5.0),
    _p("solver_time_limit_max_seconds", 120, "int", "solver", "solver", "supuesto_equipo", "Tiempo maximo de busqueda",
       "Tope de segundos de busqueda, tanto automatico como pedido por la corrida.", "s", 1, 600),

    # --- Servicios externos ---
    _p("usar_osrm", True, "bool", "servicios_externos", "servicio", "supuesto_equipo", "Usar distancias viales (OSRM)",
       "false: la matriz se calcula siempre con la estimacion de respaldo (linea recta x sinuosidad)."),
    _p("max_nodos_osrm", 100, "int", "servicios_externos", "servicio", "supuesto_equipo", "Maximo de puntos para OSRM",
       "Sobre este numero de puntos (tecnicos + OTs) se usa la matriz de respaldo: limite practico del servidor "
       "publico de OSRM.", "puntos", 2, 1000),
    _p("usar_geocoding", True, "bool", "servicios_externos", "servicio", "supuesto_equipo", "Geocodificar direcciones",
       "false: las OTs sin coordenadas se ubican directamente en el centro de su comuna."),
    _p("geocoding_max_segundos", 60, "int", "servicios_externos", "servicio", "supuesto_equipo",
       "Tiempo maximo de geocodificacion por corrida",
       "Las direcciones nuevas que no alcancen (~3 s cada una) usan el centro de la comuna y se resuelven en la "
       "proxima corrida.", "s", 0, 600),

    # --- Ejecucion ---
    _p("aplicar_cambios_por_defecto", False, "bool", "ejecucion", "negocio", "supuesto_equipo",
       "Aplicar asignaciones automaticamente",
       "Valor de aplicar_cambios cuando la corrida no lo indica: true asigna los tecnicos en la API de ordenes."),
]

DEFAULT_CONFIG_VRP: Dict[str, Any] = {p["clave"]: p["default"] for p in PARAMETROS}

CONFIG_VRP: Dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG_VRP)


def obtener_configuracion() -> Dict[str, Any]:
    """Retorna una copia de la configuracion actual del optimizador."""
    return copy.deepcopy(CONFIG_VRP)


def obtener_catalogo_parametros() -> Dict[str, Any]:
    """Parametros documentados: los persistentes (con su valor actual) y los propios de cada corrida."""
    cfg = obtener_configuracion()
    ejecucion = [
        _p("fecha", None, "str", "ejecucion", "negocio", "operacion_cliente", "Fecha a planificar",
           "YYYY-MM-DD. Sin valor se usa la fecha programada mas temprana entre las OTs por asignar."),
        _p("aplicar_cambios", None, "bool", "ejecucion", "negocio", "supuesto_equipo", "Aplicar asignaciones",
           "true asigna los tecnicos en la API de ordenes; false solo propone las rutas. Sin valor se usa "
           "aplicar_cambios_por_defecto de la configuracion."),
        _p("tiempo_limite_segundos", None, "int", "ejecucion", "solver", "supuesto_equipo", "Tiempo de busqueda",
           "Sin valor (recomendado) se calcula segun la cantidad de OTs con los parametros solver_* de la configuracion.",
           "s", 1, cfg["solver_time_limit_max_seconds"]),
    ]
    for p in ejecucion:
        p["default_efectivo"] = cfg["aplicar_cambios_por_defecto"] if p["clave"] == "aplicar_cambios" else None
    return {
        "ambitos": AMBITOS,
        "origenes": ORIGENES,
        "parametros_configuracion": [dict(p, valor=cfg[p["clave"]]) for p in PARAMETROS],
        "parametros_ejecucion": ejecucion,
    }


def actualizar_configuracion(nuevos_valores: Dict[str, Any]) -> Dict[str, Any]:
    """Actualiza parametros existentes del optimizador (claves desconocidas se ignoran)."""
    for k, v in nuevos_valores.items():
        if k not in CONFIG_VRP or v is None:
            continue
        if isinstance(CONFIG_VRP[k], dict) and isinstance(v, dict):
            CONFIG_VRP[k].update(v)
        else:
            CONFIG_VRP[k] = v
    print("   [CONFIG] Parametros VRP actualizados en memoria.")
    return obtener_configuracion()


def restaurar_configuracion() -> Dict[str, Any]:
    """Restaura los parametros del optimizador a sus valores por defecto."""
    global CONFIG_VRP
    CONFIG_VRP = copy.deepcopy(DEFAULT_CONFIG_VRP)
    print("   [CONFIG] Parametros VRP restaurados a valores iniciales.")
    return obtener_configuracion()

# =============================================================================
# MODELO DE OPTIMIZACION (declaracion explicita) Y CAUSAS DE NO ASIGNACION
# =============================================================================
# Causas por las que una OT queda sin asignar: codigo -> (categoria, prefijo del texto, descripcion).
CAUSAS: Dict[str, Tuple[str, str, str]] = {
    "SIN_GEORREFERENCIA": ("georreferencia", "GEORREFERENCIA",
                           "La OT no tiene coordenadas, su direccion no se pudo geocodificar y su comuna no se reconoce."),
    "SECTOR_RESERVADO_INTERNOS": ("sectorial", "SECTOR",
                                  "La OT es de un sector reservado a internos y la regla de sector impidio que la tomara "
                                  "un externo que si alcanzaba."),
    "DURACION_EXCEDE_JORNADA": ("temporal", "DURACION", "El servicio dura mas que la jornada completa."),
    "HORA_FUERA_DE_JORNADA": ("temporal", "HORARIO", "La hora programada queda fuera de la jornada."),
    "VENTANA_INALCANZABLE": ("temporal", "TIEMPO",
                             "Ningun tecnico alcanza a llegar dentro de la ventana horaria y volver a su base."),
    "CAPACIDAD_AGOTADA": ("capacidad", "CAPACIDAD", "Los tecnicos que podian atenderla completaron su maximo de OTs."),
    "SIN_CUPO_EN_RUTAS": ("optimizacion", "OPTIMIZACION",
                          "Individualmente era atendible, pero no cabe en la jornada junto a las demas OTs asignadas."),
}


def _causa(codigo: str, detalle: str) -> Dict[str, str]:
    return {"codigo": codigo, "categoria": CAUSAS[codigo][0], "detalle": detalle}


def _razon(causa: Dict[str, str]) -> str:
    """Texto con prefijo ('TIEMPO: ...'): formato historico del campo `razones`."""
    return f"{CAUSAS[causa['codigo']][1]}: {causa['detalle']}"


def describir_modelo() -> Dict[str, Any]:
    """Declaracion del modelo tal como lo construyen construir_modelo() y calcular_matrices_costo()."""
    return {
        "tipo": "VRP con ventanas horarias, capacidad, multiples depositos y visitas opcionales (Google OR-Tools Routing)",
        "nodos": "Una base por tecnico (inicio y fin de su ruta) y un nodo por OT.",
        "variables": [
            {"nombre": "next[i]", "descripcion": "Nodo que se visita despues del nodo i (define la secuencia de cada ruta)."},
            {"nombre": "vehiculo[i]", "descripcion": "Tecnico que atiende la OT i."},
            {"nombre": "activa[i]", "descripcion": "1 si la OT i queda asignada, 0 si queda sin asignar."},
            {"nombre": "tiempo[i]", "descripcion": "Minuto (desde el inicio de jornada) en que comienza el servicio en i."},
            {"nombre": "carga[i]", "descripcion": "Cantidad de OTs acumuladas por el tecnico al llegar a i."},
        ],
        "funcion_objetivo": {
            "sentido": "minimizar",
            "unidad": "metros (las penalizaciones se expresan en metros equivalentes)",
            "terminos": [
                {"termino": "Distancia vial recorrida por todos los tecnicos", "parametros": []},
                {"termino": "penalty_drop_node por cada OT sin asignar", "parametros": ["penalty_drop_node"]},
                {"termino": "penalty_mix_sector por cada cambio de sector en la ruta de un interno",
                 "parametros": ["penalty_mix_sector"]},
                {"termino": "penalty_externo_sector_interno por cada OT de un sector reservado atendida por un externo",
                 "parametros": ["penalty_externo_sector_interno", "umbral_ots_sector_interno"]},
                {"termino": "costo_por_ot_externo por cada OT atendida por un externo", "parametros": ["costo_por_ot_externo"]},
                {"termino": "span_cost_coefficient x duracion de la ruta mas larga (balance de carga)",
                 "parametros": ["span_cost_coefficient"]},
                {"termino": "1 por cada minuto de la hora de retorno a la base (rutas compactas, sin esperas)",
                 "parametros": []},
            ],
        },
        "restricciones_implementadas": [
            {"codigo": "R1", "nombre": "Asignacion unica", "tipo": "dura",
             "descripcion": "Cada OT se visita a lo mas una vez y por un solo tecnico."},
            {"codigo": "R2", "nombre": "Base del tecnico", "tipo": "dura",
             "descripcion": "Cada ruta parte y termina en la base del tecnico."},
            {"codigo": "R3", "nombre": "Jornada", "tipo": "dura",
             "descripcion": "Toda la ruta (viajes, esperas y servicios) ocurre dentro de la jornada.",
             "parametros": ["inicio_jornada_horas", "fin_jornada_minutos"]},
            {"codigo": "R4", "nombre": "Ventana horaria", "tipo": "dura",
             "descripcion": "Una OT con hora programada inicia dentro de hora +/- tolerancia; el servicio debe "
                            "terminar antes del fin de jornada.",
             "parametros": ["ventana_tolerancia_min", "tiempos_servicio_por_tipo", "tiempo_servicio_default"]},
            {"codigo": "R5", "nombre": "Capacidad", "tipo": "dura",
             "descripcion": "Cada tecnico atiende como maximo su capacidad de OTs.",
             "parametros": ["capacidad_max_interno", "capacidad_max_externo"]},
            {"codigo": "R6", "nombre": "Georreferencia", "tipo": "dura (configurable)",
             "descripcion": "Una OT sin ubicacion resoluble no se rutea.",
             "parametros": ["excluir_ots_sin_georreferencia"]},
            {"codigo": "R7", "nombre": "Sector reservado a internos", "tipo": "blanda, o dura si sectores_internos_exclusivos",
             "descripcion": "Los sectores con mas OTs que el umbral se asignan a internos.",
             "parametros": ["umbral_ots_sector_interno", "sectores_internos_exclusivos", "penalty_externo_sector_interno"]},
            {"codigo": "R8", "nombre": "Continuidad de sector", "tipo": "blanda",
             "descripcion": "Se evita que un interno mezcle sectores en su ruta.", "parametros": ["penalty_mix_sector"]},
            {"codigo": "R9", "nombre": "Visita opcional", "tipo": "blanda",
             "descripcion": "Una OT puede quedar sin asignar pagando una penalizacion alta; asi el modelo siempre "
                            "tiene solucion y reporta las OTs pendientes con su causa.",
             "parametros": ["penalty_drop_node"]},
        ],
        "restricciones_fuera_de_esta_version": [
            "Habilidades o certificaciones del tecnico segun el tipo de OT.",
            "Horario individual por tecnico: todos comparten la misma jornada.",
            "Pausa de colacion y descansos.",
            "Trafico variable segun la hora: la matriz de tiempos es unica para todo el dia.",
            "Prioridad o SLA de las OTs: todas pesan lo mismo al decidir cual queda sin asignar.",
            "Materiales, equipos o stock del vehiculo: la capacidad solo cuenta OTs.",
            "Dependencias entre OTs (precedencias) y OTs que requieren mas de un tecnico.",
            "Planificacion de varios dias y re-optimizacion durante la jornada.",
        ],
        "causas_no_asignacion": [{"codigo": c, "categoria": cat, "prefijo_razon": pre, "descripcion": desc}
                                 for c, (cat, pre, desc) in CAUSAS.items()],
        "resolucion": {
            "solver": "OR-Tools Routing (CP + busqueda local)",
            "estrategias_iniciales": list(ESTRATEGIAS_INICIALES),
            "metaheuristica": "GUIDED_LOCAL_SEARCH",
            "nota": "Es una heuristica con limite de tiempo: entrega la mejor solucion encontrada, no garantiza el optimo.",
        },
    }

# =============================================================================
# UTILIDADES DE TIEMPO Y TEXTO
# =============================================================================
def minutos_desde_inicio(hora_str: Optional[str], inicio_horas: int) -> Optional[int]:
    """'HH:MM' -> minutos desde el inicio de jornada (puede ser negativo si es antes)."""
    if not hora_str:
        return None
    try:
        h, m = (int(x) for x in str(hora_str).strip().split(":")[:2])
    except ValueError:
        return None
    return (h * 60 + m) - inicio_horas * 60


def minutos_a_hora_str(minutos: int, inicio_horas: int) -> str:
    """Minutos desde el inicio de jornada -> 'HH:MM'."""
    total = inicio_horas * 60 + int(minutos)
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


# =============================================================================
# UBICACION DE TECNICOS Y SECTORES (geocodificacion de OTs en geocoding.py)
# =============================================================================
def resolver_base_tecnico(tecnico: Dict[str, Any]) -> Tuple[Tuple[float, float], str]:
    """Coordenadas de la base de un tecnico: explicitas -> centroide de su zona/comuna -> Santiago Centro."""
    for k_lat, k_lon in (("base_latitud", "base_longitud"), ("latitud", "longitud")):
        if coordenada_valida(tecnico.get(k_lat), tecnico.get(k_lon)):
            return (float(tecnico[k_lat]), float(tecnico[k_lon])), "original"
    comuna = extraer_comuna(tecnico.get("zona") or "")
    if comuna:
        return cargar_coordenadas_comunas()[comuna][:2], "comuna"
    return COORD_DEFAULT, "aproximada"


def sector_de_orden(ot: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Retorna (clave normalizada del sector, nombre para mostrar). Clave None si no se reconoce."""
    clave = resolver_comuna_ot(ot)
    if clave:
        return clave, cargar_coordenadas_comunas()[clave][2]
    comuna_ot = (ot.get("comuna") or "").strip()  # Comuna no reconocida: se usa el texto tal cual
    return (normalizar_texto(comuna_ot), comuna_ot) if comuna_ot else (None, "Sin sector")

# =============================================================================
# MATRICES DE DISTANCIA Y TIEMPO
# =============================================================================
def distancia_haversine_metros(p1: Tuple[float, float], p2: Tuple[float, float], factor: float = 1.0) -> int:
    """Distancia ortodromica entre (lat, lon) en metros, multiplicada por un factor de sinuosidad."""
    lat1, lon1 = p1
    lat2, lon2 = p2
    if lat1 == lat2 and lon1 == lon2:
        return 0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return int(6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)) * factor)


def estimar_tiempo_viaje_minutos(distancia_metros: int, velocidad_kmh: float) -> int:
    if distancia_metros <= 0:
        return 0
    return max(1, int(round(distancia_metros / (velocidad_kmh * 1000.0 / 60.0))))


def generar_matrices_haversine(coords: List[Tuple[float, float]], cfg: Dict[str, Any]) -> Tuple[List[List[int]], List[List[int]]]:
    factor = float(cfg.get("factor_sinuosidad_vial", 1.30))
    velocidad = float(cfg.get("velocidad_promedio_kmh", 30.0))
    n = len(coords)
    dist = [[0] * n for _ in range(n)]
    tiempo = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                d = distancia_haversine_metros(coords[i], coords[j], factor)
                dist[i][j] = d
                tiempo[i][j] = estimar_tiempo_viaje_minutos(d, velocidad)
    return dist, tiempo


def obtener_matrices_osrm(
    coords: List[Tuple[float, float]],
    cfg: Dict[str, Any],
    session: Optional[requests.Session] = None,
) -> Tuple[List[List[int]], List[List[int]], str]:
    """Matrices desde OSRM; las celdas sin ruta (o todo, si OSRM falla) se completan con Haversine."""
    dist, tiempo = generar_matrices_haversine(coords, cfg)
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in coords)
    print(f"   [INFO] Solicitando matriz a OSRM para {len(coords)} puntos...")
    client = session or requests
    try:
        res = client.get(f"{OSRM_TABLE_URL}/{coords_str}", params={"annotations": "distance,duration"}, timeout=15)
        res.raise_for_status()
        data = res.json()
        if data.get("code") == "Ok":
            for i, (fila_t, fila_d) in enumerate(zip(data["durations"], data["distances"])):
                for j, (t, d) in enumerate(zip(fila_t, fila_d)):
                    if i != j and t is not None and d is not None:
                        tiempo[i][j] = max(1, int(round(t / 60.0)))
                        dist[i][j] = int(round(d))
            return dist, tiempo, "osrm"
        print(f"   [WARNING] OSRM respondio code={data.get('code')}; usando Haversine.")
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        print(f"   [WARNING] OSRM no disponible ({e}); usando Haversine.")
    return dist, tiempo, "haversine"


def alertas_radio_operacional(
    coords_bases: List[Tuple[float, float]],
    coords_ots: List[Tuple[float, float]],
    ordenes: List[Dict[str, Any]],
    cfg: Dict[str, Any],
) -> List[str]:
    """Alertas de OTs demasiado lejos del centroide de las bases de la flota."""
    if not coords_bases:
        return []
    centroide = (sum(p[0] for p in coords_bases) / len(coords_bases),
                 sum(p[1] for p in coords_bases) / len(coords_bases))
    max_radio = float(cfg.get("max_radio_operacional_km", 80.0))
    alertas = []
    for ot, p in zip(ordenes, coords_ots):
        dist_km = distancia_haversine_metros(centroide, p) / 1000.0
        if dist_km > max_radio:
            alertas.append(f"OT {ot.get('id')}: esta a {dist_km:.1f} km del centroide de la flota (radio maximo {max_radio:.0f} km).")
    return alertas

# =============================================================================
# 1. EXTRACCION (API) - SOLO LECTURA
# =============================================================================
def _get_json(client: Any, url: str, params: Optional[Dict[str, str]] = None) -> Any:
    res = client.get(url, params=params, timeout=25)
    res.raise_for_status()
    return res.json()


def obtener_ordenes_pendientes(client: Any, api_base_url: str, fecha: Optional[str]) -> List[Dict[str, Any]]:
    """OTs 'por_asignar'. Con fecha, solo las programadas para ese dia (o sin fecha programada)."""
    ordenes = _get_json(client, f"{api_base_url}/ordenes", {"estado": "por_asignar"})
    if fecha:
        ordenes = [o for o in ordenes if not o.get("fecha_programada") or o["fecha_programada"] == fecha]
    return ordenes


def inferir_fecha(ordenes: List[Dict[str, Any]]) -> str:
    """Fecha programada mas temprana entre las OTs, o hoy."""
    fechas = sorted({o["fecha_programada"] for o in ordenes if o.get("fecha_programada")})
    return fechas[0] if fechas else date.today().isoformat()


def obtener_tecnicos_disponibles(
    client: Any,
    api_base_url: str,
    fecha: str,
    permitir_otra_fecha: bool,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Tecnicos con disponibilidad para la fecha. Si no hay y se permite (fecha no indicada
    explicitamente), usa la primera fecha con disponibilidad desde esa fecha en adelante.
    """
    tecnicos = _get_json(client, f"{api_base_url}/tecnicos")
    disp = _get_json(client, f"{api_base_url}/disponibilidad", {"fecha": fecha})
    ids = {d["tecnico_id"] for d in disp if d.get("disponible") and d.get("fecha", fecha) == fecha}

    if not ids and permitir_otra_fecha:
        todas = _get_json(client, f"{api_base_url}/disponibilidad")
        fechas = sorted({d["fecha"] for d in todas if d.get("disponible") and d.get("fecha")})
        candidatas = [f for f in fechas if f >= fecha] or fechas
        if candidatas:
            print(f"   [WARNING] Sin tecnicos disponibles el {fecha}; se usa {candidatas[0]}.")
            fecha = candidatas[0]
            ids = {d["tecnico_id"] for d in todas if d.get("disponible") and d.get("fecha") == fecha}

    return [t for t in tecnicos if t.get("id") in ids], fecha

# =============================================================================
# 2. TRANSFORMACION (MODELO DE DATOS VRP)
# =============================================================================
def preparar_modelo_datos(
    tecnicos: List[Dict[str, Any]],
    ordenes: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Construye el modelo de datos para OR-Tools. Nodos: [bases de tecnicos..., OTs...]."""
    print("\n2. Transformando datos para el modelo VRP...")
    V = len(tecnicos)
    alertas: List[str] = []

    # Bases de tecnicos
    coords_bases = []
    for t in tecnicos:
        coord, precision = resolver_base_tecnico(t)
        if precision == "aproximada":
            alertas.append(f"Tecnico {t.get('id')}: zona '{t.get('zona')}' no reconocida; se usa Santiago Centro como base.")
        coords_bases.append(coord)

    # Ubicacion de OTs
    precisiones, sin_tiempo = resolver_coordenadas_ordenes(
        ordenes, usar_geocoding=cfg.get("usar_geocoding", True),
        max_segundos=float(cfg.get("geocoding_max_segundos", 60)), session=session)
    excluir_sin_geo = bool(cfg.get("excluir_ots_sin_georreferencia", True))
    no_ruteables: Dict[int, Dict[str, str]] = {}   # OTs que se excluyen del modelo, con su causa
    for i, (ot, precision) in enumerate(zip(ordenes, precisiones)):
        if precision == "aproximada" and excluir_sin_geo:
            no_ruteables[i] = _causa(
                "SIN_GEORREFERENCIA",
                f"no trae coordenadas, la direccion '{ot.get('direccion_instalacion') or ''}' no se pudo "
                f"geocodificar y la comuna '{ot.get('comuna') or ''}' no se reconoce.")
        elif precision == "aproximada":
            alertas.append(f"OT {ot.get('id')}: ubicacion no resuelta; se usa Santiago Centro (ruta poco confiable).")
        elif precision == "comuna":
            alertas.append(f"OT {ot.get('id')}: direccion '{ot.get('direccion_instalacion')}' no encontrada en el mapa; "
                           f"se usa el centro de la comuna.")
    if sin_tiempo:
        alertas.append(f"{sin_tiempo} direcciones nuevas no alcanzaron a geocodificarse (limite de "
                       f"{cfg.get('geocoding_max_segundos', 60)} s); se resolveran en las proximas ejecuciones.")
    coords_ots = [(ot["latitud"], ot["longitud"]) for ot in ordenes]
    con_ubicacion = [i for i in range(len(ordenes)) if i not in no_ruteables]
    alertas += alertas_radio_operacional(coords_bases, [coords_ots[i] for i in con_ubicacion],
                                         [ordenes[i] for i in con_ubicacion], cfg)

    # Matrices: distancias viales (OSRM) con respaldo en linea recta x factor de sinuosidad
    coords = coords_bases + coords_ots
    max_nodos = int(cfg.get("max_nodos_osrm", 100))
    if not cfg.get("usar_osrm", True):
        dist, tiempo = generar_matrices_haversine(coords, cfg)
        fuente = "haversine"
    elif len(coords) > max_nodos:
        dist, tiempo = generar_matrices_haversine(coords, cfg)
        fuente = "haversine"
        alertas.append(f"Matriz de distancias estimada (respaldo): {len(coords)} puntos superan el maximo de "
                       f"{max_nodos} del servicio de ruteo.")
    else:
        dist, tiempo, fuente = obtener_matrices_osrm(coords, cfg, session)
        if fuente != "osrm":
            alertas.append("Matriz de distancias estimada (respaldo): el servicio de ruteo vial no respondio.")

    # Tiempos de servicio
    tiempos_cfg = cfg.get("tiempos_servicio_por_tipo", {})
    st_default = int(cfg.get("tiempo_servicio_default", 30))
    service_times = [0] * V + [int(tiempos_cfg.get(ot.get("tipo"), st_default)) for ot in ordenes]

    # Capacidades: la configuracion es el tope por tipo; cap_max individual solo lo reduce
    cap_ext = int(cfg.get("capacidad_max_externo", 8))
    cap_int = int(cfg.get("capacidad_max_interno", 12))
    capacidades = []
    for t in tecnicos:
        cap_tipo = cap_ext if t.get("tipo") == "externo" else cap_int
        cap_custom = t.get("cap_max") or t.get("capacidad")
        capacidades.append(min(int(cap_custom), cap_tipo) if cap_custom is not None else cap_tipo)

    # Sectores
    sectores = [sector_de_orden(ot) for ot in ordenes]
    sector_counts: Dict[str, int] = {}
    for clave, _ in sectores:
        if clave:
            sector_counts[clave] = sector_counts.get(clave, 0) + 1

    # Sectores de alta concentracion (> umbral OTs): deben ser atendidos por internos
    umbral = int(cfg.get("umbral_ots_sector_interno", 10))
    sectores_internos = {s for s, c in sector_counts.items() if c > umbral} if umbral > 0 else set()
    if sectores_internos and not any(t.get("tipo") == "interno" for t in tecnicos):
        nombres = sorted({nombre for clave, nombre in sectores if clave in sectores_internos})
        alertas.append(f"Sectores con mas de {umbral} OTs sin tecnicos internos disponibles: {', '.join(nombres)}. Los atienden externos.")
        sectores_internos = set()

    # Ventanas horarias (y OTs imposibles de rutear, que se excluyen del modelo)
    fin_jornada = int(cfg.get("fin_jornada_minutos", 600))
    inicio_h = int(cfg.get("inicio_jornada_horas", 8))
    tolerancia = int(cfg.get("ventana_tolerancia_min", 30))
    time_windows: List[Tuple[int, int]] = [(0, fin_jornada)] * V

    for i, ot in enumerate(ordenes):
        st = service_times[V + i]
        ultima_hora_inicio = fin_jornada - st
        if ultima_hora_inicio < 0:
            no_ruteables.setdefault(i, _causa(
                "DURACION_EXCEDE_JORNADA", f"el servicio ({st} min) excede la jornada completa ({fin_jornada} min)."))
            time_windows.append((0, 0))
            continue

        prog = minutos_desde_inicio(ot.get("hora_programada"), inicio_h)
        if prog is None:
            time_windows.append((0, ultima_hora_inicio))
            continue

        inicio_v = max(0, prog - tolerancia)
        fin_v = min(ultima_hora_inicio, prog + tolerancia)
        if fin_v < inicio_v:
            no_ruteables.setdefault(i, _causa(
                "HORA_FUERA_DE_JORNADA",
                f"hora programada {ot.get('hora_programada')} fuera de la jornada "
                f"({minutos_a_hora_str(0, inicio_h)}-{minutos_a_hora_str(ultima_hora_inicio, inicio_h)} "
                f"como ultimo inicio para {st} min de servicio)."))
            time_windows.append((0, ultima_hora_inicio))
        else:
            time_windows.append((inicio_v, fin_v))

    return {
        "num_vehicles": V,
        "starts": list(range(V)),
        "ends": list(range(V)),
        "coords_bases": coords_bases,
        "distance_matrix": dist,
        "time_matrix": tiempo,
        "fuente_matriz": fuente,
        "service_times": service_times,
        "demands": [0] * V + [1] * len(ordenes),
        "vehicle_capacities": capacidades,
        "tipos_tecnico": [t.get("tipo") for t in tecnicos],
        "orden_sectores": [clave for clave, _ in sectores],
        "orden_sectores_nombre": [nombre for _, nombre in sectores],
        "sector_counts": sector_counts,
        "sectores_internos": sectores_internos,
        # Restriccion sectorial dura: en sectores reservados solo se permiten internos
        "sector_exclusivo": bool(cfg.get("sectores_internos_exclusivos", False)) and bool(sectores_internos),
        "time_windows": time_windows,
        "no_ruteables": no_ruteables,
        "precisiones": precisiones,
        "alertas": alertas,
    }

# =============================================================================
# 3. MOTOR DE OPTIMIZACION VRP (OR-TOOLS)
# =============================================================================
def calcular_matrices_costo(data: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, List[List[int]]]:
    """
    Matrices de costo por tipo de tecnico (unidades = metros):
      - internos: + penalty_mix_sector al pasar entre OTs de sectores distintos
      - externos: + costo_por_ot_externo por OT atendida
                  + penalty_externo_sector_interno por OT de un sector reservado a internos
    """
    V = data["num_vehicles"]
    dist = data["distance_matrix"]
    n = len(dist)
    sectores = data["orden_sectores"]
    matrices = {"base": dist, "interno": dist, "externo": dist}

    penalty_mix = int(cfg.get("penalty_mix_sector", 100_000))
    if penalty_mix > 0:
        internos = [fila[:] for fila in dist]
        for i in range(V, n):
            for j in range(V, n):
                si, sj = sectores[i - V], sectores[j - V]
                if i != j and si and sj and si != sj:
                    internos[i][j] += penalty_mix
        matrices["interno"] = internos

    costo_ot_externo = int(cfg.get("costo_por_ot_externo", 0))
    penalty_ext_sector = int(cfg.get("penalty_externo_sector_interno", 1_000_000))
    if costo_ot_externo > 0 or data["sectores_internos"]:
        extra = [0] * V + [costo_ot_externo + (penalty_ext_sector if s in data["sectores_internos"] else 0)
                           for s in sectores]
        matrices["externo"] = [[d + extra[j] for j, d in enumerate(fila)] for fila in dist]
    return matrices


def construir_modelo(data: Dict[str, Any], cfg: Dict[str, Any], matrices: Dict[str, List[List[int]]]):
    """Crea el modelo de ruteo: costos por tipo, tiempo con ventanas, capacidad y descarte penalizado."""
    V = data["num_vehicles"]
    n = len(data["distance_matrix"])
    manager = pywrapcp.RoutingIndexManager(n, V, data["starts"], data["ends"])
    routing = pywrapcp.RoutingModel(manager)

    penalty_drop = int(cfg.get("penalty_drop_node", 10_000_000))
    span_coeff = int(cfg.get("span_cost_coefficient", 50))
    fin_jornada = int(cfg.get("fin_jornada_minutos", 600))

    # Matrices registradas en C++ (RegisterTransitMatrix): el solver no llama a Python por cada arco,
    # lo que multiplica las iteraciones de busqueda en el mismo tiempo.
    callbacks = {}
    for v, tipo in enumerate(data["tipos_tecnico"]):
        clave = tipo if tipo in ("interno", "externo") else "base"
        if clave not in callbacks:
            callbacks[clave] = routing.RegisterTransitMatrix(matrices[clave])
        routing.SetArcCostEvaluatorOfVehicle(callbacks[clave], v)

    # Dimension de tiempo: viaje + servicio en el nodo de origen
    tiempo, st = data["time_matrix"], data["service_times"]
    cb_tiempo = routing.RegisterTransitMatrix([[tiempo[i][j] + st[i] for j in range(n)] for i in range(n)])
    routing.AddDimension(cb_tiempo, fin_jornada, fin_jornada, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    if span_coeff > 0:
        time_dim.SetGlobalSpanCostCoefficient(span_coeff)

    # Horarios concretos: costo minimo (1 por minuto) a la hora de termino para que las rutas
    # terminen lo antes posible en vez de "flotar" hacia el final de la jornada. El span cost
    # (fin - inicio) hace que, con ese termino, la salida sea lo mas tarde posible (sin esperas).
    for v in range(V):
        time_dim.SetCumulVarSoftUpperBound(routing.End(v), 0, 1)

    # Ventanas horarias, descarte penalizado y sectores exclusivos de internos
    no_internos = [v for v, tipo in enumerate(data["tipos_tecnico"]) if tipo != "interno"]
    for node in range(V, n):
        index = manager.NodeToIndex(node)
        routing.AddDisjunction([index], penalty_drop)
        if (node - V) in data["no_ruteables"]:
            routing.ActiveVar(index).SetValue(0)
            continue
        ini, fin = data["time_windows"][node]
        time_dim.CumulVar(index).SetRange(ini, fin)
        if data["sector_exclusivo"] and data["orden_sectores"][node - V] in data["sectores_internos"]:
            for v in no_internos:
                routing.VehicleVar(index).RemoveValue(v)

    # Capacidad (cantidad de OTs por tecnico)
    cb_demanda = routing.RegisterUnaryTransitVector(data["demands"])
    routing.AddDimensionWithVehicleCapacity(cb_demanda, 0, data["vehicle_capacities"], True, "Capacity")
    return manager, routing


# Ninguna estrategia inicial domina en todos los tamanos de problema (medido con datos reales y
# sinteticos de 18 a 100 OTs): se prueban ambas repartiendo el tiempo y se conserva la mejor.
ESTRATEGIAS_INICIALES = ("PATH_CHEAPEST_ARC", "PARALLEL_CHEAPEST_INSERTION")


def calcular_tiempo_limite(cfg: Dict[str, Any], n_ots: int, solicitado: Optional[int] = None) -> int:
    """Segundos de busqueda: el pedido por la corrida o, si no, el automatico segun la cantidad de OTs."""
    maximo = int(cfg.get("solver_time_limit_max_seconds", 120))
    if solicitado:
        return max(1, min(maximo, int(solicitado)))
    # Automatico: la configuracion es el minimo y crece con el problema
    # (con 60 OTs, 10 s dejaban soluciones ~20% peores que 30 s).
    por_ot = float(cfg.get("solver_segundos_por_ot", 0.5))
    return min(maximo, max(int(cfg.get("solver_time_limit_seconds", 10)), math.ceil(n_ots * por_ot)))


def resolver_rutas(
    data: Dict[str, Any],
    cfg: Dict[str, Any],
    tiempo_limite_segundos: Optional[int] = None,
) -> Tuple[pywrapcp.RoutingIndexManager, pywrapcp.RoutingModel, Any]:
    """Resuelve el VRP con cada estrategia inicial (Guided Local Search) y retorna la mejor solucion."""
    print("\n3. Ejecutando motor de optimizacion OR-Tools...")
    V = data["num_vehicles"]
    n_ots = len(data["distance_matrix"]) - V
    time_limit = calcular_tiempo_limite(cfg, n_ots, tiempo_limite_segundos)
    data["tiempo_limite_s"] = time_limit
    tiempo_por_estrategia = max(1.0, time_limit / len(ESTRATEGIAS_INICIALES))
    matrices = calcular_matrices_costo(data, cfg)

    print(f"   [PROCESS] Resolviendo modelo VRP ({V} tecnicos, {n_ots} OTs, limite {time_limit}s)...")
    mejor = (None, None, None)
    for estrategia in ESTRATEGIAS_INICIALES:
        manager, routing = construir_modelo(data, cfg, matrices)
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = getattr(routing_enums_pb2.FirstSolutionStrategy, estrategia)
        params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        params.time_limit.FromMilliseconds(int(tiempo_por_estrategia * 1000))

        solution = routing.SolveWithParameters(params)
        if solution:
            print(f"   [OK] {estrategia}: costo objetivo {solution.ObjectiveValue()}")
            if mejor[2] is None or solution.ObjectiveValue() < mejor[2].ObjectiveValue():
                mejor = (manager, routing, solution)
        else:
            print(f"   [WARNING] {estrategia}: sin solucion factible")

    if mejor[2] is None:
        return manager, routing, None
    return mejor

# =============================================================================
# 4. DIAGNOSTICO DE OTs NO ASIGNADAS
# =============================================================================
def diagnosticar_orden_pendiente(idx_orden: int, data: Dict[str, Any], cfg: Dict[str, Any],
                                 carga: List[int]) -> List[Dict[str, str]]:
    """Clasifica por que una OT quedo sin asignar. Retorna causas {codigo, categoria, detalle}; la primera es la principal."""
    if idx_orden in data["no_ruteables"]:
        return [data["no_ruteables"][idx_orden]]

    V = data["num_vehicles"]
    nodo = V + idx_orden
    fin_jornada = int(cfg.get("fin_jornada_minutos", 600))
    inicio_h = int(cfg.get("inicio_jornada_horas", 8))
    tiempo = data["time_matrix"]
    st = data["service_times"][nodo]
    ini_v, fin_v = data["time_windows"][nodo]

    # Factibilidad horaria: llegar dentro de la ventana y volver a la base antes del fin de jornada
    def factible(v: int) -> bool:
        llegada = tiempo[v][nodo]
        return llegada <= fin_v and max(ini_v, llegada) + st + tiempo[nodo][v] <= fin_jornada

    alcanzan = [v for v in range(V) if factible(v)]
    if not alcanzan:
        viaje_min = min(tiempo[v][nodo] for v in range(V))
        return [_causa(
            "VENTANA_INALCANZABLE",
            f"ningun tecnico alcanza a iniciar entre "
            f"{minutos_a_hora_str(ini_v, inicio_h)} y {minutos_a_hora_str(fin_v, inicio_h)} "
            f"y volver antes del fin de jornada (viaje minimo desde base: {viaje_min} min, servicio: {st} min).")]

    con_cupo = [v for v in alcanzan if carga[v] < data["vehicle_capacities"][v]]

    # Sectorial: la regla de sector reservado dejo fuera a externos que alcanzaban y tenian cupo.
    # Aplica si la regla es dura, o si la penalizacion configurada es tan cara como dejar la OT sin asignar.
    if data["orden_sectores"][idx_orden] in data["sectores_internos"]:
        regla_impide = data["sector_exclusivo"] or (
            int(cfg.get("penalty_externo_sector_interno", 0)) >= int(cfg.get("penalty_drop_node", 0)))
        externos_libres = [v for v in con_cupo if data["tipos_tecnico"][v] != "interno"]
        if regla_impide and externos_libres:
            sector = data["orden_sectores_nombre"][idx_orden]
            return [_causa(
                "SECTOR_RESERVADO_INTERNOS",
                f"el sector '{sector}' ({data['sector_counts'][data['orden_sectores'][idx_orden]]} OTs) esta reservado a "
                f"tecnicos internos y ninguno pudo tomarla; {len(externos_libres)} externo(s) con cupo y horario "
                f"quedaron excluidos por la regla de sector.")]
        if data["sector_exclusivo"]:
            con_cupo = [v for v in con_cupo if data["tipos_tecnico"][v] == "interno"]

    if not con_cupo:
        return [_causa("CAPACIDAD_AGOTADA",
                       "los tecnicos que alcanzan a atenderla completaron su capacidad maxima de OTs.")]
    return [_causa(
        "SIN_CUPO_EN_RUTAS",
        "no cabe en la jornada junto a las demas OTs asignadas (ventanas horarias o tiempos de viaje). "
        "Prueba aumentar el tiempo del solver o la flota.")]

# =============================================================================
# 5. RESULTADO Y APLICACION EN API
# =============================================================================
def _resultado_vacio(status: str, mensaje: str, fecha: Optional[str], ordenes: List[Dict[str, Any]],
                     alertas: Optional[List[str]] = None, total_tecnicos: int = 0) -> Dict[str, Any]:
    return {
        "status": status,
        "mensaje": mensaje,
        "fecha": fecha,
        "resumen": {
            "total_ots": len(ordenes),
            "ots_asignadas": 0,
            "ots_pendientes": len(ordenes),
            "pendientes_por_causa": {categoria: 0 for categoria in dict.fromkeys(c[0] for c in CAUSAS.values())},
            "total_tecnicos": total_tecnicos,
            "tecnicos_utilizados": 0,
        },
        "diagnosticos": [],
        "alertas": alertas or [],
        "rutas": [],
        "aplicacion_cambios": {"aplicado": False},
    }


def construir_resultado(
    manager: pywrapcp.RoutingIndexManager,
    routing: pywrapcp.RoutingModel,
    solution: Any,
    tecnicos: List[Dict[str, Any]],
    ordenes: List[Dict[str, Any]],
    data: Dict[str, Any],
    cfg: Dict[str, Any],
    fecha: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Arma hojas de ruta, diagnosticos y la lista de asignaciones (tecnico por OT)."""
    if not solution:
        return _resultado_vacio("infeasible", "No se encontro solucion factible.", fecha, ordenes,
                                data["alertas"], len(tecnicos)), []

    V = data["num_vehicles"]
    dist, tiempo, st = data["distance_matrix"], data["time_matrix"], data["service_times"]
    time_dim = routing.GetDimensionOrDie("Time")
    inicio_h = int(cfg.get("inicio_jornada_horas", 8))
    hora = lambda m: minutos_a_hora_str(m, inicio_h)

    rutas: List[Dict[str, Any]] = []
    asignaciones: List[Dict[str, Any]] = []
    carga = [0] * V

    for v, tec in enumerate(tecnicos):
        index = routing.Start(v)
        t_salida = solution.Min(time_dim.CumulVar(index))
        prev_node, prev_inicio = v, t_salida
        paradas, distancia_m, espera_total = [], 0, 0

        index = solution.Value(routing.NextVar(index))
        while True:
            node = manager.IndexToNode(index)
            distancia_m += dist[prev_node][node]
            if routing.IsEnd(index):
                break
            i = node - V
            ot = ordenes[i]
            inicio = solution.Min(time_dim.CumulVar(index))
            llegada = prev_inicio + st[prev_node] + tiempo[prev_node][node]
            espera = max(0, inicio - llegada)
            espera_total += espera

            parada = {
                "secuencia": len(paradas) + 1,
                "ot_id": ot["id"],
                "tipo": ot.get("tipo", "N/A"),
                "cliente": ot.get("cliente"),
                "direccion": ot.get("direccion_instalacion", ""),
                "latitud": ot.get("latitud"),
                "longitud": ot.get("longitud"),
                "precision_ubicacion": data["precisiones"][i],
                "hora_programada": ot.get("hora_programada"),
                "hora_estimada_llegada": hora(inicio),
                "hora_estimada_salida": hora(inicio + st[node]),
                "espera_min": espera,
                "duracion_servicio_min": st[node],
                "sector": data["orden_sectores_nombre"][i],
            }
            paradas.append(parada)
            asignaciones.append({
                "ot_id": ot["id"],
                "tecnico_id": tec["id"],
                **{k: parada[k] for k in ("secuencia", "hora_estimada_llegada", "hora_estimada_salida",
                                          "duracion_servicio_min", "sector")},
            })
            prev_node, prev_inicio = node, inicio
            index = solution.Value(routing.NextVar(index))

        t_retorno = solution.Min(time_dim.CumulVar(index))
        carga[v] = len(paradas)
        base_lat, base_lon = data["coords_bases"][v]
        nombre = f"{tec.get('nombre', '')} {tec.get('apellidos') or ''}".strip() or "Tecnico"
        rutas.append({
            "tecnico_id": tec["id"],
            "nombre": nombre,
            "tipo": tec.get("tipo", "N/A"),
            "zona_base": tec.get("zona", "N/A"),
            "base_latitud": base_lat,
            "base_longitud": base_lon,
            "capacidad_uso": f"{len(paradas)}/{data['vehicle_capacities'][v]}",
            "hora_salida_base": hora(t_salida) if paradas else None,
            "hora_retorno_base": hora(t_retorno) if paradas else None,
            "duracion_total_min": (t_retorno - t_salida) if paradas else 0,
            "distancia_total_km": round(distancia_m / 1000.0, 1),
            "tiempo_espera_total_min": espera_total,
            "total_ots": len(paradas),
            "paradas": paradas,
        })

    diagnosticos = []
    for i, ot in enumerate(ordenes):
        index = manager.NodeToIndex(V + i)
        if solution.Value(routing.NextVar(index)) == index:
            causas = diagnosticar_orden_pendiente(i, data, cfg, carga)
            sin_ubicacion = causas[0]["codigo"] == "SIN_GEORREFERENCIA"  # Sus coordenadas son un relleno, no se informan
            diagnosticos.append({
                "ot_id": ot["id"],
                "tipo": ot.get("tipo"),
                "cliente": ot.get("cliente"),
                "direccion": ot.get("direccion_instalacion", ""),
                "latitud": None if sin_ubicacion else ot.get("latitud"),
                "longitud": None if sin_ubicacion else ot.get("longitud"),
                "precision_ubicacion": data["precisiones"][i],
                "hora_programada": ot.get("hora_programada") or "Libre",
                "sector": data["orden_sectores_nombre"][i],
                "causa_principal": causas[0]["categoria"],
                "causas": causas,
                "razones": [_razon(c) for c in causas],
            })

    # Verificacion de sectores de internos: quien los atendio
    alertas = list(data["alertas"])
    sector_por_ot = {ot["id"]: data["orden_sectores"][i] for i, ot in enumerate(ordenes)}
    nombre_sector = dict(zip(data["orden_sectores"], data["orden_sectores_nombre"]))
    for sector in sorted(data["sectores_internos"]):
        tipos = [ru["tipo"] for ru in rutas for p in ru["paradas"] if sector_por_ot[p["ot_id"]] == sector]
        nombre = nombre_sector[sector]
        if "interno" not in tipos:
            alertas.append(f"Sector '{nombre}' ({data['sector_counts'][sector]} OTs) requiere interno, "
                           f"pero ningun interno pudo atenderlo (horario/capacidad).")
        elif "externo" in tipos:
            alertas.append(f"Sector '{nombre}': los internos no alcanzaron a cubrir todo; "
                           f"{tipos.count('externo')} OTs las atiende un externo de apoyo.")

    pendientes_por_causa = {categoria: 0 for categoria in dict.fromkeys(c[0] for c in CAUSAS.values())}
    for d in diagnosticos:
        pendientes_por_causa[d["causa_principal"]] += 1
    precision_ubicaciones: Dict[str, int] = {}
    for precision in data["precisiones"]:
        precision_ubicaciones[precision] = precision_ubicaciones.get(precision, 0) + 1

    resultado = {
        "status": "success",
        "fecha": fecha,
        "resumen": {
            "total_ots": len(ordenes),
            "ots_asignadas": len(asignaciones),
            "ots_pendientes": len(diagnosticos),
            "pendientes_por_causa": pendientes_por_causa,
            "total_tecnicos": len(tecnicos),
            "tecnicos_utilizados": sum(1 for r in rutas if r["total_ots"] > 0),
            "distancia_total_km": round(sum(r["distancia_total_km"] for r in rutas), 1),
            "costo_objetivo": solution.ObjectiveValue(),
            "fuente_matriz": data["fuente_matriz"],
            "precision_ubicaciones": precision_ubicaciones,
        },
        "diagnosticos": diagnosticos,
        "alertas": alertas,
        "rutas": rutas,
    }
    return resultado, asignaciones


def aplicar_asignaciones_api(asignaciones: List[Dict[str, Any]], client: Any, api_base_url: str) -> Dict[str, Any]:
    """Asigna el tecnico de cada OT en la API externa. Omite las OTs que no existen alla."""
    out: Dict[str, Any] = {"aplicado": True, "enviadas": 0, "omitidas": [], "errores": []}
    print(f"\n5. Aplicando {len(asignaciones)} asignaciones en la API externa...")
    try:
        existentes = {o.get("id") for o in _get_json(client, f"{api_base_url}/ordenes")}
    except (requests.RequestException, ValueError) as e:
        out["errores"].append(f"No se pudo validar las OTs en la API externa: {e}")
        return out

    for a in asignaciones:
        if a["ot_id"] not in existentes:
            out["omitidas"].append(a["ot_id"])
            continue
        try:
            res = client.patch(f"{api_base_url}/ordenes/{a['ot_id']}/tecnico",
                               json={"tecnico_id": a["tecnico_id"]}, timeout=15)
            if res.status_code == 200:
                out["enviadas"] += 1
            else:
                out["errores"].append(f"{a['ot_id']}: HTTP {res.status_code}")
        except requests.RequestException as e:
            out["errores"].append(f"{a['ot_id']}: {e}")
    print(f"   [API] {out['enviadas']} aplicadas, {len(out['omitidas'])} omitidas, {len(out['errores'])} con error.")
    return out

# =============================================================================
# FUNCION PRINCIPAL
# =============================================================================
def optimizar_jornada(
    fecha: Optional[str] = None,
    aplicar_cambios: Optional[bool] = None,
    tiempo_limite_segundos: Optional[int] = None,
    api_base_url: str = API_BASE_URL,
    session: Optional[requests.Session] = None,
    tecnicos: Optional[List[Dict[str, Any]]] = None,
    ordenes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Ejecuta el pipeline completo. `tecnicos`/`ordenes` recibidos por parametro se usan tal cual
    (los tecnicos se asumen disponibles); lo que falte se obtiene de la API externa.
    `aplicar_cambios` y `tiempo_limite_segundos` en None toman su valor de la configuracion.
    """
    cfg = obtener_configuracion()  # Snapshot: cambios de configuracion durante la ejecucion no la afectan
    resultado = _optimizar(cfg, fecha, aplicar_cambios, tiempo_limite_segundos, api_base_url, session, tecnicos, ordenes)
    # Parametros con los que efectivamente se resolvio (los de la corrida y la configuracion vigente)
    resultado["parametros_corrida"] = {
        "fecha": resultado.get("fecha"),
        "aplicar_cambios": cfg["aplicar_cambios_por_defecto"] if aplicar_cambios is None else aplicar_cambios,
        "tiempo_limite_segundos": resultado.pop("_tiempo_limite_s", None),
        "tiempo_limite_origen": "corrida" if tiempo_limite_segundos else "automatico",
        "configuracion": cfg,
    }
    return resultado


def _optimizar(
    cfg: Dict[str, Any],
    fecha: Optional[str],
    aplicar_cambios: Optional[bool],
    tiempo_limite_segundos: Optional[int],
    api_base_url: str,
    session: Optional[requests.Session],
    tecnicos: Optional[List[Dict[str, Any]]],
    ordenes: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    if aplicar_cambios is None:
        aplicar_cambios = bool(cfg["aplicar_cambios_por_defecto"])
    client = session or requests.Session()

    print("1. Obteniendo datos operativos...")
    try:
        if ordenes is None:
            ordenes = obtener_ordenes_pendientes(client, api_base_url, fecha)
        fecha_efectiva = fecha or inferir_fecha(ordenes)
        if tecnicos is None:
            tecnicos, fecha_efectiva = obtener_tecnicos_disponibles(
                client, api_base_url, fecha_efectiva, permitir_otra_fecha=not fecha)
    except (requests.RequestException, ValueError) as e:
        print(f"   [ERROR] API externa: {e}")
        return _resultado_vacio("error_api", f"No se pudo consultar la API externa: {e}", fecha, ordenes or [])

    print(f"   [RESUMEN] {len(tecnicos)} tecnicos | {len(ordenes)} OTs (fecha: {fecha_efectiva})")
    if not tecnicos or not ordenes:
        return _resultado_vacio("no_data", "Faltan tecnicos disponibles u OTs por asignar para la fecha.",
                                fecha_efectiva, ordenes, total_tecnicos=len(tecnicos))

    # Copias: el modelo completa coordenadas y no debe mutar los datos del llamador
    tecnicos = [dict(t) for t in tecnicos]
    ordenes = [dict(o) for o in ordenes]

    data = preparar_modelo_datos(tecnicos, ordenes, cfg, client)
    manager, routing, solution = resolver_rutas(data, cfg, tiempo_limite_segundos)
    resultado, asignaciones = construir_resultado(manager, routing, solution, tecnicos, ordenes, data, cfg, fecha_efectiva)
    resultado["_tiempo_limite_s"] = data["tiempo_limite_s"]

    if aplicar_cambios and asignaciones:
        resultado["aplicacion_cambios"] = aplicar_asignaciones_api(asignaciones, client, api_base_url)
    else:
        resultado["aplicacion_cambios"] = {"aplicado": False}
    return resultado


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Optimizador de Rutas VRP")
    parser.add_argument("--fecha", help="Fecha a optimizar (YYYY-MM-DD). Por defecto se infiere de las OTs.")
    parser.add_argument("--reset", action="store_true", help="Llama a /api/reset de la API externa antes de optimizar")
    parser.add_argument("--aplicar", action="store_true", help="Aplica las asignaciones en la API externa")
    args = parser.parse_args()

    print("OPTIMIZADOR DE RUTAS VRP")
    with requests.Session() as s:
        if args.reset:
            try:
                r = s.post(f"{API_BASE_URL}/reset", timeout=25)
                print(f"-> /api/reset: HTTP {r.status_code}")
            except requests.RequestException as e:
                print(f"-> /api/reset fallo: {e}")
        res = optimizar_jornada(fecha=args.fecha, aplicar_cambios=args.aplicar, session=s)

    print("\n=== RESULTADO ===")
    print(json.dumps(res["resumen"], indent=2, ensure_ascii=False))
    for r in res["rutas"]:
        print(f"- {r['nombre']} ({r['tipo']}): {r['capacidad_uso']} OTs, {r['distancia_total_km']} km, "
              f"{r['hora_salida_base'] or '--'} -> {r['hora_retorno_base'] or '--'}")
    for d in res["diagnosticos"]:
        print(f"! {d['ot_id']}: {' | '.join(d['razones'])}")
    for a in res["alertas"]:
        print(f"~ {a}")
