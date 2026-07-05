"""Конфигурация из окружения / .env. Только stdlib."""

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Минимальный .env-лоадер: KEY=VALUE, # — комментарий, без перезаписи окружения."""
    env_file = path or PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    bot_token: str
    anthropic_api_key: str
    users: dict[int, str]  # telegram_id -> имя (для LLM-контекста)
    tz_offset: int = 3     # МСК
    db_path: str = "data/eda.db"
    llm_model: str = "claude-haiku-4-5"
    allowed_ids: set[int] = field(init=False)

    def __post_init__(self) -> None:
        self.allowed_ids = set(self.users)


def parse_users(raw: str) -> dict[int, str]:
    """"135366416:Никита,556591668:Богдан" -> {135366416: "Никита", ...}"""
    users: dict[int, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        uid, _, name = chunk.partition(":")
        users[int(uid.strip())] = name.strip() or f"user{uid}"
    return users


def load_config() -> Config:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    users_raw = os.environ.get("USERS", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан (.env)")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY не задан (.env)")
    if not users_raw:
        raise RuntimeError("USERS не задан (.env), формат: id:Имя,id:Имя")
    db_path = os.environ.get("DB_PATH", "data/eda.db")
    if not os.path.isabs(db_path):
        db_path = str(PROJECT_ROOT / db_path)
    return Config(
        bot_token=token,
        anthropic_api_key=api_key,
        users=parse_users(users_raw),
        tz_offset=int(os.environ.get("TZ_OFFSET", "3")),
        db_path=db_path,
        llm_model=os.environ.get("LLM_MODEL", "claude-haiku-4-5"),
    )
