import discord
from discord.ext import commands
import socket
import time
import datetime
import json
import os
import logging
from logging.handlers import TimedRotatingFileHandler
from zoneinfo import ZoneInfo
import config

# 「跨日重發」用的時區——鎖定台灣時間，不管容器本身系統時區是什麼
# （很多虛擬主機容器預設是 UTC，不鎖的話會變成每天台灣時間早上 8 點才重發）
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
from ptero import send_power_signal, send_console_command
from checks import control_channel_only, admin_only
from confirm import DangerousConfirmView
from ws_listener import listen_forever

# ===== 執行紀錄（log 檔案）=====
# 主控台（Console）畫面捲動太快、重啟後也不會保留，
# 這裡額外把紀錄寫進 logs/bot.log，方便事後在 File Manager 打開查閱。
# 用 TimedRotatingFileHandler 設定每天午夜（台灣時間）自動換一份新檔案，
# 舊檔案會自動改名成 bot.log.YYYY-MM-DD，backupCount=30 代表只保留最近 30 天，
# 超過一個月的舊 log 會被自動刪除，不用手動清理、也不會無限占用主機空間。
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("ftbnet-bot")
logger.setLevel(logging.INFO)

_file_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, "bot.log"),
    when="midnight",
    interval=1,
    backupCount=30,  # 只保留最近 30 天，超過自動刪除
    encoding="utf-8",
)
_file_handler.suffix = "%Y-%m-%d"
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_file_handler)

# 同時維持主控台輸出（跟原本的 print 效果一樣，即時查看時還是看得到）
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_console_handler)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(
    command_prefix=config.COMMAND_PREFIX,
    intents=intents
)

# 避免 on_ready 觸發多次時重複啟動監聽任務
_listener_started = False

# ===== 跨重啟持久化狀態（訊息 ID 等）=====
# 電源狀態、即時狀態監控、伺服器資訊卡片都用「編輯同一則訊息」的方式運作，
# 但訊息物件原本只存在記憶體裡，機器人一重啟就會忘記，導致重複發送新訊息。
# 這裡改成把訊息 ID（以及跨日用的日期字串）存進小 JSON 檔案，
# 重啟後先嘗試用存起來的 ID 把舊訊息抓回來繼續編輯。
BOT_STATE_FILE = "bot_state.json"


