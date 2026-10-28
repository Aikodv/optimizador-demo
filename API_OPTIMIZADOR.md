# Optimizador de rutas: endpoints y funcionamiento

Servicio FastAPI (versión 3.1.0) que **propone** rutas diarias para los técnicos usando Google OR-Tools. **No asigna técnicos ni guarda hojas de ruta.** La asignación se registra en el backend de GoodRoute cuando la planificadora confirma la propuesta.

- Backend (fuente de técnicos, OTs y asignaciones): `https://goodroute-backend.onrender.com/api`. Se cambia con la variable de entorno `API_BASE_URL`.
- Ejecución local: `python -m uvicorn main:app --reload --port 8000`
- Dashboard: `http://127.0.0.1:8000/` · Swagger: `http://127.0.0.1:8000/docs`

## Flujo

```
Backend GoodRoute ──(técnicos y OTs porAsignar)──> Optimizador ──(propuesta)──> Dashboard / página
                                                                                      │
Backend GoodRoute <──────(POST /api/asignar-tecnicos, al presionar Confirmar)─────────┘
```

1. La página llama a `POST /api/optimizador/ejecutar`.
2. El optimizador lee del backend las OTs `porAsignar` de la fecha y los técnicos disponibles ese día.
3. Resuelve el problema y devuelve `rutas`, `diagnosticos` (OTs sin asignar y su causa) y `alertas`.
4. La planificadora revisa la propuesta y, al confirmar, la página la envía **directamente al backend**.

Si el backend no responde, el optimizador falla (HTTP 502 o `status: "error_api"`). No hay datos locales de respaldo.

## Endpoints

### Optimizador

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/api/optimizador/ejecutar` | Ejecuta la optimización y devuelve la propuesta. |
| `GET` | `/api/optimizador/pendientes?fecha=` | OTs que la última corrida no pudo asignar, con su causa. Sin `fecha`: la última corrida. |
| `GET` | `/api/optimizador/modelo` | Declaración del modelo: variables, función objetivo, restricciones y causas de no asignación. |
| `GET` | `/api/optimizador/configuracion` | Parámetros vigentes. |
| `PUT` | `/api/optimizador/configuracion` | Modifica parámetros. Solo se cambian los que se envían. |
| `GET` | `/api/optimizador/configuracion/parametros` | Catálogo documentado de parámetros, para construir el panel de configuración. |
| `POST` | `/api/optimizador/configuracion/restaurar` | Restaura los valores por defecto. |

### Datos del backend (solo lectura)

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/tecnicos` | Técnicos del backend en snake_case, incluida `disponibilidad` (7 booleanos, de lunes a domingo). |
| `GET` | `/api/disponibilidad?fecha=` | Disponibilidad de cada técnico en la fecha. Sin `fecha`: hoy en Chile. |
| `GET` | `/api/ordenes?estado=` | OTs del backend en snake_case. `hora_programada` viene en hora de Chile; la original (UTC) queda en `hora_programada_utc`. |
| `GET` | `/api/metricas/resumen-diario?fecha=` | KPIs: total de OTs, asignadas (las que tienen `tecnico_id` en el backend), pendientes, técnicos disponibles y técnicos con OTs. |

