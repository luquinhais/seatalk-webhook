from flask import Flask, request, jsonify
import hashlib, os, json, time, requests
from collections import deque

app = Flask(__name__)

# --- Vars de ambiente ---
SIGNING_SECRET = os.getenv("SEATALK_SIGNING_SECRET", "")
GROUP_WEBHOOK_URL = os.getenv("SEATALK_GROUP_WEBHOOK_URL", "")  # webhook de grupo (System Account)

# --- Dedupe simples de eventos (evita duplicados) ---
_recent_event_ids = deque(maxlen=512)
def _already_processed(event_id: str) -> bool:
    if not event_id:
        return False
    if event_id in _recent_event_ids:
        return True
    _recent_event_ids.append(event_id)
    return False

# --- Helpers ---
def _send_group_text(content: str):
    """Envia uma mensagem de texto simples via webhook de grupo."""
    if not GROUP_WEBHOOK_URL:
        print("⚠️ SEATALK_GROUP_WEBHOOK_URL não configurada; pulando envio.")
        return None
    payload = {
        "tag": "text",
        "text": { "content": content }
    }
    r = requests.post(
        GROUP_WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=5
    )
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}
    print("↪️ webhook(text) resp:", r.status_code, j)
    return r.status_code, j

def _send_group_ack_interactive(protocolo: str = "-"):
    """Envia um card interativo simples de agradecimento (sem botão) via webhook."""
    if not GROUP_WEBHOOK_URL:
        print("⚠️ SEATALK_GROUP_WEBHOOK_URL não configurada; pulando envio.")
        return None
    payload = {
        "tag": "interactive_message",
        "interactive_message": {
            "elements": [
                {
                    "element_type": "description",
                    "description": { "text": f"Obrigado por responder ✅ (Protocolo: {protocolo})" }
                }
            ]
        }
    }
    r = requests.post(
        GROUP_WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=5
    )
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}
    print("↪️ webhook(interactive) resp:", r.status_code, j)
    return r.status_code, j

def _extract_protocolo(evt: dict) -> str:
    """Extrai 'protocolo' de evt.value (string JSON ou dict)."""
    try:
        value = evt.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                return "-"
        if isinstance(value, dict):
            return str(value.get("protocolo", "-"))
        return "-"
    except Exception:
        return "-"

@app.get("/")
def health():
    return "ok", 200

@app.post("/")
def callback():
    # corpo cru (bytes) e JSON (dict)
    body = request.get_data()
    data = request.get_json(force=True)

    # 1) Verificação do callback URL → responder JSON com seatalk_challenge
    if data.get("event_type") == "event_verification":
        challenge = data.get("event", {}).get("seatalk_challenge")
        return jsonify({"seatalk_challenge": challenge}), 200

    # 2) Validação de assinatura para demais eventos (sha256(<body> + signing_secret))
    signature = request.headers.get("Signature") or request.headers.get("signature") or ""
    expected = hashlib.sha256(body + SIGNING_SECRET.encode()).hexdigest()
    if expected != signature:
        return "unauthorized", 403

    # 3) Dedupe
    event_id = data.get("event_id")
    if _already_processed(event_id):
        print("ℹ️ Evento duplicado ignorado:", event_id)
        return "ok", 200

    etype = data.get("event_type")
    evt   = data.get("event", {}) or {}
    print("✅ Evento recebido:", data)

    # 4) Fallback: ao clicar no botão, enviar "Obrigado por responder" via webhook de grupo
    if etype == "interactive_message_click":
        protocolo = _extract_protocolo(evt)
        # Você pode escolher entre texto simples OU card interativo sem botão:
        # a) texto simples:
        _send_group_text(f"Obrigado por responder ✅ (Protocolo: {protocolo})")
        # b) ou card interativo sem botão:
        # _send_group_ack_interactive(protocolo)

    # 5) Responder rápido
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


