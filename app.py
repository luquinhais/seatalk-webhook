# app.py
import os, time, json, hashlib, requests, re, threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

# ========= SeaTalk Endpoints =========
AUTH_URL        = "https://openapi.seatalk.io/auth/app_access_token"
CONTACTS_URL    = "https://openapi.seatalk.io/contacts/v2/get_employee_code_with_email"
SINGLE_DM_URL   = "https://openapi.seatalk.io/messaging/v2/single_chat"
GROUP_DM_URL    = "https://openapi.seatalk.io/messaging/v2/group_chat"
UPDATE_URL      = "https://openapi.seatalk.io/messaging/v2/update"  # não será usado, mas mantido p/ referência

# ========= Config (env) =========
SEATALK_APP_ID         = (os.getenv("SEATALK_APP_ID") or "").strip()
SEATALK_APP_SECRET     = (os.getenv("SEATALK_APP_SECRET") or "").strip()
SEATALK_SIGNING_SECRET = (os.getenv("SEATALK_SIGNING_SECRET") or "").strip()
UI_ADMIN_TOKEN         = (os.getenv("UI_ADMIN_TOKEN") or "").strip()

# Defaults (se a UI não enviar sheet_id/sheet_name)
GOOGLE_SHEET_ID   = (os.getenv("GOOGLE_SHEET_ID") or "").strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "seatalk_logs")

# ========= Google Sheets (Service Account via gspread) =========
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

def _ensure_headers(ws):
    try:
        values = ws.get_values("A1:E1")
    except Exception:
        values = []
    if not values or not any(values[0]):
        ws.update("A1:E1", [[
            "timestamp_utc", "email_or_id", "action", "message_id", "group_id"
        ]])

def _append_click_row(ts_iso, email_or_id, action, message_id, group_id, sheet_id=None, sheet_name=None):
    """Grava no Sheets; se sheet_id/name não vierem, usa os defaults das env vars."""
    sid = (sheet_id or GOOGLE_SHEET_ID or "").strip()
    sname = (sheet_name or GOOGLE_SHEET_NAME or "seatalk_logs").strip()
    if not sid:
        return  # sem planilha definida, não grava

    gc = _get_gspread_client()
    sh = gc.open_by_key(sid)
    try:
        ws = sh.worksheet(sname)
    except Exception:
        ws = sh.add_worksheet(sname, rows=100, cols=10)
    _ensure_headers(ws)
    ws.append_row([ts_iso, email_or_id, action, message_id, group_id], value_input_option="USER_ENTERED")

# ========= Token cache =========
_token = {"v": None, "exp": 0}
def get_token():
    if not SEATALK_APP_ID or not SEATALK_APP_SECRET:
        raise RuntimeError("SEATALK_APP_ID/SEATALK_APP_SECRET ausentes")
    now = int(time.time())
    if _token["v"] and now < _token["exp"] - 60:
        return _token["v"]
    r = requests.post(AUTH_URL, json={"app_id": SEATALK_APP_ID, "app_secret": SEATALK_APP_SECRET}, timeout=10)
    data = r.json()
    token = data.get("access_token") or data.get("app_access_token")
    exp   = now + int(data.get("expires_in") or data.get("expire") or 7200)
    if not token:
        raise RuntimeError(f"Falha ao obter token: {data}")
    _token.update({"v": token, "exp": exp})
    return token

# ========= Helpers =========
def expected_signature(raw: bytes) -> str:
    if not SEATALK_SIGNING_SECRET:
        return ""
    return hashlib.sha256(raw + SEATALK_SIGNING_SECRET.encode()).hexdigest()

def _extract_action(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return "-"
    if isinstance(value, dict):
        return str(value.get("acao", "-"))
    return "-"

def _extract_sheet_meta(value) -> dict:
    """Extrai sheet_id e sheet_name de evt.value (string JSON ou dict)."""
    try:
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, dict):
            sid = str(value.get("sheet_id") or "").strip()
            sname = str(value.get("sheet_name") or "").strip()
            out = {}
            if sid:
                out["sheet_id"] = sid
            if sname:
                out["sheet_name"] = sname
            return out
    except Exception:
        pass
    return {}

def _is_http_url(u: str) -> bool:
    return bool(re.match(r"^https?://", (u or "").strip(), flags=re.I))

