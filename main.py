from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import math
import requests
import json
import unicodedata
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# =============================================================================
# 1. FUNCIONES AUXILIARES
# =============================================================================
def normalizar_texto(texto):
    if not texto: return ""
    texto = str(texto).strip().lower()
    return ''.join((c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn'))

def cargar_coordenadas_comunas():
    comunas_dict = {}
    try:
        with open("Latitud - Longitud Chile.json", "r", encoding="utf-8") as f:
            datos = json.load(f)
        for item in datos:
            nombre = normalizar_texto(item.get("Comuna", ""))
            lat = item.get("Latitud (Decimal)")
            lon = item.get("Longitud (decimal)") or item.get("Longitud (Decimal)")
            if nombre and lat is not None and lon is not None:
                comunas_dict[nombre] = (float(lat), float(lon))
    except FileNotFoundError:
        pass
    return comunas_dict

def calcular_distancia_km(coord1, coord2):
    lat1, lon1 = coord1
    lat2, lon2 = coord2
    R = 6371  
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return int(R * c)

# =============================================================================
# 2. CONFIGURACIÓN API
# =============================================================================
app = FastAPI(title="Optimizador API (Sprint 1)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       
    allow_credentials=True,
    allow_methods=["GET"],     
    allow_headers=["*"],       
)

API_URL = "https://api-dummy-yurf.onrender.com/api"
DICCIONARIO_COMUNAS = cargar_coordenadas_comunas()

@app.get("/")
def inicio():
    return {"mensaje": "¡La API está activa! Ve a /docs para probarla."}

# =============================================================================
# 3. ENDPOINT PRINCIPAL
# =============================================================================
@app.get("/api/v1/optimizar")
def optimizar_rutas():
    try:
        res_tec = requests.get(f"{API_URL}/tecnicos", timeout=15).json()
        res_ot = requests.get(f"{API_URL}/ordenes?estado=por_asignar", timeout=15).json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error conectando a la API: {str(e)}")

    # Ya no limitamos a 3 técnicos, usamos todos los disponibles
    tecnicos = []
    for t in res_tec: 
        zona = normalizar_texto(t.get("zona", ""))
        if zona in DICCIONARIO_COMUNAS:
            tecnicos.append({
                "id": t["id"], 
                "nombre": t["nombre"], 
                "cap_max": 5, 
                "coords": DICCIONARIO_COMUNAS[zona]
            })

    # Ya no limitamos a 10 OTs, usamos todas las pendientes
    ordenes = []
    for ot in res_ot: 
        direccion = normalizar_texto(ot.get("direccion_instalacion", ""))
        coords_ot = None
        for comuna, c_coords in DICCIONARIO_COMUNAS.items():
            if comuna in direccion:
                coords_ot = c_coords
                break
        if coords_ot:
            ordenes.append({"id": ot["id"], "coords": coords_ot})

    if not tecnicos:
        return {"mensaje": "Ningún técnico pudo ser mapeado."}
    
    if not ordenes:
        return {"estado": "exito", "asignaciones": [], "mensaje": "No hay órdenes pendientes mapeables."}

    # --- Motor OR-Tools ---
    nodos = [t["coords"] for t in tecnicos] + [ot["coords"] for ot in ordenes]
    num_v = len(tecnicos)
    
    matriz = [[calcular_distancia_km(nodos[i], nodos[j]) for j in range(len(nodos))] for i in range(len(nodos))]

    manager = pywrapcp.RoutingIndexManager(len(nodos), num_v, list(range(num_v)), list(range(num_v)))
    routing = pywrapcp.RoutingModel(manager)

    # 1. Función de Costo (Distancia)
    idx_dist = routing.RegisterTransitCallback(lambda f, t: matriz[manager.IndexToNode(f)][manager.IndexToNode(t)])
    routing.SetArcCostEvaluatorOfVehicle(idx_dist, 0)

    # 2. Dimensión de Capacidad (Max 5 OTs)
    demanda = [0] * num_v + [1] * len(ordenes)
    idx_cap = routing.RegisterUnaryTransitCallback(lambda f: demanda[manager.IndexToNode(f)])
    routing.AddDimensionWithVehicleCapacity(idx_cap, 0, [t["cap_max"] for t in tecnicos], True, 'Capacidad')

    # 3. ¡NUEVO! Límite geográfico estricto: Máximo 100 km de recorrido total por técnico
    distancia_maxima_km = 100 
    routing.AddDimension(
        idx_dist,
        0,  
        distancia_maxima_km,
        True,  
        'Distancia'
    )

    # Nodos opcionales (Multa alta para que asigne lo más posible dentro del radio de 100km)
    for i in range(num_v, len(nodos)):
        routing.AddDisjunction([manager.NodeToIndex(i)], 100000)

    # Parámetros de búsqueda con límite de tiempo de 3 segundos para no saturar Render
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.time_limit.FromSeconds(3) 
    
    solucion = routing.SolveWithParameters(params)

    if not solucion:
        raise HTTPException(status_code=422, detail="No se encontró solución factible")

    # --- Armar JSON ---
    resultado = []
    for v in range(num_v):
        ruta = []
        idx = routing.Start(v)
        while not routing.IsEnd(idx):
            nodo = manager.IndexToNode(idx)
            if nodo >= num_v: 
                ruta.append(ordenes[nodo - num_v]["id"])
            idx = solucion.Value(routing.NextVar(idx))
        
        resultado.append({
            "tecnico_id": tecnicos[v]["id"],
            "tecnico_nombre": tecnicos[v]["nombre"],
            "ordenes_asignadas": ruta
        })

    return {
        "estado": "exito",
        "total_tecnicos": len(tecnicos),
        "total_ordenes_procesadas": len(ordenes),
        "asignaciones": resultado
    }
