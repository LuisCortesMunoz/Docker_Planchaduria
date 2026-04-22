# ================================
# app.py
# ================================
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.utils import secure_filename
import firebase_admin
from firebase_admin import credentials, firestore, db, auth as firebase_auth, storage
import os
import requests
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

# =========================================================
# CONFIGURACIÓN GENERAL
# =========================================================
app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = "fotos_temp"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

FIREBASE_CRED_FILE = os.environ.get("FIREBASE_CRED_FILE", "/etc/secrets/serviceAccountKey.json")
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "https://db-planchaduria-default-rtdb.firebaseio.com")
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY", "")
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "TU_BUCKET_REAL.firebasestorage.app")
ADMIN_UID = os.environ.get("ADMIN_UID", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER).strip()
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").strip().lower() == "true"

REGISTER_CODE_MINUTES = 10
REGISTER_MAX_ATTEMPTS = 5
RESET_CODE_MINUTES = 10
RESET_MAX_ATTEMPTS = 5

print("[CONFIG] FIREBASE_CRED_FILE:", FIREBASE_CRED_FILE)
print("[CONFIG] FIREBASE_DB_URL:", FIREBASE_DB_URL)
print("[CONFIG] FIREBASE_STORAGE_BUCKET:", FIREBASE_STORAGE_BUCKET)
print("[CONFIG] SMTP_HOST:", SMTP_HOST)
print("[CONFIG] SMTP_PORT:", SMTP_PORT)
print("[CONFIG] SMTP_FROM:", SMTP_FROM)

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_CRED_FILE)
    firebase_admin.initialize_app(cred, {
        "databaseURL": FIREBASE_DB_URL,
        "storageBucket": FIREBASE_STORAGE_BUCKET
    })

fs = firestore.client()

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
# HORA DE ARRANQUE DEL BACKEND
# =========================================================
BACKEND_START_TIME = datetime.now(ZoneInfo("America/Mexico_City"))
BACKEND_START_TIME_ISO = BACKEND_START_TIME.isoformat()

print("[INIT] Backend arrancó en:", BACKEND_START_TIME_ISO)

# =========================================================
# HELPERS
# =========================================================
def now_mx():
    return datetime.now(ZoneInfo("America/Mexico_City"))


def ok_json(payload=None, message="OK", status=200):
    body = {"ok": True, "message": message}
    if payload:
        body.update(payload)
    return jsonify(body), status


def fail(message="Error", status=400, extra=None):
    body = {"ok": False, "message": message}
    if extra and isinstance(extra, dict):
        body.update(extra)
    return jsonify(body), status


def is_admin_uid(uid):
    return bool(ADMIN_UID) and uid == ADMIN_UID


def normalize_email(email):
    return str(email or "").strip().lower()


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


def parse_iso_datetime(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def was_created_after_backend_start(created_at_value):
    dt = parse_iso_datetime(created_at_value)

    if dt is None:
        return False

    try:
        return dt >= BACKEND_START_TIME
    except Exception:
        return False


def user_doc_to_json(doc):
    data = doc.to_dict() or {}
    return {
        "uid": doc.id,
        "nombre": data.get("nombre", ""),
        "apellido": data.get("apellido", ""),
        "nombreCompleto": f"{data.get('nombre', '')} {data.get('apellido', '')}".strip(),
        "email": data.get("email", ""),
        "telefono": data.get("telefono", ""),
        "created_at": data.get("created_at", "")
    }


def order_doc_to_json(doc):
    data = doc.to_dict() or {}
    fotos = data.get("fotos", [])
    if not isinstance(fotos, list):
        fotos = []

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
        "fotos": fotos,
        "rutina_activa": data.get("rutina_activa", False),
        "started_at": data.get("started_at", ""),
        "completed_at": data.get("completed_at", ""),
        "ultimo_error": data.get("ultimo_error", ""),
        "created_at": data.get("created_at", ""),
        "updated_at": data.get("updated_at", ""),
        "activado_hmi": data.get("activado_hmi", False),
        "hmi_activated_at": data.get("hmi_activated_at", "")
    }


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