def build_elements(title: str, desc: str, buttons: list, meta: dict | None = None) -> list:
    """Constrói card com botões de callback. meta pode conter sheet_id/sheet_name para logging."""
    els = [
        {"element_type": "title", "title": {"text": title}},
        {"element_type": "description", "description": {"format": 1, "text": desc}},
    ]
    meta = meta or {}
    for b in (buttons or [])[:3]:
        text = str(b.get("text") or "").strip()
        action = str(b.get("action") or "").strip()
        if not text or not action:
            continue
        payload = {"acao": action}
        if meta.get("sheet_id"):
            payload["sheet_id"] = str(meta["sheet_id"]).strip()
        if meta.get("sheet_name"):
            payload["sheet_name"] = str(meta["sheet_name"]).strip()

        els.append({
            "element_type": "button",
            "button": {
                "button_type": "callback",
                "text": text,
                "value": json.dumps(payload)
            }
        })
    return els

def build_redirect_elements(title: str, desc: str, redirects: list) -> list:
    els = [
        {"element_type": "title", "title": {"text": title}},
        {"element_type": "description", "description": {"format": 1, "text": desc}},
    ]
    for r in (redirects or [])[:3]:
        text = str(r.get("text") or "").strip()
        url  = str(r.get("url") or "").strip()
        if not text or not _is_http_url(url):
            continue
        els.append({
            "element_type": "button",
            "button": {
                "button_type": "redirect",
                "text": text,
                "mobile_link":  {"type": "web", "path": url},
                "desktop_link": {"type": "web", "path": url}
            }
        })
    return els

# (mantido para referência, não usado neste fluxo)
def update_card(message_id: str, elements: list):
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

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

    payload2 = {"message_id": message_id,
                "message": {"tag": "interactive_message", "interactive_message": {"elements": elements}}}
    r2 = requests.post(UPDATE_URL, headers=headers, json=payload2, timeout=10)
    print("update #2:", r2.status_code, r2.text)
    r2.raise_for_status()
    try:
        return r2.json()
    except Exception:
        return {"raw": r2.text}

def resolve_employee_code(token: str, email: str) -> str:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(CONTACTS_URL, headers=h, json={"emails": [email]}, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0 or not j.get("employees"):
        raise RuntimeError(f"Falha employee_code para {email}: {j}")
    emp = next((e for e in j["employees"] if e.get("employee_status") == 2), None)
    if not emp:
        raise RuntimeError(f"Usuário inativo: {email}")
    return emp["employee_code"]

def send_card_to_employee(token: str, employee_code: str, elements: list):
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"employee_code": employee_code,
               "message": {"tag": "interactive_message", "interactive_message": {"elements": elements}}}
    r = requests.post(SINGLE_DM_URL, headers=h, json=payload, timeout=10)
    print("send single:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()

def send_card_to_group(token: str, group_id: str, elements: list):
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"group_id": group_id,
               "message": {"tag": "interactive_message", "interactive_message": {"elements": elements}}}
    r = requests.post(GROUP_DM_URL, headers=h, json=payload, timeout=10)
    print("send group:", group_id, r.status_code, r.text)
    r.raise_for_status()
    return r.json()

def send_text_to_employee(token: str, employee_code: str, text: str):
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"employee_code": employee_code, "message": {"tag": "text", "text": {"content": text}}}
    r = requests.post(SINGLE_DM_URL, headers=h, json=payload, timeout=10)
    print("send text single:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()

def send_text_to_group(token: str, group_id: str, text: str):
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"group_id": group_id, "message": {"tag": "text", "text": {"content": text}}}
    r = requests.post(GROUP_DM_URL, headers=h, json=payload, timeout=10)
    print("send text group:", group_id, r.status_code, r.text)
    r.raise_for_status()
    return r.json()

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

    # Verificação do endpoint
    if etype == "event_verification":
        ch = (data.get("event") or {}).get("seatalk_challenge")
        return jsonify({"seatalk_challenge": ch}), 200

    # Assinatura (opcional)
    if SEATALK_SIGNING_SECRET:
        calc = expected_signature(raw)
        if not sig or calc.lower() != sig.lower():
            print("signature mismatch", sig, calc)
            # return "unauthorized", 403

    # Clique em card
    if etype == "interactive_message_click":
        evt        = data.get("event") or {}
        message_id = str(evt.get("message_id", ""))
        value      = evt.get("value")
        action     = _extract_action(value)  # apenas para log
        meta       = _extract_sheet_meta(value)  # sheet_id/sheet_name enviados no botão
        email_or_id= str(evt.get("email") or evt.get("seatalk_id") or "")
        group_id   = str(evt.get("group_id") or evt.get("chat_id") or "")

        # Log Sheets (não bloqueia)
        try:
            ts_iso = datetime.now(timezone.utc).isoformat()
            _append_click_row(
                ts_iso, email_or_id, action, message_id, group_id,
                sheet_id=meta.get("sheet_id"), sheet_name=meta.get("sheet_name")
            )
        except Exception as e:
            print("sheets log error:", repr(e))

        # NÃO atualiza o card. Apenas envia a mensagem "Resposta enviada".
        try:
            token = get_token()
            thank_msg = "Resposta enviada"
            if group_id:
                # se clique veio de grupo, responde no grupo
                send_text_to_group(token, group_id, thank_msg)
            elif email_or_id and "@" in email_or_id:
                # se clique veio de DM, responde ao usuário
                emp_code = resolve_employee_code(token, email_or_id)
                send_text_to_employee(token, emp_code, thank_msg)
            else:
                print("no direct target to thank (missing group_id/email)")
        except Exception as e:
            print("send thank text error:", repr(e))

        return "ok", 200

    return "ok", 200

