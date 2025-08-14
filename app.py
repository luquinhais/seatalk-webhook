# app.py
import os, time, json, hashlib, requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

# ========= SeaTalk =========
AUTH_URL        = "https://openapi.seatalk.io/auth/app_access_token"
UPDATE_URL      = "https://openapi.seatalk.io/messaging/v2/update"
CONTACTS_URL    = "https://openapi.seatalk.io/contacts/v2/get_employee_code_with_email"
SINGLE_DM_URL   = "https://openapi.seatalk.io/messaging/v2/single_chat"

SEATALK_APP_ID     = (os.getenv("SEATALK_APP_ID") or "").strip()
SEATALK_APP_SECRET = (os.getenv("SEATALK_APP_SECRET") or "").strip()
SEATALK_SIGNING_SECRET = (os.getenv("SEATALK_SIGNING_SECRET") or "").strip()

# ========= Google Sheets (Service Account) =========
GOOGLE_SHEET_ID   = (os.getenv("GOOGLE_SHEET_ID") or "").strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "seatalk_logs")

_gspread_client = None
def _get_gspread_client():
    """Inicializa gspread com service account do env GOOGLE_CREDENTIALS_JSON."""
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

def _ensure_headers(ws):
    """Garante cabeçalho A..E na linha 1."""
    try:
        values = ws.get_values("A1:E1")
    except Exception:
        values = []
    if not values or not any(values[0]):
        ws.update("A1:E1", [[
            "timestamp_utc", "email_or_id", "action", "message_id", "group_id"
        ]])

def _append_click_row(ts_iso, email_or_id, action, message_id, group_id):
    """Append fixo nas colunas A..E."""
    if not GOOGLE_SHEET_ID:
        return
    gc = _get_gspread_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(GOOGLE_SHEET_NAME)
    except Exception:
        ws = sh.add_worksheet(GOOGLE_SHEET_NAME, rows=100, cols=10)
    _ensure_headers(ws)
    ws.append_row(
        [ts_iso, email_or_id, action, message_id, group_id],
        value_input_option="USER_ENTERED"
    )

# ========= Token cache =========
_token = {"v": None, "exp": 0}
def get_token():
    if not SEATALK_APP_ID or not SEATALK_APP_SECRET:
        raise RuntimeError("SEATALK_APP_ID/SEATALK_APP_SECRET ausentes")
    now = int(time.time())
    if _token["v"] and now < _token["exp"] - 60:
        return _token["v"]
    r = requests.post(AUTH_URL, json={
        "app_id": SEATALK_APP_ID, "app_secret": SEATALK_APP_SECRET
    }, timeout=10)
    data = r.json()
    token = data.get("access_token") or data.get("app_access_token")
    exp   = now + int(data.get("expires_in") or data.get("expire") or 7200)
    if not token:
        raise RuntimeError(f"Falha ao obter token: {data}")
    _token.update({"v": token, "exp": exp})
    return token

# ========= Utils =========
def expected_signature(raw: bytes) -> str:
    """Alguns tenants usam SHA256(body + secret) em hex."""
    if not SEATALK_SIGNING_SECRET:
        return ""
    return hashlib.sha256(raw + SEATALK_SIGNING_SECRET.encode()).hexdigest()

