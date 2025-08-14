from flask import Flask, request, jsonify
import os, requests, time, json

app = Flask(__name__)

AUTH_URL  = "https://openapi.seatalk.io/auth/app_access_token"
SEND_URL  = os.getenv("SEATALK_GROUP_SEND_URL", "")
GROUP_ID  = os.getenv("SEATALK_GROUP_ID", "")
APP_ID    = os.getenv("SEATALK_APP_ID", "")
APP_SECRET= os.getenv("SEATALK_APP_SECRET", "")

_token = {"v": None, "exp": 0}

def _require_env():
    missing = []
    if not APP_ID:    missing.append("SEATALK_APP_ID")
    if not APP_SECRET:missing.append("SEATALK_APP_SECRET")
    if not SEND_URL:  missing.append("SEATALK_GROUP_SEND_URL")
    if not GROUP_ID:  missing.append("SEATALK_GROUP_ID")
    if missing:
        raise RuntimeError("Env vars faltando: " + ", ".join(missing))

def get_token():
    now = int(time.time())
    if _token["v"] and now < _token["exp"] - 60:
        return _token["v"]
    resp = requests.post(AUTH_URL, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=6)
    data = resp.json()
    token = data.get("access_token") or data.get("app_access_token")
    exp   = now + int(data.get("expires_in") or data.get("expire") or 7200)
    if not token:
        raise RuntimeError(f"Falha ao obter token: {data}")
    _token.update({"v": token, "exp": exp})
    return token

def send_group_text(text: str):
    _require_env()
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"group_id": GROUP_ID, "message": {"tag": "text", "text": {"content": text}}}
    r = requests.post(SEND_URL, headers=headers, json=payload, timeout=8)
    print("send_group_text:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()

def send_group_interactive(protocolo="TESTE123"):
    _require_env()
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "group_id": GROUP_ID,
        "message": {
            "tag": "interactive_message",
            "interactive_message": {
                "elements": [
                    {"element_type": "title", "title": {"text": "Teste callback (API)"}},
                    {"element_type": "description", "description": {"text": "Clique para confirmar."}},
                    {"element_type": "button", "button": {
                        "button_type": "callback",
                        "text": "Confirmar",
                        "value": json.dumps({"action":"ack","protocolo": protocolo})
                    }}
                ]
            }
        }
    }
    r = requests.post(SEND_URL, headers=headers, json=payload, timeout=8)
    print("send_group_interactive:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()

@app.get("/")
def health():
    return "ok", 200

# ---- Rotas de teste para disparar mensagens ----
@app.post("/test/send-text")
def http_send_text():
    body = request.get_json(silent=True) or {}
    txt = body.get("text", "Ping via API ✅")
    try:
        resp = send_group_text(txt)
        return jsonify(resp), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/test/send-interactive")
def http_send_interactive():
    body = request.get_json(silent=True) or {}
    protocolo = body.get("protocolo", "API123")
    try:
        resp = send_group_interactive(protocolo)
        return jsonify(resp), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# (Opcional) Se você também quiser receber eventos aqui:
# from hashlib import sha256
# SIGNING_SECRET = os.getenv("SEATALK_SIGNING_SECRET", "")
# def _sig(body: bytes) -> str: return sha256(body + SIGNING_SECRET.encode()).hexdigest()
# @app.post("/callback")
# def seatalk_callback():
#     raw = request.get_data()
#     data = request.get_json(force=True)
#     if data.get("event_type") == "event_verification":
#         ch = data.get("event", {}).get("seatalk_challenge")
#         return jsonify({"seatalk_challenge": ch}), 200
#     sig = request.headers.get("Signature") or request.headers.get("signature") or ""
#     if _sig(raw) != sig:
#         return "unauthorized", 403
#     print("Evento:", data)
#     return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
