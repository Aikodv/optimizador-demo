# =============================================================================
# API REST & Dashboard Server - Optimizacion de Rutas y Despacho VRP
# =============================================================================
#
# DESCRIPCION:
#   Servidor FastAPI que alimenta el sistema de optimizacion de rutas en Chile.
#   Provee endpoints REST para gestion de tecnicos, ordenes de trabajo,
#   disponibilidades, simulacion dinamica de datos, hojas de ruta y una
#   interfaz web interactiva con mapas Leaflet.js para control y despacho.
#
# EJECUCION:
#   python -m uvicorn main:app --reload --port 8000
#
#   - Dashboard Web:  http://127.0.0.1:8000/
#   - Swagger UI:     http://127.0.0.1:8000/docs
# =============================================================================

import random
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

import requests
from faker import Faker
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

# =============================================================================
# CONFIGURACION INICIAL Y RUTAS
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

fake = Faker("es_CL")
random.seed(42)
Faker.seed(42)

app = FastAPI(
    title="Backoffice & Dispatch API - Optimizacion de Rutas",
    description=(
        "API REST y Dashboard Web para gestion de tecnicos, ordenes de trabajo, "
        "simulacion de datos en terreno, hojas de ruta y optimizacion con Google OR-Tools."
    ),
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# ENUMS Y CONSTANTES GEOGRAFICAS
# =============================================================================

TIPOS_TECNICO = ["interno", "externo"]

TIPOS_OT = [
    "instalacion_simple",
    "instalacion_con_corte",
    "mantencion",
    "retiro",
]

ESTADOS_OT = [
    "por_revisar",
    "por_asignar",
    "asignacion_por_confirmar",
    "asignada",
    "en_terreno",
    "finalizada",
    "enviada_cobranza",
]

ZONAS_SANTIAGO = [
    "Santiago Centro",
    "Providencia",
    "Las Condes",
    "Vitacura",
    "Nunoa",
    "La Florida",
    "Maipu",
    "Pudahuel",
    "Quilicura",
    "Penalolen",
    "La Reina",
    "Macul",
    "San Miguel",
    "La Cisterna",
    "El Bosque",
    "Puente Alto",
    "San Bernardo",
    "Lo Barnechea",
    "Cerrillos",
    "Estacion Central",
]

ZONAS_COORDENADAS = {
    "Santiago Centro": (-33.4372, -70.6572),
    "Providencia": (-33.4289, -70.6080),
    "Las Condes": (-33.3937, -70.5840),
    "Vitacura": (-33.3999, -70.5731),
    "Nunoa": (-33.4520, -70.6120),
    "La Florida": (-33.4930, -70.5863),
    "Maipu": (-33.4865, -70.7602),
    "Pudahuel": (-33.4584, -70.7838),
    "Quilicura": (-33.3752, -70.7715),
    "Penalolen": (-33.4750, -70.5709),
    "La Reina": (-33.4688, -70.5507),
    "Macul": (-33.4804, -70.6224),
    "San Miguel": (-33.5034, -70.7097),
    "La Cisterna": (-33.5193, -70.6529),
    "El Bosque": (-33.5431, -70.6675),
    "Puente Alto": (-33.6142, -70.5755),
    "San Bernardo": (-33.6002, -70.7243),
    "Lo Barnechea": (-33.3559, -70.5250),
    "Cerrillos": (-33.5028, -70.7425),
    "Estacion Central": (-33.4578, -70.6676),
}

CALLES_SANTIAGO = [
    "Avenida Libertador Bernardo O Higgins",
    "Avenida Providencia",
    "Avenida Apoquindo",
    "Avenida Vitacura",
    "Avenida Las Condes",
    "Calle Huerfanos",
    "Calle Agustinas",
    "Avenida Irarrazaval",
    "Avenida Grecia",
    "Calle Merced",
    "Paseo Ahumada",
    "Avenida Americo Vespucio",
    "Avenida Tobalaba",
    "Avenida Vicuna Mackenna",
    "Avenida Recoleta",
    "Calle San Antonio",
    "Avenida Matta",
    "Calle Condell",
    "Avenida El Golf",
    "Calle Estado",
    "Avenida Pajaritos",
    "Gran Avenida Jose Miguel Carrera",
    "Avenida Concha y Toro",
    "Avenida Macul",
    "Avenida Los Leones",
]

def generar_direccion_santiago() -> tuple[str, str]:
    """Genera una direccion ficticia y su zona asociada en Santiago, Chile."""
    calle = random.choice(CALLES_SANTIAGO)
    numero = random.randint(100, 9999)
    piso_o_depto = ""
    if random.random() > 0.5:
        tipo = random.choice(["Depto", "Of", "Piso"])
        num = random.randint(1, 20)
        piso_o_depto = f", {tipo}. {num}"
    zona = random.choice(ZONAS_SANTIAGO)
    direccion = f"{calle} #{numero}{piso_o_depto}, {zona}, Santiago, Chile"
    return direccion, zona

def generar_coordenadas_por_zona(zona: str) -> tuple[float, float]:
    """Genera coordenadas aleatorias dentro de la zona seleccionada."""
    base = ZONAS_COORDENADAS.get(zona, (-33.45, -70.66))
    lat = base[0] + random.uniform(-0.008, 0.008)
    lon = base[1] + random.uniform(-0.008, 0.008)
    return lat, lon

# =============================================================================
# FUNCIONES DE GENERACION DE DATOS SIMULADOS
# =============================================================================

def generar_tecnicos(n: int = 4) -> list:
    """Genera lista de técnicos con capacidades operativas."""
    tecnicos = []
    for _ in range(n):
        tipo_tec = random.choice(TIPOS_TECNICO)
        tecnico = {
            "id": str(uuid.uuid4()),
            "nombre": fake.first_name(),
            "apellidos": f"{fake.last_name()} {fake.last_name()}",
            "tipo": tipo_tec,
            "zona": random.choice(ZONAS_SANTIAGO),
            "cap_max": 8 if tipo_tec == "externo" else 12,
        }
        tecnicos.append(tecnico)
    return tecnicos

def generar_disponibilidades(tecnicos: list, dias: int = 14) -> list:
    """Genera disponibilidades para cada técnico."""
    disponibilidades = []
    hoy = date.today()

    for tecnico in tecnicos:
        for offset in range(dias):
            fecha = hoy + timedelta(days=offset)
            es_fin_de_semana = fecha.weekday() >= 5
            prob_disponible = 0.4 if es_fin_de_semana else 0.95

            disponibilidad = {
                "id": str(uuid.uuid4()),
                "tecnico_id": tecnico["id"],
                "fecha": fecha.isoformat(),
                "disponible": random.random() < prob_disponible,
            }
            disponibilidades.append(disponibilidad)

    return disponibilidades

def generar_ordenes_trabajo(tecnicos: list, n: int = 15) -> list:
    """Genera lista de órdenes de trabajo por asignar con horarios y tipos variados."""
    ordenes = []
    for i in range(1, n + 1):
        direccion, zona = generar_direccion_santiago()
        latitud, longitud = generar_coordenadas_por_zona(zona)
        
        hora_prog = None
        # 60% de probabilidad de tener hora acordada
        if random.random() > 0.4:
            hora = random.randint(9, 16)
            minuto = random.choice([0, 30])
            hora_prog = f"{hora:02d}:{minuto:02d}"

        orden = {
            "id": f"OT-{i:04d}",
            "tipo": random.choice(TIPOS_OT),
            "estado": "por_asignar",
            "tecnico_id": None,
            "cliente": f"{fake.first_name()} {fake.last_name()}",
            "direccion_instalacion": direccion,
            "latitud": latitud,
            "longitud": longitud,
            "fecha_programada": date.today().isoformat(),
            "hora_programada": hora_prog,
            "secuencia": None,
            "hora_estimada_llegada": None,
            "hora_estimada_salida": None,
        }
        ordenes.append(orden)

    return ordenes

# Base de datos en memoria
DB_TECNICOS = generar_tecnicos(n=4)
DB_DISPONIBILIDADES = generar_disponibilidades(DB_TECNICOS, dias=14)
DB_ORDENES = generar_ordenes_trabajo(DB_TECNICOS, n=15)
DB_RUTAS_PLANIFICADAS: Dict[str, Any] = {}

# =============================================================================
# MODELOS PYDANTIC
# =============================================================================

class AsignarTecnicoRequest(BaseModel):
    tecnico_id: str

class AsignacionItem(BaseModel):
    ot_id: str
    tecnico_id: str
    secuencia: Optional[int] = None
    hora_estimada_llegada: Optional[str] = None
    hora_estimada_salida: Optional[str] = None
    duracion_servicio_min: Optional[int] = None
    sector: Optional[str] = None

class AsignacionesMasivasRequest(BaseModel):
    fecha: Optional[str] = Field(default_factory=lambda: date.today().isoformat())
    asignaciones: List[AsignacionItem]

class EjecutarOptimizacionRequest(BaseModel):
    fecha: Optional[str] = Field(default_factory=lambda: date.today().isoformat())
    aplicar_cambios: bool = True
    tiempo_limite_segundos: int = 10

class RegenerarDatosRequest(BaseModel):
    num_tecnicos: int = Field(default=4, ge=1, le=10)
    num_ordenes: int = Field(default=15, ge=1, le=60)
    dias_disponibilidad: int = Field(default=14, ge=1, le=30)

class ConfiguracionVRPRequest(BaseModel):
    tiempos_servicio_por_tipo: Optional[Dict[str, int]] = None
    tiempo_servicio_default: Optional[int] = None
    inicio_jornada_horas: Optional[int] = Field(default=None, ge=5, le=12)
    fin_jornada_minutos: Optional[int] = Field(default=None, ge=180, le=1440)
    capacidad_max_externo: Optional[int] = Field(default=None, ge=1, le=30)
    capacidad_max_interno: Optional[int] = Field(default=None, ge=1, le=30)
    factor_sinuosidad_vial: Optional[float] = Field(default=None, ge=1.0, le=3.0)
    velocidad_promedio_kmh: Optional[float] = Field(default=None, ge=10.0, le=120.0)
    max_radio_operacional_km: Optional[float] = Field(default=None, ge=10.0, le=300.0)
    penalty_drop_node: Optional[int] = None
    penalty_mix_sector: Optional[int] = None
    span_cost_coefficient: Optional[int] = Field(default=None, ge=0, le=500)
    solver_time_limit_seconds: Optional[int] = Field(default=None, ge=1, le=120)
    usar_osrm: Optional[bool] = None
    usar_geocoding: Optional[bool] = None

# =============================================================================
# ENDPOINTS REST Y FRONTEND
# =============================================================================

@app.get("/", tags=["Dashboard Web"], response_class=HTMLResponse)
def serve_dashboard():
    """Sirve la interfaz web interactiva del optimizador."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse("<h2>Dashboard no encontrado. Asegurese de que static/index.html exista.</h2>")

@app.get("/api/info", tags=["Root"])
def info():
    return {
        "mensaje": "API Backoffice & Despacho - Optimizador de Rutas",
        "version": "2.1.0",
        "docs": "/docs",
        "dashboard": "/",
    }

# --- Simulacion y Regeneracion de Datos ---

@app.post(
    "/api/simulacion/regenerar",
    tags=["Simulacion"],
    summary="Regenerar datos ficticios de prueba (Tecnicos, Disponibilidades, OTs)",
    response_description="Nuevos datos simulados cargados en memoria.",
)
def regenerar_datos(body: RegenerarDatosRequest):
    """
    Regenera la base de datos en memoria con nuevos tecnicos, disponibilidades y ordenes de trabajo.
    Limpia las hojas de ruta previas para permitir una nueva optimizacion desde cero.
    """
    global DB_TECNICOS, DB_DISPONIBILIDADES, DB_ORDENES, DB_RUTAS_PLANIFICADAS
    
    DB_TECNICOS = generar_tecnicos(n=body.num_tecnicos)
    DB_DISPONIBILIDADES = generar_disponibilidades(DB_TECNICOS, dias=body.dias_disponibilidad)
    DB_ORDENES = generar_ordenes_trabajo(DB_TECNICOS, n=body.num_ordenes)
    DB_RUTAS_PLANIFICADAS.clear()

    return {
        "status": "success",
        "mensaje": f"Base de datos regenerada con exito: {len(DB_TECNICOS)} tecnicos y {len(DB_ORDENES)} OTs.",
        "total_tecnicos": len(DB_TECNICOS),
        "total_ordenes": len(DB_ORDENES),
        "fecha": date.today().isoformat()
    }

# --- Tecnicos ---

@app.get("/api/tecnicos", tags=["Tecnicos"], summary="Obtener lista de tecnicos")
def get_tecnicos():
    return DB_TECNICOS

# --- Disponibilidad ---

@app.get("/api/disponibilidad", tags=["Disponibilidad"], summary="Obtener disponibilidades")
def get_disponibilidad(fecha: Optional[str] = Query(default=None, example="2026-08-26")):
    if fecha is None:
        return DB_DISPONIBILIDADES
    try:
        date.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Fecha invalida: '{fecha}'. Use YYYY-MM-DD.")
    return [d for d in DB_DISPONIBILIDADES if d["fecha"] == fecha]

# --- Ordenes de Trabajo ---

@app.get("/api/ordenes", tags=["Ordenes de Trabajo"], summary="Obtener lista de ordenes")
def get_ordenes(estado: Optional[str] = Query(default=None, example="por_asignar")):
    if estado is None:
        return DB_ORDENES
    if estado not in ESTADOS_OT:
        raise HTTPException(status_code=422, detail=f"Estado invalido. Permitidos: {', '.join(ESTADOS_OT)}")
    return [o for o in DB_ORDENES if o["estado"] == estado]

@app.patch("/api/ordenes/{id}/tecnico", tags=["Ordenes de Trabajo"], summary="Asignacion individual")
def asignar_tecnico(id: str, body: AsignarTecnicoRequest):
    orden = next((o for o in DB_ORDENES if o["id"] == id), None)
    if orden is None:
        raise HTTPException(status_code=404, detail=f"OT '{id}' no encontrada.")
    tecnico = next((t for t in DB_TECNICOS if t["id"] == body.tecnico_id), None)
    if tecnico is None:
        raise HTTPException(status_code=404, detail=f"Tecnico '{body.tecnico_id}' no encontrado.")
    orden["tecnico_id"] = body.tecnico_id
    if orden["estado"] in ("por_revisar", "por_asignar"):
        orden["estado"] = "asignacion_por_confirmar"
    return orden

@app.patch(
    "/api/ordenes/asignaciones-masivas",
    tags=["Ordenes de Trabajo"],
    summary="Asignacion masiva y enriquecida de ordenes (Bulk Update)",
)
def asignaciones_masivas(body: AsignacionesMasivasRequest):
    fecha = body.fecha or date.today().isoformat()
    actualizadas = 0
    errores = []

    if fecha not in DB_RUTAS_PLANIFICADAS:
        DB_RUTAS_PLANIFICADAS[fecha] = {}

    for asig in body.asignaciones:
        orden = next((o for o in DB_ORDENES if o["id"] == asig.ot_id), None)
        if not orden:
            errores.append(f"OT '{asig.ot_id}' no encontrada.")
            continue

        tecnico = next((t for t in DB_TECNICOS if t["id"] == asig.tecnico_id), None)
        if not tecnico:
            errores.append(f"Tecnico '{asig.tecnico_id}' no encontrado.")
            continue

        orden["tecnico_id"] = asig.tecnico_id
        orden["secuencia"] = asig.secuencia
        orden["hora_estimada_llegada"] = asig.hora_estimada_llegada
        orden["hora_estimada_salida"] = asig.hora_estimada_salida
        if orden["estado"] in ("por_revisar", "por_asignar"):
            orden["estado"] = "asignacion_por_confirmar"

        tec_id = asig.tecnico_id
        if tec_id not in DB_RUTAS_PLANIFICADAS[fecha]:
            base_coords = ZONAS_COORDENADAS.get(tecnico["zona"], (-33.4372, -70.6572))
            DB_RUTAS_PLANIFICADAS[fecha][tec_id] = {
                "tecnico_id": tec_id,
                "nombre": f"{tecnico['nombre']} {tecnico['apellidos']}",
                "tipo": tecnico["tipo"],
                "zona_base": tecnico["zona"],
                "base_latitud": base_coords[0],
                "base_longitud": base_coords[1],
                "fecha": fecha,
                "paradas": []
            }

        DB_RUTAS_PLANIFICADAS[fecha][tec_id]["paradas"].append({
            "secuencia": asig.secuencia,
            "ot_id": orden["id"],
            "tipo": orden["tipo"],
            "cliente": orden.get("cliente", "Cliente Particular"),
            "direccion": orden["direccion_instalacion"],
            "latitud": orden.get("latitud"),
            "longitud": orden.get("longitud"),
            "hora_estimada_llegada": asig.hora_estimada_llegada,
            "hora_estimada_salida": asig.hora_estimada_salida,
            "duracion_servicio_min": asig.duracion_servicio_min,
            "sector": asig.sector,
        })
        actualizadas += 1

    for tec_id, ruta in DB_RUTAS_PLANIFICADAS[fecha].items():
        ruta["paradas"].sort(key=lambda p: p.get("secuencia") or 0)
        ruta["total_ots"] = len(ruta["paradas"])

    return {
        "status": "success",
        "mensaje": f"{actualizadas} OTs asignadas y planificadas en lote.",
        "fecha": fecha,
        "total_actualizadas": actualizadas,
        "errores": errores,
    }

# --- Rutas y Despacho ---

@app.get("/api/rutas", tags=["Rutas y Despacho"], summary="Consultar hojas de ruta")
def get_rutas(
    fecha: Optional[str] = Query(default=None),
    tecnico_id: Optional[str] = Query(default=None)
):
    target_fecha = fecha or date.today().isoformat()
    rutas_dia = DB_RUTAS_PLANIFICADAS.get(target_fecha, {})

    if tecnico_id:
        if tecnico_id in rutas_dia:
            return [rutas_dia[tecnico_id]]
        return []

    return list(rutas_dia.values())

@app.get("/api/tecnicos/{id}/ruta", tags=["Rutas y Despacho"], summary="Ruta individual")
def get_ruta_tecnico(id: str, fecha: Optional[str] = Query(default=None)):
    target_fecha = fecha or date.today().isoformat()
    rutas_dia = DB_RUTAS_PLANIFICADAS.get(target_fecha, {})
    
    if id not in rutas_dia:
        tecnico = next((t for t in DB_TECNICOS if t["id"] == id), None)
        if not tecnico:
            raise HTTPException(status_code=404, detail=f"Tecnico '{id}' no encontrado.")
        return {
            "tecnico_id": id,
            "nombre": f"{tecnico['nombre']} {tecnico['apellidos']}",
            "fecha": target_fecha,
            "total_ots": 0,
            "paradas": []
        }

    return rutas_dia[id]

@app.get("/api/ruteo/geometria", tags=["Rutas y Despacho"], summary="Obtener trazado detallado de calles vía OSRM")
def get_geometria_ruta(coordenadas: str = Query(..., description="Coordenadas lon,lat separadas por punto y coma ';'")):
    """
    Consulta a OSRM para obtener la geometría exacta siguiendo las calles (turn-by-turn road geometry).
    Entrada: lon1,lat1;lon2,lat2;...
    Retorna arreglo de puntos [lat, lon] listos para Leaflet.
    """
    osrm_url = f"https://router.project-osrm.org/route/v1/driving/{coordenadas}?overview=full&geometries=geojson"
    try:
        res = requests.get(osrm_url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == "Ok" and data.get("routes"):
                coords = data["routes"][0]["geometry"]["coordinates"]
                # OSRM entrega [lon, lat], convertimos a [lat, lon] para Leaflet
                latlngs = [[c[1], c[0]] for c in coords]
                return {
                    "status": "success",
                    "distancia_metros": data["routes"][0].get("distance"),
                    "duracion_segundos": data["routes"][0].get("duration"),
                    "coordenadas": latlngs
                }
    except Exception as e:
        print(f"   [WARNING] Error consultando OSRM routing: {e}")

    # Fallback determinista si OSRM no responde
    try:
        pts = []
        for pair in coordenadas.split(";"):
            if pair.strip():
                lon, lat = pair.strip().split(",")
                pts.append([float(lat), float(lon)])
        return {
            "status": "fallback",
            "coordenadas": pts
        }
    except Exception:
        raise HTTPException(status_code=400, detail="Formato de coordenadas inválido.")

# --- Ejecucion del Optimizador On-Demand ---

@app.post("/api/optimizador/ejecutar", tags=["Optimizador"], summary="Disparar optimizacion")
def ejecutar_optimizador_endpoint(body: EjecutarOptimizacionRequest):
    try:
        from optimizador import optimizar_jornada
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Error importando optimizador: {e}")

    target_fecha = body.fecha or date.today().isoformat()
    
    # Obtener técnicos disponibles y órdenes pendientes directamente en memoria
    disp_hoy = [d for d in DB_DISPONIBILIDADES if d["fecha"] == target_fecha and d.get("disponible")]
    ids_disponibles = {d["tecnico_id"] for d in disp_hoy}
    tecnicos_hoy = [t for t in DB_TECNICOS if t["id"] in ids_disponibles]
    ordenes_pendientes = [o for o in DB_ORDENES if o.get("estado") in ("por_asignar", "por_revisar")]

    if not tecnicos_hoy or not ordenes_pendientes:
        return {
            "status": "no_data",
            "mensaje": f"Faltan técnicos disponibles ({len(tecnicos_hoy)}) u órdenes por asignar ({len(ordenes_pendientes)}) para {target_fecha}.",
            "kpis": {"total_ots": len(ordenes_pendientes), "asignadas": 0, "pendientes": len(ordenes_pendientes)},
            "rutas": []
        }

    # Ejecución sin depender de llamadas HTTP a localhost (vital para Render/producción)
    resultado = optimizar_jornada(
        fecha=target_fecha,
        aplicar_cambios=False,
        tiempo_limite_segundos=body.tiempo_limite_segundos,
        tecnicos=tecnicos_hoy,
        ordenes=ordenes_pendientes
    )

    if body.aplicar_cambios and resultado.get("status") == "success":
        if target_fecha not in DB_RUTAS_PLANIFICADAS:
            DB_RUTAS_PLANIFICADAS[target_fecha] = {}

        for ruta in resultado.get("rutas", []):
            tec_id = ruta["tecnico_id"]
            DB_RUTAS_PLANIFICADAS[target_fecha][tec_id] = {
                "tecnico_id": tec_id,
                "nombre": ruta["nombre"],
                "tipo": ruta["tipo"],
                "zona_base": ruta["zona_base"],
                "base_latitud": ruta.get("base_latitud"),
                "base_longitud": ruta.get("base_longitud"),
                "capacidad_uso": ruta.get("capacidad_uso"),
                "hora_salida_base": ruta.get("hora_salida_base"),
                "hora_retorno_base": ruta.get("hora_retorno_base"),
                "duracion_total_min": ruta.get("duracion_total_min"),
                "fecha": target_fecha,
                "paradas": []
            }
            for p in ruta.get("paradas", []):
                DB_RUTAS_PLANIFICADAS[target_fecha][tec_id]["paradas"].append(p)
                orden = next((o for o in DB_ORDENES if o["id"] == p["ot_id"]), None)
                if orden:
                    orden["tecnico_id"] = tec_id
                    orden["secuencia"] = p["secuencia"]
                    orden["hora_estimada_llegada"] = p["hora_estimada_llegada"]
                    orden["hora_estimada_salida"] = p["hora_estimada_salida"]
                    orden["estado"] = "asignacion_por_confirmar"

    return resultado

@app.get("/api/optimizador/configuracion", tags=["Optimizador"], summary="Consultar parametros del optimizador")
def get_configuracion_optimizador():
    """Retorna la configuracion actual de tiempos de servicio, capacidades, jornada y penalizaciones."""
    try:
        from optimizador import obtener_configuracion
        return obtener_configuracion()
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Error cargando configuracion: {e}")

@app.put("/api/optimizador/configuracion", tags=["Optimizador"], summary="Modificar parametros del optimizador")
def update_configuracion_optimizador(body: ConfiguracionVRPRequest):
    """Actualiza dinamicamente los parametros de optimizacion VRP en tiempo de ejecucion."""
    try:
        from optimizador import actualizar_configuracion
        datos = body.dict(exclude_unset=True)
        cfg_actualizada = actualizar_configuracion(datos)
        return {
            "status": "success",
            "mensaje": "Parametros del optimizador actualizados con exito.",
            "configuracion": cfg_actualizada
        }
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Error actualizando configuracion: {e}")

@app.post("/api/optimizador/configuracion/restaurar", tags=["Optimizador"], summary="Restaurar parametros por defecto")
def reset_configuracion_optimizador():
    """Restaura los parametros de optimizacion a sus valores por defecto iniciales."""
    try:
        from optimizador import restaurar_configuracion
        cfg_default = restaurar_configuracion()
        return {
            "status": "success",
            "mensaje": "Parametros restaurados a valores por defecto.",
            "configuracion": cfg_default
        }
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Error restaurando configuracion: {e}")

# --- Metricas y KPIs ---

@app.get("/api/metricas/resumen-diario", tags=["Metricas y KPIs"], summary="KPIs diarios")
def get_metricas_resumen(fecha: Optional[str] = Query(default=None)):
    target_fecha = fecha or date.today().isoformat()
    
    disp_hoy = [d for d in DB_DISPONIBILIDADES if d["fecha"] == target_fecha and d["disponible"]]
    ids_disponibles = {d["tecnico_id"] for d in disp_hoy}
    tecnicos_disponibles = [t for t in DB_TECNICOS if t["id"] in ids_disponibles]
    
    total_ots = len(DB_ORDENES)
    ots_asignadas = sum(1 for o in DB_ORDENES if o.get("tecnico_id") is not None)
    ots_pendientes = total_ots - ots_asignadas
    
    rutas_dia = DB_RUTAS_PLANIFICADAS.get(target_fecha, {})
    tecnicos_con_ruta = len([r for r in rutas_dia.values() if r.get("total_ots", 0) > 0])
    
    pct_asignacion = (ots_asignadas / total_ots * 100.0) if total_ots > 0 else 0.0
    pct_utilizacion_flota = (tecnicos_con_ruta / len(tecnicos_disponibles) * 100.0) if tecnicos_disponibles else 0.0

    return {
        "fecha": target_fecha,
        "kpis_ordenes": {
            "total_ots": total_ots,
            "asignadas": ots_asignadas,
            "pendientes": ots_pendientes,
            "tasa_asignacion_pct": round(pct_asignacion, 1)
        },
        "kpis_flota": {
            "total_tecnicos": len(DB_TECNICOS),
            "disponibles_hoy": len(tecnicos_disponibles),
            "tecnicos_activos_con_ruta": tecnicos_con_ruta,
            "tasa_utilizacion_flota_pct": round(pct_utilizacion_flota, 1)
        }
    }

# Montar archivos estaticos si existen
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Iniciando servidor en 0.0.0.0:{port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
