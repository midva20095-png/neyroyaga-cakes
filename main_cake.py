import asyncio
import base64
import io
import json
import os
import re
import time
import traceback
import uuid
from pathlib import Path
from typing import List, Literal, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from google import genai
from google.genai import types
from PIL import Image, ImageDraw  # Image раньше не был импортирован -> ошибка 400 на каждый макет
from pydantic import BaseModel, Field
from requests.auth import HTTPBasicAuth

from pdf_builder import A4LayoutEngine

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "generated"  # PDF отдельно от кода и .env
OUTPUT_DIR.mkdir(exist_ok=True)
# Галерея готовых макетов: просто положите картинки в папку gallery рядом с кодом.
# Имя файла становится названием: "Единорог с шарами.png" -> «Единорог с шарами».
GALLERY_DIR = BASE_DIR / "gallery"
GALLERY_DIR.mkdir(exist_ok=True)
GALLERY_EXT = {".png", ".jpg", ".jpeg", ".webp"}

app = FastAPI(title="NeiroYaga Vector Cake Editor & Layout A4")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

_client = None


def get_client():
    """Клиент Gemini создаётся при первой генерации, а не при старте:
    раньше пустой GEMINI_API_KEY валил весь сервер (ValueError при импорте)."""
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("на сервере не задан GEMINI_API_KEY")
        _client = genai.Client(api_key=key)
    return _client


if not os.getenv("GEMINI_API_KEY"):
    print("⚠️ GEMINI_API_KEY не задан: сайт и PDF-макеты работают, генерация арта — нет")
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_AUTH = HTTPBasicAuth(YOOKASSA_SHOP_ID or "", YOOKASSA_SECRET_KEY or "")
RETURN_URL = os.getenv("RETURN_URL", "https://babayagaland.ru/")

SINGLE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image")
# 1 кредит = 1 ₽. Каждый готовый результат платный; цены правятся ТОЛЬКО здесь.
PRICES = {"ai": 50, "pdf": 50, "fix": 25}  # ai — генерация макета, pdf — готовый PDF, fix — доработка одной вырезанной детали
CREDIT_COST = PRICES["ai"]
# Цену пакета задаёт СЕРВЕР. Раньше сумму и число кредитов присылал браузер,
# и можно было купить 100000 кредитов за 1 ₽. ПОСТАВЬТЕ СВОИ ЦЕНЫ: {кредиты: рубли}.
CREDIT_PACKS = {50: 50, 100: 100, 250: 250, 500: 500}
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://neyroyaga-cakes-production.up.railway.app")
ORDER_NOTIFY_URL = os.getenv("ORDER_NOTIFY_URL")  # необязательно: куда слать уведомление о новом заказе
STYLES = {  # стиль приходит из Tilda отдельным полем, а не приклеенным к тексту
    "cartoon": " Стиль: яркий мультяшный.", "watercolor": " Стиль: нежная акварель.",
    "flat": " Стиль: плоская векторная графика, минимализм.", "3d": " Стиль: объёмный 3D-рендер.",
    "realistic": " Стиль: реалистичный.", "line-art": " Стиль: чистый контурный лайн-арт.",
}