def normalize_folio(value):
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return raw if raw.startswith("#") else f"#{raw}"


def get_order_doc_by_folio(folio):
    folio = normalize_folio(folio)
    if not folio:
        return None, None

    docs = list(fs.collection("pedidos").where("Folio", "==", folio).limit(1).stream())
    if not docs:
        return None, folio

    return docs[0], folio


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


# =========================================================
# HELPERS REGISTRO / RESET
# =========================================================
def generate_six_digit_code():
    return f"{random.randint(0, 999999):06d}"


def registration_doc_ref(email):
    return fs.collection("pending_registrations").document(normalize_email(email))


def reset_doc_ref(email):
    return fs.collection("pending_password_resets").document(normalize_email(email))


def email_exists_in_firebase(email):
    email = normalize_email(email)
    try:
        firebase_auth.get_user_by_email(email)
        return True
    except firebase_auth.UserNotFoundError:
        return False
    except Exception:
        raise


def get_firebase_user_by_email(email):
    email = normalize_email(email)
    return firebase_auth.get_user_by_email(email)


def send_email_smtp(to_email, subject, html_body, text_body=""):
    if not SMTP_HOST or not SMTP_PORT or not SMTP_USER or not SMTP_PASS or not SMTP_FROM:
        raise RuntimeError("Faltan variables SMTP en Environment")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        if SMTP_USE_TLS:
            server.starttls()
            server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_FROM, [to_email], msg.as_string())


def send_register_code_email(to_email, nombre, code):
    display_name = str(nombre or "").strip() or "usuario"
    subject = "Código de verificación - Planchado Express"

    text_body = (
        f"Hola {display_name},\n\n"
        f"Tu código de verificación es: {code}\n"
        f"Este código expira en {REGISTER_CODE_MINUTES} minutos.\n\n"
        f"Si tú no solicitaste esta cuenta, puedes ignorar este correo.\n"
    )

    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color:#222;">
        <div style="max-width:520px; margin:0 auto; padding:24px; border:1px solid #eee; border-radius:16px;">
          <h2 style="color:#e63329; margin-top:0;">Planchado Express</h2>
          <p>Hola <strong>{display_name}</strong>,</p>
          <p>Tu código de verificación para crear tu cuenta es:</p>
          <div style="font-size:32px; font-weight:700; letter-spacing:6px; text-align:center; margin:24px 0; color:#111;">
            {code}
          </div>
          <p>Este código expira en <strong>{REGISTER_CODE_MINUTES} minutos</strong>.</p>
          <p>Si tú no solicitaste esta cuenta, puedes ignorar este correo.</p>
        </div>
      </body>
    </html>
    """

    send_email_smtp(to_email, subject, html_body, text_body)


def send_reset_code_email(to_email, code):
    subject = "Código para recuperar tu contraseña - Planchado Express"

    text_body = (
        f"Hola,\n\n"
        f"Tu código para recuperar tu contraseña es: {code}\n"
        f"Este código expira en {RESET_CODE_MINUTES} minutos.\n\n"
        f"Si tú no solicitaste este cambio, puedes ignorar este correo.\n"
    )

    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color:#222;">
        <div style="max-width:520px; margin:0 auto; padding:24px; border:1px solid #eee; border-radius:16px;">
          <h2 style="color:#e63329; margin-top:0;">Planchado Express</h2>
          <p>Tu código para recuperar tu contraseña es:</p>
          <div style="font-size:32px; font-weight:700; letter-spacing:6px; text-align:center; margin:24px 0; color:#111;">
            {code}
          </div>
          <p>Este código expira en <strong>{RESET_CODE_MINUTES} minutos</strong>.</p>
          <p>Si tú no solicitaste este cambio, puedes ignorar este correo.</p>
        </div>
      </body>
    </html>
    """

    send_email_smtp(to_email, subject, html_body, text_body)


def create_firebase_user(email, password, nombre, apellido):
    return firebase_auth.create_user(
        email=normalize_email(email),
        password=password,
        display_name=f"{str(nombre).strip()} {str(apellido).strip()}".strip()
    )


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
# ESTADO MEMORIA
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
        "message": "Backend Render activo",
        "backend_start_time": BACKEND_START_TIME_ISO
    })

