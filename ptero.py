import requests
import config

# ===== 面板 API 設定 =====
PANEL_URL = config.PTERO_PANEL_URL.strip().rstrip("/")
if not PANEL_URL.startswith(("http://", "https://")):
    PANEL_URL = f"https://{PANEL_URL}"

API_KEY = config.PTERO_API_KEY.strip()
SERVER_ID = config.SERVER_UUID.strip()

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def send_power_signal(signal):
    """傳送開機、關機、重啟訊號"""

    url = f"{PANEL_URL}/api/client/servers/{SERVER_ID}/power"
    print(f"\n[發送訊號] 正在請求網址: {url}")

    try:
        response = requests.post(
            url,
            json={"signal": signal},
            headers=HEADERS,
            timeout=10
        )

        print(f"[發送訊號] 主機回應狀態碼: {response.status_code}")

        if response.status_code != 204:
            print(f"[發送訊號] 主機拒絕訊息: {response.text}")

        return response.status_code == 204

    except requests.RequestException as e:
        print(f"[發送訊號] 連線發生異常錯誤: {e}")
        return False


def send_console_command(command_text):
    """傳送 Minecraft 主控台指令"""

    url = f"{PANEL_URL}/api/client/servers/{SERVER_ID}/command"
    print(f"\n[發送指令] 正在請求網址: {url}")

    try:
        response = requests.post(
            url,
            json={"command": command_text},
            headers=HEADERS,
            timeout=10
        )

        print(f"[發送指令] 主機回應狀態碼: {response.status_code}")

        if response.status_code != 204:
            print(f"[發送指令] 主機拒絕訊息: {response.text}")

        return response

    except requests.RequestException as e:
        print(f"[發送指令] 連線發生異常錯誤: {e}")
        return None

def get_websocket_credentials():
    """向面板換取 WebSocket 的臨時 Token 與連線網址"""
    url = f"{PANEL_URL}/api/client/servers/{SERVER_ID}/websocket"
    print(f"\n[獲取WS憑證] 正在請求網址: {url}")
    
    try:
        response = requests.get(
            url, 
            headers=HEADERS, 
            timeout=10
        )
        print(f"[獲取WS憑證] 主機回應狀態碼: {response.status_code}")
        
        if response.status_code == 200:
            # 成功時會拿到包含 data.token 和 data.socket 的 JSON 字典
            return response.json().get("data", {})
        else:
            print(f"[獲取WS憑證] 失敗訊息: {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"[獲取WS憑證] 連線發生異常錯誤: {e}")
        return None
