"""
連線 Pterodactyl 相容面板（目前為 Calagopus, panel.ftbnet.net）的 Client API WebSocket，
單純拿來監控伺服器狀態：

  1. 伺服器電源狀態變化（開機中／已啟動／關機中／已離線）
  2. CPU、記憶體等即時資源用量（面板原生的 stats 事件）
  3. TPS（面板本身不會推送，改成每隔一段時間送 /tps 指令，
     再從主控台回覆的那一行文字裡解析數字出來）

面板的 WebSocket 連線每隔一段時間會過期（token expiring / token expired），
本模組會自動重新取得 token 並重連，發生斷線也會自動重試，不需要手動處理。
"""

import asyncio
import json
import re

import requests
import websockets

import config
import ptero

# Paper/Spigot 執行 /tps 後，主控台通常會回覆類似：
# "TPS from last 1m, 5m, 15m: 20.0, 19.98, 19.95"
# 這個正則只抓「最近 1 分鐘」那個數字，夠日常監控用了。
_TPS_PATTERN = re.compile(r"TPS from last 1m.*?:\s*([\d.]+)")

# 連線失敗的重試等待：從 5 秒開始逐次加倍，最多 60 秒；驗證成功後重設
_RETRY_DELAY_MIN = 5
_RETRY_DELAY_MAX = 60


async def _get_ws_credentials():
    """跟面板要一組 WebSocket 專用 token + 連線位址（同步 API 包成背景執行緒跑，不卡住事件迴圈）"""

    def _fetch():
        url = (
            f"{config.PTERO_PANEL_URL.rstrip('/')}"
            f"/api/client/servers/{config.SERVER_UUID}/websocket"
        )
        headers = {
            "Authorization": f"Bearer {config.PTERO_API_KEY}",
            "Accept": "application/json",
        }
        resp = requests.get(url, headers=headers, timeout=10)

        if resp.status_code != 200:
            # 把完整回應內容印出來，才能判斷是 Pterodactyl 自己拒絕（會是 JSON）
            # 還是前面的防護層擋下來的（通常是一段 HTML）
            print(f"[WebSocket] 取得憑證失敗，狀態碼：{resp.status_code}")
            print(f"[WebSocket] 回應標頭：{dict(resp.headers)}")
            print(f"[WebSocket] 回應內容：{resp.text[:2000]}")

        resp.raise_for_status()
        body = resp.json()

        # Pterodactyl / Pelican 會把結果包在 "data" 這層底下；
        # Calagopus 的回應格式不一定相同，可能直接把 token/socket 放在最外層。
        # 這裡兩種格式都相容：先看有沒有 "data"，沒有就直接用最外層當作資料本體。
        data = body.get("data", body) if isinstance(body, dict) else {}

        # Pterodactyl/Pelican 用 "socket" 這個欄位名，Calagopus 實測用的是 "url"
        socket_url = data.get("socket") or data.get("url")

        if "token" not in data or not socket_url:
            print(f"[WebSocket] 回應格式異於預期，完整內容：{body}")
            raise KeyError(f"回應中找不到 token/socket(url)，原始內容：{body}")

        return data["token"], socket_url

    return await asyncio.to_thread(_fetch)


async def _poll_tps_forever(interval_seconds: int, should_poll):
    """
    背景任務：每隔 interval_seconds 秒，送一次 /tps 指令。
    真正的數字要靠下面主迴圈收到 console output 時解析，
    這裡只負責「定期戳一下伺服器」。
    """
    while True:
        await asyncio.sleep(interval_seconds)
        if not should_poll():
            continue
        try:
            await asyncio.to_thread(ptero.send_console_command, "tps")
        except Exception as e:
            print(f"[TPS輪詢] 送出 /tps 失敗：{e}")


