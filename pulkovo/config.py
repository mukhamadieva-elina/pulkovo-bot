import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    database: Path
    vk_token: str = ""
    group_id: int = 0
    gigachat_key: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2-Pro"
    ca_file: str | bool = True
    external_user_data: bool = False
    admin_password: str = ""
    secret_key: str = ""

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        path = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
        ca = os.getenv("GIGACHAT_CA_BUNDLE_FILE", "").strip()
        if not ca and (ROOT / ".certs/gigachat.pem").exists():
            ca = str(ROOT / ".certs/gigachat.pem")
        if ca and not Path(ca).is_absolute():
            ca = str(ROOT / ca)
        return cls(
            database=path if path.is_absolute() else ROOT / path,
            vk_token=os.getenv("VK_TOKEN", ""),
            group_id=int(os.getenv("VK_GROUP_ID", "0") or 0),
            gigachat_key=os.getenv("GIGACHAT_AUTH_TOKEN", ""),
            gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            gigachat_model=os.getenv("GIGACHAT_MODEL", "GigaChat-2-Pro"),
            ca_file=ca or True,
            external_user_data=os.getenv("ALLOW_EXTERNAL_USER_DATA", "false").lower() == "true",
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            secret_key=os.getenv("ADMIN_SECRET_KEY", ""),
        )
