# HU-16: Optimizador v2

> Como planificador, quiero que las rutas propuestas provengan de un modelo de optimización resuelto
> sobre datos operacionales, para que las asignaciones sean utilizables y no una aproximación por
> comparación de direcciones.

Este documento describe el modelo, sus restricciones, las causas de no asignación y los parámetros
configurables. Lo mismo se puede consultar en vivo desde el servicio:

| Qué | Endpoint |
|---|---|
| Declaración del modelo (variables, objetivo, restricciones, causas) | `GET /api/optimizador/modelo` |
| Catálogo documentado de parámetros | `GET /api/optimizador/configuracion/parametros` |
| Valores vigentes | `GET /api/optimizador/configuracion` |
| Modificar / restaurar | `PUT /api/optimizador/configuracion`, `POST /api/optimizador/configuracion/restaurar` |
| Ejecutar una corrida | `POST /api/optimizador/ejecutar` |
| OTs sin asignar de la última corrida | `GET /api/optimizador/pendientes` |

## 1. Criterios de aceptación

| Criterio | Dónde se cumple |
|---|---|
| El servicio resuelve mediante el solver, con variables, objetivo y restricciones explícitas | `construir_modelo()` y `calcular_matrices_costo()` en `optimizador.py`; declarado en la sección 2 y en `GET /api/optimizador/modelo` |
| Direcciones resueltas a coordenadas con niveles de respaldo | `geocoding.py`; sección 3 |
| Matriz sobre distancias viales con respaldo | `obtener_matrices_osrm()` y `generar_matrices_haversine()`; sección 4 |
| Restricciones implementadas y documentadas, distinguiendo las que quedaron fuera | Secciones 2.3 y 2.4 |
| OTs no asignadas con causa clasificada | `diagnosticar_orden_pendiente()`; sección 5 |
| Parámetros de la corrida configurables vía endpoint | Catálogo `PARAMETROS` en `optimizador.py`; sección 6 |
| Parámetros documentados, indicando cliente vs. supuesto del equipo | Sección 6 (campo `origen`) |

## 2. Modelo de optimización

Problema de ruteo de vehículos (VRP) con ventanas horarias, capacidad, múltiples depósitos y visitas
opcionales, resuelto con Google OR-Tools Routing.

### 2.1 Nodos y variables

Hay un nodo por base de técnico (inicio y fin de su ruta) y un nodo por OT.

| Variable | Significado |
|---|---|
| `next[i]` | Nodo que se visita después de `i`; define la secuencia de cada ruta |
| `vehiculo[i]` | Técnico que atiende la OT `i` |
| `activa[i]` | 1 si la OT queda asignada, 0 si queda sin asignar |
| `tiempo[i]` | Minuto, desde el inicio de jornada, en que comienza el servicio en `i` |
| `carga[i]` | OTs acumuladas por el técnico al llegar a `i` |

### 2.2 Función objetivo

Se minimiza, en metros (las penalizaciones son metros equivalentes):

1. La distancia vial recorrida por todos los técnicos.
2. `penalty_drop_node` por cada OT sin asignar.
3. `penalty_mix_sector` por cada cambio de sector en la ruta de un interno.
4. `penalty_externo_sector_interno` por cada OT de un sector reservado atendida por un externo.
5. `costo_por_ot_externo` por cada OT atendida por un externo.
6. `span_cost_coefficient` × duración de la ruta más larga (balance de carga).
7. 1 por cada minuto de la hora de retorno a la base (rutas compactas, sin esperas).

### 2.3 Restricciones implementadas

| Código | Restricción | Tipo | Parámetros |
|---|---|---|---|
| R1 | Cada OT se visita a lo más una vez y por un solo técnico | Dura | |
| R2 | Cada ruta parte y termina en la base del técnico | Dura | |
| R3 | Toda la ruta ocurre dentro de la jornada | Dura | `inicio_jornada_horas`, `fin_jornada_minutos` |
| R4 | Una OT con hora programada inicia dentro de hora ± tolerancia y termina antes del fin de jornada | Dura | `ventana_tolerancia_min`, `tiempos_servicio_por_tipo`, `tiempo_servicio_default` |
| R5 | Cada técnico atiende como máximo su capacidad de OTs | Dura | `capacidad_max_interno`, `capacidad_max_externo` |
| R6 | Una OT sin ubicación resoluble no se rutea | Dura (configurable) | `excluir_ots_sin_georreferencia` |
| R7 | Los sectores con más OTs que el umbral se asignan a internos | Blanda; dura si `sectores_internos_exclusivos` | `umbral_ots_sector_interno`, `penalty_externo_sector_interno` |
| R8 | Un interno evita mezclar sectores en su ruta | Blanda | `penalty_mix_sector` |
| R9 | Una OT puede quedar sin asignar pagando una penalización alta | Blanda | `penalty_drop_node` |