async def listen_forever(on_status_change, on_stats, on_tps=None, tps_poll_interval=60):
    """
    持續監聽面板 WebSocket，直到程式結束為止。
    斷線／連線失敗會自動重試，等待時間從 5 秒逐次加倍、最多 60 秒，連上後重設。
    伺服器離線或面板暫時查不到狀態（例如整機備份暫停面板時）會每 60 秒再查一次。

    on_status_change(old_status, new_status) -> 需為 async function
    on_stats(stats: dict)                    -> 需為 async function，收到面板原生的資源用量資料
    on_tps(tps: float)                        -> 需為 async function（可選），
                                                  解析到 /tps 回覆時呼叫
    tps_poll_interval                         -> 每隔幾秒送一次 /tps，預設 60 秒
    """

    last_status = None
    paused_reason = None
    retry_delay = _RETRY_DELAY_MIN

    while True:
        tps_task = None
        try:
            # 伺服器關機時，面板的 websocket 憑證端點可能回 502。
            # 先查資源狀態，避免關機期間反覆打到 websocket 端點。
            # 查不到狀態（None）代表面板本身連不上，例如整機備份暫停面板時，
            # 這時連 websocket 也只會一直 502，同樣先暫停。
            server_state = await asyncio.to_thread(ptero.get_server_status)
            if server_state == "offline" or server_state is None:
                reason = "伺服器目前離線" if server_state == "offline" else "面板暫時無法取得伺服器狀態"
                if paused_reason != reason:
                    print(f"[WebSocket] {reason}，暫停 WebSocket 與 TPS 輪詢，每 60 秒重新檢查。")
                    paused_reason = reason
                await asyncio.sleep(60)
                continue

            if paused_reason is not None:
                print(f"[WebSocket] 偵測到伺服器狀態：{server_state}，恢復連線嘗試。")
                paused_reason = None

            token, socket_url = await _get_ws_credentials()

            panel_origin = config.PTERO_PANEL_URL.rstrip('/')
            async with websockets.connect(socket_url, ping_interval=20, origin=panel_origin) as ws:

                await ws.send(json.dumps({"event": "auth", "args": [token]}))
                print("[WebSocket] 已連線並送出驗證，等待面板回應中...")

                if on_tps is not None:
                    tps_task = asyncio.create_task(_poll_tps_forever(tps_poll_interval, lambda: last_status == "running"))

                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    event = payload.get("event")
                    args = payload.get("args") or []

                    if event == "auth success":
                        print("[WebSocket] 驗證成功，開始接收即時資料。")
                        retry_delay = _RETRY_DELAY_MIN

                    elif event == "token expiring":
                        # 連線快過期，取新 token 續命，不用重新建立連線
                        try:
                            new_token, _ = await _get_ws_credentials()
                            await ws.send(json.dumps({"event": "auth", "args": [new_token]}))
                            print("[WebSocket] Token 即將過期，已自動更新。")
                        except Exception as e:
                            print(f"[WebSocket] 更新 token 失敗：{e}")

                    elif event == "token expired":
                        print("[WebSocket] Token 已過期，準備重新連線...")
                        break

                    elif event == "status":
                        new_status = args[0] if args else None
                        if new_status and new_status != last_status:
                            old_status = last_status
                            last_status = new_status
                            await on_status_change(old_status, new_status)

                    elif event == "stats":
                        # args[0] 是一段 JSON 字串，內容大概長這樣：
                        # {"memory_bytes":..., "cpu_absolute":..., "network": {...}, ...}
                        if args:
                            try:
                                stats = json.loads(args[0])
                                await on_stats(stats)
                            except json.JSONDecodeError:
                                pass

                    elif event == "console output" and on_tps is not None:
                        # 只在找 TPS 這一行，其餘主控台輸出一律忽略
                        for line in args:
                            match = _TPS_PATTERN.search(line)
                            if match:
                                try:
                                    await on_tps(float(match.group(1)))
                                except ValueError:
                                    pass

                    elif event in ("daemon error", "jwt error"):
                        print(f"[WebSocket] 面板回報錯誤：{args}")

        except Exception as e:
            print(f"[WebSocket] 連線中斷或發生錯誤：{e}，{retry_delay} 秒後重試...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, _RETRY_DELAY_MAX)
        finally:
            if tps_task is not None:
                tps_task.cancel()
