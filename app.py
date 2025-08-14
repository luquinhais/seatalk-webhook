import os, requests, time

AUTH_URL = "https://openapi.seatalk.io/auth/app_access_token"
APP_ID   = os.getenv("SEATALK_APP_ID")
APP_SEC  = os.getenv("SEATALK_APP_SECRET")
SEND_URL = os.getenv("SEATALK_GROUP_SEND_URL")  # endpoint de envio p/ grupo (da doc)
GROUP_ID = os.getenv("SEATALK_GROUP_ID")

_token = {"v": None, "exp": 0}

def get_token():
    now = int(time.time())
    if _token["v"] and now < _token["exp"] - 60:
        return _token["v"]
    r = requests.post(AUTH_URL, json={"app_id": APP_ID, "app_secret": APP_SEC}, timeout=5)
    data = r.json()
    token = data.get("access_token") or data.get("app_access_token")
    exp   = now + int(data.get("expires_in") or data.get("expire") or 7200)
    if not token:
        raise RuntimeError(f"Falha ao obter token: {data}")
    _token.update({"v": token, "exp": exp})
    return token

def send_group_text(text: str):
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "group_id": GROUP_ID,
        "message": {"tag": "text", "text": {"content": text}}
    }
    r = requests.post(SEND_URL, headers=headers, json=payload, timeout=5)
    print("send_group_text:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()

def send_group_interactive():
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
                        "value": "{\"action\":\"ack\",\"protocolo\":\"TESTE123\"}"
                    }}
                ]
            }
        }
    }
    r = requests.post(SEND_URL, headers=headers, json=payload, timeout=5)
    print("send_group_interactive:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()
