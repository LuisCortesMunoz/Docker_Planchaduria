# =========================================================
# Planchado Express - Backend Render
# Soporta Firebase key desde:
# 1) FIREBASE_SERVICE_ACCOUNT_JSON
# 2) FIREBASE_SERVICE_ACCOUNT_B64
# 3) /etc/secrets/serviceAccountKey.json
# =========================================================

import os
import io
import json
import base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, auth, firestore, storage, initialize_app

# =========================================================
# CONFIG GENERAL
# =========================================================
app = Flask(__name__)
CORS(app, supports_credentials=True)

SESSION_COOKIE_NAME = "pe_session"
SESSION_DAYS = 7

PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "db-planchaduria")
STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "db-planchaduria.firebasestorage.app")
ADMIN_UID = os.environ.get("ADMIN_UID", "HrGtBnzEtBXLK19YpeI8wTAaSM42")

# Para login por email/password con Firebase Auth REST
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY", "").strip()

# Ruta opcional si usas Secret File
FIREBASE_CRED_FILE = "/etc/secrets/serviceAccountKey.json"

db = None
bucket = None

# =========================================================
# HELPERS DE TIEMPO
# =========================================================
def mexico_now():
    return datetime.now(ZoneInfo("America/Mexico_City"))

def json_error(message, status=400):
    return jsonify({"ok": False, "message": message}), status

# =========================================================
# CARGA DE FIREBASE
# =========================================================
def load_firebase_credential():
    """
    Intenta cargar credenciales en este orden:
    1) FIREBASE_SERVICE_ACCOUNT_JSON
    2) FIREBASE_SERVICE_ACCOUNT_B64
    3) /etc/secrets/serviceAccountKey.json
    """
    json_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    b64_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT_B64", "").strip()

    if json_env:
        print("✅ Usando FIREBASE_SERVICE_ACCOUNT_JSON desde Environment")
        data = json.loads(json_env)
        return credentials.Certificate(data)

    if b64_env:
        print("✅ Usando FIREBASE_SERVICE_ACCOUNT_B64 desde Environment")
        decoded = base64.b64decode(b64_env).decode("utf-8")
        data = json.loads(decoded)
        return credentials.Certificate(data)

    if os.path.exists(FIREBASE_CRED_FILE):
        print(f"✅ Usando Secret File: {FIREBASE_CRED_FILE}")
        return credentials.Certificate(FIREBASE_CRED_FILE)

    raise RuntimeError(
        "No se encontró la credencial de Firebase. "
        "Usa FIREBASE_SERVICE_ACCOUNT_JSON, FIREBASE_SERVICE_ACCOUNT_B64 "
        "o un Secret File en /etc/secrets/serviceAccountKey.json"
    )

