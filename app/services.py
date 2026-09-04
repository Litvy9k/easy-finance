import mimetypes
import re
import uuid
from pathlib import Path

from azure.storage.blob import BlobServiceClient, ContentSettings

from .config import settings


UPLOAD_DIR = Path(settings.upload_dir)


def safe_image_name(original: str) -> str:
    suffix = Path(original or "receipt.jpg").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".heic"}:
        suffix = ".jpg"
    stem = re.sub(r"[^a-zA-Z0-9_-]", "-", Path(original).stem)[:50] or "receipt"
    return f"{uuid.uuid4().hex[:12]}-{stem}{suffix}"


def save_image(data: bytes, filename: str, content_type: str) -> str:
    name = safe_image_name(filename)
    if settings.blob_connection_string:
        service = BlobServiceClient.from_connection_string(settings.blob_connection_string)
        container = service.get_container_client(settings.blob_container)
        try:
            container.create_container()
        except Exception:
            pass
        container.upload_blob(
            name=name,
            data=data,
            overwrite=False,
            content_settings=ContentSettings(content_type=content_type or mimetypes.guess_type(name)[0]),
        )
    else:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        (UPLOAD_DIR / name).write_bytes(data)
    return name


def load_image(name: str) -> tuple[bytes, str]:
    if settings.blob_connection_string:
        service = BlobServiceClient.from_connection_string(settings.blob_connection_string)
        data = service.get_blob_client(settings.blob_container, name).download_blob().readall()
    else:
        data = (UPLOAD_DIR / Path(name).name).read_bytes()
    return data, mimetypes.guess_type(name)[0] or "application/octet-stream"


def delete_image(name: str | None) -> None:
    if not name:
        return
    try:
        if settings.blob_connection_string:
            service = BlobServiceClient.from_connection_string(settings.blob_connection_string)
            service.get_blob_client(settings.blob_container, name).delete_blob(delete_snapshots="include")
        else:
            (UPLOAD_DIR / Path(name).name).unlink(missing_ok=True)
    except Exception:
        pass
