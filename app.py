# Step 1: Import libraries
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from pymodbus.client import ModbusTcpClient
import cv2
import os
import time
import threading
from datetime import datetime

# Step 2: Create Flask app
app = Flask(__name__)

# Step 3: Enable CORS for GitHub Pages or any frontend
CORS(app)

# =========================================================
# Step 4: PLC CONFIG
# =========================================================
PLC_IP = "192.168.3.151"
PLC_PORT = 502
PLC_UNIT_ID = 1

COIL_CMD_START = 0
COIL_CMD_STOP = 1

# =========================================================
# Step 5: CAMERA CONFIG
# =========================================================
CARPETA = "fotos"
os.makedirs(CARPETA, exist_ok=True)

cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("❌ No se pudo abrir la cámara")
    exit()

cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

time.sleep(3)
print("✅ Cámara abierta correctamente")

# =========================================================
# Step 6: GLOBAL STATE
# =========================================================
estado_sistema = {
    "trabajo_activo": False,
    "total_prendas": 0,
    "prenda_actual": 0,
    "estado": "Esperando trabajo",
    "fotos": [],
    "error": None
}

lock = threading.Lock()

# =========================================================
# Step 7: MODBUS FUNCTIONS
# =========================================================
def write_coil_value(coil_address, value):
    client = ModbusTcpClient(PLC_IP, port=PLC_PORT)

    try:
        if not client.connect():
            return False, "No se pudo conectar al PLC"

        result = client.write_coil(coil_address, value, device_id=PLC_UNIT_ID)

        if result.isError():
            return False, f"Error Modbus al escribir coil {coil_address}"

        return True, f"Coil {coil_address} escrita con valor {value}"

    except Exception as e:
        return False, str(e)

    finally:
        client.close()


def pulse_coil(coil_address, pulse_time=0.2):
    ok1, msg1 = write_coil_value(coil_address, True)
    print(f"[MODBUS] ON coil {coil_address}: {ok1}, {msg1}")

    if not ok1:
        return False, msg1

    time.sleep(pulse_time)

    ok2, msg2 = write_coil_value(coil_address, False)
    print(f"[MODBUS] OFF coil {coil_address}: {ok2}, {msg2}")

    if not ok2:
        return False, msg2

    return True, "Pulso enviado correctamente"


def startMotor():
    print("[PLC] startMotor()")

    ok0, msg0 = write_coil_value(COIL_CMD_STOP, False)
    print("[PLC] limpiar STOP:", ok0, msg0)

    if not ok0:
        return False, msg0

    time.sleep(0.1)

    ok1, msg1 = pulse_coil(COIL_CMD_START)
    print("[PLC] START:", ok1, msg1)

    if not ok1:
        return False, msg1

    return True, "Motor iniciado"


def stopMotor():
    print("[PLC] stopMotor()")

    ok0, msg0 = write_coil_value(COIL_CMD_START, False)
    print("[PLC] limpiar START:", ok0, msg0)

    if not ok0:
        return False, msg0

    time.sleep(0.1)

    ok1, msg1 = pulse_coil(COIL_CMD_STOP)
    print("[PLC] STOP:", ok1, msg1)

    if not ok1:
        return False, msg1

    return True, "Motor detenido"

# =========================================================
# Step 8: PHOTO FUNCTION
# =========================================================
def capturar_foto(indice):
    global cap

    ret = False
    frame = None

    for _ in range(15):
        ret, frame = cap.read()
        time.sleep(0.03)

    if not ret or frame is None:
        return None, "No se pudo capturar la imagen"

    nombre = datetime.now().strftime(f"prenda_{indice}_%Y%m%d_%H%M%S.jpg")
    ruta = os.path.join(CARPETA, nombre)

    cv2.imwrite(ruta, frame)

    return ruta, None