# Aceita POST em "/" também (algumas verificações usam a raiz)
@app.post("/")
def seatalk_callback_root():
    return seatalk_callback()

# ========= UI (SEM f-string) =========
@app.get("/ui")
def ui_send():
    html = """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Enviar SeaTalk</title>
  <style>
    body { font-family: system-ui, Arial, sans-serif; max-width: 960px; margin: 40px auto; padding: 0 16px; }
    h1 { margin-bottom: 8px; }
    fieldset { border: 1px solid #ddd; padding: 16px; border-radius: 12px; margin-bottom: 16px; }
    label { display:block; font-size:14px; margin:10px 0 4px; }
    input[type=text], input[type=email], textarea { width:100%; padding:10px; border:1px solid #ccc; border-radius:8px; }
    textarea { min-height: 80px; }
    .row { display:grid; grid-template-columns: 1fr 1fr; gap:12px; }
    .btn { background:#111; color:#fff; border:none; padding:12px 16px; border-radius:10px; cursor:pointer; }
    .btn:hover { opacity:.9; }
    .muted { color:#666; font-size:13px; }
    .tabs { display:flex; gap:8px; margin:16px 0; flex-wrap: wrap; }
    .tab { padding:8px 12px; border:1px solid #ccc; border-radius:8px; cursor:pointer; }
    .tab.active { background:#111; color:#fff; border-color:#111; }
    .panel { display:none; }
    .panel.active { display:block; }
    .msg { white-space: pre-wrap; background:#f7f7f7; padding:10px; border-radius:8px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .notice {
      background:#0b3a7e; color:#fff; padding:14px 16px; border-radius:12px; margin:16px 0;
      line-height:1.4; box-shadow:0 2px 10px rgba(0,0,0,.06);
    }
    .notice code { background: rgba(255,255,255,.12); padding:2px 6px; border-radius:6px; color:#fff; }
    .counter { font-size:12px; margin-top:6px; }
    .counter.limit-ok { color:#666; }
    .counter.limit-warn { color:#b45309; }
    .counter.limit-bad { color:#b91c1c; }
  </style>
</head>
<body>
  <h1>Enviar SeaTalk</h1>

  <div class="notice">
    <div style="font-weight:600; margin-bottom:6px;">Instruções para menções e limites</div>
    <div style="margin-bottom:6px;">
      Para marcar usuários por e-mail, utilize a tag:
      <code>&lt;mention-tag target="seatalk://user?email=nome.sobrenome@shopee.com"/&gt;</code>
    </div>
    <div style="margin-bottom:6px;">
      Para marcar todos os participantes do grupo, utilize a tag:
      <code>&lt;mention-tag target="seatalk://user?id=0"/&gt;</code>
    </div>
    <div style="margin-bottom:6px;">
      <b>Onde inserir:</b> cole a tag no campo de <u>mensagem (descrição)</u> do card.
    </div>
    <div>
      <b>Limites:</b> Mensagem simples (sem card/botões): <b>4096</b> caracteres. —
      Mensagem com card/botões: <b>500</b> caracteres (campo de descrição).
    </div>
  </div>

  <p class="muted">Cards (callback/redirect) e texto simples. Cliques de callback chegam em <code>/callback</code> e são logados no Sheets. O card não é atualizado; apenas é enviada a mensagem "Resposta enviada".</p>

  <fieldset>
    <legend>Autorização da UI (opcional)</legend>
    <label>UI Admin Token</label>
    <input id="adm" type="text" placeholder="preencha se a UI estiver protegida com UI_ADMIN_TOKEN" />
  </fieldset>

  <fieldset>
    <legend>Destino do log (Google Sheets) — opcional</legend>
    <label>Spreadsheet ID (a Service Account precisa ter acesso de edição)</label>
    <input id="sheet_id" type="text" placeholder="ex.: 1A2b3C... (ID da planilha)" />
    <label>Sheet name</label>
    <input id="sheet_name" type="text" placeholder="ex.: seatalk_logs" />
    <p class="muted">Se você deixar em branco, serão usados os valores-padrão de ambiente (GOOGLE_SHEET_ID / GOOGLE_SHEET_NAME).</p>
  </fieldset>

  <div class="tabs">
    <div class="tab active" onclick="selTab('ind')">Card — Individual (callback)</div>
    <div class="tab" onclick="selTab('grp')">Card — Grupos (callback)</div>
    <div class="tab" onclick="selTab('redir')">Card — Grupos (redirect)</div>
    <div class="tab" onclick="selTab('txt')">Mensagem simples (texto)</div>
  </div>

  <!-- Painel: Card Individual (callback) -->
  <div id="panel-ind" class="panel active">
    <fieldset>
      <legend>Destino (Individual)</legend>
      <label>E-mails (um por linha ou separados por vírgula)</label>
      <textarea id="emails" placeholder="alguem@empresa.com&#10;outra@empresa.com"></textarea>
    </fieldset>

    <fieldset>
      <legend>Conteúdo</legend>
      <label>Título</label>
      <input id="title1" type="text" value="📌 Confirme sua leitura" />
      <label>Descrição <span class="muted">(limite: 500)</span></label>
      <textarea id="desc1" oninput="updateCount('desc1', 500)"></textarea>
      <div id="desc1_count" class="counter limit-ok">0 / 500</div>
    </fieldset>

    <fieldset>
      <legend>Botões (até 3)</legend>
      <div class="row">
        <div><label>Rótulo</label><input id="b1_text1" type="text" value="✅ Sim" /></div>
        <div><label>Ação</label><input id="b1_action1" type="text" value="sim" /></div>
      </div>
      <div class="row">
        <div><label>Rótulo</label><input id="b2_text1" type="text" value="❌ Não" /></div>
        <div><label>Ação</label><input id="b2_action1" type="text" value="nao" /></div>
      </div>
      <div class="row">
        <div><label>Rótulo</label><input id="b3_text1" type="text" value="🤔 Talvez" /></div>
        <div><label>Ação</label><input id="b3_action1" type="text" value="talvez" /></div>
      </div>
    </fieldset>

    <button class="btn" onclick="enviarInd()">Enviar (Card / Individual)</button>
    <h3>Resposta</h3>
    <pre id="out1" class="msg"></pre>
  </div>

  <!-- Painel: Card Grupos (callback) -->
  <div id="panel-grp" class="panel">
    <fieldset>
      <legend>Destino (Grupos)</legend>
      <label>Group IDs (um por linha ou separados por vírgula)</label>
      <textarea id="group_ids">OTc3OTg4MjY2NTk0
NzYzNTgyOTcyNjY0</textarea>
      <p class="muted">Ex.: Grupo A: OTc3OTg4MjY2NTk0 • Grupo B: NzYzNTgyOTcyNjY0</p>
    </fieldset>

    <fieldset>
      <legend>Conteúdo</legend>
      <label>Título</label>
      <input id="title2" type="text" value="📌 Confirme sua leitura" />
      <label>Descrição <span class="muted">(limite: 500)</span></label>
      <textarea id="desc2" oninput="updateCount('desc2', 500)"></textarea>
      <div id="desc2_count" class="counter limit-ok">0 / 500</div>
    </fieldset>

    <fieldset>
      <legend>Botões (até 3)</legend>
      <div class="row">
        <div><label>Rótulo</label><input id="b1_text2" type="text" value="✅ Sim" /></div>
        <div><label>Ação</label><input id="b1_action2" type="text" value="sim" /></div>
      </div>
      <div class="row">
        <div><label>Rótulo</label><input id="b2_text2" type="text" value="❌ Não" /></div>
        <div><label>Ação</label><input id="b2_action2" type="text" value="nao" /></div>
      </div>
      <div class="row">
        <div><label>Rótulo</label><input id="b3_text2" type="text" value="🤔 Talvez" /></div>
        <div><label>Ação</label><input id="b3_action2" type="text" value="talvez" /></div>
      </div>
    </fieldset>

    <button class="btn" onclick="enviarGrp()">Enviar (Card / Grupos / callback)</button>
    <h3>Resposta</h3>
    <pre id="out2" class="msg"></pre>
  </div>

  <!-- Painel: Card Grupos (redirect) -->
  <div id="panel-redir" class="panel">
    <fieldset>
      <legend>Destino (Grupos)</legend>
      <label>Group IDs (um por linha ou separados por vírgula)</label>
      <textarea id="group_ids_redir">OTc3OTg4MjY2NTk0
NzYzNTgyOTcyNjY0</textarea>
    </fieldset>

    <fieldset>
      <legend>Conteúdo</legend>
      <label>Título</label>
      <input id="titleR" type="text" value="🔗 Ações rápidas" />
      <label>Descrição <span class="muted">(limite: 500)</span></label>
      <textarea id="descR" oninput="updateCount('descR', 500)">Escolha um dos links abaixo para abrir.</textarea>
      <div id="descR_count" class="counter limit-ok">0 / 500</div>
    </fieldset>

    <fieldset>
      <legend>Botões Redirect (até 3)</legend>
      <div class="row">
        <div><label>Rótulo</label><input id="br1_text" type="text" value="Abrir Portal" /></div>
        <div><label>URL (https://...)</label><input id="br1_url" type="text" value="https://www.example.com" /></div>
      </div>
      <div class="row">
        <div><label>Rótulo</label><input id="br2_text" type="text" value="Docs" /></div>
        <div><label>URL (https://...)</label><input id="br2_url" type="text" value="https://www.example.com/docs" /></div>
      </div>
      <div class="row">
        <div><label>Rótulo</label><input id="br3_text" type="text" value="Help" /></div>
        <div><label>URL (https://...)</label><input id="br3_url" type="text" value="https://www.example.com/help" /></div>
      </div>
    </fieldset>

    <button class="btn" onclick="enviarGrpRedirect()">Enviar (Card / Grupos / redirect)</button>
    <h3>Resposta</h3>
    <pre id="outR" class="msg"></pre>
  </div>

  <!-- Painel: Texto simples -->
  <div id="panel-txt" class="panel">
    <fieldset>
      <legend>Individual</legend>
      <label>E-mails (um por linha ou separados por vírgula)</label>
      <textarea id="emails_txt" placeholder="alguem@empresa.com&#10;outra@empresa.com"></textarea>
      <label>Mensagem de texto <span class="muted">(limite: 4096)</span></label>
      <textarea id="text_msg_ind" oninput="updateCount('text_msg_ind', 4096)"></textarea>
      <div id="text_msg_ind_count" class="counter limit-ok">0 / 4096</div>
      <button class="btn" onclick="enviarTextoInd()">Enviar texto (Individual)</button>
    </fieldset>

    <fieldset>
      <legend>Grupos</legend>
      <label>Group IDs (um por linha ou separados por vírgula)</label>
      <textarea id="group_ids_txt">OTc3OTg4MjY2NTk0
NzYzNTgyOTcyNjY0</textarea>
      <label>Mensagem de texto <span class="muted">(limite: 4096)</span></label>
      <textarea id="text_msg_grp" oninput="updateCount('text_msg_grp', 4096)"></textarea>
      <div id="text_msg_grp_count" class="counter limit-ok">0 / 4096</div>
      <button class="btn" onclick="enviarTextoGrp()">Enviar texto (Grupos)</button>
    </fieldset>

    <h3>Resposta</h3>
    <pre id="out3" class="msg"></pre>
  </div>

<script>
function selTab(which) {
  document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); });
  document.querySelectorAll('.panel').forEach(function(p){ p.classList.remove('active'); });
  var map = {'ind':0, 'grp':1, 'redir':2, 'txt':3};
  document.querySelectorAll('.tab')[map[which]].classList.add('active');
  document.getElementById('panel-' + which).classList.add('active');
}
function parseList(txt) {
  return txt.split(/[\\n,]/).map(function(s){ return s.trim(); }).filter(Boolean);
}
function buildButtons(prefix) {
  var bts = [];
  var b1t = document.getElementById('b1_text'+prefix).value.trim();
  var b1a = document.getElementById('b1_action'+prefix).value.trim();
  var b2t = document.getElementById('b2_text'+prefix).value.trim();
  var b2a = document.getElementById('b2_action'+prefix).value.trim();
  var b3t = document.getElementById('b3_text'+prefix).value.trim();
  var b3a = document.getElementById('b3_action'+prefix).value.trim();
  if (b1t && b1a) bts.push({ text:b1t, action:b1a });
  if (b2t && b2a) bts.push({ text:b2t, action:b2a });
  if (b3t && b3a) bts.push({ text:b3t, action:b3a });
  return bts;
}
function buildRedirects() {
  var out = [];
  var t1 = document.getElementById('br1_text').value.trim();
  var u1 = document.getElementById('br1_url').value.trim();
  var t2 = document.getElementById('br2_text').value.trim();
  var u2 = document.getElementById('br2_url').value.trim();
  var t3 = document.getElementById('br3_text').value.trim();
  var u3 = document.getElementById('br3_url').value.trim();
  if (t1 && u1) out.push({ text:t1, url:u1 });
  if (t2 && u2) out.push({ text:t2, url:u2 });
  if (t3 && u3) out.push({ text:t3, url:u3 });
  return out;
}
function updateCount(id, limit) {
  var el = document.getElementById(id);
  var cnt = document.getElementById(id + '_count');
  var len = el.value.length;
  cnt.textContent = len + ' / ' + limit;
  cnt.classList.remove('limit-ok','limit-warn','limit-bad');
  if (len <= limit) {
    cnt.classList.add(len > limit*0.85 ? 'limit-warn' : 'limit-ok');
  } else {
    cnt.classList.add('limit-bad');
  }
}
async function enviarInd() {
  var adm = document.getElementById('adm').value.trim();
  var emails = parseList(document.getElementById('emails').value);
  var title  = document.getElementById('title1').value.trim();
  var desc   = document.getElementById('desc1').value.trim();
  var sheet_id   = document.getElementById('sheet_id').value.trim();
  var sheet_name = document.getElementById('sheet_name').value.trim();
  if (desc.length > 500) { alert('A descrição do card excede 500 caracteres.'); return; }
  var buttons= buildButtons('1');
  var res = await fetch('/api/send-interactive', {
    method:'POST',
    headers: { 'Content-Type':'application/json', 'X-Admin-Token': adm },
    body: JSON.stringify({ emails:emails, title:title, desc:desc, buttons:buttons, sheet_id:sheet_id, sheet_name:sheet_name })
  });
  document.getElementById('out1').textContent = await res.text();
}
async function enviarGrp() {
  var adm = document.getElementById('adm').value.trim();
  var group_ids = parseList(document.getElementById('group_ids').value);
  var title  = document.getElementById('title2').value.trim();
  var desc   = document.getElementById('desc2').value.trim();
  var sheet_id   = document.getElementById('sheet_id').value.trim();
  var sheet_name = document.getElementById('sheet_name').value.trim();
  if (desc.length > 500) { alert('A descrição do card excede 500 caracteres.'); return; }
  var buttons= buildButtons('2');
  var res = await fetch('/api/send-group-interactive', {
    method:'POST',
    headers: { 'Content-Type':'application/json', 'X-Admin-Token': adm },
    body: JSON.stringify({ group_ids:group_ids, title:title, desc:desc, buttons:buttons, sheet_id:sheet_id, sheet_name:sheet_name })
  });
  document.getElementById('out2').textContent = await res.text();
}
async function enviarGrpRedirect() {
  var adm = document.getElementById('adm').value.trim();
  var group_ids = parseList(document.getElementById('group_ids_redir').value);
  var title  = document.getElementById('titleR').value.trim();
  var desc   = document.getElementById('descR').value.trim();
  if (desc.length > 500) { alert('A descrição do card excede 500 caracteres.'); return; }
  var redirects = buildRedirects();
  var res = await fetch('/api/send-group-redirect', {
    method:'POST',
    headers: { 'Content-Type':'application/json', 'X-Admin-Token': adm },
    body: JSON.stringify({ group_ids:group_ids, title:title, desc:desc, redirects:redirects })
  });
  document.getElementById('outR').textContent = await res.text();
}
async function enviarTextoInd() {
  var adm = document.getElementById('adm').value.trim();
  var emails = parseList(document.getElementById('emails_txt').value);
  var text   = document.getElementById('text_msg_ind').value.trim();
  if (text.length > 4096) { alert('A mensagem de texto excede 4096 caracteres.'); return; }
  var res = await fetch('/api/send-text', {
    method:'POST',
    headers: { 'Content-Type':'application/json', 'X-Admin-Token': adm },
    body: JSON.stringify({ emails:emails, text:text })
  });
  document.getElementById('out3').textContent = await res.text();
}
async function enviarTextoGrp() {
  var adm = document.getElementById('adm').value.trim();
  var group_ids = parseList(document.getElementById('group_ids_txt').value);
  var text      = document.getElementById('text_msg_grp').value.trim();
  if (text.length > 4096) { alert('A mensagem de texto excede 4096 caracteres.'); return; }
  var res = await fetch('/api/send-group-text', {
    method:'POST',
    headers: { 'Content-Type':'application/json', 'X-Admin-Token': adm },
    body: JSON.stringify({ group_ids:group_ids, text:text })
  });
  document.getElementById('out3').textContent = await res.text();
}
</script>
</body>
</html>
    """
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

