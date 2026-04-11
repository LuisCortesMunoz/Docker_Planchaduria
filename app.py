from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
import firebase_admin
from firebase_admin import credentials, firestore, db, auth as firebase_auth
import os
import requests
import base64
import secrets
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

# =========================================================
# CONFIGURACIÓN GENERAL
# =========================================================
app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = "fotos"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

FIREBASE_CRED_FILE = os.environ.get("FIREBASE_CRED_FILE", "/etc/secrets/serviceAccountKey.json")
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "https://db-planchaduria-default-rtdb.firebaseio.com")
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY", "")
ADMIN_UID = os.environ.get("ADMIN_UID", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

print("[CONFIG] FIREBASE_CRED_FILE:", FIREBASE_CRED_FILE)
print("[CONFIG] FIREBASE_DB_URL:", FIREBASE_DB_URL)
print("[CONFIG] SMTP_HOST:", SMTP_HOST)
print("[CONFIG] SMTP_PORT:", SMTP_PORT)
print("[CONFIG] SMTP_FROM:", SMTP_FROM)

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CRED_FILE)
    firebase_admin.initialize_app(cred, {
        "databaseURL": FIREBASE_DB_URL
    })

fs = firestore.client()

# Realtime DB para rutina y fotos generales
estado_ref = db.reference("estado_actual")
historial_ref = db.reference("historial")
fotos_ref = db.reference("fotos")

estado_memoria = {
    "usuario_actual": "",
    "cantidad": 0,
    "activo": False,
    "estado": "Esperando trabajo",
    "updated_at": None
}

# =========================================================
# HELPERS GENERALES
# =========================================================
def now_mx():
    return datetime.now(ZoneInfo("America/Mexico_City"))


def ok_json(payload=None, message="OK", status=200):
    body = {
        "ok": True,
        "message": message
    }
    if payload:
        body.update(payload)
    return jsonify(body), status


def fail(message="Error", status=400):
    return jsonify({
        "ok": False,
        "message": message
    }), status


def is_admin_uid(uid):
    return bool(ADMIN_UID) and uid == ADMIN_UID


def get_bearer_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip()


def require_auth(admin=False):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = get_bearer_token()
            if not token:
                return fail("Falta token de autenticación", 401)

            try:
                decoded = firebase_auth.verify_id_token(token)
            except Exception:
                return fail("Token inválido o expirado", 401)

            request.user = decoded
            request.user_uid = decoded.get("uid")
            request.user_is_admin = is_admin_uid(request.user_uid)

            if admin and not request.user_is_admin:
                return fail("No autorizado", 403)

            return fn(*args, **kwargs)
        return wrapper
    return decorator


def user_doc_to_json(doc):
    data = doc.to_dict() or {}
    return {
        "uid": doc.id,
        "nombre": data.get("nombre", ""),
        "apellido": data.get("apellido", ""),
        "nombreCompleto": f"{data.get('nombre', '')} {data.get('apellido', '')}".strip(),
        "email": data.get("email", ""),
        "telefono": data.get("telefono", ""),
        "created_at": data.get("created_at", ""),
        "emailVerified": data.get("emailVerified", False)
    }


def order_doc_to_json(doc):
    data = doc.to_dict() or {}
    return {
        "id": doc.id,
        "Folio": data.get("Folio", ""),
        "Contador": data.get("Contador", 0),
        "cliente": data.get("cliente", ""),
        "clienteUid": data.get("clienteUid", ""),
        "telefono": data.get("telefono", ""),
        "tipoPrenda": data.get("tipoPrenda", ""),
        "material": data.get("material", ""),
        "cantidad": data.get("cantidad", 1),
        "precio": data.get("precio"),
        "fechaIngreso": data.get("fechaIngreso", ""),
        "FechaEntrega": data.get("FechaEntrega", ""),
        "notas": data.get("notas", ""),
        "Estado": data.get("Estado", "pendiente"),
        "Validado": data.get("Validado", False),
        "origenCliente": data.get("origenCliente", False),
        "FolioIngresado": data.get("FolioIngresado", ""),
        "fotos": data.get("fotos", []),
        "rutina_activa": data.get("rutina_activa", False),
        "started_at": data.get("started_at", ""),
        "completed_at": data.get("completed_at", ""),
        "ultimo_error": data.get("ultimo_error", ""),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", "")
    }


