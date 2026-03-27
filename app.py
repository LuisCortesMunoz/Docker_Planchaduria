from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
import firebase_admin
from firebase_admin import credentials, db
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = "fotos"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

FIREBASE_CRED_FILE = "/etc/secrets/serviceAccountKey.json"
FIREBASE_DB_URL = "https://practicaplc-4c90b-default-rtdb.firebaseio.com/"

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CRED_FILE)
    firebase_admin.initialize_app(cred, {
        "databaseURL": FIREBASE_DB_URL
    })

estado_ref = db.reference("estado_actual")
historial_ref = db.reference("historial")
fotos_ref = db.reference("fotos")

estado_memoria = {
    "usuario_actual": "",
    "activo": False,
    "cantidad": 0,
    "estado": "Esperando trabajo",
    "updated_at": None
}

def guardar_estado(usuario=None, activo=None, cantidad=None, estado=None):
    global estado_memoria

    if usuario is not None:
        estado_memoria["usuario_actual"] = str(usuario)

    if activo is not None:
        estado_memoria["activo"] = activo

    if cantidad is not None:
        estado_memoria["cantidad"] = cantidad

    if estado is not None:
        estado_memoria["estado"] = estado

    estado_memoria["updated_at"] = datetime.now().isoformat()

    estado_ref.set(estado_memoria)

    historial_ref.push({
        "usuario_actual": estado_memoria["usuario_actual"],
        "activo": estado_memoria["activo"],
        "cantidad": estado_memoria["cantidad"],
        "estado": estado_memoria["estado"],
        "timestamp": estado_memoria["updated_at"]
    })

def cargar_estado():
    global estado_memoria
    try:
        data = estado_ref.get()
        if data:
            estado_memoria = data
    except Exception as e:
        print("Error al cargar estado:", e)

cargar_estado()

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "message": "Backend Render activo",
        "endpoints": [
            "/estado",
            "/set_usuario",
            "/set_cantidad",
            "/activar_plc",
            "/desactivar_plc",
            "/subir_foto",
            "/fotos_usuario/<usuario>",
            "/fotos/<nombre_archivo>"
        ]
    })

@app.route("/estado", methods=["GET"])
def obtener_estado():
    return jsonify({
        "ok": True,
        "data": estado_memoria
    })

@app.route("/set_usuario", methods=["POST"])
def set_usuario():
    data = request.get_json(silent=True)

    if not data or "usuario" not in data:
        return jsonify({"ok": False, "message": "Debes enviar usuario"}), 400

    usuario = str(data.get("usuario")).strip()

    if not usuario.isdigit() or len(usuario) > 5:
        return jsonify({"ok": False, "message": "Usuario inválido, máximo 5 dígitos"}), 400

    guardar_estado(usuario=usuario, estado=f"Usuario actual: {usuario}")

    return jsonify({
        "ok": True,
        "message": f"Usuario {usuario} guardado correctamente",
        "data": estado_memoria
    })

@app.route("/set_cantidad", methods=["POST"])
def set_cantidad():
    data = request.get_json(silent=True)

    if not data or "cantidad" not in data or "usuario" not in data:
        return jsonify({"ok": False, "message": "Debes enviar usuario y cantidad"}), 400

    usuario = str(data.get("usuario")).strip()
    cantidad = data.get("cantidad")

    if not usuario.isdigit() or len(usuario) > 5:
        return jsonify({"ok": False, "message": "Usuario inválido, máximo 5 dígitos"}), 400

    if not isinstance(cantidad, int) or cantidad <= 0:
        return jsonify({"ok": False, "message": "Cantidad inválida"}), 400

    guardar_estado(
        usuario=usuario,
        cantidad=cantidad,
        estado=f"Usuario {usuario}: cantidad actualizada a {cantidad}"
    )

    return jsonify({
        "ok": True,
        "message": "Cantidad enviada a la base de datos",
        "data": estado_memoria
    })

@app.route("/activar_plc", methods=["POST"])
def activar_plc():
    data = request.get_json(silent=True)

    if not data or "usuario" not in data:
        return jsonify({"ok": False, "message": "Debes enviar usuario"}), 400

    usuario = str(data.get("usuario")).strip()

    if not usuario.isdigit() or len(usuario) > 5:
        return jsonify({"ok": False, "message": "Usuario inválido, máximo 5 dígitos"}), 400

    guardar_estado(
        usuario=usuario,
        activo=True,
        estado=f"Proceso activo para usuario {usuario}"
    )

    return jsonify({
        "ok": True,
        "message": f"PLC activado para usuario {usuario}",
        "data": estado_memoria
    })

@app.route("/desactivar_plc", methods=["POST"])
def desactivar_plc():
    guardar_estado(
        activo=False,
        estado="Proceso desactivado"
    )

    return jsonify({
        "ok": True,
        "message": "PLC desactivado correctamente",
        "data": estado_memoria
    })

@app.route("/subir_foto", methods=["POST"])
def subir_foto():
    if "foto" not in request.files:
        return jsonify({"ok": False, "message": "No se recibió archivo"}), 400

    usuario = str(request.form.get("usuario", "")).strip()

    if not usuario.isdigit() or len(usuario) > 5:
        return jsonify({"ok": False, "message": "Usuario inválido, máximo 5 dígitos"}), 400

    archivo = request.files["foto"]

    if archivo.filename == "":
        return jsonify({"ok": False, "message": "Archivo vacío"}), 400

    now = datetime.now()
    fecha = now.strftime("%Y-%m-%d")
    hora = now.strftime("%H:%M:%S")
    stamp = now.strftime("%Y%m%d_%H%M%S")

    nombre_seguro = secure_filename(archivo.filename)
    nombre_final = f"u{usuario}_{stamp}_{nombre_seguro}"
    ruta = os.path.join(app.config["UPLOAD_FOLDER"], nombre_final)

    archivo.save(ruta)

    foto_info = {
        "usuario": usuario,
        "nombre": nombre_final,
        "ruta": f"/fotos/{nombre_final}",
        "fecha": fecha,
        "hora": hora,
        "timestamp": now.isoformat()
    }

    fotos_ref.push(foto_info)

    return jsonify({
        "ok": True,
        "message": "Foto subida correctamente",
        "foto": foto_info
    })

@app.route("/fotos_usuario/<usuario>", methods=["GET"])
def fotos_usuario(usuario):
    usuario = str(usuario).strip()

    if not usuario.isdigit() or len(usuario) > 5:
        return jsonify({"ok": False, "message": "Usuario inválido"}), 400

    data = fotos_ref.get()
    lista = []

    if data:
        for _, foto in data.items():
            if str(foto.get("usuario", "")) == usuario:
                lista.append({
                    "usuario": foto.get("usuario", ""),
                    "nombre": foto.get("nombre", ""),
                    "url": foto.get("ruta", ""),
                    "fecha": foto.get("fecha", ""),
                    "hora": foto.get("hora", "")
                })

    lista.sort(key=lambda x: x["nombre"], reverse=True)

    return jsonify({
        "ok": True,
        "usuario": usuario,
        "total": len(lista),
        "fotos": lista
    })

@app.route("/fotos/<nombre_archivo>", methods=["GET"])
def ver_foto(nombre_archivo):
    return send_from_directory(app.config["UPLOAD_FOLDER"], nombre_archivo)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
