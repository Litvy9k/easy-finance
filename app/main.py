import csv
import hmac
import hashlib
import io
import json
import logging
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import extract, func, inspect, select, text, update
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import ExpenseCategory, FundingSource, IncomeCategory, Transaction, TransactionSubmission
from .services import delete_image, load_image, save_image
from .ocr import InvalidOCRImage, OCRBusy, recognize_receipt


app = FastAPI(title=settings.app_name, root_path=settings.root_path)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="easy_finance_session",
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=settings.cookie_secure,
    path=settings.root_path + "/",
)
APP_DIR = Path(__file__).resolve().parent
REVISION_FILE = APP_DIR.parent / "REVISION"
RELEASE_REVISION = REVISION_FILE.read_text().strip() if REVISION_FILE.is_file() else ""
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.globals["base_path"] = settings.root_path


def app_path(path: str) -> str:
    return settings.root_path + path


def internal_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(app_path(path), status_code=303)


def saved_response(request: Request, month: str):
    path = f"/?month={month}"
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"redirect_url": app_path(path)})
    return internal_redirect(path)


def safe_login_target(target: str) -> str:
    # Keep login redirects inside this application, including encoded paths.
    decoded = unquote(target)
    if not decoded.startswith("/") or decoded.startswith("//") or "\\" in decoded:
        return app_path("/")
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        return app_path("/")
    path = urlsplit(decoded).path
    if any(part in {".", ".."} for part in path.split("/")) or "%" in path:
        return app_path("/")
    if settings.root_path and not path.startswith(settings.root_path + "/"):
        return app_path("/")
    return target


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("transactions")}
    if "purpose" not in columns:
        column_type = "NVARCHAR(200)" if engine.dialect.name == "mssql" else "VARCHAR(200)"
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE transactions ADD purpose {column_type} NOT NULL DEFAULT ''"))
    with SessionLocal() as db:
        if not db.scalar(select(func.count(FundingSource.id))):
            db.add_all([FundingSource(name="微信支付"), FundingSource(name="支付宝")])
        if not db.scalar(select(func.count(ExpenseCategory.id))):
            db.add_all([ExpenseCategory(name=name) for name in ["食品", "水电", "房租", "交通", "购物", "娱乐", "医疗", "其他"]])
        if not db.scalar(select(func.count(IncomeCategory.id))):
            db.add_all([IncomeCategory(name=name) for name in ["工资", "理财", "利息", "奖金", "退款", "其他收入"]])
        db.commit()
        for model in (FundingSource, ExpenseCategory, IncomeCategory):
            if not db.scalar(select(model).where(model.name == "默认")):
                db.add(model(name="默认"))
        db.commit()
        expense_names = set(db.scalars(select(ExpenseCategory.name)))
        income_names = set(db.scalars(select(IncomeCategory.name)))
        db.execute(update(Transaction).where(Transaction.kind == "expense", ~Transaction.category.in_(expense_names)).values(category="默认"))
        db.execute(update(Transaction).where(Transaction.kind == "income", ~Transaction.category.in_(income_names)).values(category="默认"))
        db.commit()


CHART_COLORS = ["#275b45", "#d7895f", "#d5b84b", "#728f7d", "#ba6b65", "#7c789c", "#5597a5", "#9a8065"]


def donut_data(items):
    total = sum((value for _, value in items), Decimal("0"))
    cursor = Decimal("0")
    stops = []
    legend = []
    for index, (name, value) in enumerate(items):
        color = CHART_COLORS[index % len(CHART_COLORS)]
        start = float(cursor / total * 100) if total else 0
        cursor += value
        end = float(cursor / total * 100) if total else 0
        stops.append(f"{color} {start:.2f}% {end:.2f}%")
        legend.append((name, value, color, round(float(value / total * 100), 1) if total else 0))
    return ", ".join(stops) if stops else "#e9ebe5 0 100%", legend


def require_auth(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401)


