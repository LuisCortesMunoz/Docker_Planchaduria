import time
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

STATE = {
    "hex": "#ff0000",
    "r": 255,
    "g": 0,
    "b": 0,
    "leds": [False, False, False, False, False, False, False, False],  # 8 LEDs
    "updated_at": time.time()
}

def hex_to_rgb(hex_str):
    s = str(hex_str).strip().lstrip("#")
    if len(s) != 6:
        return 255, 0, 0
    r = int(s[0:2], 16)
    g = int(s[2:4], 16)
    b = int(s[4:6], 16)
    return r, g, b

@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "message": "Flask LED API running on Render",
        "routes": ["/api/state_leds (GET)", "/api/set_leds (POST)"]
    })

@app.get("/api/state_leds")
def get_state_leds():
    return jsonify({"ok": True, "state": STATE})

@app.post("/api/set_leds")
def set_leds():
    data = request.get_json(silent=True) or {}

    hex_color = str(data.get("hex", "#ff0000"))
    leds = data.get("leds", STATE["leds"])

    # Validate leds array
    if (not isinstance(leds, list)) or (len(leds) != 8):
        return jsonify({"ok": False, "error": "leds must be a list of 8 booleans"}), 400

    leds_bool = [bool(x) for x in leds]
    r, g, b = hex_to_rgb(hex_color)

    STATE["hex"] = hex_color
    STATE["r"] = r
    STATE["g"] = g
    STATE["b"] = b
    STATE["leds"] = leds_bool
    STATE["updated_at"] = time.time()

    return jsonify({"ok": True, "state": STATE})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