# =========================================================
# Step 9: AUTOMATIC BATCH
# =========================================================
def ejecutar_lote(total_prendas, tiempo_giro=5.0, tiempo_estabilizacion=1.0):
    with lock:
        estado_sistema["trabajo_activo"] = True
        estado_sistema["total_prendas"] = total_prendas
        estado_sistema["prenda_actual"] = 0
        estado_sistema["estado"] = "Iniciando lote"
        estado_sistema["fotos"] = []
        estado_sistema["error"] = None

    try:
        for i in range(1, total_prendas + 1):
            print(f"\n========== CICLO {i} DE {total_prendas} ==========")

            with lock:
                estado_sistema["prenda_actual"] = i
                estado_sistema["estado"] = f"Prenda {i}/{total_prendas}: iniciando motor"

            ok, msg = startMotor()
            print("[LOTE] Resultado startMotor:", ok, msg)

            if not ok:
                raise Exception(f"Error al iniciar motor: {msg}")

            with lock:
                estado_sistema["estado"] = f"Prenda {i}/{total_prendas}: motor girando durante {tiempo_giro} s"

            time.sleep(tiempo_giro)

            ok, msg = stopMotor()
            print("[LOTE] Resultado stopMotor:", ok, msg)

            if not ok:
                raise Exception(f"Error al detener motor: {msg}")

            with lock:
                estado_sistema["estado"] = f"Prenda {i}/{total_prendas}: esperando estabilización"

            time.sleep(tiempo_estabilizacion)

            with lock:
                estado_sistema["estado"] = f"Prenda {i}/{total_prendas}: tomando foto"

            ruta, error = capturar_foto(i)
            print("[LOTE] Resultado capturar_foto:", ruta, error)

            if error:
                raise Exception(error)

            with lock:
                estado_sistema["fotos"].append(ruta)
                estado_sistema["estado"] = f"Prenda {i}/{total_prendas}: foto tomada"

            time.sleep(0.3)

        with lock:
            estado_sistema["estado"] = "Lote terminado correctamente"
            estado_sistema["trabajo_activo"] = False

        print("✅ Lote terminado correctamente")

    except Exception as e:
        print("❌ Error en lote:", e)

        with lock:
            estado_sistema["estado"] = "Error en ejecución"
            estado_sistema["error"] = str(e)
            estado_sistema["trabajo_activo"] = False

# =========================================================
# Step 10: ROUTES
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "message": "Backend activo",
        "endpoints": [
            "/estado",
            "/iniciar_lote",
            "/start",
            "/stop",
            "/tomar_foto",
            "/fotos/<nombre_archivo>"
        ]
    })


@app.route("/estado", methods=["GET"])
def estado():
    with lock:
        return jsonify(estado_sistema)


@app.route("/tomar_foto", methods=["POST"])
def tomar_foto():
    ruta, error = capturar_foto(0)

    if error:
        return jsonify({"ok": False, "error": error}), 500

    return jsonify({
        "ok": True,
        "archivo": ruta
    })


@app.route("/iniciar_lote", methods=["POST"])
def iniciar_lote():
    data = request.get_json()
    cantidad = data.get("cantidad")

    if not isinstance(cantidad, int) or cantidad <= 0:
        return jsonify({"ok": False, "message": "Cantidad inválida"}), 400

    with lock:
        if estado_sistema["trabajo_activo"]:
            return jsonify({"ok": False, "message": "Ya hay un lote en ejecución"}), 400

    hilo = threading.Thread(
        target=ejecutar_lote,
        args=(cantidad, 5.0, 1.0),
        daemon=False
    )
    hilo.start()

    return jsonify({
        "ok": True,
        "message": f"Lote iniciado con {cantidad} prendas"
    })


@app.route("/start", methods=["POST"])
def start_manual():
    ok, msg = startMotor()

    if ok:
        return jsonify({"ok": True, "message": msg})

    return jsonify({"ok": False, "message": f"Error: {msg}"}), 500


@app.route("/stop", methods=["POST"])
def stop_manual():
    ok, msg = stopMotor()

    if ok:
        return jsonify({"ok": True, "message": msg})

    return jsonify({"ok": False, "message": f"Error: {msg}"}), 500


@app.route("/fotos/<path:nombre_archivo>", methods=["GET"])
def ver_foto(nombre_archivo):
    return send_from_directory(CARPETA, nombre_archivo)


# =========================================================
# Step 11: MAIN
# =========================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
