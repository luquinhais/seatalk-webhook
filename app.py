from flask import Flask, request, jsonify
import hashlib
import os

app = Flask(__name__)
SIGNING_SECRET = os.getenv("SEATALK_SIGNING_SECRET", "")

@app.get("/")
def health():
    return "ok", 200

@app.post("/")
def callback():
    body = request.get_data()                 # bytes do corpo cru
    data = request.get_json(force=True)       # dict
    # Header pode vir como "Signature" ou "signature"
    signature = request.headers.get("Signature") or request.headers.get("signature") or ""

    # 1) Verificação de URL (NÃO valide assinatura aqui)
    # Responder exatamente {"seatalk_challenge": "..."} como JSON
    if data.get("event_type") == "event_verification":
        challenge = data.get("event", {}).get("seatalk_challenge")
        if challenge:
            return jsonify({"seatalk_challenge": challenge}), 200  # JSON + content-type correto
        return "bad request", 400

    # 2) Demais eventos: validar assinatura sha256(<corpo_bruto> + signing_secret).hexdigest()
    expected = hashlib.sha256(body + SIGNING_SECRET.encode()).hexdigest()
    if expected != signature:
        return "unauthorized", 403

    # 3) Lógica normal
    print("✅ Evento recebido:", data)
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
