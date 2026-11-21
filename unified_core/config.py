"""Central configuration (environment / .env driven)."""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from unified_core.paths import BASE_DIR, ENV_FILE


def parse_ids(raw: str) -> set[int]:
    """Parse a comma/space separated list of integer IDs."""
    ids: set[int] = set()
    for chunk in str(raw or "").replace(",", " ").split():
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return ids


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    API_ID: int = 0
    API_HASH: str = ""
    SESSION_NAME: str = "mau_saver_session"
    CONTROL_BOT_TOKEN: str = ""

    # --- AI caption translation (optional, OpenAI compatible) ---
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "auto"
    TRANSLATE_ENABLED: bool = True
    TRANSLATE_TIMEOUT: float = 40.0
    TRANSLATE_PROVIDER: str = "auto"   # auto | ollama | openai | g4f  (auto: ollama when OLLAMA_API_KEY is set)
    TRANSLATE_FALLBACK_G4F: bool = False  # use GPT4Free as a last resort when the chosen provider fails/is limited
    G4F_MODEL: str = ""                # empty = let g4f choose
    G4F_TIMEOUT: float = 30.0
    TRANSLATE_CONCURRENCY: int = 1     # Ollama Cloud free plan runs one model at a time

    # --- Ollama Cloud (free API key from https://ollama.com/settings/keys) or a local Ollama ---
    OLLAMA_API_KEY: str = ""
    OLLAMA_BASE_URL: str = "https://ollama.com"
    OLLAMA_MODEL: str = "auto"         # auto = best available model, re-chosen from the live catalogue
    OLLAMA_PREFER_UNCENSORED: bool = True

    # --- Hosting (Render etc.) / webhook ---
    SESSION_STRING: str = ""           # Telegram login as text (python -m scripts.export_session) - no session file needed
    COOKIES_B64: str = ""              # base64 of a cookies.txt (Instagram/YouTube...) written to the data folder at start
    X_ACCOUNTS: str = ""               # "user:auth_token:ct0,user2:auth_token:ct0" imported into the X account pool
    PORT: int = 0                      # set by Render/Heroku: start the HTTP server on this port
    WEBHOOK_ENABLED: bool = True
    WEBHOOK_SECRET: str = ""           # empty = webhook endpoint disabled (health check still works)
    WEBHOOK_HOST: str = "0.0.0.0"
    WEBHOOK_PORT: int = 8080           # used when PORT is not set but WEBHOOK_SECRET is
    WEBHOOK_MAX_QUEUE: int = 200
    WEBHOOK_RATE_PER_MIN: int = 60
    KEEPALIVE_ENABLED: bool = True     # ping our own public URL so a FREE Render instance does not fall asleep
    KEEPALIVE_URL: str = ""            # default: RENDER_EXTERNAL_URL (set by Render automatically)
    KEEPALIVE_INTERVAL: int = 600
    RENDER_EXTERNAL_URL: str = ""
    WEBHOOK_CORS_ORIGINS: str = ""     # comma separated origins allowed to call the webhook from a browser

    # --- OpenRouter (free models only: ids ending in ":free" + the free router) ---
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "auto"

    # --- Extra Telegram accounts (login / logout from the control bot: /login /accounts /logout /resume) ---
    MAX_ACCOUNTS: int = 5              # total userbot accounts running at once (the SESSION_STRING one counts)
    EXTRA_ACCOUNT_WORKERS: int = 2     # download/upload workers for every extra account
    EXTRA_SESSIONS: str = ""           # optional: session keys (space/comma separated) started with the bot - survives
                                       # hosts whose disk is wiped on restart (Render free)

    # --- Persistence across redeploys ---
    STATE_KEY: str = ""                # secret that encrypts the extra accounts' session keys kept in Saved Messages
                                       # (empty = derived from API_HASH + CONTROL_BOT_TOKEN)

    # --- Routing ---
    TARGET_CHANNEL: int = 0   # where media is posted
    COMMENT_CHAT: int = 0     # discussion group (fallback for comment replies)
    ADMIN_IDS: str = ""       # control-bot admins

    # --- Caption / behaviour ---
    INCLUDE_ORIGINAL_CAPTION: bool = True
    INCLUDE_SOURCE_LINK: bool = True
    INCLUDE_COMMENT_COUNT: bool = True
    DELETE_TRIGGER_MESSAGE: bool = True
    NOTIFY_SAVED_MESSAGES: bool = True
    AUTO_DEFAULT: bool = False  # global auto-scrape in every chat (dangerous)

    # --- Performance / limits ---
    MAX_CONCURRENCY: int = 4
    UPLOAD_CONCURRENCY: int = 3
    COMMENT_CONCURRENCY: int = 2   # comment threads posted in the background at the same time
    COMMENTS_ENABLED: bool = True  # master switch for reposting the source's comments (changeable at runtime)
    MAX_COMMENTS: int = 20
    COMMENT_DELAY: float = 1.0
    COMMENT_INCLUDE_AUTHOR: bool = True
    YTDLP_FETCH_COMMENTS: bool = False
    MAX_RETRIES: int = 2
    MAX_VIDEO_HEIGHT: int = 1080
    MAX_FILE_MB: int = 0        # 0 = auto (2000 MiB, 4000 MiB for Premium)
    MAX_BATCH_URLS: int = 1000
    JOB_TIMEOUT: int = 1800     # seconds per job
    RESUME_BATCHES: bool = True  # continue unfinished .json/.txt batches after a restart
    X_COOLDOWN_SECONDS: int = 900  # an X account that hit its rate limit is skipped for this long

    # --- Watermark (all changeable live: .wm command or /admin) ---
    WM_TEXT: str = ""              # empty = watermark never applied
    WM_ENABLED: bool = False       # watermark media that is sent directly to the bot / Saved Messages
    WM_ON_URL: bool = False        # ... media downloaded from links (.dl / auto)
    WM_ON_FWD: bool = False        # ... media copied by the forwarder (.fwd)
    WM_FONT: str = "DejaVuSans-Bold"  # font name or file in DATA_DIR/fonts (.wm fonts lists them)
    WM_SIZE: int = 5               # text height, % of media width
    WM_TEXT_COLOR: str = "#FFFFFF"
    WM_BG_COLOR: str = "#000000"   # "none" = no box
    WM_BG_OPACITY: int = 50        # 0-100
    WM_OPACITY: int = 90           # text opacity 0-100
    WM_POSITION: str = "br"        # tl tc tr ml mc mr bl bc br
    WM_MARGIN: int = 2             # % of media width
    WM_ANGLE: int = 0              # rotation in degrees (-180..180), counter-clockwise

    # --- Message forwarding (.fwdsrc / .fwddst) ---
    FWD_BAN: str = ""              # banned words/URLs removed from forwarded captions (comma separated)
    FWD_ENABLED: bool = True       # master switch for the source -> destination copier
    FWD_DELAY: float = 0.7         # pause between two copied messages (flood protection)
    FWD_ID_PREFIX: str = "ZF"      # serial number stamped under every copied message: ZF-000123
    FWD_STRIP_LINKS: bool = True   # remove every link (http, www, t.me, hidden links) from copied text

    # --- Storage ---
    DATA_DIR: Path = BASE_DIR / "data"
    DATABASE_PATH: str = ""
    TWSCRAPE_DB: str = ""
    COOKIES_FILE: str = ""
    LOG_LEVEL: str = "INFO"

    @field_validator("API_ID", "TARGET_CHANNEL", "COMMENT_CHAT", mode="before")
    @classmethod
    def _lenient_int(cls, v):
        """Placeholders such as 'your_api_id' count as 'not set' (the setup wizard then asks)."""
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return 0

    @model_validator(mode="after")
    def _fill_paths(self) -> "Settings":
        data_dir = Path(self.DATA_DIR)
        if not data_dir.is_absolute():
            data_dir = BASE_DIR / data_dir
        self.DATA_DIR = data_dir
        self.DATABASE_PATH = self.DATABASE_PATH or str(data_dir / "unified_database.db")
        self.TWSCRAPE_DB = self.TWSCRAPE_DB or str(data_dir / "accounts.db")
        self.MAX_CONCURRENCY = max(1, self.MAX_CONCURRENCY)
        self.UPLOAD_CONCURRENCY = max(1, self.UPLOAD_CONCURRENCY)
        self.COMMENT_CONCURRENCY = max(1, self.COMMENT_CONCURRENCY)
        self.TRANSLATE_CONCURRENCY = max(1, self.TRANSLATE_CONCURRENCY)
        self.MAX_ACCOUNTS = max(1, self.MAX_ACCOUNTS)
        self.EXTRA_ACCOUNT_WORKERS = max(1, self.EXTRA_ACCOUNT_WORKERS)
        return self

    # ---- derived paths ----
    @property
    def admin_ids(self) -> set[int]:
        return parse_ids(self.ADMIN_IDS)

    @property
    def session_path(self) -> Path:
        p = Path(self.SESSION_NAME)
        return p if p.is_absolute() else self.DATA_DIR / p

    @property
    def tmp_dir(self) -> Path:
        return self.DATA_DIR / "tmp"

    @property
    def log_dir(self) -> Path:
        return self.DATA_DIR / "logs"

    @property
    def cookies_path(self) -> Path | None:
        candidates = [self.COOKIES_FILE] if self.COOKIES_FILE else []
        candidates += [str(self.DATA_DIR / "cookies.txt"), str(BASE_DIR / "cookies.txt")]
        for c in candidates:
            if c and Path(c).is_file():
                return Path(c)
        return None

    @property
    def comments_on(self) -> bool:
        return bool(self.COMMENTS_ENABLED) and self.MAX_COMMENTS > 0

    @property
    def cookies_target(self) -> Path:
        """Where the wizard stores the cookies.txt used by yt-dlp."""
        return self.DATA_DIR / "cookies.txt"

    def ensure_dirs(self) -> None:
        for d in (self.DATA_DIR, self.tmp_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)

    def validate_required(self) -> list[str]:
        errors: list[str] = []
        if not self.API_ID or not self.API_HASH:
            errors.append("API_ID / API_HASH missing (get them from https://my.telegram.org)")
        if not self.TARGET_CHANNEL:
            errors.append("TARGET_CHANNEL missing (channel where media is posted)")
        return errors


settings = Settings()
