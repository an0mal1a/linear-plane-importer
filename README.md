# linear-plane-importer

Importador sencillo de CSV de Linear a Plane (versión gratuita / Open Source).

Este repositorio contiene un script en Python para migrar datos exportados desde Linear (CSV) hacia Plane usando la API. Lo único necesario es exportar tu CSV desde Linear, crear una API key en Plane y colocar las credenciales en un archivo `.env`.

---

## Resumen rápido

1. Exporta un CSV desde Linear.
2. Crea una API key en Plane.
3. Guarda la API key (y opcionalmente la URL de tu instancia) en `.env`.
4. Ejecuta el script para importar los ítems.

---

## Requisitos

- Python 3.8+.
- Paquetes: requests, python-dotenv (u otros que use el script). Si hay `requirements.txt`, instálalos con `pip install -r requirements.txt`.
- Acceso a tu instancia de Plane y una API key con permisos para crear ítems/proyectos.

---

## Exportar CSV desde Linear

1. Entra a tu workspace en Linear.
2. Ve a Settings → Data export (o Export).
3. Exporta los datos en formato CSV (por ejemplo `issues.csv` o `linear_export.csv`).
4. Descarga el archivo y colócalo en la raíz del proyecto o pásale su ruta al script.

---

## Crear API Key en Plane

1. Accede a tu instancia de Plane (cloud o self-hosted).
2. Ve a Settings → API Keys (o similar).
3. Genera una nueva API key con permisos para crear issues/boards/items.
4. Copia la API key.

---

## Configuración (.env)

Crea un archivo `.env` en la raíz del proyecto con al menos estas variables:

```
# .env
PLANE_API_KEY=tu_api_key_aqui
# Si usas una instancia self-hosted o URL personalizada:
# PLANE_BASE_URL=https://plane.example.com/api
# Ruta al CSV (opcional si lo pasas por argumento al script):
CSV_PATH=linear_export.csv
# Opcional: id del proyecto/board en Plane si tu script lo requiere:
# PLANE_PROJECT_ID=...
```

- NO subas tu `.env` al repositorio (añádelo a `.gitignore`).

---

## Instalación

Si existe `requirements.txt`:

```bash
python -m pip install -r requirements.txt
```

Si no, instala al menos las dependencias que el script utilice, por ejemplo:

```bash
python -m pip install requests python-dotenv
```

---

## Uso

Ejemplo de ejecución (ajusta el nombre del script si difiere):

```bash
# Copia el ejemplo y edita .env
cp .env.example .env
# Edita .env y añade tu PLANE_API_KEY y CSV_PATH

# Ejecuta el script de migración
python migrate_linear_to_plane.py --csv linear_export.csv
```

El script leerá el CSV y creará los ítems en Plane siguiendo la lógica implementada (título, descripción, etiquetas, estado, etc.).

---

## Notas y recomendaciones

- Haz primero una prueba con un CSV pequeño para verificar el comportamiento.
- Revisa el mapeo de campos: el CSV de Linear puede tener columnas distintas según la versión y configuración. Ajusta el script si es necesario.
- Manejo de límites (rate limits): si migras muchos registros, confirma que el script respete los límites de la API (retries, backoff).
- Logs: revisa la salida del script para verificar errores.

---

## Contribuir

Si quieres mejorar este script, sugerencias útiles:

- Añadir un `.env.example` con variables mínimas.
- Permitir mapeos configurables entre columnas del CSV y campos de Plane.
- Añadir soporte para reintentos y manejo de rate limits.
- Documentar ejemplos de CSV y tests.

Si vas a enviar un PR, abre uno con cambios pequeños y claros; comentaré o reviso rápido.

---

## Licencia

Añade la licencia que prefieras (por ejemplo MIT). Si no añades licencia, el proyecto no tendrá una licencia explícita.

---

Si quieres, puedo añadir también un archivo `.env.example` y/o un `README.md` en inglés o una descripción corta para la página del repo.
