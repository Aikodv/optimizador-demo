# =============================================================================
# API REST & Dashboard Server - Optimizacion de Rutas y Despacho VRP
# =============================================================================
#
# DESCRIPCION:
#   Servidor FastAPI que alimenta el sistema de optimizacion de rutas en Chile.
#   Los tecnicos y las OTs se leen del backend del modelo 3.0 (API_BASE_URL). No hay
#   respaldo local: datos inventados tendrian ids que el backend no conoce.
#   El optimizador solo propone rutas: las asignaciones se registran en el backend
#   (POST /api/asignar-tecnicos), no aqui.
#
# EJECUCION:
#   python -m uvicorn main:app --reload --port 8000
#
#   - Dashboard Web:  http://127.0.0.1:8000/
#   - Swagger UI:     http://127.0.0.1:8000/docs
# =============================================================================

import os
import re
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional

import requests
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

EXTERNAL_API_BASE = optimizador.API_BASE_URL
VERSION = "3.1.0"

app = FastAPI(
    title="Backoffice & Dispatch API - Optimizacion de Rutas",
    description=(
        "API REST y Dashboard Web que propone rutas para tecnicos y ordenes de trabajo "
        "con Google OR-Tools. Las asignaciones se registran en el backend."
    ),
    version=VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# OTs que el optimizador no pudo asignar, con sus razones: {fecha | "default": [diagnostico]}.
DB_PENDIENTES: Dict[str, List[Dict[str, Any]]] = {}

# =============================================================================
# ACCESO A DATOS (backend del modelo 3.0, normalizado a snake_case y hora de Chile)
# =============================================================================

def _desde_backend(consulta, *args) -> Any:
    """Ejecuta una lectura del backend; si no responde, HTTP 502 (no se inventan datos)."""
    try:
        return consulta(requests, EXTERNAL_API_BASE, *args)
    except (requests.RequestException, ValueError) as e:
        print(f"Error consultando el backend: {e}")
        raise HTTPException(status_code=502, detail=f"El backend ({EXTERNAL_API_BASE}) no respondio: {e}")


def obtener_tecnicos_fuente() -> list:
    return _desde_backend(optimizador.obtener_tecnicos)


def obtener_ordenes_fuente(estado: Optional[str] = None) -> list:
    return _desde_backend(optimizador.obtener_ordenes, estado)

# =============================================================================
# MODELOS PYDANTIC
# =============================================================================

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
    hora_programada: Optional[str] = Field(default=None, pattern=r"^\d{1,2}:\d{2}$",
                                           description="Hora de Chile (no UTC): formato interno del optimizador.")
    estado: str = "por_asignar"


class EjecutarOptimizacionRequest(BaseModel):
    fecha: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    tiempo_limite_segundos: Optional[int] = Field(
        default=None, ge=1, description="Por defecto se calcula segun la cantidad de OTs (parametros solver_* de la "
                                        "configuracion). Maximo: solver_time_limit_max_seconds.")
    tecnicos: Optional[List[TecnicoInput]] = Field(
        default=None, description="Si se envian, se usan en vez de consultar la API externa (se asumen disponibles).")
    ordenes: Optional[List[OrdenInput]] = Field(
        default=None, description="Si se envian, se usan en vez de consultar las OTs por_asignar de la API externa.")

    @model_validator(mode="before")
    @classmethod
    def rechazar_aplicar_cambios(cls, datos: Any):
        # Un cliente antiguo que pida aplicar no debe creer que se asigno: se rechaza en vez de ignorarlo
        if isinstance(datos, dict) and "aplicar_cambios" in datos:
            raise ValueError("'aplicar_cambios' ya no existe: el optimizador solo propone rutas y la "
                             "asignacion se realiza cuando la planificadora confirma.")
        return datos

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
    semilla: Optional[int] = Field(
        default=None, description="Semilla de las OTs del backend. Sin ella el backend sortea una nueva.")


class _ConfiguracionBase(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def rechazar_tolerancia_simetrica(cls, datos: Any):
        # Un cliente antiguo no debe creer que cambio la tolerancia: se rechaza en vez de ignorarlo
        if isinstance(datos, dict) and "ventana_tolerancia_min" in datos:
            raise ValueError("'ventana_tolerancia_min' ya no existe: use 'tolerancia_antes_min' y "
                             "'tolerancia_despues_min'.")
        return datos


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
    "ConfiguracionVRPRequest", __base__=_ConfiguracionBase,
    **{p["clave"]: _campo_configuracion(p) for p in optimizador.PARAMETROS})

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

@app.post("/api/simulacion/regenerar", tags=["Simulacion"], summary="Regenerar los datos del backend")
def regenerar_datos(body: Optional[RegenerarDatosRequest] = None):
    """Reinicia el backend (GET /api/reset) y limpia los pendientes del optimizador."""
    body = body or RegenerarDatosRequest()
    params = {"semilla": body.semilla} if body.semilla is not None else None
    try:
        res = requests.get(f"{EXTERNAL_API_BASE}/reset", params=params, timeout=optimizador.API_TIMEOUT_S)
        res.raise_for_status()
        externa = res.json()
    except (requests.RequestException, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"No se pudo reiniciar el backend: {e}")
    DB_PENDIENTES.clear()
    return {"status": "success", "mensaje": "Datos del backend regenerados.", "backend": externa}

# =============================================================================
# TECNICOS, DISPONIBILIDAD Y ORDENES
# =============================================================================

@app.get("/api/tecnicos", tags=["Tecnicos"], summary="Obtener lista de tecnicos")
def get_tecnicos():
    """Tecnicos del backend 3.0 en snake_case (incluye `disponibilidad`: boolean[7], lunes a domingo)."""
    return obtener_tecnicos_fuente()


@app.get("/api/disponibilidad", tags=["Disponibilidad"], summary="Disponibilidad de los tecnicos en una fecha")
def get_disponibilidad(fecha: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    """Derivada de `disponibilidad` de cada tecnico segun el dia de la semana. Sin fecha: hoy en Chile."""
    fecha = fecha or optimizador.hoy_chile()
    return [{"tecnico_id": t["id"], "fecha": fecha, "disponible": optimizador.tecnico_disponible(t, fecha)}
            for t in obtener_tecnicos_fuente()]


@app.get("/api/ordenes", tags=["Ordenes de Trabajo"], summary="Obtener lista de ordenes")
def get_ordenes(estado: Optional[str] = None):
    """
    OTs del backend 3.0 en snake_case. `hora_programada` esta en hora de Chile; la original (UTC)
    queda en `hora_programada_utc`. `estado` acepta snake_case o camelCase (por_asignar, porAsignar).
    """
    return obtener_ordenes_fuente(estado)

# =============================================================================
# RUTAS Y DESPACHO
# =============================================================================

@app.get("/api/geocoding", tags=["Rutas y Despacho"], summary="Probar la geocodificacion de una direccion")
def probar_geocoding(direccion: str = Query(..., min_length=3), comuna: Optional[str] = None):
    """Muestra como se interpreta y resuelve una direccion (usa y actualiza el cache de geocodificacion)."""
    ot = {"direccion_instalacion": direccion, "comuna": comuna}
    comuna_norm = geocoding.resolver_comuna_ot(ot)
    calle, numero = geocoding.separar_calle_numero(direccion)
    cache = geocoding.cargar_cache()
    resultado = geocoding.geocodificar_direccion(direccion, comuna_norm, cache=cache)
    geocoding.guardar_cache(cache)
    return {
        "interpretacion": {"calle": calle, "numero": numero, "comuna": comuna_norm},
        "resultado": resultado or {"precision": "sin_ubicacion", "lat": None, "lon": None, "fuente": None},
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
    Ejecuta el optimizador VRP y retorna la propuesta de rutas. No asigna tecnicos ni guarda las
    rutas: al confirmar la propuesta, la planificadora la registra en el backend (POST /api/asignar-tecnicos).

    - Sin `tecnicos`/`ordenes`: se obtienen del backend 3.0 (OTs porAsignar de la fecha y tecnicos
      disponibles ese dia de la semana).
    - Con `tecnicos` y/o `ordenes`: se usan los del request (formato interno, horas de Chile); lo que
      falte se completa desde el backend.
    - `tiempo_limite_segundos` omitido: se calcula con la configuracion.

    Las rutas son abiertas: terminan en la ultima OT (`hora_fin_ruta`), sin volver a la base, y
    `distancia_total_km` no incluye regreso. Cada parada trae su `kilometraje_estimado`
    (km del tramo hacia esa OT desde la base o la OT anterior, no acumulado).

    Cada OT sin asignar viene en `diagnosticos` con su causa clasificada (`causa_principal` y `causas`).
    Una OT sin ubicacion nunca se rutea: queda en `diagnosticos` con causa `georreferencia`.
    Si el backend no responde, `status` es `error_api`: no se optimizan datos inventados.
    """
    maximo = optimizador.obtener_configuracion()["solver_time_limit_max_seconds"]
    if body.tiempo_limite_segundos and body.tiempo_limite_segundos > maximo:
        raise HTTPException(status_code=422, detail=f"tiempo_limite_segundos no puede superar {maximo} "
                                                    f"(parametro solver_time_limit_max_seconds).")
    tecnicos = [t.model_dump() for t in body.tecnicos] if body.tecnicos is not None else None
    ordenes = [o.model_dump() for o in body.ordenes] if body.ordenes is not None else None
    parametros = dict(fecha=body.fecha, tiempo_limite_segundos=body.tiempo_limite_segundos, api_base_url=EXTERNAL_API_BASE)

    resultado = optimizador.optimizar_jornada(tecnicos=tecnicos, ordenes=ordenes, **parametros)

    if resultado.get("status") == "success":
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
    try:
        cfg = optimizador.actualizar_configuracion(body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
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
    """Las asignaciones son las que registra el backend: una OT esta asignada si tiene `tecnico_id`."""
    ordenes = obtener_ordenes_fuente()
    target_fecha = fecha or optimizador.inferir_fecha(ordenes)
    tecnicos = obtener_tecnicos_fuente()
    disponibles = [t for t in tecnicos if optimizador.tecnico_disponible(t, target_fecha)]

    total_ots = len(ordenes)
    ots_asignadas = sum(1 for o in ordenes if o.get("tecnico_id"))
    tecnicos_con_ruta = len({o["tecnico_id"] for o in ordenes
                             if o.get("tecnico_id") and o.get("fecha_programada") == target_fecha})

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