SYSTEM_PROMPT = (
    "Ты иллюстратор. Выполняй описание буквально: рисуй только то, что названо, ничего не добавляй от себя. "
    "Никогда не добавляй текст, буквы и цифры."
)
ART_PROMPT = (
    "Нарисуй иллюстрацию строго по описанию: {prompt}.\n"
    "ПРАВИЛА:\n"
    "1. Изображай только то, что названо в описании. Ничего не добавляй от себя.\n"
    "2. Запрещены любые лишние предметы вокруг: звёзды, конфетти, шарики, сердечки, цветы, а также торты, кексы, "
    "пирожные, сладости и любая еда, если их нет в описании.\n"
    "3. Один цельный объект (или группа из описания) по центру, на ровном чисто белом фоне, без теней на фоне, рамок и узоров.\n"
    "4. Никаких надписей, букв, цифр и логотипов."
)
FIX_PROMPT = (
    "На изображении — фрагмент, вырезанный прямоугольной рамкой из чужой картинки, поэтому края рваные "
    "и по углам остался лишний фон. Вокруг уже добавлено белое поле — используй его.\n"
    "ЗАДАЧА: перерисуй ТОЛЬКО главный объект этого фрагмента аккуратно и ЦЕЛИКОМ.\n"
    "ГЛАВНОЕ ПРАВИЛО: НИЧЕГО НЕ ОБРЕЗАЙ. Объект должен полностью помещаться в кадр — "
    "макушка, ноги, руки и все вытянутые части (шланг, струя, хвост, крылья, палка) видны целиком. "
    "Ни одна часть объекта не должна касаться края изображения и уходить за него. "
    "Если объект не помещается — уменьши его, а не обрезай кадр.\n"
    "ОСТАЛЬНЫЕ ПРАВИЛА:\n"
    "1. Сохрани форму, пропорции, цвета, стиль и все детали объекта — это тот же объект, а не новый.\n"
    "2. Дорисуй части, срезанные рамкой, чтобы объект стал целым.\n"
    "3. Убери посторонний фон и обрывки соседних предметов. Фон — ровный чисто белый.\n"
    "4. Объект по центру, вокруг него свободное белое поле со всех четырёх сторон "
    "шириной примерно в одну десятую кадра.\n"
    "5. Ровные чистые края, никаких надписей, букв, цифр, рамок и теней на фоне.\n"
    "{extra}"
)
REF_PROMPT = (
    "Выше {n} изображений-образцов: это детали, выбранные пользователем. Воспроизведи их форму, цвета и стиль "
    "максимально точно и собери результат по описанию: {prompt}.\n"
    "Не добавляй никаких других предметов (особенно тортов, кексов и еды, если их нет на образцах или в описании). "
    "Один цельный результат по центру на ровном чисто белом фоне, без надписей."
)


# ---------- Google Sheets (кредиты). requests блокирующий -> уходит в поток ----------
def _sheets_call(payload: dict, timeout: int):
    """Один запрос к Apps Script. Он отвечает редиректом на script.googleusercontent.com/macros/echo:
    POST шлём один раз, ответ забираем GET-ом (его можно повторять, 404 там бывает временным)."""
    r = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=timeout, allow_redirects=False)
    for _ in range(3):
        if not r.is_redirect:
            break
        loc = r.headers["Location"]
        for attempt in range(3):
            r = requests.get(loc, timeout=timeout)
            if r.status_code != 404:
                break
            time.sleep(0.8 * (attempt + 1))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]!r}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"ответ не JSON: {r.text[:160]!r}")


async def sheet(payload: dict, timeout: int = 20, tries: int = 3):
    """Все кредиты живут в Google-таблице. У каждого запроса есть request_id, и скрипт таблицы
    повторный запрос с тем же id не выполняет дважды — поэтому при таймауте можно спокойно повторить."""
    if not GOOGLE_SCRIPT_URL:
        return None  # режим разработки без таблицы
    payload = {**payload, "request_id": payload.get("request_id") or uuid.uuid4().hex}
    for attempt in range(1, tries + 1):
        try:
            return await asyncio.to_thread(_sheets_call, payload, timeout)
        except Exception as e:
            print(f"❌ Таблица ({payload.get('action')}, попытка {attempt}/{tries}): {e}")
            if attempt < tries:
                await asyncio.sleep(1.5 * attempt)
    return {"success": False, "unavailable": True}


async def deduct(email: str, cost: int, request_id: str = None) -> dict:
    res = await sheet({"action": "deduct", "email": (email or "").strip(), "cost": cost, "request_id": request_id})
    if res is None:
        return {"success": True, "credits": 999}
    if res.get("unavailable"):
        return {"success": False, "credits": None, "unavailable": True}
    return {"success": bool(res.get("success")), "credits": res.get("credits", 0)}


async def add_credits(email: str, amount: int, request_id: str = None) -> bool:
    res = await sheet({"action": "add", "email": (email or "").strip(), "amount": amount, "request_id": request_id}, 20)
    ok = res is None or res.get("success", True) is not False
    if not ok:
        print(f"⚠️ НЕ УДАЛОСЬ начислить {amount} кр. для {email} — поправьте баланс в таблице вручную")
    return ok


