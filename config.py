import os

from dotenv import load_dotenv

# 讀取跟這支檔案同一層的 .env 檔案，
# 把裡面的值載入成環境變數，下面才拿得到。
load_dotenv()


def _require(name: str) -> str:
    """
    讀取一個必要的環境變數，讀不到就直接報錯中止，
    而不是讓程式帶著空字串繼續跑、之後才在莫名其妙的地方出錯。
    """
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f".env 裡缺少 {name}，請檢查 .env 檔案")
    return value


# ==========================
# Discord（機密：Bot Token 放 .env）
# ==========================

DISCORD_BOT_TOKEN = _require("DISCORD_BOT_TOKEN")

# 指令前綴
COMMAND_PREFIX = "!"

# 管理指令專用頻道 ID
CONTROL_CHANNEL_ID = int(os.getenv("CONTROL_CHANNEL_ID", "0"))

# 可使用管理指令的 Discord 身分組 ID
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))

# 伺服器狀態監控（記憶體/CPU/TPS）專屬頻道 ID
STATS_CHANNEL_ID = int(os.getenv("STATS_CHANNEL_ID", "0"))

# 伺服器資訊公告頻道 ID（!serverinfo 指令發送目標）
INFO_CHANNEL_ID = int(os.getenv("INFO_CHANNEL_ID", "0"))


# ==========================
# Pterodactyl（機密：API Key 放 .env）
# ==========================

# 面板網址
PTERO_PANEL_URL = _require("PTERO_PANEL_URL")

# Client API Key
PTERO_API_KEY = _require("PTERO_API_KEY")

# Server UUID（Short UUID）
SERVER_UUID = _require("SERVER_UUID")


# ==========================
# Minecraft
# ==========================

MC_HOST = os.getenv("MC_HOST", "")
MC_PORT = int(os.getenv("MC_PORT", "25565"))


# ==========================
# mccmd 限制（非機密，維持寫死）
# ==========================

# 已有專屬 Bot 指令，不允許透過 !mccmd 執行
BLOCKED_MCCMD = {
    "start": "!mcstart",
    "stop": "!mcstop",
    "restart": "!mcrestart",
}


# ==========================
# 高風險主控台指令（非機密，維持寫死）
# ==========================

# 執行前需要二次確認
DANGEROUS_COMMANDS = {
    "op",
    "deop",
    "reload",
    "ban",
    "unban",
    "ban-ip",
    "unban-ip",
    "whitelist",
    "save-all",
    "save-off",
}


# ==========================
# Embed 顏色（非機密，維持寫死）
# ==========================

COLOR_SUCCESS = 0x2ECC71
COLOR_ERROR = 0xE74C3C
COLOR_WARNING = 0xF1C40F
COLOR_INFO = 0x3498DB
COLOR_LOADING = 0x95A5A6
