# =========================================================
# CARGA DE FIREBASE
# =========================================================
def load_firebase_credential():
    """
    Optimizado para Render:
    Prioriza el Secret File en /etc/secrets/serviceAccountKey.json
    """
    # 1. Intentar primero por el archivo secreto (Recomendado en Render)
    if os.path.exists(FIREBASE_CRED_FILE):
        print(f"✅ Cargando credenciales desde Secret File: {FIREBASE_CRED_FILE}")
        try:
            return credentials.Certificate(FIREBASE_CRED_FILE)
        except Exception as e:
            print(f"❌ Error al leer el archivo JSON: {e}")

    # 2. Backup: Intentar por Variable de Entorno (JSON directo)
    json_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if json_env:
        print("✅ Usando FIREBASE_SERVICE_ACCOUNT_JSON desde Environment")
        data = json.loads(json_env)
        return credentials.Certificate(data)

    # 3. Backup: Intentar por Variable de Entorno (Base64)
    b64_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT_B64", "").strip()
    if b64_env:
        print("✅ Usando FIREBASE_SERVICE_ACCOUNT_B64 desde Environment")
        decoded = base64.b64decode(b64_env).decode("utf-8")
        data = json.loads(decoded)
        return credentials.Certificate(data)

    raise RuntimeError(
        f"No se encontró la llave de Firebase. Verificaste que el archivo esté en {FIREBASE_CRED_FILE}?"
    )

def init_firebase():
    global db, bucket

    print("========================================")
    print("Iniciando backend de Planchado Express")
    print("PROJECT_ID:", PROJECT_ID)
    # Verificación rápida de existencia de archivo para debug en logs de Render
    print(f"¿Existe el archivo en {FIREBASE_CRED_FILE}?:", os.path.exists(FIREBASE_CRED_FILE))
    print("========================================")

    cred = load_firebase_credential()

    if not firebase_admin._apps:
        initialize_app(cred, {
            "storageBucket": STORAGE_BUCKET
        })

    db = firestore.client()
    bucket = storage.bucket()
    print("✅ Conexión a Firestore y Storage exitosa")