@app.exception_handler(401)
async def unauthorized(request: Request, exc: HTTPException):
    path = request.scope["path"]
    if settings.root_path and (path == settings.root_path or path.startswith(settings.root_path + "/")):
        path = path[len(settings.root_path):] or "/"
    target = app_path(path)
    if request.url.query:
        target += "?" + request.url.query
    return internal_redirect("/login?" + urlencode({"next": target}))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    if not hmac.compare_digest(password.encode("utf-8"), settings.app_password.encode("utf-8")):
        return templates.TemplateResponse("login.html", {"request": request, "error": "密码不正确"}, status_code=401)
    request.session.clear()
    request.session["authenticated"] = True
    return RedirectResponse(safe_login_target(next), status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return internal_redirect("/login")


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def dashboard(request: Request, month: str = "", db: Session = Depends(get_db)):
    try:
        selected = datetime.strptime(month, "%Y-%m").date() if month else date.today().replace(day=1)
    except ValueError:
        selected = date.today().replace(day=1)
    query = (
        select(Transaction)
        .options(joinedload(Transaction.funding_source))
        .where(extract("year", Transaction.transaction_date) == selected.year)
        .where(extract("month", Transaction.transaction_date) == selected.month)
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    )
    records = list(db.scalars(query))
    expense = sum((r.amount for r in records if r.kind == "expense"), Decimal("0"))
    income = sum((r.amount for r in records if r.kind == "income"), Decimal("0"))
    categories = defaultdict(Decimal)
    for item in records:
        if item.kind == "expense":
            categories[item.category] += item.amount
    chart = sorted(categories.items(), key=lambda x: x[1], reverse=True)
    expense_donut, expense_legend = donut_data(chart)
    ratio_items = [("收入", income), ("支出", expense)]
    ratio_donut, ratio_legend = donut_data(ratio_items)
    return templates.TemplateResponse("index.html", {
        "request": request, "records": records, "sources": list(db.scalars(select(FundingSource).order_by(FundingSource.id))),
        "expense_categories": list(db.scalars(select(ExpenseCategory).order_by(ExpenseCategory.id))),
        "income_categories": list(db.scalars(select(IncomeCategory).order_by(IncomeCategory.id))),
        "expense": expense, "income": income, "balance": income - expense, "chart": chart,
        "expense_donut": expense_donut, "expense_legend": expense_legend,
        "ratio_donut": ratio_donut, "ratio_legend": ratio_legend,
        "month": selected.strftime("%Y-%m"), "today": date.today().isoformat(),
        "submission_id": str(uuid4()),
    })


@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
def settings_page(request: Request, message: str = "", db: Session = Depends(get_db)):
    return templates.TemplateResponse("settings.html", {
        "request": request, "message": message,
        "expense_categories": list(db.scalars(select(ExpenseCategory).order_by(ExpenseCategory.id))),
        "income_categories": list(db.scalars(select(IncomeCategory).order_by(IncomeCategory.id))),
        "sources": list(db.scalars(select(FundingSource).order_by(FundingSource.id))),
    })


def add_dictionary_item(db: Session, model, name: str):
    clean = name.strip()[:80]
    if clean and not db.scalar(select(model).where(model.name == clean)):
        db.add(model(name=clean))
        db.commit()


@app.post("/settings/expense-categories", dependencies=[Depends(require_auth)])
def add_expense_category(name: str = Form(...), db: Session = Depends(get_db)):
    add_dictionary_item(db, ExpenseCategory, name)
    return internal_redirect("/settings")


@app.post("/settings/income-categories", dependencies=[Depends(require_auth)])
def add_income_category(name: str = Form(...), db: Session = Depends(get_db)):
    add_dictionary_item(db, IncomeCategory, name)
    return internal_redirect("/settings")


@app.post("/settings/sources", dependencies=[Depends(require_auth)])
def add_source(name: str = Form(...), db: Session = Depends(get_db)):
    add_dictionary_item(db, FundingSource, name)
    return internal_redirect("/settings")


def delete_category_item(db: Session, model, item_id: int, kind: str):
    item = db.get(model, item_id)
    if not item or item.name == "默认":
        return
    db.execute(update(Transaction).where(Transaction.kind == kind, Transaction.category == item.name).values(category="默认"))
    db.delete(item)
    db.commit()


@app.post("/settings/expense-categories/{item_id}/delete", dependencies=[Depends(require_auth)])
def delete_expense_category(item_id: int, db: Session = Depends(get_db)):
    delete_category_item(db, ExpenseCategory, item_id, "expense")
    return internal_redirect("/settings")


@app.post("/settings/income-categories/{item_id}/delete", dependencies=[Depends(require_auth)])
def delete_income_category(item_id: int, db: Session = Depends(get_db)):
    delete_category_item(db, IncomeCategory, item_id, "income")
    return internal_redirect("/settings")


@app.post("/settings/sources/{item_id}/delete", dependencies=[Depends(require_auth)])
def delete_source(item_id: int, db: Session = Depends(get_db)):
    item = db.get(FundingSource, item_id)
    default_source = db.scalar(select(FundingSource).where(FundingSource.name == "默认"))
    if item and item.name != "默认" and default_source:
        db.execute(update(Transaction).where(Transaction.funding_source_id == item.id).values(funding_source_id=default_source.id))
        db.delete(item)
        db.commit()
    return internal_redirect("/settings")


def validate_transaction(db: Session, kind: str, amount: str, category: str, funding_source_id: int):
    if kind not in {"expense", "income"}:
        raise HTTPException(400, "收支类型无效")
    try:
        parsed_amount = Decimal(amount).quantize(Decimal("0.01"))
        if parsed_amount <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "金额必须大于 0")
    if not db.get(FundingSource, funding_source_id):
        raise HTTPException(400, "资金来源不存在")
    category_model = ExpenseCategory if kind == "expense" else IncomeCategory
    clean_category = category.strip()[:80]
    if not db.scalar(select(category_model).where(category_model.name == clean_category)):
        raise HTTPException(400, "请选择设置中已有的收支类型")
    return parsed_amount, clean_category


