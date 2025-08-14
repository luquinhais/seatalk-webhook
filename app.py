from flask import Flask, request, jsonify
import hashlib, os, json, requests, time

app = Flask(__name__)

SIGNING_SECRET   = os.getenv("SEATALK_SIGNING_SECRET", "")
APP_ID           = os.getenv("SEATALK_APP_ID", "")
APP_SECRET       = os.getenv("SEATALK_APP_SECRET", "")
AUTH_URL         = "https://openapi.seatalk.io/auth/app_access_token"
UPDATE_URL       = os.getenv("SEATALK_UPDATE_URL", "")  # exato conforme a doc “Update Interactive Message Card”

# cache simples do token para reduzir chamadas
_token_cache = {"token": None, "exp": 0}

def get_app_token():
    now = int(time.time())
    if _token_cache["token"] and now < _token_cache["exp"] - 60:
        return _token_cache["token"]
    resp = requests.post(AUTH_URL, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=5)
    data = resp.json()
    # Seatalk retorna { code:0, access_token/app_access_token, expires_in/expire }
    token = data.get("access_token") or data.get("app_access_token")
    exp   = now + int(data.get("expires_in") or data.get("expire") or 7200)
    if not token:
        raise RuntimeError(f"Falha ao obter token: {data}")
    _token_cache.update({"token": token, "exp": exp})
    return token

def update_interactive_message(message_id: str, elements: list):
    """
    Envia a atualização do card interativo para substituir os elementos (removendo botões).
    O body segue o EXEMPLO da doc que você recebeu.
    """
    if not UPDATE_URL:
        raise RuntimeError("Configure SEATALK_UPDATE_URL com o endpoint da API de update de card.")
    token = get_app_token()
    payload = {
        "message_id": message_id,
        "message": {
            "interactive_message": {
                "elements": elements
            }
        }
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(UPDATE_URL, headers=headers, json=payload, timeout=5)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}
    print("↪️ update_card resp:", r.status_code, j)
    return r.status_code, j

@app.get("/")
def health():
    return "ok", 200

@app.post("/")
def callback():
    body = request.get_data()                          # bytes do corpo cru
    data = request.get_json(force=True)                # dict
    signature = request.headers.get("Signature") or request.headers.get("signature") or ""

    # 1) Verificação do callback URL → responde JSON com seatalk_challenge
    if data.get("event_type") == "event_verification":
        challenge = data.get("event", {}).get("seatalk_challenge")
        return jsonify({"seatalk_challenge": challenge}), 200  # JSON correto (doc)

    # 2) Demais eventos: valida assinatura sha256(<body> + signing_secret)
    expected = hashlib.sha256(body + SIGNING_SECRET.encode()).hexdigest()
    if expected != signature:
        return "unauthorized", 403

    etype = data.get("event_type")
    evt   = data.get("event", {}) or {}
    print("✅ Evento recebido:", data)

    # 3) Ao clicar no botão: atualiza o card removendo o botão e mostrando “Obrigado por responder”
    if etype == "interactive_message_click":
        msg_id = evt.get("message_id")
        # monte os novos elementos (sem botão)
        new_elements = [
            {
                "element_type": "description",
                "description": { "text": "Obrigado por responder ✅" }
            }
        ]
        try:
            status, resp_json = update_interactive_message(msg_id, new_elements)
            # Opcional: se quiser tratar status != 200/code != 0, logue ou faça fallback
        except Exception as e:
            print("❌ Falha ao atualizar card:", repr(e))
            # (Opcional) Fallback: enviar uma mensagem simples no grupo via webhook de grupo
            # ... se for necessário

    # 4) Sempre responda rápido 200
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
