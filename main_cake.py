import asyncio
import base64
import io
import json
import os
import re
import time
import traceback
import uuid
import zipfile
import gc
import ctypes
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
# Папку для готовых файлов можно вынести на постоянный диск: OUTPUT_DIR=/data/generated
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR") or BASE_DIR / "generated")  # PDF отдельно от кода и .env
OUTPUT_DIR.mkdir(exist_ok=True)
# Галерея готовых макетов: просто положите картинки в папку gallery рядом с кодом.
# Имя файла становится названием: "Единорог с шарами.png" -> «Единорог с шарами».
GALLERY_DIR = BASE_DIR / "gallery"
GALLERY_DIR.mkdir(exist_ok=True)
GALLERY_EXT = {".png", ".jpg", ".jpeg", ".webp"}
# Библиотека объектов для песочницы: картинки в папке objects. Имя файла = название объекта.
OBJECTS_DIR = BASE_DIR / "objects"
OBJECTS_DIR.mkdir(exist_ok=True)
OBJECT_EXT = {".svg", ".png", ".webp"}
# Каталог фонов для песочницы: картинки в папке backgrounds, названия — в backgrounds/titles.json
BACKGROUNDS_DIR = BASE_DIR / "backgrounds"
BACKGROUNDS_DIR.mkdir(exist_ok=True)
HALF_SHEET_MM = 148.5                      # половина листа A4: кондитеры печатают на остатках

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
PRICES = {"pdf": 10, "background": 3, "fix": 25, "ai": 50, "cut": 50, "bg": 50, "enhance": 50}  # pdf — готовый PDF без ИИ (песочница и раскладка); остальное — работа ИИ
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
    "Текст на картинке делай только если пользователь прямо о нём попросил."
)
# Пользователь часто сам просит надпись («нарисуй торт и надпись Настя»). Если при этом
# в запросе стоит «никаких надписей», Gemini видит противоречие и отказывается рисовать.
TEXT_WORDS = ("текст", "надпис", "подпис", "напиши", "слово", "слова", "буквы", "имя")
NO_TEXT_RULE = "4. Никаких надписей, букв, цифр и логотипов."
TEXT_RULE = (
    "4. Надпись из описания напиши ровно один раз, крупно и БЕЗ ОШИБОК, строго теми же словами "
    "и буквами, что в описании. Другого текста, букв и логотипов не добавляй."
)


def wants_text(prompt: str) -> bool:
    p = prompt or ""
    return any(w in p.lower() for w in TEXT_WORDS) or '"' in p or "«" in p