# =========================================================
# AUTH REGISTRO CON CÓDIGO
# =========================================================
@app.route("/api/auth/request-register-code", methods=["POST"])
def api_request_register_code():
    data = request.get_json(silent=True) or {}

    nombre = str(data.get("nombre", "")).strip()
    apellido = str(data.get("apellido", "")).strip()
    email = normalize_email(data.get("email", ""))
    telefono = str(data.get("telefono", "")).strip()
    password = str(data.get("password", "")).strip()

    if not nombre or not apellido or not email or not password:
        return fail("Completa todos los campos obligatorios", 400)

    if len(password) < 6:
        return fail("La contraseña debe tener al menos 6 caracteres", 400)

    try:
        if email_exists_in_firebase(email):
            return fail("Este correo ya está registrado.", 400)

        code = generate_six_digit_code()
        now = now_mx()
        expires_at = now + timedelta(minutes=REGISTER_CODE_MINUTES)

        registration_doc_ref(email).set({
            "nombre": nombre,
            "apellido": apellido,
            "email": email,
            "telefono": telefono,
            "password": password,
            "code": code,
            "attempts": 0,
            "verified": False,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "code_expires_at": expires_at.isoformat()
        })

        send_register_code_email(email, nombre, code)

        return jsonify({
            "ok": True,
            "message": "Te enviamos un código de verificación a tu correo.",
            "email": email
        }), 200

    except Exception as e:
        print("[REGISTER_CODE] Error:", type(e).__name__, str(e))
        return fail(f"No se pudo enviar el código: {e}", 500)


@app.route("/api/auth/verify-register-code", methods=["POST"])
def api_verify_register_code():
    data = request.get_json(silent=True) or {}

    email = normalize_email(data.get("email", ""))
    code = str(data.get("code", "")).strip()

    if not email or not code:
        return fail("Debes enviar correo y código", 400)

    try:
        doc_ref = registration_doc_ref(email)
        snap = doc_ref.get()

        if not snap.exists:
            return fail("No existe un registro pendiente para este correo", 404)

        reg = snap.to_dict() or {}

        if reg.get("verified", False):
            return fail("Este registro ya fue verificado", 400)

        attempts = int(reg.get("attempts", 0))
        if attempts >= REGISTER_MAX_ATTEMPTS:
            return fail("Se alcanzó el máximo de intentos. Solicita un nuevo código.", 400)

        expires_at = parse_iso_datetime(reg.get("code_expires_at"))
        if not expires_at or now_mx() > expires_at:
            return fail("El código expiró. Solicita uno nuevo.", 400)

        if str(reg.get("code", "")).strip() != code:
            doc_ref.update({
                "attempts": attempts + 1,
                "updated_at": now_mx().isoformat()
            })
            return fail("Código incorrecto", 400)

        if email_exists_in_firebase(email):
            doc_ref.delete()
            return fail("Este correo ya está registrado.", 400)

        nombre = str(reg.get("nombre", "")).strip()
        apellido = str(reg.get("apellido", "")).strip()
        telefono = str(reg.get("telefono", "")).strip()
        password = str(reg.get("password", "")).strip()

        if not nombre or not apellido or not password:
            return fail("El registro temporal está incompleto. Vuelve a registrarte.", 400)

        created_user = create_firebase_user(email, password, nombre, apellido)
        uid = created_user.uid

        fs.collection("usuarios").document(uid).set({
            "nombre": nombre,
            "apellido": apellido,
            "email": email,
            "telefono": telefono,
            "created_at": now_mx().isoformat()
        })

        auth_data = firebase_sign_in(email, password)

        user = {
            "uid": uid,
            "nombre": nombre,
            "apellido": apellido,
            "nombreCompleto": f"{nombre} {apellido}".strip(),
            "email": email,
            "telefono": telefono,
            "isAdmin": is_admin_uid(uid)
        }

        doc_ref.delete()

        return jsonify({
            "ok": True,
            "message": "Cuenta creada correctamente.",
            "token": auth_data.get("idToken"),
            "refreshToken": auth_data.get("refreshToken"),
            "user": user
        }), 200

    except Exception as e:
        print("[VERIFY_REGISTER_CODE] Error:", type(e).__name__, str(e))
        return fail(f"No se pudo verificar el código: {e}", 500)


