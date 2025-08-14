# app.py
import os, time, json, hashlib, requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify

# ==== Flask ====
app = Flask(__name__)

# ==== SeaTalk config ====
AUTH_URL   = "https://openapi.seatalk.io/auth/app_access_token"
UPDATE_URL = "https://openapi.seatalk.io/messaging/v2/update"

APP_ID         = (os.getenv("SEATALK_APP_ID") or "").strip()
APP_SECRET     = (os.getenv("SEATALK_APP_SECRET") or "").strip()
SIGNING_SECRET = (os.getenv("SEATALK_SIGNING_SECRET") or "").strip()

# ==== Google Sheets (via Service Account) ====
GOOGLE_SHEET_ID   = (os.getenv("GOOGLE_SHEET_ID") or "").strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "seatalk_logs")

# gspread lazy init
_gspread_client = None
def _get_gspread_client():
    global _gspread_client
    if _gspread_client:
        return _gspread_client
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON") or ""
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON não configurada")
    info = json.loads(creds_json)
    from google.oauth2.service_account import Credentials
    import gspread
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(info, scopes=scope)
    _gspread_client = gspread.authorize(creds)
    return _gspread_client

def _append_row(values):
    if not GOOGLE_SHEET_ID:
        return
    gc = _get_gspread_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(GOOGLE_SHEET_NAME)
    except Exception:
        ws = sh.add_worksheet(GOOGLE_SHEET_NAME, rows=100, cols=10)
    ws.append_row(values, value_input_option="USER_ENTERED")

# ==== Token cache ====
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

def expected_signature(raw: bytes) -> str:
    if not SIGNING_SECRET:
        return ""
    # mesma regra que você já usou: SHA-256 do corpo + segredo
    return hashlib.sha256(raw + SIGNING_SECRET.encode()).hexdigest()

def update_card(message_id: str, elements: list):
    """Atualiza o card. Tenta payload 'puro', depois 'com tag'."""
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # tentativa #1: payload "puro"
    payload1 = {"message_id": message_id, "message": {"interactive_message": {"elements": elements}}}
    r1 = requests.post(UPDATE_URL, headers=headers, json=payload1, timeout=8)
    print("update #1:", r1.status_code, r1.text)
    ok1 = False
    try:
        j1 = r1.json()
        ok1 = (r1.status_code == 200 and str(j1.get("code", 0)) == "0")
    except Exception:
        pass
    if ok1:
        return j1

    # tentativa #2: payload com tag
    payload2 = {"message_id": message_id, "message": {"tag":"interactive_message","interactive_message":{"elements": elements}}}
    r2 = requests.post(UPDATE_URL, headers=headers, json=payload2, timeout=8)
    print("update #2:", r2.status_code, r2.text)
    r2.raise_for_status()
    return r2.json()

def _extract_action(value):
    # value pode ser string JSON ou dict
    if isinstance(value, str):
        try: value = json.loads(value)
        except Exception: return "-"
    if isinstance(value, dict):
        return str(value.get("acao", "-"))
    return "-"

# ==== Health ====
@app.get("/")
def health():
    return "ok", 200

# ==== Callback (oficial) ====
@app.post("/callback")
def seatalk_callback():
    raw = request.get_data()  # bytes
    data = request.get_json(force=True)  # dict
    etype = str(data.get("event_type", ""))
    sig   = request.headers.get("Signature") or request.headers.get("signature") or ""

    # 1) verificação
    if etype == "event_verification":
        ch = (data.get("event") or {}).get("seatalk_challenge")
        return jsonify({"seatalk_challenge": ch}), 200

    # 2) validação de assinatura (opcional)
    if SIGNING_SECRET:
        calc = expected_signature(raw)
        if not sig or calc.lower() != sig.lower():
            print("signature mismatch", sig, calc)
            # você pode devolver 403; aqui seguimos 200 para não travar o SeaTalk
            # return "unauthorized", 403

    # 3) trata clique
    if etype == "interactive_message_click":
        evt = data.get("event") or {}
        message_id = str(evt.get("message_id", ""))
        action = _extract_action(evt.get("value"))

        # log no sheets (não bloqueia)
        try:
            ts_iso = datetime.now(timezone.utc).isoformat()
            who = str(evt.get("seatalk_id") or evt.get("email") or "")
            group = str(evt.get("group_id") or evt.get("chat_id") or "")
            _append_row([ts_iso, "interactive_message_click", message_id, group, who, action])
        except Exception as e:
            print("sheets log error:", repr(e))

        # update visual do card
        if message_id:
            try:
                elements = [
                    {"element_type": "description",
                     "description": {"text": f"Obrigado por responder ✅ ({action})", "format": 1}}
                ]
                body = update_card(message_id, elements)
                print("updated:", body)
            except Exception as e:
                print("update error:", repr(e))

        return "ok", 200

    # outros eventos (se quiser logar)
    try:
        ts_iso = datetime.now(timezone.utc).isoformat()
        _append_row([ts_iso, etype, json.dumps(data, ensure_ascii=False)])
    except Exception:
        pass

    return "ok", 200

# Também aceitar POST na raiz (algumas verificações usam "/")
@app.post("/")
def seatalk_callback_root():
    return seatalk_callback()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
