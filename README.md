# Backend Flask para Render

Este backend permite:

- guardar cantidad de prendas
- activar PLC
- desactivar PLC
- subir fotos
- listar fotos
- guardar datos en Firebase Realtime Database

## Archivos necesarios
- app.py
- requirements.txt
- Dockerfile

## Importante
La llave de Firebase no se sube a GitHub.
Debe cargarse en Render como Secret File con nombre:

serviceAccountKey.json

## Endpoints
- GET /estado
- POST /set_cantidad
- POST /activar_plc
- POST /desactivar_plc
- POST /subir_foto
- GET /fotos
- GET /historial