@app.route("/api/auth/resend-register-code", methods=["POST"])
def api_resend_register_code():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))

    if not email:
        return fail("Debes enviar el correo", 400)

    try:
        doc_ref = registration_doc_ref(email)
        snap = doc_ref.get()

        if not snap.exists:
            return fail("No existe un registro pendiente para este correo", 404)

        reg = snap.to_dict() or {}

        if email_exists_in_firebase(email):
            doc_ref.delete()
            return fail("Este correo ya está registrado.", 400)

        code = generate_six_digit_code()
        now = now_mx()
        expires_at = now + timedelta(minutes=REGISTER_CODE_MINUTES)

        doc_ref.update({
            "code": code,
            "attempts": 0,
            "verified": False,
            "updated_at": now.isoformat(),
            "code_expires_at": expires_at.isoformat()
        })

        send_register_code_email(
            to_email=email,
            nombre=reg.get("nombre", ""),
            code=code
        )

        return jsonify({
            "ok": True,
            "message": "Te enviamos un nuevo código de verificación."
        }), 200

    except Exception as e:
        print("[RESEND_REGISTER_CODE] Error:", type(e).__name__, str(e))
        return fail(f"No se pudo reenviar el código: {e}", 500)


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    return fail("Usa /api/auth/request-register-code para iniciar el registro con código", 400)


# =========================================================
# AUTH RECUPERAR CONTRASEÑA
# =========================================================
@app.route("/api/auth/request-reset-code", methods=["POST"])
def api_request_reset_code():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))

    if not email:
        return fail("Debes enviar el correo", 400)

    try:
        user = get_firebase_user_by_email(email)

        code = generate_six_digit_code()
        now = now_mx()
        expires_at = now + timedelta(minutes=RESET_CODE_MINUTES)

        reset_doc_ref(email).set({
            "email": email,
            "uid": user.uid,
            "code": code,
            "attempts": 0,
            "verified": False,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "code_expires_at": expires_at.isoformat()
        })

        send_reset_code_email(email, code)

        return jsonify({
            "ok": True,
            "message": "Te enviamos un código para recuperar tu contraseña.",
            "email": email
        }), 200

    except firebase_auth.UserNotFoundError:
        return fail("Correo no registrado.", 404)
    except Exception as e:
        print("[REQUEST_RESET_CODE] Error:", type(e).__name__, str(e))
        return fail(f"No se pudo enviar el código de recuperación: {e}", 500)


@app.route("/api/auth/verify-reset-code", methods=["POST"])
def api_verify_reset_code():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))
    code = str(data.get("code", "")).strip()

    if not email or not code:
        return fail("Debes enviar correo y código", 400)

    try:
        doc_ref = reset_doc_ref(email)
        snap = doc_ref.get()

        if not snap.exists:
            return fail("No existe una solicitud de recuperación para este correo", 404)

        reg = snap.to_dict() or {}

        attempts = int(reg.get("attempts", 0))
        if attempts >= RESET_MAX_ATTEMPTS:
            return fail("Se alcanzó el máximo de intentos. Solicita un nuevo código.", 400)

        expires_at = parse_iso_datetime(reg.get("code_expires_at"))
        if not expires_at or now_mx() > expires_at:
            return fail("El código expiró. Solicita uno nuevo.", 400)

        if str(reg.get("code", "")).strip() != code:
            doc_ref.update({
                "attempts": attempts + 1,
                "updated_at": now_mx().isoformat()
            })
            return fail("Código incorrecto", 400)

        doc_ref.update({
            "verified": True,
            "updated_at": now_mx().isoformat()
        })

        return jsonify({
            "ok": True,
            "message": "Código correcto. Ahora escribe tu nueva contraseña."
        }), 200

    except Exception as e:
        print("[VERIFY_RESET_CODE] Error:", type(e).__name__, str(e))
        return fail(f"No se pudo verificar el código: {e}", 500)


