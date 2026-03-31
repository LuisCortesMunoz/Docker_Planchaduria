import os
import json
from functools import wraps
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, auth, firestore

# Step 1: Crear app Flask
app = Flask(__name__)

# Step 2: Configurar CORS
ALLOWED_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://127.0.0.1:5500,http://localhost:5500"
).split(",")

CORS(
    app,
    resources={r"/api/*": {"origins": [origin.strip() for origin in ALLOWED_ORIGINS]}},
    supports_credentials=True
)

# Step 3: UID admin
ADMIN_UID = os.getenv("ADMIN_UID", "HrGtBnzEtBXLK19YpeI8wTAaSM42")

# Step 4: Inicializar Firebase Admin
def init_firebase():
    if firebase_admin._apps:
        return firestore.client()

    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

    if service_account_json:
        info = json.loads(service_account_json)
    else:
        private_key = os.getenv("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")
        info = {
            "type": os.getenv("FIREBASE_TYPE", "service_account"),
            "project_id": os.getenv("FIREBASE_PROJECT_ID"),
            "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
            "private_key": private_key,
            "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
            "client_id": os.getenv("FIREBASE_CLIENT_ID"),
            "auth_uri": os.getenv("FIREBASE_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri": os.getenv("FIREBASE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
            "auth_provider_x509_cert_url": os.getenv(
                "FIREBASE_AUTH_PROVIDER_CERT_URL",
                "https://www.googleapis.com/oauth2/v1/certs"
            ),
            "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_CERT_URL")
        }

    if not info.get("project_id") or not info.get("private_key") or not info.get("client_email"):
        raise RuntimeError("Faltan variables de entorno de Firebase.")

    cred = credentials.Certificate(info)
    firebase_admin.initialize_app(cred)
    return firestore.client()

db = init_firebase()

# Step 5: Helpers generales
def now_utc():
    return datetime.now(timezone.utc)

def serialize_data(data):
    if isinstance(data, dict):
        return {k: serialize_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [serialize_data(v) for v in data]
    if isinstance(data, datetime):
        return data.isoformat()
    return data

def doc_to_dict(doc):
    data = doc.to_dict() or {}
    data["id"] = doc.id
    return serialize_data(data)

def get_bearer_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return auth_header.split("Bearer ", 1)[1].strip()

def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = get_bearer_token()
        if not token:
            return jsonify({"ok": False, "error": "Falta token de autorización"}), 401
        try:
            decoded = auth.verify_id_token(token)
            request.user = decoded
            return fn(*args, **kwargs)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Token inválido: {str(e)}"}), 401
    return wrapper

def admin_required(fn):
    @wraps(fn)
    @auth_required
    def wrapper(*args, **kwargs):
        uid = request.user.get("uid")
        if uid != ADMIN_UID:
            return jsonify({"ok": False, "error": "Acceso solo para administrador"}), 403
        return fn(*args, **kwargs)
    return wrapper

def full_name(profile):
    nombre = (profile.get("nombre") or "").strip()
    apellido = (profile.get("apellido") or "").strip()
    return f"{nombre} {apellido}".strip()

# Step 6: Generar folio seguro con contador
def generate_folio_transaction():
    meta_ref = db.collection("_meta").document("pedidos_counter")

    @firestore.transactional
    def update_in_transaction(transaction, ref):
        snapshot = ref.get(transaction=transaction)
        last_counter = 0

        if snapshot.exists:
            last_counter = int(snapshot.to_dict().get("ultimo_contador", 0))

        new_counter = last_counter + 1
        transaction.set(ref, {"ultimo_contador": new_counter}, merge=True)

        folio = "#" + str(new_counter).zfill(5)
        return folio, new_counter

    transaction = db.transaction()
    return update_in_transaction(transaction, meta_ref)

# Step 7: Rutas base
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "message": "Backend Planchado Express activo"
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "status": "healthy"
    })

# Step 8: Perfil usuario
@app.route("/api/me/profile", methods=["POST"])
@auth_required
def save_profile():
    uid = request.user["uid"]
    email_from_token = request.user.get("email", "")

    payload = request.get_json(silent=True) or {}

    data = {
        "nombre": (payload.get("nombre") or "").strip(),
        "apellido": (payload.get("apellido") or "").strip(),
        "telefono": (payload.get("telefono") or "").strip(),
        "email": (payload.get("email") or email_from_token).strip(),
        "uid": uid,
        "actualizadoEn": now_utc()
    }

    ref = db.collection("usuarios").document(uid)
    old = ref.get()

    if not old.exists:
        data["FechaCreacion"] = now_utc()

    ref.set(data, merge=True)
    saved = ref.get().to_dict() or {}

    return jsonify({
        "ok": True,
        "profile": serialize_data(saved)
    })

