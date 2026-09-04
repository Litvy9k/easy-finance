import os
import re
from dataclasses import dataclass


def configured_root_path() -> str:
    value = os.getenv("APP_ROOT_PATH", "").rstrip("/")
    if value and not re.fullmatch(r"(?:/[a-zA-Z0-9_-]+)+", value):
        raise ValueError("APP_ROOT_PATH must be empty or a path such as /finance")
    return value


@dataclass(frozen=True)
class Settings:
    app_name: str = "简账"
    root_path: str = configured_root_path()
    upload_dir: str = os.getenv("UPLOAD_DIR", "uploads")
    secret_key: str = os.getenv("SECRET_KEY", "change-me-before-deployment")
    app_password: str = os.getenv("APP_PASSWORD", "changeme")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./easy_finance.db")
    blob_connection_string: str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
    blob_container: str = os.getenv("AZURE_STORAGE_CONTAINER", "receipts")
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "8"))


settings = Settings()