@app.route("/api/auth/confirm-reset-password", methods=["POST"])
def api_confirm_reset_password():
    data = request.get_json(silent=True) or {}

    email = normalize_email(data.get("email", ""))
    code = str(data.get("code", "")).strip()
    new_password = str(data.get("newPassword", "")).strip()

    if not email or not code or not new_password:
        return fail("Debes enviar correo, código y nueva contraseña", 400)

    if len(new_password) < 6:
        return fail("La nueva contraseña debe tener al menos 6 caracteres", 400)

    try:
        doc_ref = reset_doc_ref(email)
        snap = doc_ref.get()

        if not snap.exists:
            return fail("No existe una solicitud de recuperación para este correo", 404)

        reg = snap.to_dict() or {}

        expires_at = parse_iso_datetime(reg.get("code_expires_at"))
        if not expires_at or now_mx() > expires_at:
            return fail("El código expiró. Solicita uno nuevo.", 400)

        if not reg.get("verified", False):
            return fail("Primero debes validar el código de recuperación.", 400)

        if str(reg.get("code", "")).strip() != code:
            return fail("Código incorrecto", 400)

        uid = str(reg.get("uid", "")).strip()
        if not uid:
            return fail("No se encontró el usuario para actualizar contraseña.", 400)

        firebase_auth.update_user(uid, password=new_password)

        auth_data = firebase_sign_in(email, new_password)

        user_doc = fs.collection("usuarios").document(uid).get()
        profile = user_doc.to_dict() if user_doc.exists else {}

        user = {
            "uid": uid,
            "nombre": profile.get("nombre", ""),
            "apellido": profile.get("apellido", ""),
            "nombreCompleto": f"{profile.get('nombre', '')} {profile.get('apellido', '')}".strip(),
            "email": profile.get("email", email),
            "telefono": profile.get("telefono", ""),
            "isAdmin": is_admin_uid(uid)
        }

        doc_ref.delete()

        return jsonify({
            "ok": True,
            "message": "Contraseña actualizada correctamente.",
            "token": auth_data.get("idToken"),
            "refreshToken": auth_data.get("refreshToken"),
            "user": user
        }), 200

    except Exception as e:
        print("[CONFIRM_RESET_PASSWORD] Error:", type(e).__name__, str(e))
        return fail(f"No se pudo actualizar la contraseña: {e}", 500)


@app.route("/api/auth/resend-reset-code", methods=["POST"])
def api_resend_reset_code():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email", ""))

    if not email:
        return fail("Debes enviar el correo", 400)

    try:
        doc_ref = reset_doc_ref(email)
        snap = doc_ref.get()

        if not snap.exists:
            return fail("No existe una solicitud de recuperación para este correo", 404)

        reg = snap.to_dict() or {}

        # Verifica que el usuario siga existiendo
        get_firebase_user_by_email(email)

        code = generate_six_digit_code()
        now = now_mx()
        expires_at = now + timedelta(minutes=RESET_CODE_MINUTES)

        doc_ref.update({
            "code": code,
            "attempts": 0,
            "verified": False,
            "updated_at": now.isoformat(),
            "code_expires_at": expires_at.isoformat()
        })

        send_reset_code_email(email, code)

        return jsonify({
            "ok": True,
            "message": "Te enviamos un nuevo código de recuperación."
        }), 200

    except firebase_auth.UserNotFoundError:
        return fail("Correo no registrado.", 404)
    except Exception as e:
        print("[RESEND_RESET_CODE] Error:", type(e).__name__, str(e))
        return fail(f"No se pudo reenviar el código de recuperación: {e}", 500)


