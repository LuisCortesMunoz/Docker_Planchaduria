# Deploy en Render

## Archivos en el repo
- app.py
- requirements.txt
- Dockerfile

## Firebase key
No subir `serviceAccountKey.json` al repositorio.

Subirla en Render así:

1. Entrar al servicio web
2. Ir a Settings
3. Buscar Environment
4. En Secret Files, agregar un archivo con nombre:
   serviceAccountKey.json
5. Pegar el contenido completo del JSON
6. Guardar
7. Hacer redeploy

## Ruta usada en el código
/etc/secrets/serviceAccountKey.json
