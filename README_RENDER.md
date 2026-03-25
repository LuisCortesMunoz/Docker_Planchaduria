# Deploy en Render (Flask + Docker)

## 1) Crear Web Service
- New -> Web Service
- Connect GitHub repo
- Runtime: Docker

## 2) Variables de entorno
- (Opcional) API_KEY = 12345

## 3) Deploy
Render detecta el Dockerfile y levanta el servicio.

## 4) Probar
Visita:
- GET  /         -> health
- GET  /api/state?key=12345
- POST /api/set?key=12345  (JSON)