# ---------- Макет A4 ----------
class ItemConfig(BaseModel):
    image_base64: str
    type: Literal["height", "width", "diameter"]
    size: float = Field(gt=0, le=27.3)  # см; рабочая зона A4 — 18.6 × 27.3 см
    copies: int = Field(default=1, ge=1, le=100)
    outline: float = Field(default=0, ge=0, le=5)  # белая обводка под вырезание, мм
    circle: bool = False                           # круглый топпер (капкейки)


class LayoutRequest(BaseModel):
    items: List[ItemConfig] = Field(min_length=1, max_length=50)
    email: str = Field(min_length=3, max_length=200)


def cleanup_old_files():
    now = time.time()
    for f in OUTPUT_DIR.glob("*"):
        ttl = 7 * 86400 if f.name.startswith("order_") else 3600  # заказы храним неделю
        if f.is_file() and now - f.stat().st_mtime > ttl:
            f.unlink(missing_ok=True)


def make_pdf(entries, prefix="layout"):
    """entries: [(картинка base64, type, size_cm, copies, outline_mm, circle)] -> (имя файла, число листов)"""
    cleanup_old_files()
    items = []
    for n, (b64, typ, size, copies, *rest) in enumerate(entries, 1):
        try:
            img = Image.open(io.BytesIO(base64.b64decode((b64 or "").split(",", 1)[-1])))
            img.load()
        except Exception:
            raise ValueError(f"Картинка №{n}: не удалось прочитать изображение")
        items.append({"image": img, "type": typ, "size": size, "copies": copies,
                      "outline": rest[0] if rest else 0, "circle": bool(rest[1]) if len(rest) > 1 else False})
    name = f"{prefix}_{os.urandom(8).hex()}.pdf"
    return name, A4LayoutEngine().generate_pdf(items, str(OUTPUT_DIR / name))


async def paid_job(email: str, kind: str, fn, *args):
    """Списывает кредиты, делает работу в потоке, при любой ошибке возвращает кредиты."""
    email = (email or "").strip()
    if not email:
        raise HTTPException(status_code=401, detail="Войдите в аккаунт")
    op = uuid.uuid4().hex
    res = await deduct(email, PRICES[kind], op)
    if res.get("unavailable"):
        raise HTTPException(status_code=503, detail="Таблица с балансом не отвечает. Повторите через минуту, кредиты не списаны.")
    if not res["success"]:
        raise HTTPException(status_code=402, detail=f"Недостаточно кредитов: нужно {PRICES[kind]} кр., на балансе {res['credits']} кр.")
    try:
        return await asyncio.to_thread(fn, *args), res["credits"]
    except Exception as e:
        await add_credits(email, PRICES[kind], "refund:" + op)
        if isinstance(e, ValueError):
            raise HTTPException(status_code=400, detail=str(e))
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Внутренняя ошибка, кредиты возвращены")


@app.get("/api/config")
def config():
    return {"prices": PRICES, "packs": [{"credits": c, "amount": a} for c, a in CREDIT_PACKS.items()]}


@app.post("/api/generate-pdf")  # набор наклеек на листах A4 (платно)
async def generate_pdf_endpoint(payload: LayoutRequest):
    entries = [(i.image_base64, i.type, i.size, i.copies, i.outline, i.circle) for i in payload.items]
    (name, sheets), credits = await paid_job(payload.email, "pdf", make_pdf, entries)
    return {"success": True, "sheets_count": sheets, "download_url": f"/api/download/{name}", "credits": credits}


def gallery_items():
    return sorted((f for f in GALLERY_DIR.iterdir() if f.suffix.lower() in GALLERY_EXT), key=lambda f: f.name.lower())


def gallery_file(item_id: str) -> Path:
    for f in gallery_items():
        if f.stem == item_id:
            return f
    raise HTTPException(status_code=404, detail="Макет не найден")