### Apoyo

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/geocoding?direccion=&comuna=` | Prueba cómo se interpreta y ubica una dirección. |
| `GET` | `/api/ruteo/geometria?coordenadas=lon,lat;lon,lat` | Trazado por calles (OSRM) para dibujar la ruta. Si OSRM falla, devuelve una línea recta (`status: "fallback"`). |
| `POST` | `/api/simulacion/regenerar` | Llama a `GET /api/reset` del backend (body opcional: `{"semilla": 123}`) y limpia los pendientes. |
| `GET` | `/api/info` | Versión y enlaces. |
| `GET` | `/` | Dashboard web. |

### Eliminados

Responden **404**: el optimizador ya no asigna ni guarda hojas de ruta.

- `PATCH /api/ordenes/asignaciones-masivas`
- `PATCH /api/ordenes/{id}/tecnico`
- `GET /api/rutas`
- `GET /api/tecnicos/{id}/ruta`

## Ejecutar la optimización

```http
POST /api/optimizador/ejecutar
{
  "fecha": "2026-10-28",            // opcional; sin ella, la fecha programada más temprana de las OTs
  "tiempo_limite_segundos": 30,     // opcional; sin él se calcula según la cantidad de OTs
  "tecnicos": [ ... ],              // opcional; reemplaza a los del backend (se asumen disponibles)
  "ordenes": [ ... ]                // opcional; reemplaza a las OTs porAsignar del backend
}
```

Lo normal es enviar `{}` o solo `fecha`. Si el request incluye `aplicar_cambios`, la API responde 422 porque ese campo ya no existe.

### Respuesta

| Campo | Contenido |
|---|---|
| `status` | `success`, `no_data` (faltan técnicos u OTs), `infeasible` (sin solución) o `error_api` (el backend no respondió). |
| `fecha` | Fecha planificada. |
| `timestamp_asignacion_utc` | Instante en que se generó la propuesta. |
| `resumen` | `total_ots`, `ots_asignadas`, `ots_pendientes`, `pendientes_por_causa`, `total_tecnicos`, `tecnicos_utilizados`, `distancia_total_km`, `costo_objetivo`, `fuente_matriz` y `precision_ubicaciones`. |
| `rutas[]` | Una por técnico: `tecnico_id`, `nombre`, `tipo`, `zona_base`, coordenadas de la base, `capacidad_uso`, `hora_salida_base`, `hora_fin_ruta`, `duracion_total_min`, `distancia_total_km`, `tiempo_espera_total_min`, `total_ots` y `paradas[]`. |
| `rutas[].paradas[]` | `secuencia`, `ot_id`, `tipo`, `cliente`, `direccion`, coordenadas, `precision_ubicacion`, `hora_programada`, `hora_estimada_llegada`, `hora_estimada_salida`, `espera_min`, `duracion_servicio_min`, `kilometraje_estimado` y `sector`. |
| `diagnosticos[]` | OTs sin asignar: datos de la OT, `causa_principal`, `causas` (`codigo`, `categoria`, `detalle`) y `razones` (texto con prefijo, por ejemplo `"TIEMPO: ..."`). |
| `alertas[]` | Avisos: OTs fuera del radio operacional, distancias estimadas en vez de viales, sectores reservados sin interno, etc. |
| `parametros_corrida` | Fecha, tiempo límite usado y la configuración con la que se resolvió. |

Todas las horas de la respuesta están en hora de Chile.

## Confirmar la propuesta (en el backend)

El botón **Confirmar** envía la corrida aprobada a:

```http
POST https://goodroute-backend.onrender.com/api/asignar-tecnicos
```

Campos obligatorios: `fecha_planificacion`, `generado_en`, `revisado_en`, `revisado_por`, `resumen_corrida` y `rutas`. Opcionales: `inicio_planificacion` y `pendientes`.

Para armar cada elemento de `rutas` a partir de la propuesta:

| Backend (`RutaTecnico`) | Propuesta del optimizador |
|---|---|
| `tecnico_id`, `zona_base`, `capacidad_uso`, `hora_salida_base`, `distancia_total_km`, `paradas` | Mismo nombre en `rutas[]`. |
| `tecnico_nombre` | `nombre` |
| `ruta_propuesta` | `ot_id` de las paradas, en orden de `secuencia`. |
| `ruta_final` | Las OTs que dejó la planificadora, en su orden final. |
| `aceptada_sin_modificacion`, `modificaciones` | Los arma la página según lo que cambió la planificadora. |

El backend aplica lo válido y reporta los errores sin detener el resto. Para asignar o reprogramar una sola OT: `PATCH /api/v1/ordenes/{orden_id}/asignar-tecnico`. El esquema completo está en `https://goodroute-backend.onrender.com/docs`.

## Cómo resuelve el optimizador

**Tipo de problema:** ruteo de vehículos (VRP) abierto, con ventanas horarias, capacidad, una base por técnico y visitas opcionales.

### 1. Obtención de datos

- **OTs:** las `porAsignar` de la fecha. La fecha y la hora del backend se interpretan como un instante UTC y se convierten a hora de Chile, considerando el horario de verano. Una OT sin fecha entra en cualquier día. Una OT con fecha u hora de formato desconocido queda pendiente con causa `DATOS_INVALIDOS`.
- **Técnicos:** los activos con `disponibilidad` verdadera ese día de la semana. Si un técnico tiene `disponibilidad` inválida o `activo: false`, se considera no disponible.

### 2. Preparación

- **Ubicación de las OTs:** se usan las coordenadas de la OT si las trae. Si no, se geocodifica la dirección y el resultado se guarda en `geocoding_cache.json`. Una OT que no se pueda ubicar **no se rutea**: nunca se usa el centro de la comuna. Queda pendiente con causa `SIN_GEORREFERENCIA`.
- **Base del técnico:** sus coordenadas, si las tiene. Si no, el centro de su comuna (`zona`). Si la comuna no se reconoce, Santiago Centro.
- **Distancias y tiempos:** se piden a OSRM (por calles). Si OSRM falla o le faltan tramos, se estima con línea recta × `factor_sinuosidad_vial`, a `velocidad_promedio_kmh`. `resumen.fuente_matriz` indica cuál se usó: `osrm`, `osrm_parcial` o `haversine`.
- **Sectores:** el sector de cada OT es su comuna. Las comunas con más de `umbral_ots_sector_interno` OTs se reservan a técnicos internos.