# ========= Auth da UI =========
def _check_ui_auth():
    if not UI_ADMIN_TOKEN:
        return None  # sem proteção
    provided = request.headers.get("X-Admin-Token", "")
    if provided != UI_ADMIN_TOKEN:
        return jsonify({"error":"unauthorized"}), 403
    return None

# ========= APIs de envio =========
@app.post("/api/send-interactive")
def api_send_interactive():
    auth_resp = _check_ui_auth()
    if auth_resp:
        return auth_resp
    try:
        body = request.get_json(force=True) or {}
        emails  = body.get("emails") or []
        title   = (body.get("title") or "📌 Confirme sua leitura").strip()
        desc    = (body.get("desc")  or "Escolha uma das opções abaixo.").strip()
        buttons = body.get("buttons") or []
        sheet_id   = (body.get("sheet_id") or "").strip()
        sheet_name = (body.get("sheet_name") or "").strip()

        if isinstance(emails, str):
            emails = [s.strip() for s in emails.replace(",", "\n").split("\n") if s.strip()]
        else:
            emails = [str(x).strip() for x in emails if str(x).strip()]
        if not emails:
            return jsonify({"error":"informe pelo menos um e-mail"}), 400

        token = get_token()
        meta  = {"sheet_id": sheet_id, "sheet_name": sheet_name}
        elements = build_elements(title, desc, buttons, meta=meta)
        results = []

        for em in emails:
            try:
                emp_code = resolve_employee_code(token, em)
                rj = send_card_to_employee(token, emp_code, elements)
                results.append({"email": em, "ok": True, "resp": rj})
            except Exception as e:
                results.append({"email": em, "ok": False, "error": str(e)})

        return jsonify({"sent": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/api/send-group-interactive")
def api_send_group_interactive():
    auth_resp = _check_ui_auth()
    if auth_resp:
        return auth_resp
    try:
        body = request.get_json(force=True) or {}
        group_ids = body.get("group_ids") or []
        title   = (body.get("title") or "📌 Confirme sua leitura").strip()
        desc    = (body.get("desc")  or "Escolha uma das opções abaixo.").strip()
        buttons = body.get("buttons") or []
        sheet_id   = (body.get("sheet_id") or "").strip()
        sheet_name = (body.get("sheet_name") or "").strip()

        if isinstance(group_ids, str):
            group_ids = [s.strip() for s in group_ids.replace(",", "\n").split("\n") if s.strip()]
        else:
            group_ids = [str(x).strip() for x in group_ids if str(x).strip()]
        if not group_ids:
            return jsonify({"error":"informe pelo menos um group_id"}), 400

        token = get_token()
        meta  = {"sheet_id": sheet_id, "sheet_name": sheet_name}
        elements = build_elements(title, desc, buttons, meta=meta)
        results = []

        for gid in group_ids:
            try:
                rj = send_card_to_group(token, gid, elements)
                results.append({"group_id": gid, "ok": True, "resp": rj})
            except Exception as e:
                results.append({"group_id": gid, "ok": False, "error": str(e)})

        return jsonify({"sent": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/api/send-group-redirect")
def api_send_group_redirect():
    auth_resp = _check_ui_auth()
    if auth_resp:
        return auth_resp
    try:
        body = request.get_json(force=True) or {}
        group_ids = body.get("group_ids") or []
        title   = (body.get("title") or "🔗 Ações rápidas").strip()
        desc    = (body.get("desc")  or "Escolha um dos links abaixo para abrir.").strip()
        redirects = body.get("redirects") or []

        if isinstance(group_ids, str):
            group_ids = [s.strip() for s in group_ids.replace(",", "\n").split("\n") if s.strip()]
        else:
            group_ids = [str(x).strip() for x in group_ids if str(x).strip()]
        if not group_ids:
            return jsonify({"error":"informe pelo menos um group_id"}), 400

        # Valida URLs básicas
        valids = []
        for r in redirects[:3]:
            t = str(r.get("text") or "").strip()
            u = str(r.get("url") or "").strip()
            if t and _is_http_url(u):
                valids.append({"text": t, "url": u})
        if not valids:
            return jsonify({"error":"informe ao menos 1 botão com URL http(s) válida"}), 400

        token = get_token()
        elements = build_redirect_elements(title, desc, valids)
        results = []

        for gid in group_ids:
            try:
                rj = send_card_to_group(token, gid, elements)
                results.append({"group_id": gid, "ok": True, "resp": rj})
            except Exception as e:
                results.append({"group_id": gid, "ok": False, "error": str(e)})

        return jsonify({"sent": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/api/send-text")
def api_send_text():
    auth_resp = _check_ui_auth()
    if auth_resp:
        return auth_resp
    try:
        body = request.get_json(force=True) or {}
        emails = body.get("emails") or []
        text   = (body.get("text") or "").strip()

        if isinstance(emails, str):
            emails = [s.strip() for s in emails.replace(",", "\n").split("\n") if s.strip()]
        else:
            emails = [str(x).strip() for x in emails if str(x).strip()]
        if not emails:
            return jsonify({"error": "informe pelo menos um e-mail"}), 400
        if not text:
            return jsonify({"error": "texto é obrigatório"}), 400

        token = get_token()
        results = []
        for em in emails:
            try:
                emp_code = resolve_employee_code(token, em)
                rj = send_text_to_employee(token, emp_code, text)
                results.append({"email": em, "ok": True, "resp": rj})
            except Exception as e:
                results.append({"email": em, "ok": False, "error": str(e)})
        return jsonify({"sent": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/api/send-group-text")
def api_send_group_text():
    auth_resp = _check_ui_auth()
    if auth_resp:
        return auth_resp
    try:
        body = request.get_json(force=True) or {}
        group_ids = body.get("group_ids") or []
        text      = (body.get("text") or "").strip()

        if isinstance(group_ids, str):
            group_ids = [s.strip() for s in group_ids.replace(",", "\n").split("\n") if s.strip()]
        else:
            group_ids = [str(x).strip() for x in group_ids if str(x).strip()]
        if not group_ids:
            return jsonify({"error": "informe pelo menos um group_id"}), 400
        if not text:
            return jsonify({"error": "texto é obrigatório"}), 400

        token = get_token()
        results = []
        for gid in group_ids:
            try:
                rj = send_text_to_group(token, gid, text)
                results.append({"group_id": gid, "ok": True, "resp": rj})
            except Exception as e:
                results.append({"group_id": gid, "ok": False, "error": str(e)})
        return jsonify({"sent": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========= Rota de teste opcional =========
@app.post("/test/send-interactive-3")
def test_send_interactive_3():
    try:
        body = request.get_json(silent=True) or {}
        email = body.get("email") or os.getenv("TEST_EMAIL") or ""
        if not email:
            return jsonify({"error": "Informe 'email' no body ou defina TEST_EMAIL"}), 400

        token = get_token()
        emp_code = resolve_employee_code(token, email)
        elements = build_elements(
            "📌 Confirme sua leitura",
            "Escolha uma das opções abaixo.",
            [{"text":"✅ Sim","action":"sim"},{"text":"❌ Não","action":"nao"},{"text":"🤔 Talvez","action":"talvez"}],
            meta={"sheet_id": GOOGLE_SHEET_ID, "sheet_name": GOOGLE_SHEET_NAME}
        )
        rj = send_card_to_employee(token, emp_code, elements)
        return jsonify(rj), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========= Keep-alive opcional =========
def _start_keepalive_thread():
    """
    Mantém pings periódicos na própria URL pública para reduzir hibernação.
    Configure:
      KEEPALIVE_URL=https://seu-servico.onrender.com/
      KEEPALIVE_INTERVAL_SEC=300
    Observação: em planos gratuitos o Render ainda pode hibernar.
    """
    url = (os.getenv("KEEPALIVE_URL") or "").strip()
    try:
        period = int(os.getenv("KEEPALIVE_INTERVAL_SEC") or "0")
    except Exception:
        period = 0
    if not url or period <= 0:
        print("keepalive disabled")
        return
    def _worker():
        while True:
            try:
                requests.get(url, timeout=10)
                print("keepalive ping ok ->", url)
            except Exception as e:
                print("keepalive ping error:", repr(e))
            time.sleep(period)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    print(f"keepalive enabled: {url} every {period}s")

if __name__ == "__main__":
    _start_keepalive_thread()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
