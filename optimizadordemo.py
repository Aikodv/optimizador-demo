import math
import requests
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

API_URL = "https://api-dummy-yurf.onrender.com/api"

def obtener_datos():
    print("-> Descargando datos de la API...")
    
    # 1. Obtener Técnicos (Tomamos 3 para el MVP y les asignamos coordenadas base)
    res_tec = requests.get(f"{API_URL}/tecnicos").json()
    tecnicos = [{"id": t["id"], "nombre": t["nombre"], "cap_max": 5, "coords": (0, i * 20)} 
                for i, t in enumerate(res_tec[:3])]

    # 2. Obtener OTs por asignar (Tomamos 10 y les asignamos coordenadas simuladas)
    res_ot = requests.get(f"{API_URL}/ordenes?estado=por_asignar").json()
    ordenes = [{"id": ot["id"], "coords": (50, i * 10)} 
               for i, ot in enumerate(res_ot[:10])]
               
    print(f"-> [OK] {len(tecnicos)} técnicos y {len(ordenes)} OTs cargadas.")
    return tecnicos, ordenes

def optimizar(tecnicos, ordenes):
    print("-> Calculando rutas óptimas...")
    # Unificamos coordenadas: Primero los técnicos (salidas), luego las OTs (destinos)
    nodos = [t["coords"] for t in tecnicos] + [ot["coords"] for ot in ordenes]
    num_v = len(tecnicos)
    
    # Matriz de distancias euclidianas (línea recta)
    matriz = [[int(math.hypot(p1[0]-p2[0], p1[1]-p2[1])) for p2 in nodos] for p1 in nodos]

    manager = pywrapcp.RoutingIndexManager(len(nodos), num_v, list(range(num_v)), list(range(num_v)))
    routing = pywrapcp.RoutingModel(manager)

    # 1. Dimensión de Distancia (Función Objetivo)
    idx_dist = routing.RegisterTransitCallback(lambda f, t: matriz[manager.IndexToNode(f)][manager.IndexToNode(t)])
    routing.SetArcCostEvaluatorOfVehicle(idx_dist, 0)

    # 2. Dimensión de Capacidad (Máximo 5 OTs por técnico)
    demanda = [0] * num_v + [1] * len(ordenes)
    idx_cap = routing.RegisterUnaryTransitCallback(lambda f: demanda[manager.IndexToNode(f)])
    routing.AddDimensionWithVehicleCapacity(idx_cap, 0, [t["cap_max"] for t in tecnicos], True, 'Capacidad')

    # Permitir que las OTs se descarten si la flota no da abasto (Penalización alta)
    for i in range(num_v, len(nodos)):
        routing.AddDisjunction([manager.NodeToIndex(i)], 10000)

    # Resolver
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    solucion = routing.SolveWithParameters(params)

    # Imprimir Salida
    print("\n=== RESULTADOS ASIGNACIÓN MVP ===")
    if not solucion:
        print("No se encontró solución factible.")
        return

    for v in range(num_v):
        ruta = []
        idx = routing.Start(v)
        while not routing.IsEnd(idx):
            nodo = manager.IndexToNode(idx)
            if nodo >= num_v:  # Si el nodo es mayor a la cantidad de técnicos, es una OT
                ruta.append(ordenes[nodo - num_v]["id"])
            idx = solucion.Value(routing.NextVar(idx))
        
        print(f"Técnico: {tecnicos[v]['nombre']}")
        print(f" -> OTs Asignadas: {ruta if ruta else 'Ninguna'}")

if __name__ == '__main__':
    t, o = obtener_datos()
    if t and o:
        optimizar(t, o)