def archivo_a_data_url(local_path, content_type="image/jpeg"):
    if not os.path.exists(local_path):
        raise RuntimeError(f"No existe el archivo local: {local_path}")

    with open(local_path, "rb") as f:
        contenido = f.read()

    if not contenido:
        raise RuntimeError("El archivo está vacío")

    b64 = base64.b64encode(contenido).decode("utf-8")
    return f"data:{content_type};base64,{b64}"


# =========================================================
# VERIFICACIÓN DE CORREO
# =========================================================
def generate_verification_code():
    return str(secrets.randbelow(900000) + 100000)


def verification_doc_ref(uid):
    return fs.collection("email_verifications").document(uid)


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def send_email_smtp(to_email, subject, body_text):
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS or not SMTP_FROM:
        raise RuntimeError("Faltan variables SMTP en Environment")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.set_content(body_text)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        if SMTP_USE_TLS:
            server.starttls()
            server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def create_or_refresh_verification(uid, email, nombre):
    code = generate_verification_code()
    created_at = now_mx()
    expires_at = created_at + timedelta(minutes=15)

    verification_doc_ref(uid).set({
        "uid": uid,
        "email": email,
        "nombre": nombre,
        "code": code,
        "used": False,
        "attempts": 0,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat()
    })

    body = f"""
Hola {nombre or 'usuario'}:

Tu código de verificación para Planchaduría es:

{code}

Este código vence en 15 minutos.

Si tú no solicitaste esta cuenta, puedes ignorar este correo.
""".strip()

    send_email_smtp(
        to_email=email,
        subject="Código de verificación - Planchaduría",
        body_text=body
    )


# =========================================================
# FIREBASE AUTH REST
# =========================================================
def firebase_sign_in(email, password):
    if not FIREBASE_WEB_API_KEY:
        raise RuntimeError("Falta FIREBASE_WEB_API_KEY en Environment")

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
    resp = requests.post(url, json={
        "email": email,
        "password": password,
        "returnSecureToken": True
    }, timeout=30)

    data = resp.json()

    if resp.status_code != 200:
        raise RuntimeError(data.get("error", {}).get("message", "No se pudo iniciar sesión"))

    return data


def auth_error_message(raw_message):
    mapping = {
        "EMAIL_EXISTS": "Este correo ya está registrado.",
        "OPERATION_NOT_ALLOWED": "Operación no permitida.",
        "TOO_MANY_ATTEMPTS_TRY_LATER": "Demasiados intentos. Intenta más tarde.",
        "EMAIL_NOT_FOUND": "Correo no registrado.",
        "INVALID_PASSWORD": "Contraseña incorrecta.",
        "USER_DISABLED": "Usuario deshabilitado.",
        "INVALID_LOGIN_CREDENTIALS": "Credenciales incorrectas."
    }
    return mapping.get(raw_message, raw_message)


# =========================================================
# CONTADOR DE FOLIOS
# =========================================================
@firestore.transactional
def next_order_counter(transaction):
    counter_ref = fs.collection("counters").document("pedidos")
    snap = counter_ref.get(transaction=transaction)

    if snap.exists:
        current = snap.to_dict().get("value", 0)
    else:
        current = 0

    new_value = current + 1
    transaction.set(counter_ref, {"value": new_value})
    return new_value


def generate_folio():
    transaction = fs.transaction()
    contador = next_order_counter(transaction)
    folio = "#" + str(contador).zfill(5)
    return folio, contador


# =========================================================
# ESTADO MEMORIA PARA RUTINA ANTERIOR
# =========================================================
def guardar_estado(usuario=None, cantidad=None, activo=None, estado=None):
    global estado_memoria

    if usuario is not None:
        estado_memoria["usuario_actual"] = str(usuario)

    if cantidad is not None:
        estado_memoria["cantidad"] = int(cantidad)

    if activo is not None:
        estado_memoria["activo"] = bool(activo)

    if estado is not None:
        estado_memoria["estado"] = estado

    estado_memoria["updated_at"] = now_mx().isoformat()

    estado_ref.set(estado_memoria)

    historial_ref.push({
        "usuario_actual": estado_memoria["usuario_actual"],
        "cantidad": estado_memoria["cantidad"],
        "activo": estado_memoria["activo"],
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

# =========================================================
# HOME
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "message": "Backend Render activo"
    })