@app.get("/api/gallery")
def gallery_list():
    return [{"id": f.stem, "title": f.stem.replace("_", " "), "preview": f"/api/gallery-preview/{f.stem}"}
            for f in gallery_items()]


@app.get("/api/gallery-preview/{item_id}")
def gallery_preview(item_id: str):
    """Превью: мелкое и с водяным знаком, чтобы его нельзя было напечатать вместо покупки."""
    src = gallery_file(item_id)
    out = OUTPUT_DIR / f"preview_{item_id}.png"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        img = Image.open(src).convert("RGBA")
        img.thumbnail((520, 520), Image.Resampling.LANCZOS)
        mark = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(mark)
        for y in range(-img.height, img.height * 2, 90):
            for x in range(-img.width, img.width, 190):
                d.text((x + (y % 180), y), "НейроЯга · образец", fill=(120, 120, 120, 110))
        img.alpha_composite(mark.rotate(30, resample=Image.BICUBIC, center=(img.width // 2, img.height // 2)))
        img.convert("RGB").save(out, "PNG")
    return FileResponse(out, media_type="image/png")


class GalleryBuy(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    item_id: str = Field(min_length=1, max_length=200)
    width_mm: float = Field(gt=10, le=210)
    height_mm: float = Field(gt=10, le=297)
    circle: bool = False
    copies: int = Field(default=1, ge=1, le=100)


def make_gallery_pdf(p: GalleryBuy) -> str:
    cleanup_old_files()
    img = Image.open(gallery_file(p.item_id))
    img.load()
    name = f"layout_{os.urandom(8).hex()}.pdf"
    engine = A4LayoutEngine()
    if p.copies > 1:                      # несколько штук — раскладываем на листы как наклейки
        engine.generate_pdf([{"image": img, "type": "diameter" if p.circle else "width",
                              "size": p.width_mm / 10, "copies": p.copies,
                              "outline": 2.5, "circle": p.circle}], str(OUTPUT_DIR / name))
    else:
        engine.export_exact(img, p.width_mm, p.width_mm if p.circle else p.height_mm, p.circle, str(OUTPUT_DIR / name))
    return name


@app.post("/api/gallery-buy")   # готовый макет из галереи (платно)
async def gallery_buy(p: GalleryBuy):
    name, credits = await paid_job(p.email, "pdf", make_gallery_pdf, p)
    return {"success": True, "sheets_count": 1, "download_url": f"/api/download/{name}", "credits": credits}


class ExactRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    image_base64: str
    width_mm: float = Field(gt=10, le=210)
    height_mm: float = Field(gt=10, le=297)
    circle: bool = False


def make_exact(p: ExactRequest) -> str:
    cleanup_old_files()
    try:
        img = Image.open(io.BytesIO(base64.b64decode(p.image_base64.split(",", 1)[-1])))
        img.load()
    except Exception:
        raise ValueError("Не удалось прочитать изображение")
    name = f"layout_{os.urandom(8).hex()}.pdf"
    h = p.width_mm if p.circle else p.height_mm
    A4LayoutEngine().export_exact(img, p.width_mm, h, p.circle, str(OUTPUT_DIR / name))
    return name


@app.post("/api/export-print")  # один макет точного размера по центру A4 (платно)
async def export_print(p: ExactRequest):
    name, credits = await paid_job(p.email, "pdf", make_exact, p)
    return {"success": True, "sheets_count": 1, "download_url": f"/api/download/{name}", "credits": credits}


@app.get("/api/download/{filename}")
def download_pdf(filename: str):
    # Раньше отдавался ЛЮБОЙ файл рядом с кодом, включая .env с ключами.
    if not re.fullmatch(r"(?:layout|order)_[0-9a-f]{16}\.pdf", filename):
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = OUTPUT_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл устарел — соберите макет заново")
    return FileResponse(path, media_type="application/pdf", filename="sugar_print_layout.pdf")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


# ---------- Генерация арта ----------
def _pad_white(src: str, frac: float = 0.18) -> str:
    """Добавляет белые поля вокруг детали перед отправкой в ИИ: так модель видит,
    что вокруг объекта есть место, и реже обрезает его под край кадра."""
    try:
        img = Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[-1]))).convert("RGB")
        pad = max(24, round(max(img.size) * frac))
        canvas = Image.new("RGB", (img.width + 2 * pad, img.height + 2 * pad), (255, 255, 255))
        canvas.paste(img, (pad, pad))
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=92)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"⚠️ Поля для детали не добавлены: {e}")
        return src