R9 garantiza que el modelo siempre tenga solución: lo que no cabe se reporta como pendiente con su causa.

### 2.4 Restricciones fuera de esta versión

- Habilidades o certificaciones del técnico según el tipo de OT.
- Horario individual por técnico: todos comparten la misma jornada.
- Pausa de colación y descansos.
- Tráfico variable según la hora: la matriz de tiempos es única para todo el día.
- Prioridad o SLA de las OTs: todas pesan lo mismo al decidir cuál queda sin asignar.
- Materiales, equipos o stock del vehículo: la capacidad solo cuenta OTs.
- Dependencias entre OTs (precedencias) y OTs que requieren más de un técnico.
- Planificación de varios días y re-optimización durante la jornada.

### 2.5 Resolución

Se resuelve con dos estrategias de solución inicial (`PATH_CHEAPEST_ARC` y `PARALLEL_CHEAPEST_INSERTION`),
cada una mejorada con Guided Local Search, y se conserva la de menor costo. Es una heurística con límite
de tiempo: entrega la mejor solución encontrada, no garantiza el óptimo.

## 3. Georreferencia de las OTs

Cada OT se resuelve al mejor nivel disponible. El nivel usado viene en `precision_ubicacion` de cada
parada y de cada OT pendiente, y el conteo por nivel en `resumen.precision_ubicaciones`.

| Nivel | `precision_ubicacion` | Fuente |
|---|---|---|
| 1 | `original` | La OT ya trae coordenadas válidas dentro de Chile |
| 2 | `numero` | Nominatim o Photon encontraron la dirección con su número |
| 3 | `calle` | Se encontró la calle dentro de la comuna, sin el número exacto |
| 4 | `comuna` | Centro de la comuna (dirección no encontrada, o geocodificación desactivada o sin tiempo) |
| 5 | `aproximada` | Tampoco se reconoce la comuna: la OT queda sin asignar por georreferencia |

Dentro de los niveles 2 y 3 se consulta Nominatim, luego Photon y luego Nominatim con la variante del
prefijo "Avenida". Los resultados se guardan en `geocoding_cache.json`. Si un proveedor no responde se
pasa al siguiente y, en último caso, al centro de la comuna: una fuente caída no impide el cálculo.

## 4. Matriz de distancias

- **Principal:** distancias y tiempos viales de OSRM (`fuente_matriz = "osrm"`).
- **Respaldo:** línea recta × `factor_sinuosidad_vial`, con tiempos a `velocidad_promedio_kmh`
  (`fuente_matriz = "haversine"`). Se usa cuando OSRM no responde, cuando hay más puntos que
  `max_nodos_osrm` o cuando `usar_osrm` es `false`. Las celdas que OSRM devuelve sin ruta también se
  completan con el respaldo.

La fuente usada viene en `resumen.fuente_matriz`; cuando se cae al respaldo sin haberlo pedido, se
agrega además una alerta.

## 5. Causas de no asignación

Cada OT pendiente trae `causa_principal` (la categoría), `causas` (lista estructurada) y `razones`
(el texto histórico con prefijo, que se mantiene por compatibilidad).

| Categoría | Código | Prefijo en `razones` | Cuándo |
|---|---|---|---|
| `georreferencia` | `SIN_GEORREFERENCIA` | `GEORREFERENCIA:` | Sin coordenadas, dirección no geocodificable y comuna no reconocida |
| `sectorial` | `SECTOR_RESERVADO_INTERNOS` | `SECTOR:` | Sector reservado a internos; un externo alcanzaba y tenía cupo, pero la regla de sector lo excluyó |
| `temporal` | `DURACION_EXCEDE_JORNADA` | `DURACION:` | El servicio dura más que la jornada |
| `temporal` | `HORA_FUERA_DE_JORNADA` | `HORARIO:` | La hora programada queda fuera de la jornada |
| `temporal` | `VENTANA_INALCANZABLE` | `TIEMPO:` | Ningún técnico alcanza a llegar en la ventana y volver a su base |
| `capacidad` | `CAPACIDAD_AGOTADA` | `CAPACIDAD:` | Los técnicos que alcanzaban completaron su máximo de OTs |
| `optimizacion` | `SIN_CUPO_EN_RUTAS` | `OPTIMIZACION:` | Era atendible por sí sola, pero no cabe junto a las demás OTs |

La causa sectorial solo aparece cuando la regla de sector efectivamente impide la asignación: con
`sectores_internos_exclusivos = true`, o con `penalty_externo_sector_interno` mayor o igual que
`penalty_drop_node`. Con los valores por defecto la regla es blanda y el externo apoya.