def init_firebase():
    global db, bucket

    print("========================================")
    print("Iniciando backend de Planchado Express")
    print("PROJECT_ID:", PROJECT_ID)
    print("STORAGE_BUCKET:", STORAGE_BUCKET)
    print("Existe FIREBASE_WEB_API_KEY:", bool(FIREBASE_WEB_API_KEY))
    print("Existe FIREBASE_SERVICE_ACCOUNT_JSON:", bool(os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")))
    print("Existe FIREBASE_SERVICE_ACCOUNT_B64:", bool(os.environ.get("FIREBASE_SERVICE_ACCOUNT_B64")))
    print("Existe Secret File:", os.path.exists(FIREBASE_CRED_FILE))
    print("========================================")

    cred = load_firebase_credential()

    if not firebase_admin._apps:
        initialize_app(cred, {
            "storageBucket": STORAGE_BUCKET
        })

    db = firestore.client()
    bucket = storage.bucket()

    print("✅ Firebase inicializado correctamente")

# Inicializar al arrancar
try:
    init_firebase()
except Exception as e:
    print("❌ ERROR AL INICIALIZAR FIREBASE:", repr(e))
    raise

# =========================================================
# HELPERS DE SESIÓN
# =========================================================
def get_session_user():
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie:
        return None

    try:
        decoded = auth.verify_session_cookie(session_cookie, check_revoked=False)
        uid = decoded["uid"]
        user = auth.get_user(uid)

        user_doc = db.collection("usuarios").document(uid).get()
        user_data = user_doc.to_dict() if user_doc.exists else {}

        return {
            "uid": uid,
            "email": user.email,
            "isAdmin": uid == ADMIN_UID,
            "nombre": user_data.get("nombre", ""),
            "apellido": user_data.get("apellido", ""),
            "telefono": user_data.get("telefono", ""),
            "nombreCompleto": f"{user_data.get('nombre', '')} {user_data.get('apellido', '')}".strip()
        }
    except Exception as e:
        print("Error verificando sesión:", repr(e))
        return None

def require_auth():
    user = get_session_user()
    if not user:
        return None, json_error("No autenticado", 401)
    return user, None

def require_admin():
    user, err = require_auth()
    if err:
        return None, err
    if not user["isAdmin"]:
        return None, json_error("No autorizado", 403)
    return user, None

def create_session_response(id_token, user_payload):
    expires_in = timedelta(days=SESSION_DAYS)
    session_cookie = auth.create_session_cookie(id_token, expires_in=expires_in)

    resp = make_response(jsonify({
        "ok": True,
        "user": user_payload
    }))

    resp.set_cookie(
        SESSION_COOKIE_NAME,
        session_cookie,
        max_age=int(expires_in.total_seconds()),
        httponly=True,
        secure=True,
        samesite="None",
        path="/"
    )
    return resp

# =========================================================
# HELPERS AUTH EMAIL/PASSWORD
# =========================================================
def sign_in_email_password(email, password):
    if not FIREBASE_WEB_API_KEY:
        raise RuntimeError("Falta FIREBASE_WEB_API_KEY en Environment")

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
    r = requests.post(url, json={
        "email": email,
        "password": password,
        "returnSecureToken": True
    }, timeout=20)

    data = r.json()

    if not r.ok:
        raise RuntimeError(data.get("error", {}).get("message", "Error de login"))

    return data

# =========================================================
# HELPERS STORAGE / NEGOCIO
# =========================================================
def upload_photo_to_storage(file_bytes: bytes, file_name: str, content_type: str = "image/jpeg"):
    blob = bucket.blob(f"pedidos/{file_name}")
    blob.upload_from_file(io.BytesIO(file_bytes), content_type=content_type)
    blob.make_public()
    return blob.public_url

def next_folio():
    snap = db.collection("pedidos").stream()
    contador = sum(1 for _ in snap) + 1
    folio = "#" + str(contador).zfill(5)
    return folio, contador

# =========================================================
# HEALTH
# =========================================================
@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "message": "Backend activo",
        "projectId": PROJECT_ID,
        "storageBucket": STORAGE_BUCKET
    })

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "firebase": True
    })

# =========================================================
# AUTH
# =========================================================
@app.post("/auth/register")
def auth_register():
    data = request.get_json(silent=True) or {}

    nombre = data.get("nombre", "").strip()
    apellido = data.get("apellido", "").strip()
    email = data.get("email", "").strip()
    telefono = data.get("telefono", "").strip()
    password = data.get("password", "")

    if not nombre or not apellido or not email or not password:
        return json_error("Completa todos los campos obligatorios")

    if len(password) < 6:
        return json_error("La contraseña debe tener al menos 6 caracteres")

    try:
        user = auth.create_user(email=email, password=password)

        db.collection("usuarios").document(user.uid).set({
            "uid": user.uid,
            "nombre": nombre,
            "apellido": apellido,
            "email": email,
            "telefono": telefono,
            "FechaCreacion": firestore.SERVER_TIMESTAMP
        })

        login_data = sign_in_email_password(email, password)

        user_payload = {
            "uid": user.uid,
            "email": email,
            "isAdmin": user.uid == ADMIN_UID,
            "nombre": nombre,
            "apellido": apellido,
            "telefono": telefono,
            "nombreCompleto": f"{nombre} {apellido}".strip()
        }

        return create_session_response(login_data["idToken"], user_payload)

    except Exception as e:
        return json_error(str(e), 400)

