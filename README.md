# linear_to_plane

Importa un export CSV de Linear a Plane (self-hosted o cloud) vía REST API.

Mapea proyectos, estados, prioridades, labels, assignees y la jerarquía de
issues (parent/child), y deja un vínculo `external_id`/`external_source` para
que re-ejecutar el script sea **idempotente**: los issues ya importados se
omiten, no se duplican.

## Qué necesitas

**1. En Linear — exportar el CSV**

`Settings > Administration > Import/Export > Export` y descarga el CSV.

El CSV debe tener (al menos) estas columnas:
`ID, Project, Title, Status, Priority, UUID`.
También se aprovechan si existen: `Description, Assignee, Labels, Started,
Completed, Canceled, Due Date, Parent issue, Related to, Blocked by, Estimate`.

**2. En Plane — crear un token**

Saca un API key / Personal Access Token en Plane (en el avatar,
`Settings > API tokens`). El workspace slug es el que aparece en la URL de tu
instancia (`https://plane.example.com/WS_SLUG/...`).

**3. Configurar `.env`**

Copia el contenido de `.env.example` a un archivo `.env` en la raíz del repo y
rellena los valores:

```env
PLANE_BASE_URL=plane.local.domain
PLANE_WORKSPACE=targetplaneworkspace
PLANE_API_KEY=plane_api_key
PLANE_VERIFY_SSL=false
PLANE_REQUESTS_PER_MINUTE=50
```

| Variable | Obligatoria | Descripción |
| --- | --- | --- |
| `PLANE_BASE_URL` | sí | URL de la instancia, p.ej. `https://plane.example.com` (con o sin `/api/v1`). |
| `PLANE_WORKSPACE` | sí | Slug del workspace desde la URL de Plane. |
| `PLANE_API_KEY` | sí | Personal Access Token / API key. |
| `PLANE_VERIFY_SSL` | no | `false`/`0`/`no`/`off` desactiva la verificación TLS (útil con HTTPS auto-firmado). Por defecto `true`. |
| `PLANE_REQUESTS_PER_MINUTE` | no | Throttle cliente de la API (por defecto `50`). |

## Instalación

Solo requiere Python 3 y `python-dotenv` (más `requests`):

```bash
pip install python-dotenv requests
```

## Uso

Seguro por defecto: **sin `--apply` solo inspecciona Plane y muestra el plan**
sin modificar nada.

```bash
# Inspecciona el CSV y el estado de Plane (no modifica nada)
python3 linear_to_plane.py linear-export.csv

# Crea/actualiza proyectos, estados, labels y work items
python3 linear_to_plane.py linear-export.csv --apply

# Además crea los proyectos de Linear que no existen en Plane
python3 linear_to_plane.py linear-export.csv --apply --create-projects

# Solo analizar el CSV (sin credenciales de Plane)
python3 linear_to_plane.py linear-export.csv --analyze-only

# Limitar la importación a proyectos concretos (repetible)
python3 linear_to_plane.py linear-export.csv --apply --only-project "Mobile" --only-project "Web"
```

## Qué hace

- **Proyectos**: busca por nombre (normalizado). Con `--create-projects` crea
  los que falten, con un nombre "Plane-safe" y un identificador corto único.
- **Estados**: crea los estados que falten por proyecto y los agrupa
  (`backlog`, `todo`, `in progress`, `in review`, `done`, `canceled`).
- **Labels**: crea las labels que falten por proyecto con color asignado.
- **Assignees**: asigna el miembro del workspace que coincida por email.
- **Prioridades**: mapea `urgent/high/medium/low/none`.
- **Fechas**: `Started` → `start_date`, `Due Date` → `target_date`.
- **Jerarquía**: aplica `Parent issue` en una segunda pasada (cuando padre e
  hijo están en el mismo proyecto).
- **Trazabilidad**: el cuerpo del issue incluye el historial de metadatos de
  Linear (IDs, fechas, relaciones, estimate).
- **Re-ejecución segura**: los issues con `external_source=linear` ya existentes
  se saltan; no se duplican.
- **Rate limiting**: respeta `429`/`Retry-After` del servidor y espacia las
  llamadas según `PLANE_REQUESTS_PER_MINUTE`. Detecta si tu instancia usa
  `/work-items/` (nuevo) o `/issues/` (versiones self-hosted antiguas).

## Notas

- El script sale con código distinto de 0 si faltan credenciales, faltan
  proyectos y no se usa `--create-projects`, etc.
- La primera ejecución recomendada es sin `--apply` para revisar el plan.