ART_PROMPT = (
    "Нарисуй иллюстрацию строго по описанию: {prompt}.\n"
    "ПРАВИЛА:\n"
    "1. Изображай только то, что названо в описании. Ничего не добавляй от себя.\n"
    "2. Запрещены любые лишние предметы вокруг: звёзды, конфетти, шарики, сердечки, цветы, а также торты, кексы, "
    "пирожные, сладости и любая еда, если их нет в описании.\n"
    "3. Один цельный объект (или группа из описания) по центру, на ровном чисто белом фоне, без теней на фоне, рамок и узоров.\n"
    "{text_rule}"
)
# Общие правила чистого выреза: именно из-за них раньше оставался цветок под бабочкой
CLEAN_RULES = (
    "ЧТО СЧИТАЕТСЯ ФОНОМ (убрать полностью): всё, на чём объект стоит, сидит, лежит или висит — цветок, ветка, трава, земля, стол, тарелка, подставка, рука, вода, а также стены, небо, предметы рядом, тени и блики на поверхности. Даже если это красиво и касается объекта — это фон.\n"
    "ЧТО ОСТАВИТЬ: только сам объект и то, что является его частью (лапы, крылья, усики, одежда, то, что объект держит в руках).\n"
    "КРАЙ ОБЪЕКТА: вырез как у наклейки под ножницы — чёткий резкий контур, без ореола, без полупрозрачной каймы, без размытых пятен и без остатков цветного фона по краю. Тонкие детали (усики, шерсть, пряди, струя) вырезаны аккуратно, а не замылены.\n"
    "ФОН РЕЗУЛЬТАТА: равномерный белый #FFFFFF по всему кадру, без градиента, серых пятен, теней под объектом и рамок.\n"
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
    "3. Объект по центру, вокруг него свободное белое поле со всех четырёх сторон "
    "шириной примерно в одну десятую кадра.\n"
    "4. Никаких надписей, букв, цифр и рамок.\n"
    + CLEAN_RULES +
    "{extra}"
)
ENHANCE_PROMPT = (
    "На изображении — макет для съедобной печати, который пользователь собрал сам из картинок и надписей.\n"
    "ЗАДАЧА: аккуратно улучшить этот макет, не меняя его сути.\n"
    "ПРАВИЛА:\n"
    "1. Сохрани композицию: те же объекты, на тех же местах, тех же размеров.\n"
    "2. Сохрани ВСЕ надписи слово в слово, без ошибок и без новых слов. Их можно сделать "
    "только аккуратнее и читабельнее.\n"
    "3. Улучшай только качество: ровные края, чистые цвета, мягкие тени, общий стиль.\n"
    "4. Ничего не добавляй от себя и ничего не убирай.\n"
    "5. Формат кадра и пропорции оставь прежними, ничего не обрезай.\n"
    "{extra}"
)
CUT_PROMPT = (
    "На изображении — фотография или рисунок. ЗАДАЧА: вырежи {what} и помести на чисто белый фон.\n"
    "ПРАВИЛА:\n"
    "1. Объект остаётся точно таким же: та же поза, те же цвета, те же детали. Не перерисовывай его заново.\n"
    "2. РОВНО ОДИН объект. Не дублируй его, не делай копий, не добавляй второго такого же.\n"
    "3. НИЧЕГО НЕ ОБРЕЗАЙ: объект виден полностью, вместе с мелкими частями, и не касается краёв кадра.\n"
    "4. Никаких надписей, букв и цифр: режим только вырезает объект.\n"
    + CLEAN_RULES +
    "ПРОВЕРЬ СЕБЯ перед ответом: на картинке остался ровно один объект на белом фоне; "
    "под ним и вокруг него нет ни кусочка прежнего фона; край чёткий; ничего не обрезано."
)
BG_PROMPT = (
    "Даны два изображения. Первое — исходная картинка с объектом. Второе — новый фон.\n"
    "ЗАДАЧА: помести объект с первого изображения ({what}) на фон со второго изображения.\n"
    "ПРАВИЛА:\n"
    "1. Объект переносится без изменений: та же форма, поза, цвета и все детали.\n"
    "2. Старый фон удаляется полностью.\n"
    "3. Объект вписывается в новый фон естественно: правильный масштаб, положение и направление света.\n"
    "4. Объект виден целиком и не обрезается краями кадра.\n"
    "5. Никаких надписей, букв и цифр.\n"
    "{extra}"
)
REF_PROMPT = (
    "Выше {n} изображений-образцов: это детали, выбранные пользователем. Воспроизведи их форму, цвета и стиль "
    "максимально точно и собери результат по описанию: {prompt}.\n"
    "Не добавляй никаких других предметов (особенно тортов, кексов и еды, если их нет на образцах или в описании). "
    "Один цельный результат по центру на ровном чисто белом фоне.\n{text_rule}"
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


def norm_email(value) -> str:
    """Почта без регистра и пробелов: иначе Vasya@mail.ru и vasya@mail.ru — два разных счёта."""
    return str(value or "").strip().lower()


async def deduct(email: str, cost: int, request_id: str = None) -> dict:
    res = await sheet({"action": "deduct", "email": norm_email(email), "cost": cost, "request_id": request_id})
    if res is None:
        return {"success": True, "credits": 999}
    if res.get("unavailable"):
        return {"success": False, "credits": None, "unavailable": True}
    return {"success": bool(res.get("success")), "credits": res.get("credits", 0)}


async def add_credits(email: str, amount: int, request_id: str = None) -> bool:
    res = await sheet({"action": "add", "email": norm_email(email), "amount": amount, "request_id": request_id}, 20)
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
    raw: bool = False                              # готовый макет: не трогать фон и поля
    cutline: bool = False                          # тонкая линия по краю: видно, где резать


class LayoutRequest(BaseModel):
    items: List[ItemConfig] = Field(min_length=1, max_length=50)
    email: str = Field(min_length=3, max_length=200)
    half_sheet: bool = False               # печатать на половинках листа A4
    fmt: Literal["pdf", "jpg"] = "pdf"     # PDF или картинки листов в JPG


def cleanup_old_files():
    now = time.time()
    for f in OUTPUT_DIR.glob("*"):
        ttl = 7 * 86400 if f.name.startswith("order_") else 3600  # заказы храним неделю
        if f.is_file() and now - f.stat().st_mtime > ttl:
            f.unlink(missing_ok=True)


def make_pdf(entries, prefix="layout", half=False, fmt="pdf"):
    """entries: [(картинка base64, type, size_cm, copies, outline_mm, circle, raw)] -> (файл, число листов)"""
    cleanup_old_files()
    items = []
    for n, (b64, typ, size, copies, *rest) in enumerate(entries, 1):
        try:
            img = Image.open(io.BytesIO(base64.b64decode((b64 or "").split(",", 1)[-1])))
            img.load()
        except Exception:
            raise ValueError(f"Картинка №{n}: не удалось прочитать изображение")
        items.append({"image": img, "type": typ, "size": size, "copies": copies,
                      "outline": rest[0] if rest else 0,
                      "circle": bool(rest[1]) if len(rest) > 1 else False,
                      "raw": bool(rest[2]) if len(rest) > 2 else False,
                      "cutline": bool(rest[3]) if len(rest) > 3 else False})
    sheet = HALF_SHEET_MM if half else 297
    stem = f"{prefix}_{os.urandom(8).hex()}"
    if fmt == "jpg":
        paths = A4LayoutEngine().generate_images(items, str(OUTPUT_DIR), stem, sheet_h_mm=sheet)
        if len(paths) == 1:                       # один лист отдаём картинкой
            return Path(paths[0]).name, 1
        zip_name = f"{stem}.zip"                  # несколько листов — архивом
        with zipfile.ZipFile(OUTPUT_DIR / zip_name, "w", zipfile.ZIP_DEFLATED) as z:
            for i, p in enumerate(paths, 1):
                z.write(p, f"list_{i}.jpg")
                Path(p).unlink(missing_ok=True)
        return zip_name, len(paths)
    name = f"{stem}.pdf"
    return name, A4LayoutEngine().generate_pdf(items, str(OUTPUT_DIR / name), sheet_h_mm=sheet)


def release_memory():
    """Отдаёт системе память после тяжёлой работы с картинками.
    Python освобождает объекты, но не всегда возвращает память ОС — отсюда рост RSS."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)      # на Linux-контейнерах возвращает кучу системе
    except Exception:
        pass


async def paid_job(email: str, kind: str, fn, *args):
    """Списывает кредиты, делает работу в потоке, при любой ошибке возвращает кредиты."""
    email = norm_email(email)
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
    finally:
        await asyncio.to_thread(release_memory)


@app.get("/api/config")
def config():
    return {"prices": PRICES, "packs": [{"credits": c, "amount": a} for c, a in CREDIT_PACKS.items()]}


@app.post("/api/generate-pdf")  # набор наклеек на листах A4 (платно)
async def generate_pdf_endpoint(payload: LayoutRequest):
    entries = [(i.image_base64, i.type, i.size, i.copies, i.outline, i.circle, i.raw, i.cutline) for i in payload.items]
    (name, sheets), credits = await paid_job(payload.email, "pdf", make_pdf, entries, "layout",
                                             payload.half_sheet, payload.fmt)
    return {"success": True, "sheets_count": sheets, "download_url": f"/api/download/{name}", "credits": credits}


def titles_of(folder: Path) -> dict:
    """Названия берём из titles.json, если он есть: имена файлов латиницей, чтобы архив
    одинаково распаковывался на macOS и Windows, а названия в интерфейсе оставались русскими."""
    f = folder / "titles.json"
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ {f}: {e}")
    return {}


def gallery_items():
    return sorted((f for f in GALLERY_DIR.iterdir() if f.suffix.lower() in GALLERY_EXT), key=lambda f: f.name.lower())


def gallery_file(item_id: str) -> Path:
    for f in gallery_items():
        if f.stem == item_id:
            return f
    raise HTTPException(status_code=404, detail="Макет не найден")


@app.get("/api/objects")
def objects_list():
    """Объекты для песочницы: имена файлов латиницей, русские названия — из objects/titles.json."""
    names = titles_of(OBJECTS_DIR)
    files = sorted((f for f in OBJECTS_DIR.iterdir() if f.suffix.lower() in OBJECT_EXT), key=lambda f: f.name.lower())
    out = []
    for f in files:
        title = names.get(f.name, f.stem.replace("-", " "))
        out.append({"id": f.name, "title": title, "group": title.split()[0], "url": f"/api/object/{f.name}"})
    return sorted(out, key=lambda x: x["title"].lower())


@app.get("/api/object/{name}")
def object_file(name: str):
    path = OBJECTS_DIR / name
    if path.suffix.lower() not in OBJECT_EXT or not path.is_file() or path.parent != OBJECTS_DIR:
        raise HTTPException(status_code=404, detail="Объект не найден")
    mime = {".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp"}[path.suffix.lower()]
    return FileResponse(path, media_type=mime)


@app.get("/api/backgrounds")
def backgrounds_list():
    """Фоны для песочницы: картинки в папке backgrounds, названия — из titles.json."""
    names = titles_of(BACKGROUNDS_DIR)
    files = sorted((f for f in BACKGROUNDS_DIR.iterdir() if f.suffix.lower() in GALLERY_EXT), key=lambda f: f.name.lower())
    return [{"id": f.stem, "title": names.get(f.name, f.stem.replace("-", " ")),
             "preview": f"/api/background-preview/{f.stem}"} for f in files]


def background_file(item_id: str) -> Path:
    for f in BACKGROUNDS_DIR.iterdir():
        if f.stem == item_id and f.suffix.lower() in GALLERY_EXT:
            return f
    raise HTTPException(status_code=404, detail="Фон не найден")


@app.get("/api/background-preview/{item_id}")
def background_preview(item_id: str):
    """Мелкое превью: по нему выбирают, но печатать его нельзя."""
    src = background_file(item_id)
    out = OUTPUT_DIR / f"bgprev_{item_id}.png"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        img = Image.open(src).convert("RGB")
        img.thumbnail((360, 360), Image.Resampling.LANCZOS)
        img.save(out, "PNG")
    return FileResponse(out, media_type="image/png")


def background_image(item_id: str, side: int = 2400) -> str:
    img = Image.open(background_file(item_id)).convert("RGB")
    img.thumbnail((side, side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


class BackgroundBuy(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    item_id: str = Field(min_length=1, max_length=200)


@app.post("/api/background-open")   # фон из каталога в песочницу (платно)
async def background_open(p: BackgroundBuy):
    background_file(p.item_id)
    img, credits = await paid_job(p.email, "background", background_image, p.item_id)
    return {"success": True, "image": img, "credits": credits}


@app.get("/api/gallery")
def gallery_list():
    names = titles_of(GALLERY_DIR)
    return [{"id": f.stem, "title": names.get(f.name, f.stem.replace("_", " ").replace("-", " ")),
             "preview": f"/api/gallery-preview/{f.stem}"} for f in gallery_items()]


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
    fmt: Literal["pdf", "jpg"] = "pdf"
    half_sheet: bool = False
    cutline: bool = True


def make_gallery_pdf(p: GalleryBuy):
    """Готовый макет из галереи в нужном размере: PDF или JPG, целый лист или половина."""
    cleanup_old_files()
    img = Image.open(gallery_file(p.item_id))
    img.load()
    stem = f"layout_{os.urandom(8).hex()}"
    sheet = HALF_SHEET_MM if p.half_sheet else 297
    engine = A4LayoutEngine()
    h_mm = p.width_mm if p.circle else p.height_mm
    if p.copies > 1:                      # несколько штук — раскладываем на листы как наклейки
        items = [{"image": img, "type": "diameter" if p.circle else "width",
                  "size": p.width_mm / 10, "copies": p.copies, "outline": 2.5, "circle": p.circle}]
        if p.fmt == "jpg":
            paths = engine.generate_images(items, str(OUTPUT_DIR), stem, sheet_h_mm=sheet)
            if len(paths) == 1:
                return Path(paths[0]).name, 1
            zip_name = f"{stem}.zip"
            with zipfile.ZipFile(OUTPUT_DIR / zip_name, "w", zipfile.ZIP_DEFLATED) as z:
                for i, path in enumerate(paths, 1):
                    z.write(path, f"list_{i}.jpg")
                    Path(path).unlink(missing_ok=True)
            return zip_name, len(paths)
        name = f"{stem}.pdf"
        return name, engine.generate_pdf(items, str(OUTPUT_DIR / name), sheet_h_mm=sheet)
    if p.fmt == "jpg":
        name = f"{stem}.jpg"
        engine.export_jpg(img, p.width_mm, h_mm, p.circle, str(OUTPUT_DIR / name), sheet_h_mm=sheet, cutline=p.cutline)
    else:
        name = f"{stem}.pdf"
        engine.export_exact(img, p.width_mm, h_mm, p.circle, str(OUTPUT_DIR / name), sheet_h_mm=sheet, cutline=p.cutline)
    return name, 1


def gallery_image(item_id: str, side: int = 2000) -> str:
    """Чистая картинка макета (без водяного знака) для песочницы."""
    img = Image.open(gallery_file(item_id))
    img.load()
    img.thumbnail((side, side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class GalleryOpen(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    item_id: str = Field(min_length=1, max_length=200)


@app.post("/api/gallery-open")   # открыть купленный макет в песочнице (платно, как PDF)
async def gallery_open(p: GalleryOpen):
    gallery_file(p.item_id)                      # проверяем, что такой макет есть
    img, credits = await paid_job(p.email, "pdf", gallery_image, p.item_id)
    return {"success": True, "image": img, "credits": credits}


@app.post("/api/gallery-buy")   # готовый макет из галереи (платно)
async def gallery_buy(p: GalleryBuy):
    (name, sheets), credits = await paid_job(p.email, "pdf", make_gallery_pdf, p)
    # отдаём и саму картинку: купленный макет можно сразу открыть в песочнице и доработать
    return {"success": True, "sheets_count": sheets, "download_url": f"/api/download/{name}",
            "credits": credits, "image": gallery_image(p.item_id)}


class ExactRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    image_base64: str
    width_mm: float = Field(gt=10, le=210)
    height_mm: float = Field(gt=10, le=297)
    circle: bool = False
    fmt: Literal["pdf", "jpg"] = "pdf"      # PDF для печати или JPG-картинка 300 DPI
    half_sheet: bool = False
    cutline: bool = True                    # линия по краю макета


def make_exact(p: ExactRequest) -> str:
    cleanup_old_files()
    try:
        img = Image.open(io.BytesIO(base64.b64decode(p.image_base64.split(",", 1)[-1])))
        img.load()
    except Exception:
        raise ValueError("Не удалось прочитать изображение")
    name = f"layout_{os.urandom(8).hex()}.{p.fmt}"
    h = p.width_mm if p.circle else p.height_mm
    engine = A4LayoutEngine()
    sheet = HALF_SHEET_MM if p.half_sheet else 297
    if p.fmt == "jpg":                      # тот же лист, только картинкой
        engine.export_jpg(img, p.width_mm, h, p.circle, str(OUTPUT_DIR / name), sheet_h_mm=sheet, cutline=p.cutline)
    else:
        engine.export_exact(img, p.width_mm, h, p.circle, str(OUTPUT_DIR / name), sheet_h_mm=sheet, cutline=p.cutline)
    return name


@app.post("/api/export-print")  # один макет точного размера по центру A4 (платно)
async def export_print(p: ExactRequest):
    name, credits = await paid_job(p.email, "pdf", make_exact, p)
    return {"success": True, "sheets_count": 1, "download_url": f"/api/download/{name}", "credits": credits}


@app.get("/api/download/{filename}")
def download_pdf(filename: str):
    # Раньше отдавался ЛЮБОЙ файл рядом с кодом, включая .env с ключами.
    if not re.fullmatch(r"(?:layout|order)_[0-9a-f]{16}(?:_\d+)?\.(?:pdf|jpg|zip)", filename):
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = OUTPUT_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл устарел — соберите макет заново")
    if path.suffix == ".jpg":
        return FileResponse(path, media_type="image/jpeg", filename="sugar_print_layout.jpg")
    if path.suffix == ".zip":
        return FileResponse(path, media_type="application/zip", filename="sugar_print_sheets.zip")
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


def crop_ratio(data_b64: str, ratio: str) -> str:
    """Обрезает готовую картинку под выбранное соотношение сторон (1:1, 9:16 и т.д.)."""
    try:
        rw, rh = (float(v) for v in ratio.split(":"))
        img = Image.open(io.BytesIO(base64.b64decode(data_b64))).convert("RGB")
        want, have = rw / rh, img.width / img.height
        if abs(want - have) < 0.02:
            return data_b64
        if have > want:                              # слишком широкая — режем по бокам
            nw = round(img.height * want)
            left = (img.width - nw) // 2
            img = img.crop((left, 0, left + nw, img.height))
        else:                                        # слишком высокая — режем сверху и снизу
            nh = round(img.width / want)
            top = (img.height - nh) // 2
            img = img.crop((0, top, img.width, top + nh))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"⚠️ Соотношение сторон не применено: {e}")
        return data_b64


def generate_art(prompt: str, file_obj, style: str = "", refs=None, mode: str = "art", ratio: str = "") -> tuple:
    sources = ([file_obj["data"]] if isinstance(file_obj, dict) and file_obj.get("data") else []) + list(refs or [])
    sources = sources[:6]
    if mode in ("fix", "cut"):
        sources = [_pad_white(src) for src in sources]
    contents = [_part(src) for src in sources]
    if mode == "enhance" and contents:      # улучшить собранный макет
        text = ENHANCE_PROMPT.format(extra=("6. Пожелание пользователя: " + prompt) if prompt else "")
    elif mode == "cut" and contents:        # вырезать объект с фотографии, без надписей
        text = CUT_PROMPT.format(what=prompt or "главный объект")
    elif mode == "bg" and len(contents) >= 2:   # замена фона: объект с первой картинки на фон со второй
        extra = f"6. Формат результата: {ratio} — скомпонуй кадр под него." if ratio else ""
        text = BG_PROMPT.format(what=prompt or "главный объект", extra=extra)
    elif mode == "fix" and contents:        # доработка одной вырезанной детали
        text = FIX_PROMPT.format(extra=("6. Пожелание пользователя: " + prompt) if prompt else "")
    elif contents:
        text = REF_PROMPT.format(n=len(contents), prompt=prompt or "по образцам",
                                 text_rule=TEXT_RULE if wants_text(prompt) else NO_TEXT_RULE)
    else:
        text = ART_PROMPT.format(prompt=prompt, text_rule=TEXT_RULE if wants_text(prompt) else NO_TEXT_RULE)
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
            out = base64.b64encode(data).decode()
            if mode == "bg" and ratio:
                out = crop_ratio(out, ratio)         # точное соотношение сторон гарантируем сами
                return out, "image/png"
            return out, inline.mime_type or "image/png"
    reason = getattr(cands[0], "finish_reason", None) if cands else None
    said = " ".join((getattr(p, "text", "") or "") for p in parts)[:150]
    print(f"❌ Gemini без изображения: finish_reason={reason} text={said!r}")
    raise RuntimeError(f"модель не вернула изображение ({reason or 'без причины'}). {said}".strip())


@app.websocket("/ws/{client_email}")
async def websocket_endpoint(websocket: WebSocket, client_email: str):
    await websocket.accept()
    client_email = norm_email(client_email)      # адрес из ссылки тоже приводим к нижнему регистру

    async def send(text, credits, type_="text", done=True, **extra):
        await websocket.send_text(json.dumps({"type": type_, "credits": credits, "text": text, "done": done, **extra}))

    first = await deduct(client_email, 0)
    balance = first["credits"]
    await send("⚠️ Таблица с балансом не отвечает, обновите страницу." if first.get("unavailable")
               else "🎂 Добро пожаловать в студию макетов!", balance)
    try:
        while True:
            raw = await websocket.receive_text()
            prompt, file_obj, style, refs, mode, ratio = raw.strip()[:500], None, "", [], "art", ""
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    prompt, file_obj = str(data.get("prompt") or "").strip()[:500], data.get("file")
                    style = str(data.get("style") or "")
                    refs = [r for r in (data.get("refs") or []) if isinstance(r, str)][:6]
                    want = str(data.get("mode") or "art")
                    mode = want if want in ("fix", "cut", "bg", "enhance") and refs else "art"
                    if mode == "bg" and len(refs) < 2:
                        mode = "art"
                    ratio = str(data.get("ratio") or "")
                    if ratio not in ("1:1", "9:16", "16:9", "4:3", "3:4", "4:5"):
                        ratio = ""
            except json.JSONDecodeError:
                pass
            if not prompt and not refs and not (isinstance(file_obj, dict) and file_obj.get("data")):
                await send("✍️ Опишите, что нарисовать, или приложите референс.", balance)
                continue  # пустой запрос больше не списывает кредиты

            cost = PRICES.get(mode, PRICES["ai"])
            op = uuid.uuid4().hex
            res = await deduct(client_email, cost, op)
            if res.get("unavailable"):
                await send("⚠️ Таблица с балансом не отвечает. Повторите через минуту, кредиты не списаны.", balance)
                continue
            balance = res["credits"]
            if not res["success"]:
                await send(f"💎 Недостаточно кредитов: нужно {cost} кр., на балансе {balance} кр.", balance)
                continue
            steps = {"fix": "🪄 Дорабатываю деталь...", "cut": "✂️ Вырезаю объект...", "bg": "🖼 Меняю фон...",
                     "enhance": "✨ Улучшаю макет...", "art": "🪄 Генерация композиции для макета..."}
            await send(steps[mode], balance, done=False)

            try:  # Gemini — блокирующий вызов: в потоке, иначе замирают все пользователи
                print(f"🎨 {mode} для {client_email}: текст={len(prompt)} симв., образцов={len(refs)}, списано {cost} кр.")
                image_b64, mime = await asyncio.to_thread(generate_art, prompt, file_obj, style, refs, mode, ratio)

                await asyncio.to_thread(release_memory)       # картинки от ИИ тоже освобождаем
            except Exception as e:
                print(f"❌ Генерация: {e}")
                await add_credits(client_email, cost, "refund:" + op)
                balance += cost
                await send(f"⚠️ Не удалось создать арт, кредиты возвращены. {str(e)[:160]}", balance)
                continue
            done_text = {"enhance": "✨ Макет улучшен! Проверьте результат в песочнице.",
                         "fix": "✨ Деталь готова! Она добавлена в список «Макет A4».",
                         "cut": "✂️ Объект вырезан! Картинка в списке «Макет A4» — задайте форму и размер.",
                         "bg": "🖼 Фон заменён! Картинка в списке «Макет A4».",
                         "art": "✨ Макет готов! Он добавлен в список «Макет A4»."}
            await send(done_text[mode], balance, "art", data=image_b64, mimeType=mime)
            print(f"✅ Арт отправлен клиенту: {len(image_b64) // 1024} КБ")
    except WebSocketDisconnect:
        print(f"Кондитер {client_email} отключился.")


# ---------- Авторизация и оплата ----------
@app.post("/api/auth")
async def authenticate_user(request: Request):
    if not GOOGLE_SCRIPT_URL:
        return {"success": True}
    data = await request.json()
    res = await sheet({"action": "auth", "email": norm_email(data.get("email")), "password": data.get("password")})
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
        email = norm_email(data.get("email"))
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


# В ЮKassa на один магазин можно задать только один адрес уведомлений. Если он уже занят
# магазином Tilda, укажите в кассе наш адрес, а в TILDA_WEBHOOK_URL — адрес Tilda:
# уведомления будут приходить сюда и пересылаться дальше, работать будут оба магазина.
TILDA_WEBHOOK_URL = os.getenv("TILDA_WEBHOOK_URL", "").strip()


def forward_to_tilda(body: bytes, content_type: str):
    """Пересылает уведомление дальше, в магазин Tilda. Ошибки не мешают нашей оплате."""
    if not TILDA_WEBHOOK_URL:
        return
    try:
        requests.post(TILDA_WEBHOOK_URL, data=body,
                      headers={"Content-Type": content_type or "application/json"}, timeout=10)
    except Exception as e:
        print(f"⚠️ Не удалось переслать уведомление в Tilda: {e}")


@app.post("/api/yookassa-webhook")
async def yookassa_webhook(request: Request):
    raw = await request.body()
    # сначала отдаём копию магазину Tilda: его заказы не должны зависеть от нашей обработки
    await asyncio.to_thread(forward_to_tilda, raw, request.headers.get("content-type", ""))
    event = json.loads(raw or b"{}")
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