## 6. Parámetros configurables

Todos se leen y modifican por endpoint; los límites se validan (HTTP 422 fuera de rango). La
configuración vive en memoria: se pierde al reiniciar el servicio.

- **Ámbito** — `negocio`: regla de la operación, la decide la planificadora. `solver`: interno del
  modelo, lo ajusta el equipo técnico. `servicio`: integración con servicios externos.
- **Origen** — `operacion_cliente`: refleja cómo opera el cliente. `supuesto_equipo`: supuesto o
  calibración del equipo.

> La clasificación de origen la hizo el equipo de desarrollo a partir del código; los valores marcados
> `operacion_cliente` deben confirmarse con el cliente.

| Parámetro | Por defecto | Límites | Ámbito | Origen |
|---|---|---|---|---|
| `tiempos_servicio_por_tipo` | instalación simple 45, con corte 90, mantención 40, retiro 25 min | 1 a 600 | negocio | operacion_cliente |
| `tiempo_servicio_default` | 30 min | 1 a 600 | negocio | supuesto_equipo |
| `inicio_jornada_horas` | 8 | 5 a 12 | negocio | operacion_cliente |
| `fin_jornada_minutos` | 600 min | 180 a 1440 | negocio | operacion_cliente |
| `ventana_tolerancia_min` | 30 min | 0 a 240 | negocio | supuesto_equipo |
| `capacidad_max_externo` | 8 OTs | 1 a 30 | negocio | operacion_cliente |
| `capacidad_max_interno` | 12 OTs | 1 a 30 | negocio | operacion_cliente |
| `umbral_ots_sector_interno` | 10 OTs | 0 a 100 | negocio | operacion_cliente |
| `sectores_internos_exclusivos` | false | | negocio | supuesto_equipo |
| `costo_por_ot_externo` | 0 m | 0 o más | negocio | supuesto_equipo |
| `max_radio_operacional_km` | 80 km | 10 a 300 | negocio | supuesto_equipo |
| `excluir_ots_sin_georreferencia` | true | | negocio | supuesto_equipo |
| `aplicar_cambios_por_defecto` | false | | negocio | supuesto_equipo |
| `factor_sinuosidad_vial` | 1.30 | 1 a 3 | solver | supuesto_equipo |
| `velocidad_promedio_kmh` | 30 km/h | 10 a 120 | solver | supuesto_equipo |
| `penalty_drop_node` | 10 000 000 m | 0 o más | solver | supuesto_equipo |
| `penalty_mix_sector` | 100 000 m | 0 o más | solver | supuesto_equipo |
| `penalty_externo_sector_interno` | 1 000 000 m | 0 o más | solver | supuesto_equipo |
| `span_cost_coefficient` | 50 | 0 a 500 | solver | supuesto_equipo |
| `solver_time_limit_seconds` | 10 s | 1 a 600 | solver | supuesto_equipo |
| `solver_segundos_por_ot` | 0.5 s/OT | 0 a 5 | solver | supuesto_equipo |
| `solver_time_limit_max_seconds` | 120 s | 1 a 600 | solver | supuesto_equipo |
| `usar_osrm` | true | | servicio | supuesto_equipo |
| `max_nodos_osrm` | 100 puntos | 2 a 1000 | servicio | supuesto_equipo |
| `usar_geocoding` | true | | servicio | supuesto_equipo |
| `geocoding_max_segundos` | 60 s | 0 a 600 | servicio | supuesto_equipo |

### Parámetros de cada corrida (`POST /api/optimizador/ejecutar`)

| Campo | Si se omite | Ámbito |
|---|---|---|
| `fecha` | Fecha programada más temprana entre las OTs por asignar | negocio |
| `aplicar_cambios` | `aplicar_cambios_por_defecto` | negocio |
| `tiempo_limite_segundos` | Automático: `OTs × solver_segundos_por_ot`, entre `solver_time_limit_seconds` y `solver_time_limit_max_seconds` | solver |

La respuesta incluye `parametros_corrida` con los valores efectivamente usados y la configuración vigente.

### Constantes que siguen fijas en el código

No son parámetros de la corrida: son límites técnicos de los servicios externos o de la validación de
direcciones, en `geocoding.py` y `optimizador.py`.

- Timeouts HTTP (OSRM 15 s, API de órdenes 15 a 25 s, geocodificadores 8 s) y pausa de 1,1 s entre
  consultas a cada geocodificador (política de uso de los servicios públicos).
- Radios de validación de un resultado de geocodificación (10 km sin comuna informada, 3 km en comuna
  limítrofe) y 30 días para reintentar una dirección no encontrada.
- Estrategias iniciales y metaheurística del solver.