def _part(src: str):
    mime = src[5:src.index(";")] if src.startswith("data:") and ";" in src else "image/jpeg"
    return types.Part.from_bytes(data=base64.b64decode(src.split(",", 1)[-1]), mime_type=mime)


def generate_art(prompt: str, file_obj, style: str = "", refs=None, mode: str = "art") -> tuple:
    sources = ([file_obj["data"]] if isinstance(file_obj, dict) and file_obj.get("data") else []) + list(refs or [])
    sources = sources[:6]
    if mode == "fix":
        sources = [_pad_white(src) for src in sources]
    contents = [_part(src) for src in sources]
    if mode == "fix" and contents:          # доработка одной вырезанной детали
        text = FIX_PROMPT.format(extra=("6. Пожелание пользователя: " + prompt) if prompt else "")
    elif contents:
        text = REF_PROMPT.format(n=len(contents), prompt=prompt or "по образцам")
    else:
        text = ART_PROMPT.format(prompt=prompt)
    contents.append(text + STYLES.get(style, ""))

    response = get_client().models.generate_content(
        model=SINGLE_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, response_modalities=["TEXT", "IMAGE"]),
    )
    cands = getattr(response, "candidates", None) or []
    parts = getattr(response, "parts", None) or (cands[0].content.parts if cands and cands[0].content else None) or []
    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline and inline.data:
            data = inline.data if isinstance(inline.data, bytes) else base64.b64decode(inline.data)
            return base64.b64encode(data).decode(), inline.mime_type or "image/png"
    reason = getattr(cands[0], "finish_reason", None) if cands else None
    said = " ".join((getattr(p, "text", "") or "") for p in parts)[:150]
    print(f"❌ Gemini без изображения: finish_reason={reason} text={said!r}")
    raise RuntimeError(f"модель не вернула изображение ({reason or 'без причины'}). {said}".strip())


@app.websocket("/ws/{client_email}")
async def websocket_endpoint(websocket: WebSocket, client_email: str):
    await websocket.accept()

    async def send(text, credits, type_="text", done=True, **extra):
        await websocket.send_text(json.dumps({"type": type_, "credits": credits, "text": text, "done": done, **extra}))

    first = await deduct(client_email, 0)
    balance = first["credits"]
    await send("⚠️ Таблица с балансом не отвечает, обновите страницу." if first.get("unavailable")
               else "🎂 Добро пожаловать в студию макетов!", balance)
    try:
        while True:
            raw = await websocket.receive_text()
            prompt, file_obj, style, refs, mode = raw.strip()[:500], None, "", [], "art"
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    prompt, file_obj = str(data.get("prompt") or "").strip()[:500], data.get("file")
                    style = str(data.get("style") or "")
                    refs = [r for r in (data.get("refs") or []) if isinstance(r, str)][:6]
                    mode = "fix" if data.get("mode") == "fix" and refs else "art"
            except json.JSONDecodeError:
                pass
            if not prompt and not refs and not (isinstance(file_obj, dict) and file_obj.get("data")):
                await send("✍️ Опишите, что нарисовать, или приложите референс.", balance)
                continue  # пустой запрос больше не списывает кредиты

            cost = PRICES["fix"] if mode == "fix" else PRICES["ai"]
            op = uuid.uuid4().hex
            res = await deduct(client_email, cost, op)
            if res.get("unavailable"):
                await send("⚠️ Таблица с балансом не отвечает. Повторите через минуту, кредиты не списаны.", balance)
                continue
            balance = res["credits"]
            if not res["success"]:
                await send(f"💎 Недостаточно кредитов: нужно {cost} кр., на балансе {balance} кр.", balance)
                continue
            await send("🪄 Дорабатываю деталь..." if mode == "fix" else "🪄 Генерация композиции для макета...", balance, done=False)

            try:  # Gemini — блокирующий вызов: в потоке, иначе замирают все пользователи
                print(f"🎨 {mode} для {client_email}: текст={len(prompt)} симв., образцов={len(refs)}, списано {cost} кр.")
                image_b64, mime = await asyncio.to_thread(generate_art, prompt, file_obj, style, refs, mode)
            except Exception as e:
                print(f"❌ Генерация: {e}")
                await add_credits(client_email, cost, "refund:" + op)
                balance += cost
                await send(f"⚠️ Не удалось создать арт, кредиты возвращены. {str(e)[:160]}", balance)
                continue
            await send("✨ Деталь готова! Она добавлена в список «Макет A4»." if mode == "fix"
                       else "✨ Макет готов! Он добавлен в список «Макет A4».", balance, "art", data=image_b64, mimeType=mime)
            print(f"✅ Арт отправлен клиенту: {len(image_b64) // 1024} КБ")
    except WebSocketDisconnect:
        print(f"Кондитер {client_email} отключился.")