@app.post("/auth/login")
def auth_login():
    data = request.get_json(silent=True) or {}

    email = data.get("email", "").strip()
    password = data.get("password", "")

    if not email or not password:
        return json_error("Completa correo y contraseña")

    try:
        login_data = sign_in_email_password(email, password)
        uid = login_data["localId"]

        user_record = auth.get_user(uid)
        user_doc = db.collection("usuarios").document(uid).get()
        u = user_doc.to_dict() if user_doc.exists else {}

        user_payload = {
            "uid": uid,
            "email": user_record.email,
            "isAdmin": uid == ADMIN_UID,
            "nombre": u.get("nombre", ""),
            "apellido": u.get("apellido", ""),
            "telefono": u.get("telefono", ""),
            "nombreCompleto": f"{u.get('nombre', '')} {u.get('apellido', '')}".strip()
        }

        return create_session_response(login_data["idToken"], user_payload)

    except Exception as e:
        return json_error(str(e), 401)

@app.post("/auth/logout")
def auth_logout():
    resp = jsonify({"ok": True, "message": "Sesión cerrada"})
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=True, samesite="None")
    return resp

@app.get("/auth/me")
def auth_me():
    user = get_session_user()
    return jsonify({"ok": True, "user": user})

# =========================================================
# USERS
# =========================================================
@app.get("/users")
def list_users():
    _, err = require_admin()
    if err:
        return err

    docs = db.collection("usuarios").stream()
    users = [d.to_dict() for d in docs]
    return jsonify({"ok": True, "users": users})

# =========================================================
# ORDERS
# =========================================================
@app.get("/orders/next-folio")
def orders_next_folio():
    _, err = require_auth()
    if err:
        return err

    folio, contador = next_folio()
    return jsonify({"ok": True, "Folio": folio, "Contador": contador})

@app.get("/orders")
def orders_list():
    user, err = require_auth()
    if err:
        return err

    mine = request.args.get("mine") == "1"
    query = db.collection("pedidos").order_by("FechaCreacion", direction=firestore.Query.DESCENDING)

    if mine and not user["isAdmin"]:
        query = query.where("clienteUid", "==", user["uid"])

    docs = query.stream()
    orders = []

    for d in docs:
        item = d.to_dict()
        item["id"] = d.id
        orders.append(item)

    return jsonify({"ok": True, "orders": orders})

@app.get("/orders/by-folio/<folio>")
def orders_by_folio(folio):
    _, err = require_auth()
    if err:
        return err

    docs = db.collection("pedidos").where("Folio", "==", folio).limit(1).stream()
    docs = list(docs)

    if not docs:
        return json_error("Pedido no encontrado", 404)

    d = docs[0]
    order = d.to_dict()
    order["id"] = d.id
    return jsonify({"ok": True, "order": order})

@app.post("/orders")
def orders_create():
    _, err = require_auth()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    folio, contador = next_folio()

    payload = {
        **data,
        "Folio": folio,
        "Contador": contador,
        "Estado": "pendiente",
        "FolioIngresado": folio,
        "Validado": False,
        "FechaCreacion": firestore.SERVER_TIMESTAMP
    }

    ref = db.collection("pedidos").document()
    ref.set(payload)

    saved = payload.copy()
    saved["id"] = ref.id
    saved["FechaCreacion"] = None

    return jsonify({"ok": True, "order": saved})

@app.patch("/orders/<order_id>")
def orders_update(order_id):
    _, err = require_admin()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    data["actualizadoEn"] = firestore.SERVER_TIMESTAMP
    db.collection("pedidos").document(order_id).update(data)

    return jsonify({"ok": True})