def _extract_action(value):
    """value vem do botão 'callback' (string JSON ou dict)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return "-"
    if isinstance(value, dict):
        return str(value.get("acao", "-"))
    return "-"

def update_card(message_id: str, elements: list):
    """Atualiza o card: tenta payload 'puro' e, se precisar, 'com tag'."""
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Tentativa #1: payload 'puro'
    payload1 = {"message_id": message_id, "message": {"interactive_message": {"elements": elements}}}
    r1 = requests.post(UPDATE_URL, headers=headers, json=payload1, timeout=10)
    print("update #1:", r1.status_code, r1.text)
    ok1 = False
    try:
        j1 = r1.json()
        ok1 = (r1.status_code == 200 and str(j1.get("code", 0)) == "0")
    except Exception:
        j1 = {"raw": r1.text}
    if ok1:
        return j1

    # Tentativa #2: payload com tag
    payload2 = {"message_id": message_id, "message": {"tag": "interactive_message", "interactive_message": {"elements": elements}}}
    r2 = requests.post(UPDATE_URL, headers=headers, json=payload2, timeout=10)
    print("update #2:", r2.status_code, r2.text)
    r2.raise_for_status()
    try:
        return r2.json()
    except Exception:
        return {"raw": r2.text}

# ========= Health =========
@app.get("/")
def health():
    return "ok", 200

# ========= Callback oficial =========
@app.post("/callback")
def seatalk_callback():
    raw = request.get_data()
    data = request.get_json(force=True)
    etype = str(data.get("event_type", ""))
    sig   = request.headers.get("Signature") or request.headers.get("signature") or ""

    # 1) Verificação
    if etype == "event_verification":
        ch = (data.get("event") or {}).get("seatalk_challenge")
        return jsonify({"seatalk_challenge": ch}), 200

    # 2) Assinatura (opcional; não bloqueia por padrão)
    if SEATALK_SIGNING_SECRET:
        calc = expected_signature(raw)
        if not sig or calc.lower() != sig.lower():
            print("signature mismatch", sig, calc)
            # return "unauthorized", 403  # habilite se quiser bloquear

    # 3) Clique em card
    if etype == "interactive_message_click":
        evt        = data.get("event") or {}
        message_id = str(evt.get("message_id", ""))
        action     = _extract_action(evt.get("value"))
        email_or_id= str(evt.get("email") or evt.get("seatalk_id") or "")
        group_id   = str(evt.get("group_id") or evt.get("chat_id") or "")

        # Log em planilha (não bloqueia a resposta ao SeaTalk)
        try:
            ts_iso = datetime.now(timezone.utc).isoformat()
            _append_click_row(ts_iso, email_or_id, action, message_id, group_id)
        except Exception as e:
            print("sheets log error:", repr(e))

        # Atualiza o card
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

    # Outros eventos: opcionalmente ignore ou logue
    return "ok", 200

# Aceita POST na raiz também (alguns ambientes chamam "/")
@app.post("/")
def seatalk_callback_root():
    return seatalk_callback()

# ========= Rota de TESTE: envia card com 3 botões para um e-mail =========
@app.post("/test/send-interactive-3")
def test_send_interactive_3():
    try:
        body = request.get_json(silent=True) or {}
        email = body.get("email") or os.getenv("TEST_EMAIL") or ""
        if not email:
            return jsonify({"error": "Informe 'email' no body ou defina TEST_EMAIL"}), 400

        token = get_token()
        # 1) resolve employee_code pelo e-mail
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        r = requests.post(CONTACTS_URL, headers=h, json={"emails":[email]}, timeout=10)
        r.raise_for_status()
        j = r.json()
        if j.get("code") != 0 or not j.get("employees"):
            return jsonify({"error":"Falha ao obter employee_code", "raw": j}), 400
        emp = next((e for e in j["employees"] if e.get("employee_status") == 2), None)
        if not emp:
            return jsonify({"error":"Usuário não ativo", "raw": j}), 400
        employee_code = emp["employee_code"]

        # 2) monta card com 3 botões (callback)
        card = {
            "elements": [
                {"element_type":"title", "title":{"text":"📌 Confirme sua leitura"}},
                {"element_type":"description", "description":{"format":1, "text":"Escolha uma das opções abaixo."}},
                {"element_type":"button", "button":{"button_type":"callback", "text":"✅ Sim",    "value": json.dumps({"acao":"sim"})}},
                {"element_type":"button", "button":{"button_type":"callback", "text":"❌ Não",    "value": json.dumps({"acao":"nao"})}},
                {"element_type":"button", "button":{"button_type":"callback", "text":"🤔 Talvez", "value": json.dumps({"acao":"talvez"})}}
            ]
        }
        payload = {"employee_code": employee_code, "message":{"tag":"interactive_message","interactive_message": card}}
        r2 = requests.post(SINGLE_DM_URL, headers=h, json=payload, timeout=10)
        print("send 3-buttons:", r2.status_code, r2.text)
        r2.raise_for_status()
        return jsonify(r2.json()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
