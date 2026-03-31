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
# Importante: Ajusta 'origins' con la URL de tu frontend si es necesario
CORS(app, supports_credentials=True)

SESSION_COOKIE_NAME = "pe_session"
SESSION_DAYS = 7

# Variables de entorno (Configúralas en el Dashboard de Render -> Environment)
PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "db-planchaduria")
STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "db-planchaduria.firebasestorage.app")
ADMIN_UID = os.environ.get("ADMIN_UID", "HrGtBnzEtBXLK19YpeI8wTAaSM42")
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY", "").strip()

# Ruta del Secret File en Render
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
# INICIALIZACIÓN DE FIREBASE
# =========================================================
def load_firebase_credential():
    # 1. Prioridad: Secret File (Render)
    if os.path.exists(FIREBASE_CRED_FILE):
        print(f"✅ Cargando desde Secret File: {FIREBASE_CRED_FILE}")
        return credentials.Certificate(FIREBASE_CRED_FILE)

    # 2. Backup: Variable de Entorno JSON
    json_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if json_env:
        print("✅ Usando FIREBASE_SERVICE_ACCOUNT_JSON")
        return credentials.Certificate(json.loads(json_env))

    raise RuntimeError("No se encontró la llave de Firebase. Revisa /etc/secrets/ en Render.")

def init_firebase():
    global db, bucket
    try:
        cred = load_firebase_credential()
        if not firebase_admin._apps:
            initialize_app(cred, {"storageBucket": STORAGE_BUCKET})
        
        db = firestore.client()
        bucket = storage.bucket()
        print("🚀 Firebase y Storage conectados exitosamente")
    except Exception as e:
        print(f"❌ ERROR CRÍTICO EN FIREBASE: {e}")

init_firebase()

# =========================================================
# LÓGICA DE SESIÓN
# =========================================================
def get_session_user():
    session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_cookie: return None
    try:
        decoded = auth.verify_session_cookie(session_cookie, check_revoked=False)
        uid = decoded["uid"]
        user_doc = db.collection("usuarios").document(uid).get()
        u_data = user_doc.to_dict() if user_doc.exists else {}
        return {
            "uid": uid,
            "isAdmin": uid == ADMIN_UID,
            "nombre": u_data.get("nombre", "Usuario"),
            "email": u_data.get("email", "")
        }
    except: return None

def require_auth():
    user = get_session_user()
    if not user: return None, json_error("No autenticado", 401)
    return user, None

# =========================================================
# RUTAS PRINCIPALES (ENDPOINTS)
# =========================================================

@app.route("/")
def index():
    """Ruta raíz para evitar el error 'Not Found'"""
    return jsonify({
        "ok": True, 
        "message": "Backend de Planchado Express activo",
        "status": "online"
    })

@app.get("/health")
def health():
    """Ruta de diagnóstico"""
    return jsonify({
        "ok": True, 
        "db_connected": db is not None,
        "bucket": STORAGE_BUCKET
    })

# --- CONTROL DE PROCESO (PC MAESTRO / UR3) ---
@app.post("/process/start")
def process_start():
    user, err = require_auth()
    if err: return err
    
    data = request.get_json(silent=True) or {}
    order_id = data.get("orderId")
    folio = data.get("folio")
    cantidad = int(data.get("cantidad", 0))

    if not order_id or not folio or cantidad < 1:
        return json_error("Datos incompletos para iniciar rutina")

    # Escribimos en la colección que la PC Maestro está monitoreando
    db.collection("control").document("proceso_actual").set({
        "active": True,
        "status": "queued",
        "orderId": order_id,
        "folio": folio,
        "cantidad": cantidad,
        "requestedBy": user["uid"],
        "requestedAt": firestore.SERVER_TIMESTAMP
    })
    
    # También actualizamos el estado del pedido
    db.collection("pedidos").document(order_id).update({"Estado": "en_proceso"})
    
    return jsonify({"ok": True, "message": "Orden enviada a la PC Maestro exitosamente"})

# --- GESTIÓN DE FOTOS (PRENDAS REGISTRADAS) ---
@app.get("/orders/<order_id>/photos")
def get_order_photos(order_id):
    _, err = require_auth()
    if err: return err
    
    # Buscamos en la subcolección de fotos del pedido
    docs = db.collection("pedidos").document(order_id).collection("fotos").order_by("timestamp", direction=firestore.Query.DESCENDING).stream()
    
    photos = []
    for d in docs:
        p_data = serialize_firestore_doc(d.to_dict())
        p_data["id"] = d.id
        photos.append(p_data)
        
    return jsonify({"ok": True, "photos": photos})

# --- CREACIÓN DE PEDIDOS ---
@app.post("/orders")
def orders_create():
    user, err = require_auth()
    if err: return err
    
    data = request.get_json(silent=True) or {}
    
    # Generación simple de Folio basado en conteo
    snap = db.collection("pedidos").get()
    nuevo_folio = f"#{str(len(snap) + 1).zfill(5)}"

    payload = {
        **data,
        "Folio": nuevo_folio,
        "clienteUid": user["uid"],
        "Estado": "pendiente",
        "FechaCreacion": firestore.SERVER_TIMESTAMP,
        "Validado": False
    }
    
    ref = db.collection("pedidos").document()
    ref.set(payload)
    
    return jsonify({"ok": True, "orderId": ref.id, "folio": nuevo_folio})

# =========================================================
# EJECUCIÓN
# =========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