@app.route("/api/me/profile", methods=["GET"])
@auth_required
def get_profile():
    uid = request.user["uid"]
    ref = db.collection("usuarios").document(uid).get()

    if not ref.exists:
        return jsonify({
            "ok": True,
            "profile": {
                "uid": uid,
                "email": request.user.get("email", "")
            }
        })

    return jsonify({
        "ok": True,
        "profile": serialize_data(ref.to_dict())
    })

# Step 9: Cliente crea pedido
@app.route("/api/orders", methods=["POST"])
@auth_required
def create_order_from_client():
    uid = request.user["uid"]
    email = request.user.get("email", "")

    payload = request.get_json(silent=True) or {}

    tipo_prenda = (payload.get("tipoPrenda") or "").strip()
    material = (payload.get("material") or "").strip()
    cantidad = int(payload.get("cantidad") or 1)
    fecha_entrega = (payload.get("FechaEntrega") or "").strip()
    notas = (payload.get("notas") or "").strip()

    if not tipo_prenda:
        return jsonify({"ok": False, "error": "tipoPrenda es obligatorio"}), 400
    if cantidad < 1:
        return jsonify({"ok": False, "error": "cantidad debe ser al menos 1"}), 400
    if not fecha_entrega:
        return jsonify({"ok": False, "error": "FechaEntrega es obligatoria"}), 400

    profile_doc = db.collection("usuarios").document(uid).get()
    profile = profile_doc.to_dict() if profile_doc.exists else {}
    cliente_name = full_name(profile) or request.user.get("name") or email

    folio, contador = generate_folio_transaction()

    data = {
        "Folio": folio,
        "Contador": contador,
        "Estado": "pendiente",
        "FolioIngresado": folio,
        "Validado": False,
        "cliente": cliente_name,
        "clienteUid": uid,
        "tipoPrenda": tipo_prenda,
        "material": material,
        "cantidad": cantidad,
        "fechaIngreso": datetime.now().date().isoformat(),
        "FechaEntrega": fecha_entrega,
        "notas": notas,
        "telefono": profile.get("telefono", ""),
        "precio": None,
        "origenCliente": True,
        "FechaCreacion": now_utc(),
        "actualizadoEn": now_utc()
    }

    doc_ref = db.collection("pedidos").document()
    doc_ref.set(data)

    return jsonify({
        "ok": True,
        "order": doc_to_dict(doc_ref.get())
    }), 201

# Step 10: Cliente ve sus pedidos
@app.route("/api/orders/mine", methods=["GET"])
@auth_required
def get_my_orders():
    uid = request.user["uid"]

    docs = db.collection("pedidos").where("clienteUid", "==", uid).stream()
    items = [doc_to_dict(doc) for doc in docs]
    items.sort(key=lambda x: x.get("Contador", 0), reverse=True)

    return jsonify({
        "ok": True,
        "orders": items
    })

# Step 11: Tracking por folio
@app.route("/api/orders/track/<folio>", methods=["GET"])
def track_order(folio):
    folio = folio.strip().upper()

    docs = list(
        db.collection("pedidos")
        .where("Folio", "==", folio)
        .limit(1)
        .stream()
    )

    if not docs:
        return jsonify({
            "ok": False,
            "error": f"No se encontró pedido con folio {folio}"
        }), 404

    return jsonify({
        "ok": True,
        "order": doc_to_dict(docs[0])
    })

# Step 12: Admin ve todos los pedidos
@app.route("/api/admin/orders", methods=["GET"])
@admin_required
def admin_get_orders():
    docs = db.collection("pedidos").stream()
    items = [doc_to_dict(doc) for doc in docs]
    items.sort(key=lambda x: x.get("Contador", 0), reverse=True)

    return jsonify({
        "ok": True,
        "orders": items
    })

