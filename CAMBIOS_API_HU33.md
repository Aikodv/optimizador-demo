# Cambios en la API del optimizador (HU-33)

Este documento es para quienes desarrollan la página que consume la API. Resume los cambios publicados en `main` (commits `b71a95b` y `221da12`).

## Resumen

1. **"Duración de la jornada" pasa a ser "Fin de jornada"**: se configura como hora de cierre, igual que el inicio.
2. **El optimizador ya no asigna técnicos**: solo propone rutas. La asignación ocurre cuando la planificadora presiona **Confirmar**.
3. **Las OTs sin ubicación nunca se rutean**: no se usa Santiago Centro ni el centro de la comuna. Esas OTs quedan como pendientes.

## 1. Configuración (`GET`/`PUT /api/optimizador/configuracion`)

| Antes | Ahora |
|---|---|
| `fin_jornada_minutos` (600) | `fin_jornada_horas` (entero entre 6 y 23; por defecto 18) |
| `aplicar_cambios_por_defecto` | Eliminado |
| `excluir_ots_sin_georreferencia` | Eliminado |

- `fin_jornada_horas` debe ser mayor que `inicio_jornada_horas`. Si no lo es, la API responde **HTTP 422** con un `detail` en texto, por ejemplo: `"El fin de jornada (7:00) debe ser posterior al inicio (8:00)."`. Muestren ese mensaje a la planificadora.
- **Atención:** si se sigue enviando `fin_jornada_minutos`, la API lo **ignora sin error**. Hay que cambiar el campo.
- El catálogo (`GET /api/optimizador/configuracion/parametros`) ya no incluye los parámetros eliminados. En `parametros_ejecucion` tampoco aparece `aplicar_cambios`.

## 2. Ejecutar (`POST /api/optimizador/ejecutar`)

- Se eliminó `aplicar_cambios`. Si el request lo incluye, con `true` o con `false`, la API responde **HTTP 422**. Envíen `{}`, o solo `fecha`, `tiempo_limite_segundos`, `tecnicos` y `ordenes`.
- En la respuesta se eliminaron `aplicacion_cambios` y `parametros_corrida.aplicar_cambios`.
- La respuesta es una **propuesta**. El optimizador no la guarda ni asigna las OTs: la página debe conservar `rutas` y `diagnosticos` de la respuesta.

## 3. Confirmar la asignación

El optimizador **no tiene capacidades de asignación**. Se eliminaron `PATCH /api/ordenes/asignaciones-masivas`, `PATCH /api/ordenes/{id}/tecnico`, `GET /api/rutas` y `GET /api/tecnicos/{id}/ruta` (responden **404**).

La página debe tener un botón **Confirmar** después de que la planificadora revise la propuesta. Al presionarlo, la corrida aprobada se envía **directamente al backend**, que es el registro de verdad de las asignaciones:

```http
POST https://goodroute-backend.onrender.com/api/asignar-tecnicos
```

- El body (`AsignarTecnicosRequest`) lleva `fecha_planificacion`, `generado_en`, `revisado_en`, `revisado_por`, `resumen_corrida`, `rutas` (con `ruta_propuesta`, `ruta_final` y `paradas` por técnico) y `pendientes`. El esquema completo está en `https://goodroute-backend.onrender.com/docs`.
- Para asignar o reprogramar una sola OT: `PATCH https://goodroute-backend.onrender.com/api/v1/ordenes/{orden_id}/asignar-tecnico`.
- `GET /api/metricas/resumen-diario` cuenta como asignadas las OTs que tienen `tecnico_id` en el backend.

## 4. OTs sin ubicación

- Una OT sin coordenadas cuya dirección no se encontró en el mapa queda en `diagnosticos` con:
  - `causas[0].codigo = "SIN_GEORREFERENCIA"`
  - `causa_principal = "georreferencia"`
  - `latitud` y `longitud` en `null`. **No la dibujen en el mapa.**
- `razones[0]` explica el motivo. Puede ser que no se encontró, que no alcanzó a geocodificarse (se resuelve en la próxima corrida), que la geocodificación está desactivada o que no tiene dirección.
- Valores de `precision_ubicacion`: `original`, `numero`, `calle`, `sin_ubicacion`. Ya no existen `comuna` ni `aproximada`.
- `GET /api/geocoding` devuelve `{"precision": "sin_ubicacion", "lat": null, "lon": null}` cuando no encuentra la dirección.

## 5. Otros

- `resumen.fuente_matriz` puede ser `osrm`, `osrm_parcial` (algunas distancias son estimadas) o `haversine` (todas son estimadas). En los dos últimos casos llega además una alerta en `alertas`.
- En la vista de pendientes, la causa `HORARIO` ahora es frecuente para OTs agendadas después del fin de jornada. La planificadora puede extender `fin_jornada_horas` si corresponde.

## Checklist

- [ ] Cambiar el campo "Duración" por "Fin de jornada" (en horas) y enviar `fin_jornada_horas`.
- [ ] Mostrar el `detail` cuando la API responda 422.
- [ ] Quitar `aplicar_cambios` de cualquier request y de la UI.
- [ ] Dejar de usar `GET /api/rutas`, `GET /api/tecnicos/{id}/ruta` y los `PATCH` de asignación del optimizador.
- [ ] Agregar el botón **Confirmar**, que envía la corrida aprobada a `POST /api/asignar-tecnicos` del backend.
- [ ] No dibujar en el mapa OTs con `latitud`/`longitud` en `null`, y mostrarlas como pendientes con su razón.
- [ ] Actualizar las etiquetas de `precision_ubicacion`, sin `comuna` ni `aproximada`.
