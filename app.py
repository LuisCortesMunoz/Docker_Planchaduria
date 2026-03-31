import os
import io
import json
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, make_response
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, auth, firestore, storage, initialize_app

# =========================================================
# CONFIGURACIÓN GENERAL
# =========================================================
app = Flask(__name__)
CORS(app, supports_credentials=True)

SESSION_COOKIE_NAME = "pe_session"
SESSION_DAYS = 7

# Variables de entorno (Configura estas en el Dashboard de Render)
PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "db-planchaduria")
STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "db-planchaduria.appspot.com")
ADMIN_UID = os.environ.get("ADMIN_UID", "HrGtBnzEtBXLK19YpeI8wTAaSM42")
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY", "").strip()

# RUTA DEL SECRET FILE EN RENDER
FIREBASE_CRED_FILE = "/etc/secrets/serviceAccountKey.json"

db = None
bucket = None

# =========================================================
# HELPERS Y SERIALIZACIÓN
# =========================================================
def mexico_now():
    return datetime.now(ZoneInfo("America/Mexico_City"))

def json_error(message, status=400):
    return jsonify({"ok": False, "message": message}), status

def serialize_firestore_value(value):
    if value is None: return None
    if isinstance(value, dict): return {k: serialize_firestore_value(v) for k, v in value.items()}
    if isinstance(value, list): return [serialize_firestore_value(v) for v in value]
    if hasattr(value, "isoformat"):
        try: return value.isoformat()
        except: pass
    return value

def serialize_firestore_doc(data):
    if not data: return {}
    return {k: serialize_firestore_value(v) for k, v in data.items()}

# =========================================================
# INICIALIZACIÓN DE FIREBASE (CORREGIDA)
# =========================================================
def load_firebase_credential():
    # 1. Prioridad: Secret File (Render)
    if os.path.exists(FIREBASE_CRED_FILE):
        print(f"✅ Cargando desde Secret File: {FIREBASE_CRED_FILE}")
        return credentials.Certificate(FIREBASE_CRED_FILE)

    # 2. Backup: Variables de Entorno
    json_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if json_env:
        print("✅ Usando FIREBASE_SERVICE_ACCOUNT_JSON")
        return credentials.Certificate(json.loads(json_env))

    raise RuntimeError("No se encontró serviceAccountKey.json en /etc/secrets/ ni variables de entorno.")

def init_firebase():
    global db, bucket
    try:
        cred = load_firebase_credential()
        if not firebase_admin._apps:
            initialize_app(cred, {"storageBucket": STORAGE_BUCKET})
        
        db = firestore.client()
        bucket = storage.bucket()
        print("🚀 Firebase conectado exitosamente")
    except Exception as e:
        print(f"❌ ERROR FATAL EN FIREBASE: {e}")
        # No relanzamos el error aquí para evitar que el worker muera sin dar logs
        # pero las rutas que usen 'db' fallarán con 500.

init_firebase()

# =========================================================
# LÓGICA DE SESIÓN Y AUTH
# =========================================================
def get_session_user():
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie: return None
    try:
        decoded = auth.verify_session_cookie(session_cookie, check_revoked=False)
        uid = decoded["uid"]
        user_record = auth.get_user(uid)
        user_doc = db.collection("usuarios").document(uid).get()
        u_data = user_doc.to_dict() if user_doc.exists else {}
        return {
            "uid": uid,
            "email": user_record.email,
            "isAdmin": uid == ADMIN_UID,
            "nombre": u_data.get("nombre", ""),
            "nombreCompleto": f"{u_data.get('nombre', '')} {u_data.get('apellido', '')}".strip()
        }
    except: return None

def require_auth():
    user = get_session_user()
    if not user: return None, json_error("No autenticado", 401)
    return user, None

def sign_in_email_password(email, password):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
    r = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True}, timeout=15)
    if not r.ok: raise RuntimeError(r.json().get("error", {}).get("message", "Login error"))
    return r.json()

# =========================================================
# RUTAS DE PROCESO (PC MAESTRO / UR3)
# =========================================================
@app.post("/process/start")
def process_start():
    user, err = require_auth()
    if err: return err
    
    data = request.get_json(silent=True) or {}
    order_id = data.get("orderId")
    folio = data.get("folio")
    cantidad = int(data.get("cantidad", 0))

    if not order_id or not folio or cantidad < 1:
        return json_error("Datos incompletos para iniciar el UR3")

    # Guardamos en la colección de control que la PC Maestro está escuchando
    db.collection("control").document("proceso_actual").set({
        "active": True,
        "status": "queued",
        "orderId": order_id,
        "folio": folio,
        "cantidad": cantidad,
        "requestedBy": user["uid"],
        "requestedAt": firestore.SERVER_TIMESTAMP
    })
    return jsonify({"ok": True, "message": "Orden enviada a la PC Maestro"})

# =========================================================
# RUTAS DE ÓRDENES Y FOTOS
# =========================================================
@app.post("/orders")
def orders_create():
    user, err = require_auth()
    if err: return err
    
    data = request.get_json(silent=True) or {}
    # Lógica de folio simple
    snap = db.collection("pedidos").get()
    folio = f"#{str(len(snap) + 1).zfill(5)}"

    payload = {
        **data,
        "Folio": folio,
        "clienteUid": user["uid"],
        "Estado": "pendiente",
        "FechaCreacion": firestore.SERVER_TIMESTAMP
    }
    ref = db.collection("pedidos").document()
    ref.set(payload)
    return jsonify({"ok": True, "orderId": ref.id, "folio": folio})

@app.get("/orders/<order_id>/photos")
def get_order_photos(order_id):
    _, err = require_auth()
    if err: return err
    
    docs = db.collection("pedidos").document(order_id).collection("fotos").order_by("timestamp").stream()
    photos = [serialize_firestore_doc(d.to_dict()) for d in docs]
    return jsonify({"ok": True, "photos": photos})

# =========================================================
# HEALTH CHECK
# =========================================================
@app.get("/health")
def health():
    return jsonify({"ok": True, "status": "online", "db": db is not None})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
