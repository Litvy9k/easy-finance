"""Offline OCR: packaged small ONNX models, one lazy instance and one job at a time."""
import io
from pathlib import Path
from threading import Lock


class InvalidOCRImage(ValueError):
    pass


class OCRBusy(RuntimeError):
    pass


_lock = Lock()
_engine = None


def get_engine():
    """Called only while holding _lock; explicit local paths prohibit auto-downloads."""
    global _engine
    if _engine is None:
        import cv2
        import rapidocr
        from rapidocr import RapidOCR

        models = Path(rapidocr.__file__).resolve().parent / "models"
        paths = {
            "Det.model_path": models / "PP-OCRv6_det_small.onnx",
            "Cls.model_path": models / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            "Rec.model_path": models / "PP-OCRv6_rec_small.onnx",
        }
        if not all(path.is_file() for path in paths.values()):
            raise RuntimeError("Local OCR models missing; reinstall rapidocr==3.9.2")
        cv2.setNumThreads(2)
        _engine = RapidOCR(params={
            **{key: str(path) for key, path in paths.items()},
            "Global.log_level": "error",
            "Global.max_side_len": 1600,
            "Det.limit_type": "max",
            "Det.limit_side_len": 960,
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
    return _engine


def recognize_receipt(data: bytes) -> str:
    if not _lock.acquire(blocking=False):
        raise OCRBusy("正在处理另一张图片，请稍后重试")
    try:
        import numpy as np
        from PIL import Image, ImageOps, UnidentifiedImageError

        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.width * source.height > 25_000_000:
                    raise InvalidOCRImage("图片分辨率过大，请缩小后重试")
                source.thumbnail((1600, 1600))
                prepared = ImageOps.exif_transpose(source).convert("RGB")
                pixels = np.asarray(prepared)[:, :, ::-1].copy()  # RapidOCR arrays use BGR.
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise InvalidOCRImage("图片无法读取，请使用 JPG、PNG 或 WebP 图片") from exc
        result = get_engine()(pixels)
        return "\n".join(text.strip() for text in (result.txts or ()) if text.strip())
    finally:
        _lock.release()
