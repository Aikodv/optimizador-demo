from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import math
import requests
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

# 1. Definición de la aplicación
app = FastAPI(title="Optimizador API (Sprint 1)")

# --- CONFIGURACIÓN DE CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # Permite peticiones desde cualquier dominio (ideal para desarrollo)
    allow_credentials=True,
    allow_methods=["GET"],     # Solo permitimos peticiones GET por seguridad
    allow_headers=["*"],       # Permite todos los headers
)
# -----------------------------

API_URL = "https://api-dummy-yurf.onrender.com/api"

# 2. Ruta raíz (Para que el enlace principal de Render no muestre error)
@app.get("/")
def inicio():
    return {"mensaje": "¡La API está activa! Ve a /docs para probarla."}

# 3. El Endpoint de optimización
@app.get("/api/v1/optimizar")
def optimizar_rutas():
    """
    Endpoint que descarga los datos, calcula las rutas óptimas 
    y devuelve la asignación en formato JSON.
    """
    # --- Descarga de Datos ---
    try:
        res_tec = requests.get(f"{API_URL}/tecnicos", timeout=10).json()
        tecnicos = [{"id": t["id"], "nombre": t["nombre"], "cap_max": 5, "coords": (0, i * 20)} 
                    for i, t in enumerate(res_tec[:3])]

        res_ot = requests.get(f"{API_URL}/ordenes?estado=por_asignar", timeout=10).json()
        ordenes = [{"id": ot["id"], "coords": (50, i * 10)} 
                   for i, ot in enumerate(res_ot[:10])]
                   
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error conectando a la API: {str(e)}")

    if not tecnicos or not ordenes:
        return {"mensaje": "No hay suficientes datos para optimizar."}

    # --- Lógica del Motor OR-Tools ---
    nodos = [t["coords"] for t in tecnicos] + [ot["coords"] for ot in ordenes]
    num_v = len(tecnicos)
    
    # Matriz de distancias
    matriz = [[int(math.hypot(p1[0]-p2[0], p1[1]-p2[1])) for p2 in nodos] for p1 in nodos]

    manager = pywrapcp.RoutingIndexManager(len(nodos), num_v, list(range(num_v)), list(range(num_v)))
    routing = pywrapcp.RoutingModel(manager)

    idx_dist = routing.RegisterTransitCallback(lambda f, t: matriz[manager.IndexToNode(f)][manager.IndexToNode(t)])
    routing.SetArcCostEvaluatorOfVehicle(idx_dist, 0)

    demanda = [0] * num_v + [1] * len(ordenes)
    idx_cap = routing.RegisterUnaryTransitCallback(lambda f: demanda[manager.IndexToNode(f)])
    routing.AddDimensionWithVehicleCapacity(idx_cap, 0, [t["cap_max"] for t in tecnicos], True, 'Capacidad')

    for i in range(num_v, len(nodos)):
        routing.AddDisjunction([manager.NodeToIndex(i)], 10000)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    solucion = routing.SolveWithParameters(params)

    if not solucion:
        raise HTTPException(status_code=422, detail="No se encontró solución factible")

    # --- Construcción del JSON de respuesta ---
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

    # Retornamos los datos directamente
    return {
        "estado": "exito",
        "total_tecnicos": len(tecnicos),
        "total_ordenes_procesadas": len(ordenes),
        "asignaciones": resultado
    }
