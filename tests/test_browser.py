"""Optional UI tests: pip install playwright; uses installed Edge by default.

All server data lives in a TemporaryDirectory. No real ledger is opened.
"""
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright, expect


def check_ui(browser, base_url, mobile):
    context = browser.new_context(viewport={"width": 390 if mobile else 1280, "height": 844},
                                  is_mobile=mobile, has_touch=mobile)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base_url + "/finance/")
    page.locator('[name="password"]').fill("ui-test-password")
    page.get_by_role("button", name="登录", exact=True).click()
    form = page.locator("#entry form")
    form.locator('[name="amount"]').fill("23.45")
    form.locator('[name="purpose"]').fill("mobile-test" if mobile else "desktop-test")
    note = form.locator('[name="note"]')
    note.fill("保留原备注  ")
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(buffer, format="PNG")
    form.locator('[name="image"]').set_input_files({"name": "test.png", "mimeType": "image/png", "buffer": buffer.getvalue()})
    pending = []
    page.route("**/finance/ocr", lambda route: pending.append(route))
    form.get_by_role("button", name="图转文", exact=True).click()
    expect(page.locator(".busy-dialog")).to_be_visible()
    page.wait_for_function("document.body.getAttribute('aria-busy') === 'true'")
    page.keyboard.press("Escape")
    expect(page.locator(".busy-dialog")).to_be_visible()
    expect(form.get_by_role("button", name="保存记录", exact=True)).to_be_disabled()
    # Even scripted repeat events must not produce another request/submission.
    page.evaluate("document.querySelector('#entry [data-role=ocr-button]').click(); document.querySelector('#entry form').requestSubmit()")
    assert len(pending) == 1
    pending.pop().fulfill(status=200, content_type="application/json", body=json.dumps({"text": "识别第一行\n金额 23.45"}))
    expect(page.locator(".busy-dialog")).not_to_be_visible()
    expect(note).to_have_value("保留原备注  \n识别第一行\n金额 23.45")
    form.get_by_role("button", name="图转文", exact=True).click()
    expect(page.locator(".busy-dialog")).to_be_visible()
    page.wait_for_timeout(50)
    pending.pop().fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "本地 OCR 暂不可用"}))
    expect(page.locator(".busy-dialog")).not_to_be_visible()
    expect(note).to_have_value("保留原备注  \n识别第一行\n金额 23.45")
    expect(form.locator('[data-role="ocr-status"]')).to_contain_text("不可用")

    page.clock.install()
    form.get_by_role("button", name="图转文", exact=True).click()
    expect(page.locator(".busy-dialog")).to_be_visible()
    page.clock.fast_forward(90001)
    expect(page.locator(".busy-dialog")).not_to_be_visible()
    expect(form.locator('[data-role="ocr-status"]')).to_contain_text("超时")
    pending.pop().abort()

    submissions = []
    page.route("**/finance/transactions", lambda route: submissions.append(route))
    form.get_by_role("button", name="保存记录", exact=True).click()
    expect(page.locator(".busy-dialog")).to_be_visible()
    page.evaluate("for (let i = 0; i < 5; i++) document.querySelector('#entry form').requestSubmit()")
    assert len(submissions) == 1
    token = form.locator('[name="submission_id"]').input_value()
    submissions.pop().abort("failed")
    expect(page.locator(".busy-dialog")).not_to_be_visible()
    expect(form.locator(".submit-status")).to_contain_text("网络异常")
    assert form.locator('[name="submission_id"]').input_value() == token
    expect(form.locator('[name="amount"]')).to_have_value("23.45")
    form.get_by_role("button", name="保存记录", exact=True).click()
    expect(page.locator(".busy-dialog")).to_be_visible()
    page.wait_for_timeout(50)
    submitted_body = submissions[0].request.post_data_buffer
    submitted_headers = submissions[0].request.headers
    with page.expect_navigation():
        submissions.pop().continue_()
    expect(page.locator(".busy-dialog")).not_to_be_visible()
    description = "mobile-test" if mobile else "desktop-test"
    expect(page.locator(".record-main > b").filter(has_text=description)).to_have_count(1)
    # Replay exactly the same POST to simulate a lost success response.
    result = context.request.post(base_url + "/finance/transactions", data=submitted_body, headers={
        "Content-Type": submitted_headers["content-type"], "Accept": "application/json",
    })
    assert result.status == 200
    page.reload()
    expect(page.locator(".record-main > b").filter(has_text=description)).to_have_count(1)
    assert page.locator('#entry [name="submission_id"]').input_value() != token
    # Busy dialog must sit above an already-open edit dialog and prevent Escape.
    record = page.locator("article.record").filter(has_text=description)
    record.click()
    record.get_by_role("button", name="编辑", exact=True).click()
    editor = page.locator("dialog.edit-dialog[open]")
    editor.locator('[name="image"]').set_input_files({"name": "test.png", "mimeType": "image/png", "buffer": buffer.getvalue()})
    editor.get_by_role("button", name="图转文", exact=True).click()
    expect(page.locator(".busy-dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(editor).to_be_visible()
    expect(page.locator(".busy-dialog")).to_be_visible()
    pending.pop().fulfill(status=200, content_type="application/json", body=json.dumps({"text": "编辑识别"}))
    expect(page.locator(".busy-dialog")).not_to_be_visible()
    assert editor.locator('[name="note"]').input_value().endswith("\n编辑识别")
    assert not errors, errors
    context.close()
    print("PASS mobile" if mobile else "PASS desktop")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="finance-ui-") as temp, tempfile.TemporaryFile() as log:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, APP_ROOT_PATH="/finance", COOKIE_SECURE="false", APP_PASSWORD="ui-test-password",
                   SECRET_KEY="ui-test-secret", DATABASE_URL="sqlite:///" + (Path(temp) / "test.db").as_posix(),
                   UPLOAD_DIR=str(Path(temp) / "uploads"), AZURE_STORAGE_CONNECTION_STRING="")
        # Browser uses full prefixed URLs; test_deployment separately tests prefix stripping + --root-path.
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
                                  cwd=root, env=env, stdout=log, stderr=log)
        try:
            base_url = f"http://127.0.0.1:{port}"
            with httpx.Client(trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get(base_url + "/finance/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Test server did not become ready")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=os.getenv("TEST_BROWSER_CHANNEL", "msedge"), headless=True)
                try:
                    check_ui(browser, base_url, mobile=False)
                    check_ui(browser, base_url, mobile=True)
                finally:
                    browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