@app.delete("/orders/<order_id>")
def orders_delete(order_id):
    _, err = require_admin()
    if err:
        return err

    db.collection("pedidos").document(order_id).delete()
    return jsonify({"ok": True})

@app.get("/orders/<order_id>/photos")
def order_photos(order_id):
    _, err = require_auth()
    if err:
        return err

    docs = db.collection("pedidos").document(order_id).collection("fotos").order_by(
        "timestamp", direction=firestore.Query.DESCENDING
    ).stream()

    photos = []
    for d in docs:
        item = d.to_dict()
        item["id"] = d.id
        photos.append(item)

    return jsonify({"ok": True, "photos": photos})

# =========================================================
# PROCESS CONTROL
# =========================================================
@app.post("/process/start")
def process_start():
    user, err = require_auth()
    if err:
        return err

    data = request.get_json(silent=True) or {}

    order_id = data.get("orderId")
    folio = data.get("folio")
    cantidad = int(data.get("cantidad", 0))
    cliente_uid = data.get("clienteUid")

    if not order_id or not folio or cantidad < 1:
        return json_error("Datos de proceso incompletos")

    db.collection("control").document("proceso_actual").set({
        "active": True,
        "status": "queued",
        "orderId": order_id,
        "folio": folio,
        "cantidad": cantidad,
        "clienteUid": cliente_uid,
        "requestedBy": user["uid"],
        "requestedAt": firestore.SERVER_TIMESTAMP,
        "lastHeartbeat": None
    })

    return jsonify({"ok": True})

@app.get("/process/current")
def process_current():
    doc = db.collection("control").document("proceso_actual").get()
    if not doc.exists:
        return jsonify({"ok": True, "process": None})
    return jsonify({"ok": True, "process": doc.to_dict()})

@app.post("/process/report")
def process_report():
    data = request.get_json(silent=True) or {}

    db.collection("control").document("proceso_actual").set({
        **data,
        "lastHeartbeat": firestore.SERVER_TIMESTAMP
    }, merge=True)

    if data.get("orderId") and data.get("EstadoPedido"):
        db.collection("pedidos").document(data["orderId"]).update({
            "Estado": data["EstadoPedido"]
        })

    return jsonify({"ok": True})

@app.post("/process/finish")
def process_finish():
    data = request.get_json(silent=True) or {}
    order_id = data.get("orderId")

    db.collection("control").document("proceso_actual").set({
        "active": False,
        "status": "idle",
        "finishedAt": firestore.SERVER_TIMESTAMP
    }, merge=True)

    if order_id:
        db.collection("pedidos").document(order_id).update({
            "Estado": "planchado"
        })

    return jsonify({"ok": True})

# =========================================================
# PHOTOS
# =========================================================
@app.post("/photos/upload")
def photos_upload():
    if "foto" not in request.files:
        return json_error("No se recibió archivo")

    order_id = request.form.get("orderId", "").strip()
    folio = request.form.get("folio", "").strip()
    usuario = request.form.get("usuario", "").strip()

    if not order_id or not folio:
        return json_error("orderId y folio son obligatorios")

    file = request.files["foto"]
    raw = file.read()

    now = mexico_now()
    fecha = now.strftime("%Y%m%d")
    hora = now.strftime("%H%M%S")
    file_name = f"{folio}_{fecha}_{hora}.jpg"
    url = upload_photo_to_storage(raw, file_name, file.mimetype or "image/jpeg")

    meta = {
        "orderId": order_id,
        "folio": folio,
        "usuario": usuario,
        "file_name": file_name,
        "fecha": now.strftime("%Y-%m-%d"),
        "hora": now.strftime("%H:%M:%S"),
        "url": url,
        "timestamp": firestore.SERVER_TIMESTAMP
    }

    db.collection("pedidos").document(order_id).collection("fotos").add(meta)
    return jsonify({"ok": True, "photo": meta})

# =========================================================
# MAIN
# =========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