# =========================================================
# AUTH
# =========================================================
@app.route("/api/auth/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}

    nombre = str(data.get("nombre", "")).strip()
    apellido = str(data.get("apellido", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    telefono = str(data.get("telefono", "")).strip()
    password = str(data.get("password", "")).strip()

    if not nombre or not apellido or not email or not password:
        return fail("Completa todos los campos obligatorios", 400)

    if len(password) < 6:
        return fail("La contraseña debe tener al menos 6 caracteres", 400)

    try:
        nombre_completo = f"{nombre} {apellido}".strip()

        try:
            existing_user = firebase_auth.get_user_by_email(email)

            if existing_user.email_verified:
                return fail("Este correo ya está registrado y validado.", 400)

            firebase_auth.update_user(
                existing_user.uid,
                password=password,
                display_name=nombre_completo
            )
            uid = existing_user.uid

        except firebase_auth.UserNotFoundError:
            new_user = firebase_auth.create_user(
                email=email,
                password=password,
                display_name=nombre_completo,
                email_verified=False
            )
            uid = new_user.uid

        fs.collection("usuarios").document(uid).set({
            "nombre": nombre,
            "apellido": apellido,
            "email": email,
            "telefono": telefono,
            "emailVerified": False,
            "created_at": now_mx().isoformat()
        }, merge=True)

        create_or_refresh_verification(uid, email, nombre_completo)

        return jsonify({
            "ok": True,
            "message": "Te enviamos un código de verificación a tu correo.",
            "requiresVerification": True,
            "email": email
        }), 200

    except Exception as e:
        return fail(auth_error_message(str(e)), 400)


@app.route("/api/auth/resend-code", methods=["POST"])
def api_resend_code():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()

    if not email:
        return fail("Debes indicar el correo", 400)

    try:
        user = firebase_auth.get_user_by_email(email)

        if user.email_verified:
            return fail("Este correo ya fue validado.", 400)

        user_doc = fs.collection("usuarios").document(user.uid).get()
        user_data = user_doc.to_dict() if user_doc.exists else {}
        nombre_completo = f"{user_data.get('nombre', '')} {user_data.get('apellido', '')}".strip()

        create_or_refresh_verification(user.uid, email, nombre_completo)

        return jsonify({
            "ok": True,
            "message": "Te enviamos un nuevo código de verificación."
        }), 200

    except firebase_auth.UserNotFoundError:
        return fail("Correo no registrado.", 404)
    except Exception as e:
        return fail(auth_error_message(str(e)), 400)


@app.route("/api/auth/verify-email-code", methods=["POST"])
def api_verify_email_code():
    data = request.get_json(silent=True) or {}

    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", "")).strip()
    code = str(data.get("code", "")).strip()

    if not email or not password or not code:
        return fail("Debes enviar correo, contraseña y código", 400)

    if not code.isdigit() or len(code) != 6:
        return fail("El código debe tener 6 dígitos", 400)

    try:
        user = firebase_auth.get_user_by_email(email)
        vref = verification_doc_ref(user.uid)
        vsnap = vref.get()

        if not vsnap.exists:
            return fail("No existe una verificación pendiente para este correo.", 404)

        vdata = vsnap.to_dict() or {}

        if vdata.get("used", False):
            return fail("Este código ya fue utilizado.", 400)

        expires_at = parse_iso_datetime(vdata.get("expires_at"))
        if not expires_at or now_mx() > expires_at:
            return fail("El código expiró. Solicita uno nuevo.", 400)

        if str(vdata.get("email", "")).lower() != email:
            return fail("El correo no coincide con la verificación.", 400)

        if str(vdata.get("code", "")).strip() != code:
            current_attempts = int(vdata.get("attempts", 0)) + 1
            vref.update({"attempts": current_attempts})
            return fail("Código incorrecto.", 400)

        firebase_auth.update_user(user.uid, email_verified=True)

        fs.collection("usuarios").document(user.uid).set({
            "emailVerified": True,
            "updated_at": now_mx().isoformat()
        }, merge=True)

        vref.update({
            "used": True,
            "verified_at": now_mx().isoformat()
        })

        auth_data = firebase_sign_in(email, password)

        profile_doc = fs.collection("usuarios").document(user.uid).get()
        profile = profile_doc.to_dict() if profile_doc.exists else {}

        user_json = {
            "uid": user.uid,
            "nombre": profile.get("nombre", ""),
            "apellido": profile.get("apellido", ""),
            "nombreCompleto": f"{profile.get('nombre', '')} {profile.get('apellido', '')}".strip(),
            "email": profile.get("email", email),
            "telefono": profile.get("telefono", ""),
            "isAdmin": is_admin_uid(user.uid)
        }

        return jsonify({
            "ok": True,
            "message": "Correo validado correctamente",
            "token": auth_data.get("idToken"),
            "refreshToken": auth_data.get("refreshToken"),
            "user": user_json
        }), 200

    except firebase_auth.UserNotFoundError:
        return fail("Correo no registrado.", 404)
    except Exception as e:
        return fail(auth_error_message(str(e)), 400)


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}

    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", "")).strip()

    if not email or not password:
        return fail("Completa correo y contraseña", 400)

    try:
        auth_data = firebase_sign_in(email, password)
        uid = auth_data.get("localId", "")

        fb_user = firebase_auth.get_user(uid)
        if not fb_user.email_verified:
            return fail("Debes validar tu correo antes de iniciar sesión.", 403)

        doc = fs.collection("usuarios").document(uid).get()
        profile = doc.to_dict() if doc.exists else {}

        user = {
            "uid": uid,
            "nombre": profile.get("nombre", ""),
            "apellido": profile.get("apellido", ""),
            "nombreCompleto": f"{profile.get('nombre', '')} {profile.get('apellido', '')}".strip(),
            "email": profile.get("email", email),
            "telefono": profile.get("telefono", ""),
            "isAdmin": is_admin_uid(uid)
        }

        return jsonify({
            "ok": True,
            "message": "Inicio de sesión correcto",
            "token": auth_data.get("idToken"),
            "refreshToken": auth_data.get("refreshToken"),
            "user": user
        }), 200

    except Exception as e:
        return fail(auth_error_message(str(e)), 400)


@app.route("/api/auth/me", methods=["GET"])
@require_auth(admin=False)
def api_me():
    uid = request.user_uid
    doc = fs.collection("usuarios").document(uid).get()
    profile = doc.to_dict() if doc.exists else {}

    user = {
        "uid": uid,
        "nombre": profile.get("nombre", ""),
        "apellido": profile.get("apellido", ""),
        "nombreCompleto": f"{profile.get('nombre', '')} {profile.get('apellido', '')}".strip(),
        "email": profile.get("email", request.user.get("email", "")),
        "telefono": profile.get("telefono", ""),
        "isAdmin": request.user_is_admin
    }

    return jsonify({
        "ok": True,
        "user": user
    }), 200


# =========================================================
# PEDIDOS CLIENTE
# =========================================================
@app.route("/api/orders", methods=["POST"])
@require_auth(admin=False)
def api_create_order_client():
    data = request.get_json(silent=True) or {}

    tipo_prenda = str(data.get("tipoPrenda", "")).strip()
    material = str(data.get("material", "")).strip()
    cantidad = int(data.get("cantidad", 1))
    fecha_entrega = str(data.get("fechaEntrega", "")).strip()
    notas = str(data.get("notas", "")).strip()

    if not tipo_prenda:
        return fail("Debes indicar la prenda", 400)

    if cantidad < 1:
        return fail("La cantidad debe ser al menos 1", 400)

    if not fecha_entrega:
        return fail("Debes indicar la fecha de entrega", 400)

    uid = request.user_uid
    user_doc = fs.collection("usuarios").document(uid).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}

    nombre = user_data.get("nombre", "")
    apellido = user_data.get("apellido", "")
    cliente_nombre = f"{nombre} {apellido}".strip() or request.user.get("email", "")

    folio, contador = generate_folio()

    payload = {
        "Folio": folio,
        "Contador": contador,
        "cliente": cliente_nombre,
        "clienteUid": uid,
        "telefono": user_data.get("telefono", ""),
        "tipoPrenda": tipo_prenda,
        "material": material,
        "cantidad": cantidad,
        "precio": None,
        "fechaIngreso": now_mx().date().isoformat(),
        "FechaEntrega": fecha_entrega,
        "notas": notas,
        "Estado": "pendiente",
        "Validado": False,
        "origenCliente": True,
        "FolioIngresado": folio,
        "fotos": [],
        "rutina_activa": False,
        "started_at": "",
        "completed_at": "",
        "ultimo_error": "",
        "created_at": now_mx().isoformat(),
        "updated_at": now_mx().isoformat()
    }

    ref = fs.collection("pedidos").document()
    ref.set(payload)

    return jsonify({
        "ok": True,
        "message": "Pedido registrado correctamente",
        "data": {
            "id": ref.id,
            **payload
        }
    }), 201


@app.route("/api/orders/my", methods=["GET"])
@require_auth(admin=False)
def api_orders_my():
    uid = request.user_uid

    docs = fs.collection("pedidos").where("clienteUid", "==", uid).stream()
    items = [order_doc_to_json(doc) for doc in docs]
    items.sort(key=lambda x: x.get("Contador", 0), reverse=True)

    return jsonify({
        "ok": True,
        "orders": items
    }), 200


@app.route("/api/orders/track/<folio>", methods=["GET"])
def api_orders_track(folio):
    folio = str(folio).strip().upper()

    docs = list(fs.collection("pedidos").where("Folio", "==", folio).stream())

    if not docs:
        return fail(f"No se encontró ningún pedido con ID {folio}", 404)

    return jsonify({
        "ok": True,
        "order": order_doc_to_json(docs[0])
    }), 200


# =========================================================
# ADMIN PEDIDOS
# =========================================================
@app.route("/api/admin/orders", methods=["GET"])
@require_auth(admin=True)
def api_admin_orders_list():
    docs = fs.collection("pedidos").stream()
    items = [order_doc_to_json(doc) for doc in docs]
    items.sort(key=lambda x: x.get("Contador", 0), reverse=True)

    return jsonify({
        "ok": True,
        "orders": items
    }), 200


@app.route("/api/admin/orders", methods=["POST"])
@require_auth(admin=True)
def api_admin_orders_create():
    data = request.get_json(silent=True) or {}

    cliente = str(data.get("cliente", "")).strip()
    telefono = str(data.get("telefono", "")).strip()
    tipo_prenda = str(data.get("tipoPrenda", "")).strip()
    material = str(data.get("material", "")).strip()
    cantidad = int(data.get("cantidad", 1))
    precio = data.get("precio", None)
    fecha_ingreso = str(data.get("fechaIngreso", "")).strip()
    fecha_entrega = str(data.get("FechaEntrega", "")).strip()
    notas = str(data.get("notas", "")).strip()

    if not cliente:
        return fail("El nombre del cliente es obligatorio", 400)

    if not tipo_prenda:
        return fail("La prenda es obligatoria", 400)

    if cantidad < 1:
        return fail("La cantidad debe ser al menos 1", 400)

    if not fecha_ingreso or not fecha_entrega:
        return fail("Debes completar las fechas", 400)

    folio, contador = generate_folio()

    payload = {
        "Folio": folio,
        "Contador": contador,
        "cliente": cliente,
        "clienteUid": "",
        "telefono": telefono,
        "tipoPrenda": tipo_prenda,
        "material": material,
        "cantidad": cantidad,
        "precio": precio,
        "fechaIngreso": fecha_ingreso,
        "FechaEntrega": fecha_entrega,
        "notas": notas,
        "Estado": "pendiente",
        "Validado": False,
        "origenCliente": False,
        "FolioIngresado": folio,
        "fotos": [],
        "rutina_activa": False,
        "started_at": "",
        "completed_at": "",
        "ultimo_error": "",
        "created_at": now_mx().isoformat(),
        "updated_at": now_mx().isoformat()
    }

    ref = fs.collection("pedidos").document()
    ref.set(payload)

    return jsonify({
        "ok": True,
        "message": "Pedido creado correctamente",
        "data": {
            "id": ref.id,
            **payload
        }
    }), 201


@app.route("/api/admin/orders/<order_id>", methods=["PATCH"])
@require_auth(admin=True)
def api_admin_orders_update(order_id):
    doc_ref = fs.collection("pedidos").document(order_id)
    snap = doc_ref.get()

    if not snap.exists:
        return fail("Pedido no encontrado", 404)

    data = request.get_json(silent=True) or {}

    allowed_keys = {
        "cliente", "telefono", "tipoPrenda", "material", "cantidad",
        "precio", "fechaIngreso", "FechaEntrega", "notas", "Estado"
    }

    payload = {}
    for key, value in data.items():
        if key in allowed_keys:
            payload[key] = value

    if "Estado" in payload:
        payload["Validado"] = payload["Estado"] == "entregado"

    payload["updated_at"] = now_mx().isoformat()

    doc_ref.update(payload)

    return jsonify({
        "ok": True,
        "message": "Pedido actualizado correctamente"
    }), 200


@app.route("/api/admin/orders/<order_id>", methods=["DELETE"])
@require_auth(admin=True)
def api_admin_orders_delete(order_id):
    doc_ref = fs.collection("pedidos").document(order_id)
    snap = doc_ref.get()

    if not snap.exists:
        return fail("Pedido no encontrado", 404)

    doc_ref.delete()

    return jsonify({
        "ok": True,
        "message": "Pedido eliminado correctamente"
    }), 200


@app.route("/api/admin/clients", methods=["GET"])
@require_auth(admin=True)
def api_admin_clients():
    user_docs = list(fs.collection("usuarios").stream())
    order_docs = list(fs.collection("pedidos").stream())

    count_by_uid = {}
    for doc in order_docs:
        data = doc.to_dict() or {}
        uid = data.get("clienteUid", "")
        if uid:
            count_by_uid[uid] = count_by_uid.get(uid, 0) + 1

    clients = []
    for doc in user_docs:
        item = user_doc_to_json(doc)
        item["totalPedidos"] = count_by_uid.get(doc.id, 0)
        clients.append(item)

    clients.sort(key=lambda x: x.get("nombreCompleto", "").lower())

    return jsonify({
        "ok": True,
        "clients": clients
    }), 200


# =========================================================
# ENDPOINTS WORKER NUEVOS
# =========================================================
@app.route("/api/worker/next-order", methods=["GET"])
def api_worker_next_order():
    docs = fs.collection("pedidos").where("Estado", "==", "pendiente").stream()
    items = [order_doc_to_json(doc) for doc in docs]
    items.sort(key=lambda x: x.get("Contador", 0))

    if not items:
        return jsonify({
            "ok": True,
            "order": None
        }), 200

    return jsonify({
        "ok": True,
        "order": items[0]
    }), 200


@app.route("/api/worker/orders/<order_id>/start", methods=["POST"])
def api_worker_order_start(order_id):
    doc_ref = fs.collection("pedidos").document(order_id)
    snap = doc_ref.get()

    if not snap.exists:
        return fail("Pedido no encontrado", 404)

    doc_ref.update({
        "Estado": "en_proceso",
        "rutina_activa": True,
        "started_at": now_mx().isoformat(),
        "updated_at": now_mx().isoformat()
    })

    return jsonify({
        "ok": True,
        "message": "Pedido iniciado"
    }), 200


@app.route("/api/worker/orders/<order_id>/status", methods=["POST"])
def api_worker_order_status(order_id):
    doc_ref = fs.collection("pedidos").document(order_id)
    snap = doc_ref.get()

    if not snap.exists:
        return fail("Pedido no encontrado", 404)

    data = request.get_json(silent=True) or {}
    estado = str(data.get("Estado", data.get("estado", ""))).strip()

    estados_validos = {
        "pendiente",
        "en_proceso",
        "planchado",
        "listo",
        "entregado"
    }

    if not estado:
        return fail("Debes enviar el estado", 400)

    if estado not in estados_validos:
        return fail("Estado inválido", 400)

    payload = {
        "Estado": estado,
        "updated_at": now_mx().isoformat()
    }

    if estado == "en_proceso":
        payload["rutina_activa"] = True
        payload["started_at"] = now_mx().isoformat()

    if estado == "planchado":
        payload["rutina_activa"] = True

    if estado == "listo":
        payload["rutina_activa"] = False
        payload["completed_at"] = now_mx().isoformat()

    if estado == "entregado":
        payload["rutina_activa"] = False
        payload["Validado"] = True

    if estado in {"pendiente", "en_proceso", "planchado", "listo"}:
        payload["Validado"] = False

    doc_ref.update(payload)

    return jsonify({
        "ok": True,
        "message": f"Estado actualizado a {estado}",
        "order_id": order_id,
        "Estado": estado
    }), 200


@app.route("/api/worker/orders/<order_id>/photo", methods=["POST"])
def api_worker_order_photo(order_id):
    try:
        print(f"[PHOTO] Iniciando carga de foto para pedido: {order_id}")

        doc_ref = fs.collection("pedidos").document(order_id)
        snap = doc_ref.get()

        if not snap.exists:
            return fail("Pedido no encontrado", 404)

        if "foto" not in request.files:
            return fail("No se recibió archivo", 400)

        archivo = request.files["foto"]

        if archivo.filename == "":
            return fail("Archivo vacío", 400)

        now = now_mx()
        fecha = now.strftime("%Y-%m-%d")
        hora = now.strftime("%H:%M:%S")
        stamp = now.strftime("%Y%m%d_%H%M%S")

        nombre_seguro = secure_filename(archivo.filename)
        nombre_final = f"{order_id}_{stamp}_{nombre_seguro}"
        ruta_local = os.path.join(app.config["UPLOAD_FOLDER"], nombre_final)

        print("[PHOTO] Guardando archivo temporal en:", ruta_local)
        archivo.save(ruta_local)

        if not os.path.exists(ruta_local):
            raise RuntimeError("No se pudo guardar el archivo temporalmente")

        tam = os.path.getsize(ruta_local)
        print("[PHOTO] Tamaño archivo local:", tam)

        if tam == 0:
            raise RuntimeError("El archivo guardado quedó vacío")

        content_type = archivo.mimetype or "image/jpeg"
        data_url = archivo_a_data_url(ruta_local, content_type)

        foto_info = {
            "nombre": nombre_final,
            "url": data_url,
            "content_type": content_type,
            "fecha": fecha,
            "hora": hora,
            "fecha_hora": f"{fecha} {hora}",
            "timestamp": now.isoformat()
        }

        data = snap.to_dict() or {}
        fotos = data.get("fotos", [])
        fotos.append(foto_info)

        doc_ref.update({
            "fotos": fotos,
            "updated_at": now.isoformat()
        })

        try:
            os.remove(ruta_local)
        except Exception as e:
            print("[PHOTO] No se pudo borrar archivo temporal:", e)

        print("[PHOTO] Foto guardada correctamente en Firestore para pedido:", order_id)

        return jsonify({
            "ok": True,
            "message": "Foto agregada al pedido",
            "foto": foto_info
        }), 200

    except Exception as e:
        print("[PHOTO] Error en /api/worker/orders/<order_id>/photo:", type(e).__name__, str(e))
        return fail(f"Error interno al guardar la foto: {type(e).__name__}: {e}", 500)


@app.route("/api/worker/orders/<order_id>/complete", methods=["POST"])
def api_worker_order_complete(order_id):
    doc_ref = fs.collection("pedidos").document(order_id)
    snap = doc_ref.get()

    if not snap.exists:
        return fail("Pedido no encontrado", 404)

    doc_ref.update({
        "Estado": "listo",
        "rutina_activa": False,
        "completed_at": now_mx().isoformat(),
        "updated_at": now_mx().isoformat()
    })

    return jsonify({
        "ok": True,
        "message": "Pedido completado"
    }), 200


@app.route("/api/worker/orders/<order_id>/error", methods=["POST"])
def api_worker_order_error(order_id):
    doc_ref = fs.collection("pedidos").document(order_id)
    snap = doc_ref.get()

    if not snap.exists:
        return fail("Pedido no encontrado", 404)

    data = request.get_json(silent=True) or {}
    error_msg = str(data.get("error", "Error no especificado"))

    doc_ref.update({
        "Estado": "pendiente",
        "rutina_activa": False,
        "ultimo_error": error_msg,
        "updated_at": now_mx().isoformat()
    })

    return jsonify({
        "ok": True,
        "message": "Error registrado"
    }), 200


# =========================================================
# ENDPOINTS ANTIGUOS DE RUTINA
# =========================================================
@app.route("/estado", methods=["GET"])
def obtener_estado():
    return jsonify({
        "ok": True,
        "data": estado_memoria
    })


@app.route("/set_usuario", methods=["POST"])
def set_usuario():
    data = request.get_json(silent=True) or {}
    usuario = str(data.get("usuario", "")).strip()

    if not usuario or not usuario.isdigit() or len(usuario) > 5:
        return fail("Usuario inválido, máximo 5 dígitos", 400)

    guardar_estado(usuario=usuario, estado=f"Usuario actual: {usuario}")

    return jsonify({
        "ok": True,
        "message": f"Usuario {usuario} guardado correctamente",
        "data": estado_memoria
    }), 200


@app.route("/set_cantidad", methods=["POST"])
def set_cantidad():
    data = request.get_json(silent=True) or {}

    usuario = str(data.get("usuario", "")).strip()
    cantidad = data.get("cantidad", None)

    if not usuario or not usuario.isdigit() or len(usuario) > 5:
        return fail("Usuario inválido", 400)

    if not isinstance(cantidad, int) or cantidad < 0:
        return fail("Cantidad inválida", 400)

    guardar_estado(
        usuario=usuario,
        cantidad=cantidad,
        activo=(cantidad > 0),
        estado=f"Usuario {usuario}: cantidad actualizada a {cantidad}"
    )

    return jsonify({
        "ok": True,
        "message": "Cantidad actualizada correctamente",
        "data": estado_memoria
    }), 200


@app.route("/activar_plc", methods=["POST"])
def activar_plc():
    data = request.get_json(silent=True) or {}
    usuario = str(data.get("usuario", "")).strip()

    if not usuario:
        return fail("Debes enviar usuario", 400)

    guardar_estado(
        usuario=usuario,
        activo=True,
        estado=f"Motor continuo activo para usuario {usuario}"
    )

    return jsonify({
        "ok": True,
        "message": "PLC activado correctamente",
        "data": estado_memoria
    }), 200


@app.route("/desactivar_plc", methods=["POST"])
def desactivar_plc():
    guardar_estado(
        activo=False,
        estado="PLC desactivado"
    )

    return jsonify({
        "ok": True,
        "message": "PLC desactivado correctamente",
        "data": estado_memoria
    }), 200


@app.route("/subir_foto", methods=["POST"])
def subir_foto():
    try:
        if "foto" not in request.files:
            return fail("No se recibió archivo", 400)

        usuario = str(request.form.get("usuario", "")).strip()

        if not usuario.isdigit() or len(usuario) > 5:
            return fail("Usuario inválido", 400)

        archivo = request.files["foto"]

        if archivo.filename == "":
            return fail("Archivo vacío", 400)

        now = now_mx()
        fecha = now.strftime("%Y-%m-%d")
        hora = now.strftime("%H:%M:%S")
        stamp = now.strftime("%Y%m%d_%H%M%S")

        nombre_seguro = secure_filename(archivo.filename)
        nombre_final = f"u{usuario}_{stamp}_{nombre_seguro}"
        ruta_local = os.path.join(app.config["UPLOAD_FOLDER"], nombre_final)

        print("[SUBIR_FOTO] Guardando archivo temporal en:", ruta_local)
        archivo.save(ruta_local)

        if not os.path.exists(ruta_local):
            raise RuntimeError("No se pudo guardar el archivo temporalmente")

        tam = os.path.getsize(ruta_local)
        print("[SUBIR_FOTO] Tamaño archivo local:", tam)

        if tam == 0:
            raise RuntimeError("El archivo guardado quedó vacío")

        content_type = archivo.mimetype or "image/jpeg"
        data_url = archivo_a_data_url(ruta_local, content_type)

        foto_info = {
            "usuario": usuario,
            "nombre": nombre_final,
            "url": data_url,
            "content_type": content_type,
            "fecha": fecha,
            "hora": hora,
            "etiqueta": f"{usuario}_{fecha}_{hora}",
            "timestamp": now.isoformat()
        }

        fotos_ref.push(foto_info)

        try:
            os.remove(ruta_local)
        except Exception as e:
            print("[SUBIR_FOTO] No se pudo borrar archivo temporal:", e)

        print("[SUBIR_FOTO] Foto guardada correctamente en Firestore para usuario:", usuario)

        return jsonify({
            "ok": True,
            "message": "Foto subida correctamente",
            "foto": foto_info
        }), 200

    except Exception as e:
        print("[SUBIR_FOTO] Error:", type(e).__name__, str(e))
        return fail(f"Error interno al subir la foto: {type(e).__name__}: {e}", 500)


@app.route("/fotos_usuario/<usuario>", methods=["GET"])
def fotos_usuario(usuario):
    usuario = str(usuario).strip()

    if not usuario.isdigit() or len(usuario) > 5:
        return fail("Usuario inválido", 400)

    data = fotos_ref.get()
    lista = []

    if data:
        for _, foto in data.items():
            if str(foto.get("usuario", "")) == usuario:
                lista.append({
                    "usuario": foto.get("usuario", ""),
                    "nombre": foto.get("nombre", ""),
                    "url": foto.get("url", ""),
                    "fecha": foto.get("fecha", ""),
                    "hora": foto.get("hora", ""),
                    "etiqueta": foto.get("etiqueta", "")
                })

    lista.sort(key=lambda x: x["nombre"], reverse=True)

    return jsonify({
        "ok": True,
        "usuario": usuario,
        "total": len(lista),
        "fotos": lista
    }), 200


@app.route("/fotos/<nombre_archivo>", methods=["GET"])
def ver_foto(nombre_archivo):
    return send_from_directory(app.config["UPLOAD_FOLDER"], nombre_archivo)


@app.route("/historial", methods=["GET"])
def historial():
    data = historial_ref.get()
    return jsonify({
        "ok": True,
        "data": data if data else {}
    }), 200


# =========================================================
# MAIN
# =========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
