
---

# 5) `README_RENDER.md`

```md
# Deploy en Render

## 1. Subir archivos al repositorio
Sube:
- app.py
- requirements.txt
- Dockerfile
- serviceAccountKey.json

## 2. Crear servicio en Render
- New Web Service
- Conectar repositorio GitHub
- Environment: Docker

## 3. Importante
El archivo `serviceAccountKey.json` debe existir dentro del proyecto.

## 4. URL final
Render te dará una URL tipo:
https://tu-backend.onrender.com

Esa URL será la que use el frontend de GitHub.