# Step 13: Admin crea pedido
@app.route("/api/admin/orders", methods=["POST"])
@admin_required
def admin_create_order():
    payload = request.get_json(silent=True) or {}

    cliente = (payload.get("cliente") or "").strip()
    telefono = (payload.get("telefono") or "").strip()
    tipo_prenda = (payload.get("tipoPrenda") or "").strip()
    material = (payload.get("material") or "").strip()
    cantidad = int(payload.get("cantidad") or 1)
    precio = payload.get("precio")
    fecha_ingreso = (payload.get("fechaIngreso") or "").strip()
    fecha_entrega = (payload.get("FechaEntrega") or "").strip()
    notas = (payload.get("notas") or "").strip()

    if not cliente:
        return jsonify({"ok": False, "error": "cliente es obligatorio"}), 400
    if not tipo_prenda:
        return jsonify({"ok": False, "error": "tipoPrenda es obligatoria"}), 400
    if cantidad < 1:
        return jsonify({"ok": False, "error": "cantidad debe ser al menos 1"}), 400
    if not fecha_ingreso:
        return jsonify({"ok": False, "error": "fechaIngreso es obligatoria"}), 400
    if not fecha_entrega:
        return jsonify({"ok": False, "error": "FechaEntrega es obligatoria"}), 400

    folio, contador = generate_folio_transaction()

    data = {
        "Folio": folio,
        "Contador": contador,
        "Estado": "pendiente",
        "FolioIngresado": folio,
        "Validado": False,
        "cliente": cliente,
        "clienteUid": None,
        "tipoPrenda": tipo_prenda,
        "material": material,
        "cantidad": cantidad,
        "fechaIngreso": fecha_ingreso,
        "FechaEntrega": fecha_entrega,
        "notas": notas,
        "telefono": telefono,
        "precio": precio,
        "origenCliente": False,
        "FechaCreacion": now_utc(),
        "actualizadoEn": now_utc()
    }

    doc_ref = db.collection("pedidos").document()
    doc_ref.set(data)

    return jsonify({
        "ok": True,
        "order": doc_to_dict(doc_ref.get())
    }), 201

# Step 14: Admin actualiza pedido
@app.route("/api/admin/orders/<order_id>", methods=["PUT"])
@admin_required
def admin_update_order(order_id):
    payload = request.get_json(silent=True) or {}
    ref = db.collection("pedidos").document(order_id)
    snap = ref.get()

    if not snap.exists:
        return jsonify({"ok": False, "error": "Pedido no encontrado"}), 404

    allowed_fields = {
        "cliente", "telefono", "tipoPrenda", "material", "cantidad",
        "precio", "fechaIngreso", "FechaEntrega", "notas", "Estado"
    }

    update_data = {}
    for key, value in payload.items():
        if key in allowed_fields:
            update_data[key] = value

    if "Estado" in update_data:
        update_data["Validado"] = update_data["Estado"] == "entregado"

    update_data["actualizadoEn"] = now_utc()

    ref.update(update_data)

    return jsonify({
        "ok": True,
        "order": doc_to_dict(ref.get())
    })

# Step 15: Admin elimina pedido
@app.route("/api/admin/orders/<order_id>", methods=["DELETE"])
@admin_required
def admin_delete_order(order_id):
    ref = db.collection("pedidos").document(order_id)
    snap = ref.get()

    if not snap.exists:
        return jsonify({"ok": False, "error": "Pedido no encontrado"}), 404

    ref.delete()

    return jsonify({
        "ok": True,
        "message": "Pedido eliminado"
    })

# Step 16: Admin ve clientes
@app.route("/api/admin/clients", methods=["GET"])
@admin_required
def admin_get_clients():
    user_docs = list(db.collection("usuarios").stream())
    order_docs = list(db.collection("pedidos").stream())

    pedidos_por_uid = {}
    for doc in order_docs:
      data = doc.to_dict() or {}
      uid = data.get("clienteUid")
      if uid:
          pedidos_por_uid[uid] = pedidos_por_uid.get(uid, 0) + 1

    clients = []
    for doc in user_docs:
        data = doc.to_dict() or {}
        nombre_completo = full_name(data) or data.get("email", "—")
        clients.append({
            "uid": doc.id,
            "nombreCompleto": nombre_completo,
            "nombre": data.get("nombre", ""),
            "apellido": data.get("apellido", ""),
            "email": data.get("email", ""),
            "telefono": data.get("telefono", ""),
            "pedidos": pedidos_por_uid.get(doc.id, 0)
        })

    clients.sort(key=lambda x: (x.get("nombreCompleto") or "").lower())

    return jsonify({
        "ok": True,
        "clients": serialize_data(clients)
    })

# Step 17: Ejecutar app
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
