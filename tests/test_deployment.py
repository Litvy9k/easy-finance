"""Isolated integration tests; run once per root path in a fresh process."""
import argparse
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit


class PrefixProxy:
    """Simulate Nginx stripping the prefix then Uvicorn --root-path restoring ASGI scope."""

    def __init__(self, app, prefix):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.prefix:
            scope = dict(scope)
            path = scope["path"]
            if not path.startswith(self.prefix + "/"):
                from starlette.responses import Response
                await Response(status_code=404)(scope, receive, send)
                return
            upstream_path = path[len(self.prefix):]
            scope["root_path"] = self.prefix
            scope["path"] = self.prefix + upstream_path
            scope["raw_path"] = scope["path"].encode("utf-8")
        await self.app(scope, receive, send)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(PrefixProxy(app, ROOT_PATH), base_url="https://l9k.dev", follow_redirects=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def url(self, path):
        return ROOT_PATH + path

    def login(self, next_path=None):
        return self.client.post(self.url("/login"), data={
            "password": "test-password-中文", "next": next_path or self.url("/"),
        })

    def assert_scoped_links(self, html):
        links = re.findall(r'(?:href|src|action)="([^"]+)"', html)
        self.assertTrue(links)
        for link in links:
            if not link.startswith("#"):
                self.assertTrue(link.startswith(self.url("/")), link)

    def test_authentication_and_cookie(self):
        for path in ("/?month=2026-09", "/settings", "/export.csv", "/images/private.png"):
            response = self.client.get(self.url(path))
            self.assertEqual(response.status_code, 303)
            location = urlsplit(response.headers["location"])
            self.assertEqual(location.path, self.url("/login"))
            self.assertEqual(parse_qs(location.query)["next"], [self.url(path)])
        login_page = self.client.get(self.url("/login"))
        self.assertEqual(login_page.status_code, 200)
        self.assert_scoped_links(login_page.text)
        self.assertEqual(self.client.post(self.url("/login"), data={"password": "wrong"}).status_code, 401)
        response = self.login(self.url("/?month=2026-09"))
        self.assertEqual(response.headers["location"], self.url("/?month=2026-09"))
        cookie = response.headers["set-cookie"].lower()
        for expected in ("httponly", "secure", "samesite=lax", "path=" + self.url("/")):
            self.assertIn(expected, cookie)
        self.assertEqual(self.client.get(self.url("/")).status_code, 200)
        response = self.client.post(self.url("/logout"))
        self.assertEqual(response.headers["location"], self.url("/login"))
        self.assertIn("path=" + self.url("/"), response.headers["set-cookie"].lower())
        self.assertEqual(self.client.get(self.url("/")).status_code, 303)

    def test_login_targets_stay_inside_application(self):
        invalid = ["https://evil.example", "//evil.example", "/\\evil.example", "/%2f%2fevil.example",
                   self.url("/../outside"), self.url("/%2e%2e/outside"), self.url("/%252e%252e/outside"), "/\nevil"]
        if ROOT_PATH:
            invalid += ["/", "/finance-other/", "/settings"]
        for target in invalid:
            with self.subTest(target=target):
                self.assertEqual(self.login(target).headers["location"], self.url("/"))

    def test_pages_assets_and_ocr(self):
        self.login()
        for path in ("/", "/settings"):
            page = self.client.get(self.url(path))
            self.assertEqual(page.status_code, 200)
            self.assert_scoped_links(page.text)
            for asset in re.findall(r'(?:href|src)="([^"]+/static/[^"]+)"', page.text):
                self.assertEqual(self.client.get(asset).status_code, 200, asset)
        script = self.client.get(self.url("/static/app.js")).text
        self.assertIn("document.documentElement.dataset.basePath", script)
        self.assertNotIn("fetch('/ocr'", script)
        result = self.client.post(self.url("/ocr"), files={"image": ("test.png", b"image", "image/png")})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.get(self.url("/health")).json(), {"status": "ok"})

    def test_record_lifecycle_settings_images_and_export(self):
        from sqlalchemy import select
        from app.database import SessionLocal
        from app.models import ExpenseCategory, FundingSource, IncomeCategory, Transaction
        self.login()
        dictionary = [("expense-categories", ExpenseCategory, "测试支出"),
                      ("income-categories", IncomeCategory, "测试收入"),
                      ("sources", FundingSource, "测试账户")]
        for path, _, name in dictionary:
            result = self.client.post(self.url("/settings/" + path), data={"name": name})
            self.assertEqual(result.headers["location"], self.url("/settings"))
        with SessionLocal() as db:
            source_id = db.scalar(select(FundingSource.id).where(FundingSource.name == "测试账户"))
        form = dict(submission_id=str(uuid4()), kind="expense", amount="12.34", category="测试支出", funding_source_id=source_id,
                    transaction_date="2026-09-04", purpose="测试午餐", note="已有备注")
        result = self.client.post(self.url("/transactions"), data=form,
                                  files={"image": ("receipt.png", b"test-image", "image/png")})
        self.assertEqual(result.headers["location"], self.url("/?month=2026-09"))
        with SessionLocal() as db:
            record = db.scalar(select(Transaction).where(Transaction.purpose == "测试午餐"))
            record_id, image_name = record.id, record.image_name
        self.assertEqual(self.client.get(self.url("/images/" + image_name)).content, b"test-image")
        self.assertTrue((Path(os.environ["UPLOAD_DIR"]) / image_name).exists())
        page = self.client.get(self.url("/?month=2026-09"))
        self.assert_scoped_links(page.text)
        for ending in ("/edit", "/delete"):
            self.assertIn(self.url(f"/transactions/{record_id}" + ending), page.text)
        result = self.client.get(self.url("/export.csv"))
        self.assertEqual(result.status_code, 200)
        self.assertIn(image_name, result.text)
        for path, model, name in dictionary:
            if model is IncomeCategory:
                continue
            with SessionLocal() as db:
                item_id = db.scalar(select(model.id).where(model.name == name))
            result = self.client.post(self.url(f"/settings/{path}/{item_id}/delete"))
            self.assertEqual(result.headers["location"], self.url("/settings"))
        with SessionLocal() as db:
            record = db.get(Transaction, record_id)
            self.assertEqual(record.category, "默认")
            self.assertEqual(record.funding_source.name, "默认")
            default_source = record.funding_source_id
        form.update(kind="income", category="测试收入", funding_source_id=default_source, purpose="测试收入说明")
        result = self.client.post(self.url(f"/transactions/{record_id}/edit"), data=form)
        self.assertEqual(result.headers["location"], self.url("/?month=2026-09"))
        with SessionLocal() as db:
            income_id = db.scalar(select(IncomeCategory.id).where(IncomeCategory.name == "测试收入"))
        self.client.post(self.url(f"/settings/income-categories/{income_id}/delete"))
        with SessionLocal() as db:
            record = db.get(Transaction, record_id)
            self.assertEqual(record.kind, "income")
            self.assertEqual(record.category, "默认")
            self.assertEqual(record.purpose, "测试收入说明")
            self.assertEqual(record.image_name, image_name)
        for path, model, _ in dictionary:
            with SessionLocal() as db:
                default_id = db.scalar(select(model.id).where(model.name == "默认"))
            self.client.post(self.url(f"/settings/{path}/{default_id}/delete"))
            with SessionLocal() as db:
                self.assertIsNotNone(db.get(model, default_id))
        result = self.client.post(self.url(f"/transactions/{record_id}/delete"))
        self.assertEqual(result.headers["location"], self.url("/?month=2026-09"))
        self.assertEqual(self.client.get(self.url("/images/" + image_name)).status_code, 404)

    def test_real_uvicorn_with_stripped_upstream_paths(self):
        import httpx
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                   "--port", str(port), "--root-path", ROOT_PATH, "--log-level", "error"]
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
                    for attempt in range(100):
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail(log.read().decode("utf-8", errors="replace"))
                        time.sleep(0.1)
                    else:
                        self.fail("Uvicorn did not become ready")
                    # These HTTP paths are exactly what Nginx sends after stripping.
                    self.assertEqual(client.get("/static/app.css").status_code, 200)
                    self.assertEqual(client.get("/static/app.js").status_code, 200)
                    self.assert_scoped_links(client.get("/login").text)
                    self.assertTrue(client.get("/").headers["location"].startswith(self.url("/login?")))
                    result = client.post("/login", data={"password": "test-password-中文", "next": self.url("/")})
                    self.assertEqual(result.headers["location"], self.url("/"))
                    # A real proxy preserves the browser's Cookie header on the stripped path.
                    cookie = result.headers["set-cookie"].split(";", 1)[0]
                    page = client.get("/", headers={"Cookie": cookie})
                    self.assertEqual(page.status_code, 200)
                    self.assert_scoped_links(page.text)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def test_idempotent_submission_and_safe_retry(self):
        from sqlalchemy import func, select
        from app.database import SessionLocal
        from app.models import FundingSource, Transaction, TransactionSubmission
        self.login()
        with SessionLocal() as db:
            source_id = db.scalar(select(FundingSource.id).where(FundingSource.name == "默认"))
            before = db.scalar(select(func.count(Transaction.id)))
        form = dict(submission_id=str(uuid4()), kind="expense", amount="18.88", category="默认",
                    funding_source_id=source_id, transaction_date="2026-09-04", purpose="重复提交测试")
        for _ in range(3):
            result = self.client.post(self.url("/transactions"), data=form, headers={"Accept": "application/json"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["redirect_url"], self.url("/?month=2026-09"))
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count(Transaction.id))), before + 1)
        changed = dict(form, amount="99.00")
        self.assertEqual(self.client.post(self.url("/transactions"), data=changed).status_code, 409)
        # A new intentional entry with identical content is allowed.
        fresh = dict(form, submission_id=str(uuid4()))
        self.assertEqual(self.client.post(self.url("/transactions"), data=fresh).status_code, 303)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count(Transaction.id))), before + 2)
        # Invalid requests do not consume the token, so corrections can be retried.
        fresh = dict(form, submission_id=str(uuid4()), amount="0")
        self.assertEqual(self.client.post(self.url("/transactions"), data=fresh).status_code, 400)
        with SessionLocal() as db:
            self.assertIsNone(db.get(TransactionSubmission, fresh["submission_id"]))
        fresh["amount"] = "20.00"
        self.assertEqual(self.client.post(self.url("/transactions"), data=fresh).status_code, 303)
        # Deleting a saved record must not let an old POST recreate it.
        with SessionLocal() as db:
            record_id = db.scalar(select(Transaction.id).where(Transaction.amount == 20))
        self.client.post(self.url(f"/transactions/{record_id}/delete"))
        self.assertEqual(self.client.post(self.url("/transactions"), data=fresh).status_code, 303)
        with SessionLocal() as db:
            self.assertIsNone(db.get(Transaction, record_id))

        concurrent_form = dict(form, submission_id=str(uuid4()), purpose="并发防重测试", note="长备注" * 4000)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.client.post(self.url("/transactions"), data=concurrent_form), range(4)))
        self.assertTrue(all(result.status_code == 303 for result in results))
        with SessionLocal() as db:
            records = list(db.scalars(select(Transaction).where(Transaction.purpose == "并发防重测试")))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].note, concurrent_form["note"])

        # Failure after reserving the key must roll back both reservation and record.
        failed_form = dict(form, submission_id=str(uuid4()))
        with patch("app.main.save_image", side_effect=RuntimeError("simulated storage failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url("/transactions"), data=failed_form,
                                 files={"image": ("test.png", b"image", "image/png")})
        with SessionLocal() as db:
            self.assertIsNone(db.get(TransactionSubmission, failed_form["submission_id"]))
        self.assertEqual(self.client.post(self.url("/transactions"), data=failed_form).status_code, 303)

    def test_ocr_errors_success_and_lock_recovery(self):
        from app import ocr
        self.login()
        upload = {"image": ("test.png", b"test", "image/png")}
        with patch("app.main.recognize_receipt", return_value="第一行\nTOTAL 123.45"):
            response = self.client.post(self.url("/ocr"), files=upload)
            self.assertEqual(response.json(), {"text": "第一行\nTOTAL 123.45"})
        for failure, expected in [(ocr.OCRBusy("busy"), 429), (ocr.InvalidOCRImage("invalid"), 400)]:
            with patch("app.main.recognize_receipt", side_effect=failure):
                self.assertEqual(self.client.post(self.url("/ocr"), files=upload).status_code, expected)
        with patch("app.main.recognize_receipt", return_value=""):
            self.assertEqual(self.client.post(self.url("/ocr"), files=upload).status_code, 422)
        with self.assertRaises(ocr.InvalidOCRImage):
            ocr.recognize_receipt(b"not-an-image")
        self.assertFalse(ocr._lock.locked())

    def test_actual_offline_ocr(self):
        import io
        from PIL import Image, ImageDraw, ImageFont
        from app.ocr import recognize_receipt
        picture = Image.new("RGB", (640, 180), "white")
        ImageDraw.Draw(picture).text((25, 40), "TOTAL 123.45", fill="black", font=ImageFont.load_default(size=48))
        data = io.BytesIO()
        picture.save(data, format="PNG")
        # Explicit model paths plus blocked networking prove inference is offline.
        with patch("socket.socket.connect", side_effect=AssertionError("OCR must not access the network")):
            result = recognize_receipt(data.getvalue())
        self.assertIn("123.45", result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-path", default="")
    args = parser.parse_args()
    ROOT_PATH = args.root_path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    # Override every storage/service setting BEFORE importing the application.
    with tempfile.TemporaryDirectory(prefix="easy-finance-test-") as temporary:
        os.environ.update(
            APP_ROOT_PATH=ROOT_PATH, APP_PASSWORD="test-password-中文", SECRET_KEY="isolated-test-key",
            COOKIE_SECURE="true", DATABASE_URL="sqlite:///" + (Path(temporary) / "test.db").as_posix(),
            UPLOAD_DIR=str(Path(temporary) / "nested" / "uploads"), AZURE_STORAGE_CONNECTION_STRING="",
        )
        try:
            result = unittest.main(argv=[sys.argv[0]], exit=False, verbosity=2).result
        finally:
            from app.database import engine
            engine.dispose()
    sys.exit(0 if result.wasSuccessful() else 1)
