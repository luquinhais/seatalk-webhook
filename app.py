from flask import Flask, request, jsonify
import hashlib
import os

app = Flask(__name__)
SIGNING_SECRET = os.getenv("SEATALK_SIGNING_SECRET", "teste")

@app.route("/", methods=["POST"])
def callback():
    body = request.get_data()
    data = request.get_json()
    signature = request.headers.get("Signature", "")

    # Verificação de assinatura (opcional após passar verificação)
    if data.get("event_type") != "event_verification":
        expected = hashlib.sha256(body + SIGNING_SECRET.encode()).hexdigest()
        if expected != signature:
            return "unauthorized", 403

    if data.get("event_type") == "event_verification":
        challenge = data["event"]["seatalk_challenge"]
        return challenge, 200

    print("✅ Evento recebido:", data)
    return "ok", 200