@app.post("/transactions", dependencies=[Depends(require_auth)])
async def add_transaction(
    request: Request, submission_id: str = Form(...),
    kind: str = Form(...), amount: str = Form(...), category: str = Form(...),
    funding_source_id: int = Form(...), transaction_date: date = Form(...), purpose: str = Form(""), note: str = Form(""),
    ocr_text: str = Form(""), image: UploadFile | None = File(None), db: Session = Depends(get_db),
):
    try:
        submission_id = str(UUID(submission_id))
    except ValueError:
        raise HTTPException(400, "提交标识无效，请刷新页面后重试")
    data = b""
    if image and image.filename:
        data = await image.read(settings.max_upload_mb * 1024 * 1024 + 1)
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "图片过大")
    fingerprint = hashlib.sha256(json.dumps([
        kind, amount, category, funding_source_id, str(transaction_date), purpose, note, ocr_text,
        hashlib.sha256(data).hexdigest(),
    ], ensure_ascii=False).encode("utf-8")).hexdigest()
    existing = db.get(TransactionSubmission, submission_id)
    if existing:
        if existing.fingerprint != fingerprint:
            raise HTTPException(409, "这次提交已经保存，请刷新页面查看记录后再记一笔")
        return saved_response(request, existing.month)
    parsed_amount, clean_category = validate_transaction(db, kind, amount, category, funding_source_id)
    month = transaction_date.strftime("%Y-%m")
    db.add(TransactionSubmission(id=submission_id, fingerprint=fingerprint, month=month))
    try:
        # The primary key handles simultaneous retries across workers/processes.
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.get(TransactionSubmission, submission_id)
        if not existing or existing.fingerprint != fingerprint:
            raise HTTPException(409, "提交冲突，请刷新页面确认记录")
        return saved_response(request, existing.month)
    image_name = None
    try:
        if image and image.filename:
            image_name = save_image(data, image.filename, image.content_type or "image/jpeg")
        db.add(Transaction(
            kind=kind, amount=parsed_amount, category=clean_category, purpose=purpose.strip()[:200],
            funding_source_id=funding_source_id, transaction_date=transaction_date,
            note=note.strip(), image_name=image_name, ocr_text=ocr_text.strip(),
        ))
        db.commit()
    except Exception:
        db.rollback()
        delete_image(image_name)
        raise
    return saved_response(request, month)


