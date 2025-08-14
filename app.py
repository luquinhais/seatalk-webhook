# app.py
import os, time, json, hashlib, requests
from collections import deque
from flask import Flask, request, jsonify

app = Flask(__name__)

# ========= ENV =========
AUTH_URL       = "https://openapi.seatalk.io/auth/app_access_token"
UPDATE_URL     = os.getenv("SEATALK_UPDATE_URL", "https://openapi.seatalk.io/messaging/v2/update")
SEND_URL       = (os.getenv("SEATALK_GROUP_SEND_URL") or "").strip()
GROUP_ID       = (os.getenv("SEATALK_GROUP_ID") or "").strip()

APP_ID         = (os.getenv("SEATALK_APP_ID") or "").strip()
APP_SECRET     = (os.getenv("SEATALK_APP_SECRET") or "").strip()
SIGNING_SECRET = (os.getenv("SEATALK_SIGNING_SECRET") or "").strip()

# ========= TOKEN CACHE =========
_token = {"v": None, "exp": 0}
def get_token():
    if not APP_ID or not APP_SECRET:
        raise RuntimeError("SEATALK_APP_ID/SEATALK_APP_SECRET ausentes")
    now = int(time.time())
    if _token["v"] and now < _token["exp"] - 60:
        return _token["v"]
    r = requests.post(AUTH_URL, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=8)
    data = r.json()
    token = data.get("access_token") or data.get("app_access_token")
    exp   = now + int(data.get("expires_in") or data.get("expire") or 7200)
    if not token:
        raise RuntimeError(f"Falha ao obter token: {data}")
    _token.update({"v": token, "exp": exp})
    return token

# ========= ENVIOS P/ GRUPO (API) =========
def send_group_text(text: str):
    if not SEND_URL or not GROUP_ID:
        raise RuntimeError("SEATALK_GROUP_SEND_URL/SEATALK_GROUP_ID ausentes")
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"group_id": GROUP_ID, "message": {"tag": "text", "text": {"content": text}}}
    r = requests.post(SEND_URL, headers=headers, json=payload, timeout=8)
    print("send_group_text:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()

def send_group_interactive(protocolo="TESTE123"):
    if not SEND_URL or not GROUP_ID:
        raise RuntimeError("SEATALK_GROUP_SEND_URL/SEATALK_GROUP_ID ausentes")
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

# ========= UPDATE DE CARD =========
def update_card(message_id: str, elements: list):
    """Atualiza o card interativo. Requer permissão do mesmo app que enviou a msg."""
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"message_id": message_id, "message": {"interactive_message": {"elements": elements}}}
    r = requests.post(UPDATE_URL, headers=headers, json=payload, timeout=8)
    try: j = r.json()
    except Exception: j = {"raw": r.text}
    print("update_card:", r.status_code, j)
    return r.status_code, j

# ========= UTILS =========
def expected_signature(raw: bytes) -> str:
    return hashlib.sha256(raw + SIGNING_SECRET.encode()).hexdigest()

def extract_protocolo(evt: dict) -> str:
    v = evt.get("value")
    if isinstance(v, str):
        try: v = json.loads(v)
        except Exception: return "-"
    if isinstance(v, dict): return str(v.get("protocolo", "-"))
    return "-"

_recent = deque(maxlen=512)
def seen(event_id: str) -> bool:
    if not event_id: return False
    if event_id in _recent: return True
    _recent.append(event_id); return False

# ========= ROTAS =========
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

# Callback “oficial”
@app.post("/callback")
def seatalk_callback():
    raw = request.get_data()             # bytes crus
    data = request.get_json(force=True)  # dict
    etype = data.get("event_type") or ""
    sig   = request.headers.get("Signature") or request.headers.get("signature") or ""

    # 1) Verificação de URL
    if etype == "event_verification":
        ch = data.get("event", {}).get("seatalk_challenge")
        return jsonify({"seatalk_challenge": ch}), 200

    # 2) Validação de assinatura (demais eventos)
    if SIGNING_SECRET:
        if expected_signature(raw) != sig:
            return "unauthorized", 403

    # 3) Dedupe
    if seen(data.get("event_id")):
        return "ok", 200

    print("✅ Evento recebido:", data)

    # 4) Clique no botão → tenta atualizar; se não rolar, fallback
    if etype == "interactive_message_click":
        evt = data.get("event", {}) or {}
        msg_id = evt.get("message_id")
        protocolo = extract_protocolo(evt)

        updated = False
        try:
            status, body = update_card(msg_id, [
                {"element_type": "description",
                 "description": {"text": "Obrigado por responder ✅"}}
            ])
            if status == 200 and str(body.get("code", 0)) == "0":
                updated = True
        except Exception as e:
            print("❌ update_card erro:", repr(e))

        if not updated:
            try:
                send_group_text(f"Obrigado por responder ✅ (Protocolo: {protocolo})")
            except Exception as e:
                print("❌ fallback envio erro:", repr(e))

    return "ok", 200

# ✅ Aceitar POST também na raiz (a plataforma às vezes posta em "/")
@app.post("/")
def seatalk_callback_root():
    return seatalk_callback()

# ===== Rotas de teste =====
@app.post("/test/send-text")
def http_send_text():
    body = request.get_json(silent=True) or {}
    txt = body.get("text", "Ping via API ✅")
    try:
        return jsonify(send_group_text(txt)), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/test/send-interactive")
def http_send_interactive():
    body = request.get_json(silent=True) or {}
    protocolo = body.get("protocolo", "API123")
    try:
        return jsonify(send_group_interactive(protocolo)), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