def _load_bot_state():
    if os.path.exists(BOT_STATE_FILE):
        try:
            with open(BOT_STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_bot_state(**updates):
    data = _load_bot_state()
    data.update(updates)
    with open(BOT_STATE_FILE, "w") as f:
        json.dump(data, f)


# 電源狀態變化對應的顯示文字與顏色
# key 為 (舊狀態, 新狀態)
STATUS_TRANSITIONS = {
    ("offline", "starting"): ("🟡 伺服器正在啟動中...", config.COLOR_LOADING),
    ("starting", "running"): ("🟢 伺服器已成功啟動！", config.COLOR_SUCCESS),
    ("running", "stopping"): ("🟡 伺服器正在關閉中...", config.COLOR_WARNING),
    ("stopping", "offline"): ("🔴 伺服器已離線", config.COLOR_ERROR),
    (None, "running"): ("🟢 伺服器目前在線上", config.COLOR_SUCCESS),
}

# 電源狀態通知改成跟即時狀態監控用同一個頻道（config.STATS_CHANNEL_ID），
# 並且改成「編輯同一則訊息」，不再每次開關機都發新訊息洗版。
# 邏輯跟下面的 _update_stats_message 一致：同一天內編輯同一則，跨日才重發新的。
_power_message = None
_power_message_date = None


async def _update_power_message(embed: discord.Embed):
    """實際負責發送或編輯電源狀態訊息，並處理跨日重發與重啟後的訊息還原"""
    global _power_message, _power_message_date

    channel = bot.get_channel(config.STATS_CHANNEL_ID)
    if channel is None:
        logger.warning(f"[事件通知] 找不到頻道 ID {config.STATS_CHANNEL_ID}，略過電源狀態通知。")
        return

    today = datetime.datetime.now(TAIPEI_TZ).date()

    # 記憶體裡沒有訊息物件（可能是剛重啟），先試著用存檔的 ID 把舊訊息抓回來
    if _power_message is None:
        state = _load_bot_state()
        saved_id = state.get("power_message_id")
        saved_date = state.get("power_message_date")
        if saved_id and saved_date == today.isoformat():
            try:
                _power_message = await channel.fetch_message(saved_id)
                _power_message_date = today
            except discord.NotFound:
                _power_message = None

    need_new_message = _power_message is None or _power_message_date != today

    try:
        if need_new_message:
            _power_message = await channel.send(embed=embed)
            _power_message_date = today
            _save_bot_state(
                power_message_id=_power_message.id,
                power_message_date=today.isoformat(),
            )
        else:
            await _power_message.edit(embed=embed)
    except discord.NotFound:
        # 訊息被人手動刪掉了，重新送一則
        _power_message = await channel.send(embed=embed)
        _power_message_date = today
        _save_bot_state(
            power_message_id=_power_message.id,
            power_message_date=today.isoformat(),
        )


async def handle_status_change(old_status, new_status):
    """伺服器電源狀態改變時（開機中/已啟動/關機中/離線）更新通知"""

    text, color = STATUS_TRANSITIONS.get(
        (old_status, new_status),
        (f"⚙️ 伺服器狀態變更：`{old_status}` → `{new_status}`", config.COLOR_INFO),
    )

    embed = discord.Embed(title="🎮 Minecraft 電源狀態", description=text, color=color)
    await _update_power_message(embed)

    if new_status == "offline":
        # 伺服器離線後，面板不會再推送 stats 事件，
        # 狀態卡片會停在離線前最後一刻的數字，容易讓人誤會伺服器還活著。
        # 這裡主動把它蓋掉，並清掉快取的 TPS，等下次開機重新累積。
        global _latest_tps
        _latest_tps = None

        offline_embed = discord.Embed(
            title="📊 伺服器即時狀態",
            description="🔴 伺服器目前離線，無即時數據。",
            color=config.COLOR_ERROR,
        )
        await _update_stats_message(offline_embed)


# ===== 伺服器狀態監控（記憶體/CPU/TPS）=====
# 面板的 stats 事件大概每秒推送一次，如果每次都發新訊息會洗版，
# 所以改成只送一則訊息，之後每隔一段時間用「編輯」的方式更新內容。
# 另外每跨過一天，會改成重新發一則新訊息，不會一直往上編輯同一則舊訊息。
_stats_message = None
_stats_message_date = None
_last_stats_update = 0.0
_latest_tps = None
STATS_UPDATE_INTERVAL = 15  # 秒


async def _update_stats_message(embed: discord.Embed):
    """實際負責發送或編輯狀態訊息，並處理跨日重發與重啟後的訊息還原"""
    global _stats_message, _stats_message_date

    channel = bot.get_channel(config.STATS_CHANNEL_ID)
    if channel is None:
        logger.warning(f"[狀態監控] 找不到頻道 ID {config.STATS_CHANNEL_ID}，略過狀態更新。")
        return

    today = datetime.datetime.now(TAIPEI_TZ).date()

    if _stats_message is None:
        state = _load_bot_state()
        saved_id = state.get("stats_message_id")
        saved_date = state.get("stats_message_date")
        if saved_id and saved_date == today.isoformat():
            try:
                _stats_message = await channel.fetch_message(saved_id)
                _stats_message_date = today
            except discord.NotFound:
                _stats_message = None

    need_new_message = _stats_message is None or _stats_message_date != today

    try:
        if need_new_message:
            _stats_message = await channel.send(embed=embed)
            _stats_message_date = today
            _save_bot_state(
                stats_message_id=_stats_message.id,
                stats_message_date=today.isoformat(),
            )
        else:
            await _stats_message.edit(embed=embed)
    except discord.NotFound:
        # 訊息被人手動刪掉了，重新送一則
        _stats_message = await channel.send(embed=embed)
        _stats_message_date = today
        _save_bot_state(
            stats_message_id=_stats_message.id,
            stats_message_date=today.isoformat(),
        )


async def on_tps(tps: float):
    """收到 /tps 解析結果時呼叫，先存起來，實際發送交給 on_stats 一起處理"""
    global _latest_tps
    _latest_tps = tps


async def on_stats(stats: dict):
    """收到面板的即時資源用量資料時呼叫，節流後更新（或建立）狀態訊息"""
    global _last_stats_update

    now = time.monotonic()
    if now - _last_stats_update < STATS_UPDATE_INTERVAL:
        return
    _last_stats_update = now

    memory_mb = stats.get("memory_bytes", 0) / 1024 / 1024
    cpu_percent = stats.get("cpu_absolute", 0)

    embed = discord.Embed(title="📊 伺服器即時狀態", color=config.COLOR_INFO)
    embed.add_field(name="記憶體用量", value=f"{memory_mb:.0f} MB", inline=True)
    embed.add_field(name="CPU 使用率", value=f"{cpu_percent:.1f}%", inline=True)

    if _latest_tps is not None:
        embed.add_field(name="TPS", value=f"{_latest_tps:.1f}", inline=True)

    embed.set_footer(text=f"每 {STATS_UPDATE_INTERVAL} 秒自動更新")

    await _update_stats_message(embed)


@bot.event
async def on_ready():
    global _listener_started

    logger.info("=" * 40)
    logger.info(f"🟢 麥塊控制小助手已成功線上登入：{bot.user}")
    logger.info("所有控制指令均已成功背進大腦！")
    logger.info(f"目前設定控制網址: {config.PTERO_PANEL_URL}")
    logger.info(f"目前設定伺服器ID: {config.SERVER_UUID}")
    logger.info("=" * 40)
    
    activity = discord.Activity(
        type=discord.ActivityType.watching, 
        name="Minecraft 伺服器"
    )
    await bot.change_presence(status=discord.Status.online, activity=activity)

    if not _listener_started:
        _listener_started = True
        bot.loop.create_task(listen_forever(handle_status_change, on_stats, on_tps, tps_poll_interval=600))
        logger.info("[事件通知] 已啟動 WebSocket 即時監聽任務。")

@bot.command()
@control_channel_only()
@admin_only()
async def mcstart(ctx):
    """開機指令（嵌入式訊息版）"""

    # 建立發送中的灰色卡片
    embed_loading = discord.Embed(
        title="🎮 Minecraft 電源控制",
        description="⏳ 正在嘗試向 SwiftPlay 發送開機訊號...",
        color=config.COLOR_LOADING,
    )

    msg = await ctx.send(embed=embed_loading)

    if send_power_signal("start"):
        # 成功：顯示綠色卡片
        embed_success = discord.Embed(
            title="🎮 Minecraft 電源控制",
            color=config.COLOR_SUCCESS,
        )
        embed_success.add_field(
            name="操作結果",
            value="🟢 成功！SwiftPlay 伺服器正在啟動中。",
            inline=False,
        )
        embed_success.add_field(
            name="提示",
            value="地圖與插件載入大約需要 1 分鐘，隨後即可使用 `!mcstatus` 查詢人數。",
            inline=False,
        )
        await msg.edit(embed=embed_success)

    else:
        # 失敗：顯示紅色卡片
        embed_fail = discord.Embed(
            title="🎮 Minecraft 電源控制",
            color=config.COLOR_ERROR,
        )
        embed_fail.add_field(
            name="操作結果",
            value="❌ 失敗！無法發送開機訊號。",
            inline=False,
        )
        embed_fail.add_field(
            name="排查建議",
            value="請確認 SwiftPlay 面板目前是否正常運行，或至 Sparked Host 查看後台日誌。",
            inline=False,
        )
        await msg.edit(embed=embed_fail)

@bot.command()
@control_channel_only()
@admin_only()
async def mcstop(ctx):
    """關機指令（嵌入式訊息版）"""
    # 建立發送中的灰色卡片
    embed_loading = discord.Embed(title="🎮 Minecraft 電源控制", description="⏳ 正在嘗試向 SwiftPlay 發送安全關機訊號...", color=config.COLOR_LOADING)
    msg = await ctx.send(embed=embed_loading)
    
    if send_power_signal("stop"):
        # 成功：顯示紅色（停止）卡片
        embed_success = discord.Embed(title="🎮 Minecraft 電源控制", color=config.COLOR_ERROR)
        embed_success.add_field(name="操作結果", value="🔴 成功！已成功發送安全關機訊號。", inline=False)
        embed_success.add_field(name="提示", value="伺服器正在儲存地圖資料並安全下線中。", inline=False)
        await msg.edit(embed=embed_success)
    else:
        # 失敗：顯示黃色卡片
        embed_fail = discord.Embed(title="🎮 Minecraft 電源控制", color=config.COLOR_WARNING)
        embed_fail.add_field(name="操作結果", value="❌ 關機失敗！無法關閉伺服器。", inline=False)
        embed_fail.add_field(name="排查建議", value="如果伺服器卡死，請直接至 SwiftPlay 網頁面板進行「強制斬殺 (Kill)」。", inline=False)
        await msg.edit(embed=embed_fail)

@bot.command()
@control_channel_only()
@admin_only()
async def mcrestart(ctx):
    """重啟指令（嵌入式訊息版）"""
    # 建立發送中的灰色卡片
    embed_loading = discord.Embed(title="🎮 Minecraft 電源控制", description="⏳ 正在嘗試向 SwiftPlay 發送重新啟動訊號...", color=config.COLOR_LOADING)
    msg = await ctx.send(embed=embed_loading)
    
    if send_power_signal("restart"):
        # 成功：顯示藍色（重啟）卡片
        embed_success = discord.Embed(title="🎮 Minecraft 電源控制", color=config.COLOR_INFO)
        embed_success.add_field(name="操作結果", value="🔄 成功！伺服器正在進行重新啟動流程。", inline=False)
        embed_success.add_field(name="提示", value="伺服器將會先進行安全關機，隨後自動開啟。", inline=False)
        await msg.edit(embed=embed_success)
    else:
        # 失敗：顯示黃色卡片
        embed_fail = discord.Embed(title="🎮 Minecraft 電源控制", color=config.COLOR_WARNING)
        embed_fail.add_field(name="操作結果", value="❌ 重啟失敗！請確認面板狀態。", inline=False)
        await msg.edit(embed=embed_fail)

@bot.command()
async def mcstatus(ctx):
    """即時狀態查詢（全新探針直連版，100%不依賴外部網站）"""
    embed_loading = discord.Embed(title="🎮 Minecraft 伺服器即時狀態", description="🔍 正在嘗試與伺服器建立直連探針...", color=config.COLOR_LOADING)
    msg = await ctx.send(embed=embed_loading)
    
    MC_HOST = config.MC_HOST
    MC_PORT = config.MC_PORT                    
    
    try:
        # 建立一個 TCP 網路探針，直接敲你伺服器的大門
        with socket.socket(socket.AF_INET,
        socket.SOCK_STREAM) as s:
          s.settimeout(5)
          result =s.connect_ex((MC_HOST, MC_PORT))

        
        if result == 0:
            # 🟢 代表大門是開著的，伺服器絕對在線上！
            embed = discord.Embed(title="🎮 Minecraft 伺服器即時狀態", color=config.COLOR_INFO)
            embed.add_field(name="運作狀態", value="🟢 線上 (Online)", inline=False)
            embed.add_field(name="連線位置", value=f"`{MC_HOST}:{MC_PORT}`", inline=False)
            embed.add_field(name="提示", value="👍 伺服器已成功對外開放，玩家可正常連線登入！", inline=False)
            embed.set_footer(text="💡 提示：開關機功能已與 SwiftPlay 面板 API 成功綁定。")
            await msg.edit(embed=embed)
        else:
            # 🔴 代表大門關閉，伺服器關機中
            embed = discord.Embed(title="🎮 Minecraft 伺服器即時狀態", color=config.COLOR_ERROR)
            embed.add_field(name="運作狀態", value="🔴 離線 (Offline)", inline=False)
            embed.add_field(name="提示", value="伺服器目前處於關機狀態。\n請在 Discord 輸入 `!mcstart` 嘗試喚醒伺服器。", inline=False)
            await msg.edit(embed=embed)
            
    except Exception as e:
        logger.error(f"[查詢狀態] 本地探針執行失敗: {e}")
        await msg.edit(content=f"❌ 探針執行失敗，請至控制台確認日誌。", embed=None)

@bot.command()
@control_channel_only()
@admin_only()
async def mccmd(ctx, *, command_text: str):
    """遠端執行遊戲主控台指令"""

    command_name = command_text.split()[0].lower()

    # 禁止執行的指令
    if command_name in config.BLOCKED_MCCMD:
        await ctx.send("❌ 此指令已被禁止使用。")
        return

    # 真正執行指令
    async def execute_command(command):
        response = send_console_command(command)

        if response is None:
            await ctx.send("❌ 無法連線至主控台。")

        elif response.status_code != 204:
            await ctx.send(
                f"❌ 指令發送失敗，主機回傳 `{response.status_code}`。"
            )

    # 危險指令需要確認
    if command_name in config.DANGEROUS_COMMANDS:
        view = DangerousConfirmView(
            author=ctx.author,
            command_text=command_text,
            callback=execute_command
        )

        await ctx.send(
            f"⚠️ **危險指令確認**\n\n即將執行：`/{command_text}`",
            view=view
        )
        return

    # 一般指令直接執行
    response = send_console_command(command_text)

    if response is None:
        await ctx.send("❌ 無法連線至主控台。")

    elif response.status_code == 204:
        await ctx.send(
            f"💻 已成功對遊戲控制台發送指令：`/{command_text}`"
        )

    else:
        await ctx.send(
            f"❌ 指令發送失敗，主機回傳 `{response.status_code}`。"
        )

@bot.command()
@control_channel_only()
@admin_only()
async def clear(ctx, amount: int = 10):
    """清除最近的訊息"""

    if amount < 1:
        await ctx.send("❌ 清除數量必須大於 0。", delete_after=5)
        return

    if amount > 100:
        await ctx.send("❌ 一次最多只能清除 100 則訊息。", delete_after=5)
        return

    deleted = await ctx.channel.purge(limit=amount + 1)

    await ctx.send(f"🧹 已清除 {len(deleted) - 1} 則訊息。", delete_after=3)

@bot.command()
@control_channel_only()
@admin_only()
async def clearbot(ctx, amount: int = 100):
    """只清除本 Bot 發送的訊息"""

    if amount < 1:
        await ctx.send("❌ 清除數量必須大於 0。", delete_after=5)
        return

    if amount > 100:
        await ctx.send("❌ 一次最多只能清除 100 則 Bot 訊息。", delete_after=5)
        return

    deleted = 0

    async for message in ctx.channel.history(limit=200):
        if message.author.id == bot.user.id:
            await message.delete()
            deleted += 1

            if deleted >= amount:
                break

    await ctx.send(
        f"🤖 已清除 {deleted} 則 Bot 訊息。",
        delete_after=3
    )

@bot.command()
@control_channel_only()
@admin_only()
async def testconfirm(ctx):

    async def dummy_callback(command):
        send_console_command(command)

    view = DangerousConfirmView(
        author=ctx.author,
        command_text="say Hello",
        callback=dummy_callback
    )

    await ctx.send(
        "⚠️ 這是確認按鈕測試",
        view=view
    )

# ===== 伺服器資訊卡片（含自動時間戳記）=====
# 第一次執行 !serverinfo 會發送新訊息，之後每次再打同樣的指令，
# 會改成編輯同一則訊息並自動更新時間戳記，不會重複洗版。
# 訊息 ID 額外存進 bot_state.json，機器人重啟後也能找回舊訊息繼續編輯。
_serverinfo_message = None


@bot.command()
@control_channel_only()
@admin_only()
async def serverinfo(ctx):
    """發送或更新伺服器資訊卡片（含自動時間戳記，重複執行會編輯同一則）"""
    global _serverinfo_message

    channel = bot.get_channel(config.INFO_CHANNEL_ID)
    if channel is None:
        await ctx.send(f"❌ 找不到頻道 ID {config.INFO_CHANNEL_ID}，請檢查 .env 裡的 INFO_CHANNEL_ID 設定。")
        return

    embed = discord.Embed(
        title="🏝️ FTBNet 空島伺服器",
        color=config.COLOR_SUCCESS,
        timestamp=datetime.datetime.now(TAIPEI_TZ)
    )
    embed.add_field(
        name="📡 連線資訊",
        value=(
            "Java版(直接連線)：play.ftbnet.net\n\n"
            "基岩版：play.ftbnet.net\n"
            "連接埠：30379"
        ),
        inline=False
    )
    embed.add_field(
        name="🎮 玩法",
        value="AOneBlock（/ob）、ChunkBlock（/ch）｜主機板：Purpur｜版本：Minecraft 26.2",
        inline=False
    )
    embed.add_field(
        name="🔗 更多資訊",
        value="（連結）",
        inline=False
    )

    try:
        if _serverinfo_message is None:
            state = _load_bot_state()
            saved_id = state.get("serverinfo_message_id")
            if saved_id:
                try:
                    _serverinfo_message = await channel.fetch_message(saved_id)
                    await _serverinfo_message.edit(embed=embed)
                except discord.NotFound:
                    _serverinfo_message = None

        if _serverinfo_message is None:
            _serverinfo_message = await channel.send(embed=embed)
            _save_bot_state(serverinfo_message_id=_serverinfo_message.id)
        else:
            await _serverinfo_message.edit(embed=embed)
    except discord.NotFound:
        _serverinfo_message = await channel.send(embed=embed)
        _save_bot_state(serverinfo_message_id=_serverinfo_message.id)

    await ctx.send(f"✅ 伺服器資訊已更新至 {channel.mention}。")

# 執行機器人
bot.run(config.DISCORD_BOT_TOKEN)