# ---------- Авторизация и оплата ----------
@app.post("/api/auth")
async def authenticate_user(request: Request):
    if not GOOGLE_SCRIPT_URL:
        return {"success": True}
    data = await request.json()
    res = await sheet({"action": "auth", "email": data.get("email"), "password": data.get("password")})
    if isinstance(res, dict) and res.get("unavailable"):
        return {"success": False, "error": "Сервис входа временно недоступен. Повторите через минуту."}
    return res if isinstance(res, dict) else {"success": False}


@app.get("/api/packs")
def packs():
    return [{"credits": c, "amount": a} for c, a in CREDIT_PACKS.items()]


@app.post("/api/create-payment")
async def create_payment_endpoint(request: Request):
    try:
        data = await request.json()
        email = str(data.get("email") or "").strip()
        credits = int(data.get("credits") or 0)
        amount = CREDIT_PACKS.get(credits)
        if not email or amount is None:
            return {"success": False, "error": "Неизвестный пакет кредитов"}
        if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
            return {"success": True, "confirmation_url": "https://yookassa.ru"}  # режим разработки

        payload = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": RETURN_URL},
            "description": f"Покупка {credits} кредитов ({email})",
            "metadata": {"email": email, "credits": credits},
        }
        res = await asyncio.to_thread(
            lambda: requests.post(
                "https://api.yookassa.ru/v3/payments", json=payload, auth=YOOKASSA_AUTH,
                headers={"Idempotence-Key": str(uuid.uuid4())}, timeout=15,
            ).json()
        )
        url = (res.get("confirmation") or {}).get("confirmation_url")
        return {"success": bool(url), "confirmation_url": url, "error": None if url else res.get("description", "Ошибка YooKassa")}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/yookassa-webhook")
async def yookassa_webhook(request: Request):
    event = await request.json()
    pid = str((event.get("object") or {}).get("id") or "")
    if event.get("event") != "payment.succeeded" or not YOOKASSA_SHOP_ID:
        return {"status": "ok"}
    if not re.fullmatch(r"[\w-]{10,64}", pid):
        return {"status": "ok"}
    # Тело вебхука может прислать кто угодно (раньше это давало бесплатные кредиты) —
    # поэтому статус и метаданные перепроверяются напрямую в YooKassa.
    pay = await asyncio.to_thread(
        lambda: requests.get(f"https://api.yookassa.ru/v3/payments/{pid}", auth=YOOKASSA_AUTH, timeout=15).json()
    )
    meta = pay.get("metadata") or {}
    credits = int(meta.get("credits") or 0)
    if pay.get("status") == "succeeded" and pay.get("paid") and meta.get("email") and credits in CREDIT_PACKS:
        # op_id = номер платежа: повторный вебхук от YooKassa не начислит кредиты дважды
        if not await add_credits(meta["email"], credits, f"pay:{pid}"):  # тот же id -> не начислится дважды
            raise HTTPException(status_code=500, detail="credits not added")  # YooKassa повторит вебхук
        print(f"💰 Оплата {pid}: {meta['email']} +{credits} кр.")
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main_cake:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))