### 3. Restricciones

| Código | Restricción | Tipo |
|---|---|---|
| R1 | Cada OT la atiende como máximo un técnico. | dura |
| R2 | La ruta parte en la base y termina en la última OT, sin regreso. | dura |
| R3 | Todo ocurre dentro de la jornada (`inicio_jornada_horas` a `fin_jornada_horas`). | dura |
| R4 | Una OT con hora empieza entre hora − `tolerancia_antes_min` y hora + `tolerancia_despues_min`. | dura |
| R5 | Cada técnico tiene un máximo de OTs (`capacidad_max_interno` / `capacidad_max_externo`). Su `cap_max` propio solo puede reducirlo. | dura |
| R6 | Una OT sin ubicación no se rutea. | dura |
| R7 | Los sectores reservados son para internos. | blanda (dura si `sectores_internos_exclusivos`) |
| R8 | Un interno evita mezclar sectores. | blanda |
| R9 | Una OT puede quedar sin asignar pagando `penalty_drop_node`. | blanda |
| R10 | Una OT con fecha u hora inválida no se rutea. | dura |

### 4. Función objetivo (minimizar, en metros equivalentes)

Distancia recorrida + `penalty_drop_node` por cada OT sin asignar + `penalty_mix_sector` + `penalty_externo_sector_interno` + `costo_por_ot_externo` + `span_cost_coefficient` × la ruta más larga (para balancear la carga) + la hora de término (para que las rutas sean compactas).

### 5. Tiempo de búsqueda

Si no se envía `tiempo_limite_segundos`, se calcula como OTs × `solver_segundos_por_ot`, con un mínimo de `solver_time_limit_seconds` y un máximo de `solver_time_limit_max_seconds`.

### 6. Causas de no asignación

| Código | Categoría | Significado |
|---|---|---|
| `DATOS_INVALIDOS` | datos | La fecha u hora no tiene un formato reconocible. |
| `SIN_GEORREFERENCIA` | georreferencia | No se pudo ubicar la dirección. |
| `SECTOR_RESERVADO_INTERNOS` | sectorial | Un externo la alcanzaba, pero el sector es exclusivo de internos. |
| `DURACION_EXCEDE_JORNADA` | temporal | El servicio dura más que la jornada. |
| `HORA_FUERA_DE_JORNADA` | temporal | La hora programada cae fuera de la jornada. |
| `VENTANA_INALCANZABLE` | temporal | Ningún técnico llega a tiempo y termina antes del fin de jornada. |
| `CAPACIDAD_AGOTADA` | capacidad | Los técnicos que podían atenderla ya completaron su máximo de OTs. |
| `SIN_CUPO_EN_RUTAS` | optimizacion | Sola era atendible, pero no cabe junto a las demás. |

## Configuración

Los parámetros vigentes están en `GET /api/optimizador/configuracion`. El catálogo con descripciones, límites y origen está en `/parametros`.

| Grupo | Parámetros (valor por defecto) |
|---|---|
| Tiempos de servicio | `tiempos_servicio_por_tipo` (instalación simple 40, con corte 40, mantención 30, retiro 30 min), `tiempo_servicio_default` (40) |
| Jornada | `inicio_jornada_horas` (8), `fin_jornada_horas` (19), `tolerancia_antes_min` (30), `tolerancia_despues_min` (15) |
| Capacidad | `capacidad_max_interno` (20), `capacidad_max_externo` (10) |
| Sectores | `umbral_ots_sector_interno` (10), `sectores_internos_exclusivos` (false), `costo_por_ot_externo` (0), `max_radio_operacional_km` (80) |
| Red vial | `factor_sinuosidad_vial` (1.30), `velocidad_promedio_kmh` (30) |
| Objetivo | `penalty_drop_node` (10 000 000), `penalty_mix_sector` (100 000), `penalty_externo_sector_interno` (1 000 000), `span_cost_coefficient` (50) |
| Solver | `solver_time_limit_seconds` (10), `solver_segundos_por_ot` (0.5), `solver_time_limit_max_seconds` (120) |
| Servicios externos | `usar_osrm` (true), `max_nodos_osrm` (100), `usar_geocoding` (true), `geocoding_max_segundos` (60) |

`fin_jornada_horas` debe ser mayor que `inicio_jornada_horas`; si no, la API responde 422.

## Limitaciones actuales

- La configuración y los pendientes viven **en memoria**: se pierden al reiniciar el servicio. Falta definir dónde persistirlos.
- No se consideran habilidades por tipo de OT, horarios individuales, colación, tráfico según la hora, prioridades o SLA, inventario, dependencias entre OTs ni planificación de varios días.
- El punto de partida del técnico es su base (sus coordenadas o el centro de su comuna). Falta confirmar con el PO si debería ser su casa o la última OT del día anterior.
