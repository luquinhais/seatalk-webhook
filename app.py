from flask import Flask, request
import hashlib
import os

app = Flask(__name__)

SIGNING_SECRET = os.getenv("SEATALK_SIGNING_SECRET", "")

@app.route("/", methods=["GET"])
def health():
    # rota de saúde para o Render
    return "ok", 200

@app.route("/", methods=["POST"])
def callback():
    try:
        body = request.get_data()                # bytes
        data = request.get_json(force=True)      # dict
        signature = request.headers.get("Signature", "")

        # 1) Verificação de callback (NÃO valida assinatura aqui)
        if data.get("event_type") == "event_verification":
            challenge = data.get("event", {}).get("seatalk_challenge")
            if challenge:
                # precisa devolver o challenge em TEXTO PURO
                return challenge, 200
            return "bad request", 400

        # 2) Para os demais eventos: validar assinatura
        if not SIGNING_SECRET:
            # opcionalmente, rejeite se não houver segredo configurado
            return "server not configured", 500

        expected = hashlib.sha256(body + SIGNING_SECRET.encode()).hexdigest()
        if expected != signature:
            return "unauthorized", 403

        # 3) Processar eventos (ex.: clique em botão, etc.)
        print("✅ Evento recebido:", data)
        return "ok", 200

    except Exception as e:
        # log para os eventos do Render
        print("❌ Erro no callback:", repr(e))
        return "error", 500

if __name__ == "__main__":
    # Render exige que o app escute em 0.0.0.0:$PORT
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
