# Step 1: Import libraries
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.utils import secure_filename
import firebase_admin
from firebase_admin import credentials, db
import os
from datetime import datetime

# Step 2: Create app
app = Flask(__name__)
CORS(app)

# Step 3: Config
UPLOAD_FOLDER = "fotos"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

# Step 4: Firebase config
# Debes subir serviceAccountKey.json al backend de Render
FIREBASE_CRED_FILE = "serviceAccountKey.json"
FIREBASE_DB_URL = "https://pruebaconexion-65273-default-rtdb.firebaseio.com/"

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CRED_FILE)
    firebase_admin.initialize_app(cred, {
        "databaseURL": FIREBASE_DB_URL
    })

estado_ref = db.reference("estado_actual")
historial_ref = db.reference("historial")
fotos_ref = db.reference("fotos")

# Step 5: Default state
estado_memoria = {
    "activo": False,
    "cantidad": 0,
    "estado": "Esperando trabajo",
    "updated_at": None
}

# Step 6: Helper to save state
def guardar_estado(activo=None, cantidad=None, estado=None):
    global estado_memoria

    if activo is not None:
        estado_memoria["activo"] = activo

    if cantidad is not None:
        estado_memoria["cantidad"] = cantidad

    if estado is not None:
        estado_memoria["estado"] = estado

    estado_memoria["updated_at"] = datetime.now().isoformat()

    estado_ref.set(estado_memoria)

    historial_ref.push({
        "activo": estado_memoria["activo"],
        "cantidad": estado_memoria["cantidad"],
        "estado": estado_memoria["estado"],
        "timestamp": estado_memoria["updated_at"]
    })

# Step 7: Load current state from Firebase at startup
def cargar_estado():
    global estado_memoria
    data = estado_ref.get()
    if data:
        estado_memoria = data

cargar_estado()

# =========================================================
# Step 8: Routes
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "message": "Backend Render activo",
        "endpoints": [
            "/estado",
            "/set_estado",
            "/set_cantidad",
            "/activar",
            "/desactivar",
            "/subir_foto",
            "/fotos",
            "/historial"
        ]
    })

@app.route("/estado", methods=["GET"])
def obtener_estado():
    return jsonify({
        "ok": True,
        "data": estado_memoria
    })

@app.route("/set_estado", methods=["POST"])
def set_estado():
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"ok": False, "message": "JSON inválido"}), 400

    activo = data.get("activo")
    cantidad = data.get("cantidad")
    estado = data.get("estado")

    if activo is not None and not isinstance(activo, bool):
        return jsonify({"ok": False, "message": "activo debe ser true o false"}), 400

    if cantidad is not None:
        if not isinstance(cantidad, int) or cantidad < 0:
            return jsonify({"ok": False, "message": "cantidad debe ser entero >= 0"}), 400

    guardar_estado(activo=activo, cantidad=cantidad, estado=estado)

    return jsonify({
        "ok": True,
        "message": "Estado actualizado",
        "data": estado_memoria
    })

@app.route("/set_cantidad", methods=["POST"])
def set_cantidad():
    data = request.get_json(silent=True)

    if not data or "cantidad" not in data:
        return jsonify({"ok": False, "message": "Debes enviar cantidad"}), 400

    cantidad = data.get("cantidad")

    if not isinstance(cantidad, int) or cantidad < 0:
        return jsonify({"ok": False, "message": "Cantidad inválida"}), 400

    guardar_estado(cantidad=cantidad, estado=f"Cantidad actualizada a {cantidad}")

    return jsonify({
        "ok": True,
        "message": "Cantidad actualizada",
        "data": estado_memoria
    })

@app.route("/activar", methods=["POST"])
def activar():
    guardar_estado(activo=True, estado="Motor activo")
    return jsonify({
        "ok": True,
        "message": "Sistema activado",
        "data": estado_memoria
    })

@app.route("/desactivar", methods=["POST"])
def desactivar():
    guardar_estado(activo=False, estado="Motor detenido")
    return jsonify({
        "ok": True,
        "message": "Sistema desactivado",
        "data": estado_memoria
    })

@app.route("/subir_foto", methods=["POST"])
def subir_foto():
    if "foto" not in request.files:
        return jsonify({"ok": False, "message": "No se recibió archivo"}), 400

    archivo = request.files["foto"]

    if archivo.filename == "":
        return jsonify({"ok": False, "message": "Archivo vacío"}), 400

    nombre_seguro = secure_filename(archivo.filename)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_final = f"{timestamp}_{nombre_seguro}"
    ruta = os.path.join(app.config["UPLOAD_FOLDER"], nombre_final)

    archivo.save(ruta)

    foto_info = {
        "nombre": nombre_final,
        "ruta": f"/fotos/{nombre_final}",
        "timestamp": datetime.now().isoformat()
    }

    fotos_ref.push(foto_info)

    return jsonify({
        "ok": True,
        "message": "Foto subida correctamente",
        "foto": foto_info
    })

@app.route("/fotos", methods=["GET"])
def listar_fotos():
    lista = []

    if os.path.exists(UPLOAD_FOLDER):
        for nombre in sorted(os.listdir(UPLOAD_FOLDER), reverse=True):
            lista.append({
                "nombre": nombre,
                "url": f"/fotos/{nombre}"
            })

    return jsonify({
        "ok": True,
        "total": len(lista),
        "fotos": lista
    })

@app.route("/fotos/<nombre_archivo>", methods=["GET"])
def ver_foto(nombre_archivo):
    from flask import send_from_directory
    return send_from_directory(app.config["UPLOAD_FOLDER"], nombre_archivo)

@app.route("/historial", methods=["GET"])
def ver_historial():
    data = historial_ref.get()
    return jsonify({
        "ok": True,
        "data": data if data else {}
    })

# =========================================================
# Step 9: Run
# =========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