@app.post("/transactions/{transaction_id}/edit", dependencies=[Depends(require_auth)])
async def edit_transaction(
    request: Request,
    transaction_id: int, kind: str = Form(...), amount: str = Form(...), category: str = Form(...),
    funding_source_id: int = Form(...), transaction_date: date = Form(...), purpose: str = Form(""), note: str = Form(""),
    ocr_text: str = Form(""), image: UploadFile | None = File(None), db: Session = Depends(get_db),
):
    item = db.get(Transaction, transaction_id)
    if not item:
        raise HTTPException(404, "记录不存在")
    parsed_amount, clean_category = validate_transaction(db, kind, amount, category, funding_source_id)
    old_image_name = item.image_name
    image_replaced = False
    if image and image.filename:
        data = await image.read()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, "图片过大")
        item.image_name = save_image(data, image.filename, image.content_type or "image/jpeg")
        image_replaced = True
    item.kind = kind
    item.amount = parsed_amount
    item.category = clean_category
    item.funding_source_id = funding_source_id
    item.transaction_date = transaction_date
    item.purpose = purpose.strip()[:200]
    item.note = note.strip()
    item.ocr_text = ocr_text.strip()
    db.commit()
    if image_replaced:
        delete_image(old_image_name)
    return saved_response(request, transaction_date.strftime('%Y-%m'))


@app.post("/transactions/{transaction_id}/delete", dependencies=[Depends(require_auth)])
def delete_transaction(transaction_id: int, db: Session = Depends(get_db)):
    item = db.get(Transaction, transaction_id)
    if not item:
        raise HTTPException(404, "记录不存在")
    month = item.transaction_date.strftime("%Y-%m")
    image_name = item.image_name
    db.delete(item)
    db.commit()
    delete_image(image_name)
    return internal_redirect(f"/?month={month}")


@app.post("/ocr", dependencies=[Depends(require_auth)])
async def ocr_image(image: UploadFile = File(...)):
    data = await image.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if not data or len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(400, "图片无效")
    try:
        text_content = await run_in_threadpool(recognize_receipt, data)
    except InvalidOCRImage as exc:
        raise HTTPException(400, str(exc))
    except OCRBusy as exc:
        raise HTTPException(429, str(exc))
    except Exception:
        logging.getLogger(__name__).exception("Local OCR failed")
        raise HTTPException(503, "本地 OCR 服务不可用，请检查依赖和模型文件")
    if not text_content.strip():
        raise HTTPException(422, "未识别到文字，请换一张清晰图片")
    return {"text": text_content}


@app.get("/images/{name}", dependencies=[Depends(require_auth)])
def image(name: str):
    try:
        data, media_type = load_image(name)
    except Exception:
        raise HTTPException(404)
    return Response(data, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/export.csv", dependencies=[Depends(require_auth)])
def export_csv(db: Session = Depends(get_db)):
    output = io.StringIO(newline="")
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["日期", "收支", "金额", "收支类型", "用途", "资金来源/去向", "备注", "图片文件名", "OCR文本"])
    rows = db.scalars(select(Transaction).options(joinedload(Transaction.funding_source)).order_by(Transaction.transaction_date, Transaction.id))
    for row in rows:
        writer.writerow([row.transaction_date, "支出" if row.kind == "expense" else "收入", row.amount, row.category, row.purpose, row.funding_source.name, row.note, row.image_name or "", row.ocr_text])
    filename = f"easy-finance-{date.today().isoformat()}.csv"
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/health")
def health():
    result = {"status": "ok"}
    if RELEASE_REVISION:
        result["revision"] = RELEASE_REVISION
    return result