# =========================================================
# AUTH LOGIN / ME
# =========================================================
@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}

    email = normalize_email(data.get("email", ""))
    password = str(data.get("password", "")).strip()

    if not email or not password:
        return fail("Completa correo y contraseña", 400)

    try:
        auth_data = firebase_sign_in(email, password)
        uid = auth_data.get("localId", "")

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

    ahora = now_mx().isoformat()

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
        "created_at": ahora,
        "updated_at": ahora,
        "activado_hmi": False,
        "hmi_activated_at": ""
    }

    ref = fs.collection("pedidos").document()
    ref.set(payload)

    return jsonify({
        "ok": True,
        "message": "Pedido registrado correctamente",
        "order": {
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
    doc, folio_normalizado = get_order_doc_by_folio(folio)

    if not doc:
        return fail(f"No se encontró ningún pedido con ID {folio_normalizado or folio}", 404)

    return jsonify({
        "ok": True,
        "order": order_doc_to_json(doc)
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

    ahora = now_mx().isoformat()

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
        "created_at": ahora,
        "updated_at": ahora,
        "activado_hmi": False,
        "hmi_activated_at": ""
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
# ENDPOINTS WORKER
# =========================================================
@app.route("/api/worker/next-order", methods=["GET"])
def api_worker_next_order():
    docs = fs.collection("pedidos").where("Estado", "==", "pendiente").stream()
    items = []

    for doc in docs:
        item = order_doc_to_json(doc)
        created_at = item.get("created_at", "")

        if not was_created_after_backend_start(created_at):
            print(
                f"[WORKER] Ignorando pedido viejo: "
                f"{item.get('Folio', '')} | created_at={created_at} | backend_start={BACKEND_START_TIME_ISO}"
            )
            continue

        if not item.get("activado_hmi", False):
            print(
                f"[WORKER] Pedido aún no activado desde HMI: "
                f"{item.get('Folio', '')}"
            )
            continue

        items.append(item)

    items.sort(key=lambda x: x.get("Contador", 0))

    if not items:
        return jsonify({"ok": True, "order": None}), 200

    print(
        f"[WORKER] Entregando pedido nuevo: "
        f"{items[0].get('Folio', '')} | created_at={items[0].get('created_at', '')}"
    )
    return jsonify({"ok": True, "order": items[0]}), 200


@app.route("/api/worker/activate-by-folio/<folio>", methods=["POST"])
def api_worker_activate_by_folio(folio):
    doc, folio_normalizado = get_order_doc_by_folio(folio)

    if not doc:
        return fail(f"Pedido no encontrado para el folio {folio_normalizado or folio}", 404)

    doc_ref = fs.collection("pedidos").document(doc.id)
    snap = doc_ref.get()
    data = snap.to_dict() or {}

    ahora = now_mx().isoformat()
    payload = {
        "activado_hmi": True,
        "hmi_activated_at": ahora,
        "updated_at": ahora
    }

    if not data.get("Estado"):
        payload["Estado"] = "pendiente"

    doc_ref.update(payload)

    updated = doc_ref.get()
    return jsonify({
        "ok": True,
        "message": f"Pedido activado en HMI: {folio_normalizado}",
        "order": order_doc_to_json(updated)
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
        "activado_hmi": True,
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

    estados_validos = {"pendiente", "en_proceso", "planchado", "listo", "entregado"}

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
        payload["activado_hmi"] = True
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
    ruta_local = ""
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
        stamp = now.strftime("%Y%m%d_%H%M%S_%f")

        nombre_seguro = secure_filename(archivo.filename)
        nombre_final = f"{order_id}_{stamp}_{nombre_seguro}"
        ruta_local = os.path.join(app.config["UPLOAD_FOLDER"], nombre_final)

        print("[PHOTO] Guardando temporal en:", ruta_local)
        archivo.save(ruta_local)

        if not os.path.exists(ruta_local):
            raise RuntimeError("No se pudo guardar el archivo temporal")

        tam = os.path.getsize(ruta_local)
        print("[PHOTO] Tamaño archivo local:", tam)

        if tam == 0:
            raise RuntimeError("El archivo guardado quedó vacío")

        content_type = archivo.mimetype or "image/jpeg"

        bucket = storage.bucket()
        blob = bucket.blob(f"pedidos/{order_id}/{nombre_final}")
        blob.upload_from_filename(ruta_local, content_type=content_type)
        blob.make_public()

        foto_info = {
            "nombre": nombre_final,
            "url": blob.public_url,
            "content_type": content_type,
            "fecha": fecha,
            "hora": hora,
            "fecha_hora": f"{fecha} {hora}",
            "timestamp": now.isoformat(),
            "size_bytes": tam,
            "storage_path": blob.name
        }

        data = snap.to_dict() or {}
        fotos = data.get("fotos", [])
        if not isinstance(fotos, list):
            fotos = []

        fotos.append(foto_info)

        doc_ref.update({
            "fotos": fotos,
            "updated_at": now.isoformat()
        })

        try:
            fotos_ref.push({
                "order_id": order_id,
                **foto_info
            })
        except Exception as e:
            print("[PHOTO] Aviso al guardar copia en RTDB:", e)

        print("[PHOTO] Foto guardada correctamente en Storage para pedido:", order_id)

        return jsonify({
            "ok": True,
            "message": "Foto agregada al pedido",
            "foto": foto_info
        }), 200

    except Exception as e:
        print("[PHOTO] Error:", type(e).__name__, str(e))
        return fail(f"Error interno al guardar la foto: {type(e).__name__}: {e}", 500)

    finally:
        if ruta_local and os.path.exists(ruta_local):
            try:
                os.remove(ruta_local)
            except Exception as e:
                print("[PHOTO] No se pudo borrar temporal:", e)


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
# ENDPOINTS LEGACY
# =========================================================
@app.route("/estado", methods=["GET"])
def obtener_estado():
    return jsonify({
        "ok": True,
        "data": estado_memoria,
        "backend_start_time": BACKEND_START_TIME_ISO
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
    ruta_local = ""
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
        stamp = now.strftime("%Y%m%d_%H%M%S_%f")

        nombre_seguro = secure_filename(archivo.filename)
        nombre_final = f"u{usuario}_{stamp}_{nombre_seguro}"
        ruta_local = os.path.join(app.config["UPLOAD_FOLDER"], nombre_final)

        print("[SUBIR_FOTO] Guardando temporal en:", ruta_local)
        archivo.save(ruta_local)

        if not os.path.exists(ruta_local):
            raise RuntimeError("No se pudo guardar el archivo")

        tam = os.path.getsize(ruta_local)
        print("[SUBIR_FOTO] Tamaño archivo local:", tam)

        if tam == 0:
            raise RuntimeError("El archivo guardado quedó vacío")

        content_type = archivo.mimetype or "image/jpeg"

        bucket = storage.bucket()
        blob = bucket.blob(f"usuarios/{usuario}/{nombre_final}")
        blob.upload_from_filename(ruta_local, content_type=content_type)
        blob.make_public()

        foto_info = {
            "usuario": usuario,
            "nombre": nombre_final,
            "url": blob.public_url,
            "content_type": content_type,
            "fecha": fecha,
            "hora": hora,
            "etiqueta": f"{usuario}_{fecha}_{hora}",
            "timestamp": now.isoformat(),
            "size_bytes": tam,
            "storage_path": blob.name
        }

        fotos_ref.push(foto_info)

        print("[SUBIR_FOTO] Foto guardada correctamente para usuario:", usuario)

        return jsonify({
            "ok": True,
            "message": "Foto subida correctamente",
            "foto": foto_info
        }), 200

    except Exception as e:
        print("[SUBIR_FOTO] Error:", type(e).__name__, str(e))
        return fail(f"Error interno al subir la foto: {type(e).__name__}: {e}", 500)

    finally:
        if ruta_local and os.path.exists(ruta_local):
            try:
                os.remove(ruta_local)
            except Exception as e:
                print("[SUBIR_FOTO] No se pudo borrar temporal:", e)


@app.route("/fotos_usuario/<usuario>", methods=["GET"])
def fotos_usuario(usuario):
    usuario = str(usuario).strip()

    if not usuario.isdigit() or len(usuario) > 5:
        return fail("Usuario inválido", 400)

    data = fotos_ref.get() or {}
    items = []

    for _, item in data.items():
        if str(item.get("usuario", "")).strip() == usuario:
            items.append(item)

    items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    return jsonify({
        "ok": True,
        "fotos": items
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
