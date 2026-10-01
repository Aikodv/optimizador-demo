# =============================================================================
# API REST & Dashboard Server - Optimizacion de Rutas y Despacho VRP
# =============================================================================
#
# DESCRIPCION:
#   Servidor FastAPI que alimenta el sistema de optimizacion de rutas en Chile.
#   Los datos (tecnicos, OTs, disponibilidad) se leen de la API externa
#   (API_BASE_URL); si no responde, se usan datos simulados locales.
#
# EJECUCION:
#   python -m uvicorn main:app --reload --port 8000
#
#   - Dashboard Web:  http://127.0.0.1:8000/
#   - Swagger UI:     http://127.0.0.1:8000/docs
# =============================================================================

import os
import random
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional

import requests
from faker import Faker
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

import geocoding
import optimizador

# =============================================================================
# CONFIGURACION INICIAL
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

EXTERNAL_API_BASE = os.environ.get("API_BASE_URL", "https://api-dummy-yurf.onrender.com/api").rstrip("/")
VERSION = "3.0.0"

fake = Faker("es_CL")
random.seed(42)
Faker.seed(42)

app = FastAPI(
    title="Backoffice & Dispatch API - Optimizacion de Rutas",
    description=(
        "API REST y Dashboard Web para gestion de tecnicos, ordenes de trabajo, "
        "hojas de ruta y optimizacion con Google OR-Tools."
    ),
    version=VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# DATOS SIMULADOS LOCALES (respaldo cuando la API externa no responde)
# =============================================================================

TIPOS_TECNICO = ["interno", "externo"]
TIPOS_OT = ["instalacion_simple", "instalacion_con_corte", "mantencion", "retiro"]

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
ZONAS_SANTIAGO = list(ZONAS_COORDENADAS)

CALLES_SANTIAGO = [
    "Avenida Libertador Bernardo O Higgins", "Avenida Providencia", "Avenida Apoquindo",
    "Avenida Vitacura", "Avenida Las Condes", "Calle Huerfanos", "Calle Agustinas",
    "Avenida Irarrazaval", "Avenida Grecia", "Calle Merced", "Paseo Ahumada",
    "Avenida Americo Vespucio", "Avenida Tobalaba", "Avenida Vicuna Mackenna",
    "Avenida Recoleta", "Calle San Antonio", "Avenida Matta", "Calle Condell",
    "Avenida El Golf", "Calle Estado", "Avenida Pajaritos", "Gran Avenida Jose Miguel Carrera",
    "Avenida Concha y Toro", "Avenida Macul", "Avenida Los Leones",
]


def generar_tecnicos(n: int) -> list:
    tecnicos = []
    for _ in range(n):
        tipo = random.choice(TIPOS_TECNICO)
        tecnicos.append({
            "id": str(uuid.uuid4()),
            "nombre": fake.first_name(),
            "apellidos": f"{fake.last_name()} {fake.last_name()}",
            "tipo": tipo,
            "zona": random.choice(ZONAS_SANTIAGO),
            "cap_max": 8 if tipo == "externo" else 12,
        })
    return tecnicos


def generar_disponibilidades(tecnicos: list, dias: int) -> list:
    hoy = date.today()
    disponibilidades = []
    for tecnico in tecnicos:
        for offset in range(dias):
            fecha = hoy + timedelta(days=offset)
            prob_disponible = 0.4 if fecha.weekday() >= 5 else 0.95
            disponibilidades.append({
                "id": str(uuid.uuid4()),
                "tecnico_id": tecnico["id"],
                "fecha": fecha.isoformat(),
                "disponible": random.random() < prob_disponible,
            })
    return disponibilidades


def generar_ordenes_trabajo(n: int) -> list:
    ordenes = []
    for i in range(1, n + 1):
        zona = random.choice(ZONAS_SANTIAGO)
        interior = ""
        if random.random() > 0.5:
            interior = f", {random.choice(['Depto', 'Of', 'Piso'])}. {random.randint(1, 20)}"
        base_lat, base_lon = ZONAS_COORDENADAS[zona]
        hora_prog = None
        if random.random() > 0.4:  # 60% con hora acordada
            hora_prog = f"{random.randint(9, 16):02d}:{random.choice([0, 30]):02d}"

        ordenes.append({
            "id": f"OT-{i:04d}",
            "tipo": random.choice(TIPOS_OT),
            "estado": "por_asignar",
            "tecnico_id": None,
            "cliente": f"{fake.first_name()} {fake.last_name()}",
            "direccion_instalacion": f"{random.choice(CALLES_SANTIAGO)} #{random.randint(100, 9999)}{interior}, {zona}, Santiago, Chile",
            "comuna": zona,
            "latitud": base_lat + random.uniform(-0.008, 0.008),
            "longitud": base_lon + random.uniform(-0.008, 0.008),
            "fecha_programada": date.today().isoformat(),
            "hora_programada": hora_prog,
        })
    return ordenes


def generar_datos_locales(num_tecnicos: int = 4, num_ordenes: int = 15, dias: int = 14) -> None:
    global DB_TECNICOS, DB_DISPONIBILIDADES, DB_ORDENES
    DB_TECNICOS = generar_tecnicos(num_tecnicos)
    DB_DISPONIBILIDADES = generar_disponibilidades(DB_TECNICOS, dias)
    DB_ORDENES = generar_ordenes_trabajo(num_ordenes)


DB_TECNICOS: list = []
DB_DISPONIBILIDADES: list = []
DB_ORDENES: list = []
generar_datos_locales()

# Hojas de ruta planificadas: {fecha | "default": {tecnico_id: ruta}}. "default" = ultima optimizacion.
DB_RUTAS_PLANIFICADAS: Dict[str, Dict[str, Any]] = {}
# OTs que el optimizador no pudo asignar, con sus razones: {fecha | "default": [diagnostico]}.
DB_PENDIENTES: Dict[str, List[Dict[str, Any]]] = {}

# =============================================================================
# ACCESO A DATOS (API externa con respaldo local)
# =============================================================================

def _consultar_api_externa(path: str, params: Optional[Dict[str, str]] = None) -> Optional[Any]:
    """GET a la API externa. None si falla o responde distinto de 200."""
    try:
        res = requests.get(f"{EXTERNAL_API_BASE}{path}", params=params, timeout=25)
        if res.status_code == 200:
            return res.json()
        print(f"API externa {path} respondio {res.status_code}; usando datos locales.")
    except (requests.RequestException, ValueError) as e:
        print(f"Error consultando {path} en API externa: {e}")
    return None


def obtener_tecnicos_fuente() -> list:
    datos = _consultar_api_externa("/tecnicos")
    return datos if datos is not None else DB_TECNICOS


def obtener_ordenes_fuente(estado: Optional[str] = None) -> list:
    datos = _consultar_api_externa("/ordenes", {"estado": estado} if estado else None)
    if datos is not None:
        return datos
    return [o for o in DB_ORDENES if estado is None or o.get("estado") == estado]


def obtener_disponibilidad_fuente(fecha: Optional[str] = None) -> list:
    datos = _consultar_api_externa("/disponibilidad", {"fecha": fecha} if fecha else None)
    if datos is not None:
        return datos
    return [d for d in DB_DISPONIBILIDADES if fecha is None or d.get("fecha") == fecha]

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
    fecha: str = Field(default_factory=lambda: date.today().isoformat())
    asignaciones: List[AsignacionItem]


class TecnicoInput(BaseModel):
    """Tecnico enviado directamente en el request. Se asume disponible para la jornada."""
    model_config = ConfigDict(extra="allow")

    id: str
    nombre: str
    apellidos: Optional[str] = ""
    tipo: Literal["interno", "externo"] = "interno"
    zona: str = Field(..., description="Comuna base del tecnico (ej: 'Providencia')")
    cap_max: Optional[int] = Field(default=None, ge=1, le=30)
    base_latitud: Optional[float] = Field(default=None, description="Opcional: coordenada exacta de la base")
    base_longitud: Optional[float] = None


class OrdenInput(BaseModel):
    """Orden de trabajo enviada directamente en el request."""
    model_config = ConfigDict(extra="allow")

    id: str
    tipo: str = Field(..., description="instalacion_simple | instalacion_con_corte | mantencion | retiro")
    direccion_instalacion: str
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    comuna: Optional[str] = None
    region: Optional[str] = None
    cliente: Optional[str] = None
    fecha_programada: Optional[str] = None
    hora_programada: Optional[str] = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    estado: str = "por_asignar"


class EjecutarOptimizacionRequest(BaseModel):
    fecha: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    aplicar_cambios: Optional[bool] = Field(
        default=None, description="Por defecto usa aplicar_cambios_por_defecto de la configuracion.")
    tiempo_limite_segundos: Optional[int] = Field(
        default=None, ge=1, description="Por defecto se calcula segun la cantidad de OTs (parametros solver_* de la "
                                        "configuracion). Maximo: solver_time_limit_max_seconds.")
    tecnicos: Optional[List[TecnicoInput]] = Field(
        default=None, description="Si se envian, se usan en vez de consultar la API externa (se asumen disponibles).")
    ordenes: Optional[List[OrdenInput]] = Field(
        default=None, description="Si se envian, se usan en vez de consultar las OTs por_asignar de la API externa.")

    @model_validator(mode="after")
    def validar_ids_unicos(self):
        for nombre, items in (("tecnicos", self.tecnicos), ("ordenes", self.ordenes)):
            if items:
                ids = [i.id for i in items]
                duplicados = sorted({i for i in ids if ids.count(i) > 1})
                if duplicados:
                    raise ValueError(f"IDs duplicados en '{nombre}': {duplicados}")
        return self


class RegenerarDatosRequest(BaseModel):
    num_tecnicos: int = Field(default=4, ge=1, le=10)
    num_ordenes: int = Field(default=15, ge=1, le=60)
    dias_disponibilidad: int = Field(default=14, ge=1, le=30)


def _campo_configuracion(p: Dict[str, Any]) -> tuple:
    """Campo opcional del request de configuracion, con el tipo y los limites del catalogo de parametros."""
    limites = {"ge": p["minimo"], "le": p["maximo"]}
    if p["tipo"] == "dict_int":
        return Optional[Dict[str, Annotated[int, Field(**limites)]]], Field(default=None, description=p["descripcion"])
    if p["tipo"] == "bool":
        return Optional[bool], Field(default=None, description=p["descripcion"])
    tipo = int if p["tipo"] == "int" else float
    return Optional[tipo], Field(default=None, description=p["descripcion"], **limites)


# Se genera desde optimizador.PARAMETROS: una sola fuente para valores por defecto, limites y documentacion
ConfiguracionVRPRequest = create_model(
    "ConfiguracionVRPRequest", **{p["clave"]: _campo_configuracion(p) for p in optimizador.PARAMETROS})

# =============================================================================
# DASHBOARD
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
    return {"mensaje": "API Backoffice & Despacho - Optimizador de Rutas", "version": VERSION,
            "docs": "/docs", "dashboard": "/"}

# =============================================================================
# SIMULACION
# =============================================================================

@app.post("/api/simulacion/regenerar", tags=["Simulacion"], summary="Regenerar datos (API externa y respaldo local)")
def regenerar_datos(body: Optional[RegenerarDatosRequest] = None):
    """
    Resetea la API externa (POST /reset), regenera los datos simulados locales con los
    parametros indicados y limpia las hojas de ruta planificadas.
    """
    body = body or RegenerarDatosRequest()
    DB_RUTAS_PLANIFICADAS.clear()
    DB_PENDIENTES.clear()
    generar_datos_locales(body.num_tecnicos, body.num_ordenes, body.dias_disponibilidad)

    externa = None
    try:
        res = requests.post(f"{EXTERNAL_API_BASE}/reset", timeout=25)
        externa = res.json() if res.status_code == 200 else {"error": f"HTTP {res.status_code}"}
    except (requests.RequestException, ValueError) as e:
        externa = {"error": str(e)}
    return {"status": "success", "mensaje": "Datos regenerados.", "api_externa": externa}

# =============================================================================
# TECNICOS, DISPONIBILIDAD Y ORDENES
# =============================================================================

@app.get("/api/tecnicos", tags=["Tecnicos"], summary="Obtener lista de tecnicos")
def get_tecnicos():
    return obtener_tecnicos_fuente()


@app.get("/api/disponibilidad", tags=["Disponibilidad"], summary="Obtener disponibilidades")
def get_disponibilidad(fecha: Optional[str] = None):
    return obtener_disponibilidad_fuente(fecha)


@app.get("/api/ordenes", tags=["Ordenes de Trabajo"], summary="Obtener lista de ordenes")
def get_ordenes(estado: Optional[str] = None):
    return obtener_ordenes_fuente(estado)


@app.patch("/api/ordenes/asignaciones-masivas", tags=["Ordenes de Trabajo"],
           summary="Registrar asignaciones manuales en lote en la hoja de ruta")
def asignaciones_masivas(body: AsignacionesMasivasRequest):
    """
    Registra asignaciones en las hojas de ruta planificadas de la fecha. Si una OT ya estaba
    planificada (en cualquier tecnico) se reemplaza, no se duplica.
    """
    fecha = body.fecha
    rutas_dia = DB_RUTAS_PLANIFICADAS.setdefault(fecha, {})
    ordenes_por_id = {o["id"]: o for o in obtener_ordenes_fuente()}
    tecnicos_por_id = {t["id"]: t for t in obtener_tecnicos_fuente()}
    actualizadas, errores = 0, []

    for asig in body.asignaciones:
        orden = ordenes_por_id.get(asig.ot_id)
        if not orden:
            errores.append(f"OT '{asig.ot_id}' no encontrada.")
            continue
        tecnico = tecnicos_por_id.get(asig.tecnico_id)
        if not tecnico:
            errores.append(f"Tecnico '{asig.tecnico_id}' no encontrado.")
            continue

        for ruta in rutas_dia.values():
            ruta["paradas"] = [p for p in ruta["paradas"] if p["ot_id"] != asig.ot_id]

        if asig.tecnico_id not in rutas_dia:
            (base_lat, base_lon), _ = optimizador.resolver_base_tecnico(tecnico)
            rutas_dia[asig.tecnico_id] = {
                "tecnico_id": asig.tecnico_id,
                "nombre": f"{tecnico.get('nombre', '')} {tecnico.get('apellidos') or ''}".strip(),
                "tipo": tecnico.get("tipo"),
                "zona_base": tecnico.get("zona"),
                "base_latitud": base_lat,
                "base_longitud": base_lon,
                "fecha": fecha,
                "paradas": [],
            }

        rutas_dia[asig.tecnico_id]["paradas"].append({
            "secuencia": asig.secuencia,
            "ot_id": orden["id"],
            "tipo": orden.get("tipo"),
            "cliente": orden.get("cliente"),
            "direccion": orden.get("direccion_instalacion"),
            "latitud": orden.get("latitud"),
            "longitud": orden.get("longitud"),
            "hora_estimada_llegada": asig.hora_estimada_llegada,
            "hora_estimada_salida": asig.hora_estimada_salida,
            "duracion_servicio_min": asig.duracion_servicio_min,
            "sector": asig.sector,
        })
        actualizadas += 1

    for ruta in rutas_dia.values():
        ruta["paradas"].sort(key=lambda p: p.get("secuencia") or 0)
        ruta["total_ots"] = len(ruta["paradas"])

    # Las OTs asignadas manualmente dejan de estar pendientes
    pendientes = DB_PENDIENTES.get(fecha)
    if pendientes:
        asignadas = {a.ot_id for a in body.asignaciones}
        pendientes[:] = [d for d in pendientes if d["ot_id"] not in asignadas]

    return {
        "status": "success",
        "mensaje": f"{actualizadas} OTs asignadas y planificadas en lote.",
        "fecha": fecha,
        "total_actualizadas": actualizadas,
        "errores": errores,
    }


@app.patch("/api/ordenes/{id}/tecnico", tags=["Ordenes de Trabajo"], summary="Asignacion individual")
def asignar_tecnico(id: str, body: AsignarTecnicoRequest):
    try:
        res = requests.patch(f"{EXTERNAL_API_BASE}/ordenes/{id}/tecnico",
                             json={"tecnico_id": body.tecnico_id}, timeout=15)
        if res.status_code == 200:
            return res.json()
        if res.status_code == 404:
            raise HTTPException(status_code=404, detail=f"OT '{id}' no encontrada en la API externa.")
    except requests.RequestException as e:
        print(f"Error asignando tecnico en API externa: {e}")

    orden = next((o for o in DB_ORDENES if o["id"] == id), None)
    if not orden:
        raise HTTPException(status_code=404, detail=f"OT '{id}' no encontrada.")
    orden["tecnico_id"] = body.tecnico_id
    return orden

# =============================================================================
# RUTAS Y DESPACHO
# =============================================================================

@app.get("/api/rutas", tags=["Rutas y Despacho"], summary="Consultar hojas de ruta")
def get_rutas(fecha: Optional[str] = None, tecnico_id: Optional[str] = None):
    """Con `fecha`: solo las rutas de ese dia. Sin fecha: las de la ultima optimizacion."""
    rutas_dia = DB_RUTAS_PLANIFICADAS.get(fecha or "default", {})
    if tecnico_id:
        return [rutas_dia[tecnico_id]] if tecnico_id in rutas_dia else []
    return list(rutas_dia.values())


@app.get("/api/tecnicos/{id}/ruta", tags=["Rutas y Despacho"], summary="Ruta individual")
def get_ruta_tecnico(id: str, fecha: Optional[str] = Query(default=None)):
    rutas_dia = DB_RUTAS_PLANIFICADAS.get(fecha or "default", {})
    if id in rutas_dia:
        return rutas_dia[id]

    tecnico = next((t for t in obtener_tecnicos_fuente() if t["id"] == id), None)
    if not tecnico:
        raise HTTPException(status_code=404, detail=f"Tecnico '{id}' no encontrado.")
    return {
        "tecnico_id": id,
        "nombre": f"{tecnico.get('nombre', '')} {tecnico.get('apellidos') or ''}".strip(),
        "fecha": fecha,
        "total_ots": 0,
        "paradas": [],
    }


@app.get("/api/geocoding", tags=["Rutas y Despacho"], summary="Probar la geocodificacion de una direccion")
def probar_geocoding(direccion: str = Query(..., min_length=3), comuna: Optional[str] = None):
    """Muestra como se interpreta y resuelve una direccion (usa y actualiza el cache de geocodificacion)."""
    ot = {"direccion_instalacion": direccion, "comuna": comuna}
    comuna_norm = geocoding.resolver_comuna_ot(ot)
    calle, numero = geocoding.separar_calle_numero(direccion)
    cache = geocoding.cargar_cache()
    resultado = geocoding.geocodificar_direccion(direccion, comuna_norm, cache=cache)
    geocoding.guardar_cache(cache)
    if not resultado and comuna_norm:
        lat, lon, _ = geocoding.cargar_coordenadas_comunas()[comuna_norm]
        resultado = {"lat": lat, "lon": lon, "precision": "comuna", "fuente": "centroide"}
    return {
        "interpretacion": {"calle": calle, "numero": numero, "comuna": comuna_norm},
        "resultado": resultado or {"precision": "aproximada", "lat": geocoding.COORD_DEFAULT[0],
                                   "lon": geocoding.COORD_DEFAULT[1], "fuente": "default"},
    }


PATRON_COORDENADAS = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?(;-?\d+(\.\d+)?,-?\d+(\.\d+)?)+$")


@app.get("/api/ruteo/geometria", tags=["Rutas y Despacho"], summary="Trazado de calles via OSRM")
def get_geometria_ruta(coordenadas: str = Query(..., description="Pares lon,lat separados por ';' (minimo 2)")):
    """Geometria siguiendo calles (OSRM). Retorna puntos [lat, lon] listos para Leaflet."""
    coordenadas = coordenadas.replace(" ", "")
    if not PATRON_COORDENADAS.match(coordenadas):
        raise HTTPException(status_code=400, detail="Formato invalido. Use lon,lat;lon,lat;...")

    try:
        res = requests.get(f"https://router.project-osrm.org/route/v1/driving/{coordenadas}",
                           params={"overview": "full", "geometries": "geojson"}, timeout=10)
        data = res.json() if res.status_code == 200 else {}
        if data.get("code") == "Ok" and data.get("routes"):
            ruta = data["routes"][0]
            return {
                "status": "success",
                "distancia_metros": ruta.get("distance"),
                "duracion_segundos": ruta.get("duration"),
                "coordenadas": [[c[1], c[0]] for c in ruta["geometry"]["coordinates"]],
            }
    except (requests.RequestException, ValueError) as e:
        print(f"   [WARNING] Error consultando OSRM routing: {e}")

    # Fallback: linea recta entre los puntos
    pts = [[float(lat), float(lon)] for lon, lat in (p.split(",") for p in coordenadas.split(";"))]
    return {"status": "fallback", "coordenadas": pts}

# =============================================================================
# OPTIMIZADOR
# =============================================================================

@app.post("/api/optimizador/ejecutar", tags=["Optimizador"], summary="Ejecutar optimizacion")
def ejecutar_optimizador_endpoint(body: EjecutarOptimizacionRequest):
    """
    Ejecuta el optimizador VRP.

    - Sin `tecnicos`/`ordenes`: se obtienen de la API externa.
    - Con `tecnicos` y/o `ordenes`: se usan los del request; lo que falte se completa desde la API.
    - `aplicar_cambios=true`: asigna los tecnicos en la API externa (OTs inexistentes alla se omiten).
    - `aplicar_cambios` y `tiempo_limite_segundos` omitidos: se toman de la configuracion.

    Cada OT sin asignar viene en `diagnosticos` con su causa clasificada (`causa_principal` y `causas`).
    """
    maximo = optimizador.obtener_configuracion()["solver_time_limit_max_seconds"]
    if body.tiempo_limite_segundos and body.tiempo_limite_segundos > maximo:
        raise HTTPException(status_code=422, detail=f"tiempo_limite_segundos no puede superar {maximo} "
                                                    f"(parametro solver_time_limit_max_seconds).")
    tecnicos = [t.model_dump() for t in body.tecnicos] if body.tecnicos is not None else None
    ordenes = [o.model_dump() for o in body.ordenes] if body.ordenes is not None else None
    parametros = dict(fecha=body.fecha, tiempo_limite_segundos=body.tiempo_limite_segundos, api_base_url=EXTERNAL_API_BASE)

    resultado = optimizador.optimizar_jornada(aplicar_cambios=body.aplicar_cambios, tecnicos=tecnicos,
                                              ordenes=ordenes, **parametros)

    if resultado.get("status") == "error_api":
        # Mismo respaldo que el dashboard: optimizar los datos simulados locales
        fecha_local = body.fecha or date.today().isoformat()
        if ordenes is None:
            ordenes = [o for o in DB_ORDENES if o.get("estado") == "por_asignar"
                       and (not body.fecha or o.get("fecha_programada") == body.fecha)]
        if tecnicos is None:
            ids = {d["tecnico_id"] for d in DB_DISPONIBILIDADES if d["fecha"] == fecha_local and d["disponible"]}
            tecnicos = [t for t in DB_TECNICOS if t["id"] in ids]
        resultado = optimizador.optimizar_jornada(aplicar_cambios=False, tecnicos=tecnicos, ordenes=ordenes, **parametros)
        resultado.setdefault("alertas", []).insert(
            0, "API externa no disponible: se optimizaron los datos simulados locales (cambios no aplicados).")

    if resultado.get("status") == "success":
        rutas = {r["tecnico_id"]: r for r in resultado["rutas"]}
        DB_RUTAS_PLANIFICADAS[resultado["fecha"]] = rutas
        DB_RUTAS_PLANIFICADAS["default"] = rutas
        pendientes = list(resultado["diagnosticos"])
        DB_PENDIENTES[resultado["fecha"]] = pendientes
        DB_PENDIENTES["default"] = pendientes
    return resultado


@app.get("/api/optimizador/pendientes", tags=["Optimizador"], summary="OTs no asignadas y sus razones")
def get_pendientes(fecha: Optional[str] = None):
    """OTs que la optimizacion no pudo asignar, con su causa clasificada. Sin fecha: las de la ultima optimizacion."""
    return DB_PENDIENTES.get(fecha or "default", [])


@app.get("/api/optimizador/modelo", tags=["Optimizador"], summary="Declaracion del modelo de optimizacion")
def get_modelo_optimizador():
    """Variables, funcion objetivo, restricciones (implementadas y fuera de esta version) y causas de no asignacion."""
    return optimizador.describir_modelo()


@app.get("/api/optimizador/configuracion", tags=["Optimizador"], summary="Consultar parametros del optimizador")
def get_configuracion_optimizador():
    return optimizador.obtener_configuracion()


@app.get("/api/optimizador/configuracion/parametros", tags=["Optimizador"],
         summary="Catalogo documentado de parametros (para construir el panel de configuracion)")
def get_catalogo_parametros():
    """
    Cada parametro con etiqueta, descripcion, tipo, unidad, limites, valor por defecto y actual, y su
    clasificacion: `ambito` (negocio | solver | servicio) y `origen` (operacion_cliente | supuesto_equipo).
    Incluye tambien los parametros propios de cada corrida (`parametros_ejecucion`).
    """
    return optimizador.obtener_catalogo_parametros()


@app.put("/api/optimizador/configuracion", tags=["Optimizador"], summary="Modificar parametros del optimizador")
def update_configuracion_optimizador(body: ConfiguracionVRPRequest):
    cfg = optimizador.actualizar_configuracion(body.model_dump(exclude_unset=True))
    return {"status": "success", "mensaje": "Parametros del optimizador actualizados.", "configuracion": cfg}


@app.post("/api/optimizador/configuracion/restaurar", tags=["Optimizador"], summary="Restaurar parametros por defecto")
def reset_configuracion_optimizador():
    cfg = optimizador.restaurar_configuracion()
    return {"status": "success", "mensaje": "Parametros restaurados a valores por defecto.", "configuracion": cfg}

# =============================================================================
# METRICAS
# =============================================================================

@app.get("/api/metricas/resumen-diario", tags=["Metricas y KPIs"], summary="KPIs diarios")
def get_metricas_resumen(fecha: Optional[str] = None):
    ordenes = obtener_ordenes_fuente()
    target_fecha = fecha or optimizador.inferir_fecha(ordenes)
    tecnicos = obtener_tecnicos_fuente()
    disponibles = [d for d in obtener_disponibilidad_fuente(target_fecha) if d.get("disponible")]

    rutas_dia = DB_RUTAS_PLANIFICADAS.get(target_fecha) or DB_RUTAS_PLANIFICADAS.get("default", {})
    ots_en_rutas = {p["ot_id"] for r in rutas_dia.values() for p in r.get("paradas", [])}
    ots_con_tecnico = {o["id"] for o in ordenes if o.get("tecnico_id")}

    total_ots = len(ordenes)
    ots_asignadas = len(ots_con_tecnico | ots_en_rutas)
    tecnicos_con_ruta = sum(1 for r in rutas_dia.values() if r.get("total_ots", 0) > 0)

    return {
        "fecha": target_fecha,
        "kpis_ordenes": {
            "total_ots": total_ots,
            "asignadas": ots_asignadas,
            "pendientes": max(0, total_ots - ots_asignadas),
            "tasa_asignacion_pct": round(ots_asignadas / total_ots * 100, 1) if total_ots else 0.0,
        },
        "kpis_flota": {
            "total_tecnicos": len(tecnicos),
            "disponibles_hoy": len(disponibles),
            "tecnicos_activos_con_ruta": tecnicos_con_ruta,
            "tasa_utilizacion_flota_pct": round(tecnicos_con_ruta / len(disponibles) * 100, 1) if disponibles else 0.0,
        },
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Iniciando servidor en 0.0.0.0:{port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
