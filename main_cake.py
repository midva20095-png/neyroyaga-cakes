import os
import json
import base64
import uuid
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from requests.auth import HTTPBasicAuth
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="NeiroYaga Vector Cake Editor - Flash")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# Использование обычной быстрой модели вместо pro
SINGLE_MODEL = 'gemini-3.1-flash-image'
CREDIT_COST = 5

def check_and_deduct_credits(email: str, cost: int) -> dict:
    if not GOOGLE_SCRIPT_URL:
        return {"success": True, "credits": 999}
    try:
        payload = {"action": "deduct", "email": email, "cost": cost}
        response = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=25)
        if response.status_code != 200:
            return {"success": False, "credits": 0}
        res_data = response.json()
        return {"success": res_data.get("success", False), "credits": res_data.get("credits", 0)}
    except Exception as e:
        print(f"❌ Ошибка Google Sheets: {e}")
        return {"success": False, "credits": 0}

@app.websocket("/ws/{client_email}")
async def websocket_endpoint(websocket: WebSocket, client_email: str):
    await websocket.accept()
    print(f"🎂 Кондитер {client_email} подключился!")
    
    init_result = check_and_deduct_credits(client_email, 0)
    initial_balance = init_result.get("credits", 0)
    
    await websocket.send_text(json.dumps({
        "type": "text",
        "credits": initial_balance,
        "text": "🎂 Добро пожаловать в студию макетов! Введите описание для чистого арта."
    }))

    try:
        while True:
            raw_message = await websocket.receive_text()
            
            prompt = ""
            file_obj = None
            
            try:
                data = json.loads(raw_message)
                if isinstance(data, dict):
                    prompt = data.get('prompt', '')
                    file_obj = data.get('file') 
            except json.JSONDecodeError:
                prompt = raw_message

            required_credits = CREDIT_COST
            credit_result = check_and_deduct_credits(client_email, required_credits)
            can_proceed = credit_result.get("success", False)
            current_balance = credit_result.get("credits", 0)

            if not can_proceed:
                await websocket.send_text(json.dumps({
                    "type": "text", 
                    "credits": current_balance,
                    "text": f"💎 Недостаточно кредитов! Требуется: {required_credits} кр., а на балансе: {current_balance} кр."
                }))
                continue
            
            await websocket.send_text(json.dumps({
                "type": "text", 
                "credits": current_balance,
                "text": "🪄 Генерация чистого графического арта без текста..."
            }))
            
            try:
                contents = []
                
                if file_obj and isinstance(file_obj, dict) and file_obj.get('data'):
                    img_data = file_obj.get('data')
                    if ',' in img_data:
                        img_data = img_data.split(',')[1]
                    file_bytes = base64.b64decode(img_data)
                    mime_type = file_obj.get('mimeType') or 'image/jpeg'
                    contents.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
                
                # ЖЕСТКИЕ ПРАВИЛА: Один объект, по центру, белый фон, строго без текста
                strict_art_prompt = (
                    f"Создай ОДИН ЕДИНСТВЕННЫЙ центральный графический элемент для кондитерского макета на тему: {prompt}. "
                    f"КОМПОЗИЦИЯ: Строго по центру, изолированный объект на абсолютном чистом белом фоне. "
                    f"НИКАКИХ коллажей, никаких паттернов, никаких множества разных предметов (никаких случайных сладостей или ассорти вокруг). "
                    f"Только один главный гармоничный рисунок с чистыми краями. "
                    f"СТРОГОЕ ТРЕБОВАНИЕ: Никаких надписей, букв, цифр или текста!"
                )
                contents.append(strict_art_prompt)

                config_params = types.GenerateContentConfig(
                    system_instruction="Ты — профессиональный иллюстратор графики для кондитерских изделий. Создавай чистые изолированные иллюстрации. НИКОГДА не добавляй текст.",
                    response_modalities=["TEXT", "IMAGE"]
                )
                
                print(f"🚀 Запрос к модели {SINGLE_MODEL}...")
                response = client.models.generate_content(
                    model=SINGLE_MODEL,
                    contents=contents,
                    config=config_params
                )
                
                text_output = ""
                image_data = None
                mime_type = "image/jpeg"
                
                all_parts = []
                try:
                    if hasattr(response, 'parts') and response.parts:
                        all_parts = response.parts
                    elif response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                        all_parts = response.candidates[0].content.parts
                except Exception as ex:
                    print(f"⚠️ Ошибка чтения parts: {ex}")

                for part in all_parts:
                    p_text = getattr(part, 'text', None)
                    if p_text:
                        text_output += ("\n" if text_output else "") + p_text
                    
                    inline = getattr(part, 'inline_data', None) or getattr(part, 'inlineData', None)
                    if inline:
                        raw_bytes = getattr(inline, 'data', None)
                        if raw_bytes:
                            if isinstance(raw_bytes, bytes):
                                image_data = base64.b64encode(raw_bytes).decode('utf-8')
                            elif isinstance(raw_bytes, str):
                                image_data = raw_bytes
                            mime_type = getattr(inline, 'mime_type', None) or getattr(inline, 'mimeType', None) or 'image/jpeg'
                            print(f"✅ Картинка успешно получена! Размер base64: {len(image_data)}")

                if image_data:
                    response_payload = {
                        "type": "art",
                        "data": image_data,
                        "mimeType": mime_type,
                        "credits": current_balance,
                        "text": "✨ Чистый графический арт успешно создан!"
                    }
                else:
                    print("⚠️ Модель не вернула картинку, возвращаем кредиты.")
                    requests.post(GOOGLE_SCRIPT_URL, json={"action": "add", "email": client_email, "amount": required_credits}, timeout=10)
                    response_payload = {
                        "type": "text",
                        "credits": current_balance,
                        "text": "⚠️ Не удалось сформировать арт. Кредиты возвращены."
                    }
                
                await websocket.send_text(json.dumps(response_payload))

            except Exception as e:
                err_str = str(e)
                print(f"❌ Ошибка генерации: {err_str}")
                requests.post(GOOGLE_SCRIPT_URL, json={"action": "add", "email": client_email, "amount": required_credits}, timeout=10)
                await websocket.send_text(json.dumps({"type": "text", "credits": current_balance, "text": f"Ошибка: {err_str}"}))
                
    except WebSocketDisconnect:
        print(f"Кондитер отключился.")

@app.post("/api/auth")
async def authenticate_user(request: Request):
    if not GOOGLE_SCRIPT_URL: return {"success": True}
    try:
        data = await request.json()
        response = requests.post(GOOGLE_SCRIPT_URL, json={"action": "auth", "email": data.get("email"), "password": data.get("password")}, timeout=25)
        return response.json() if response.status_code == 200 else {"success": False}
    except Exception as e: return {"success": False, "error": str(e)}

@app.post("/create-payment")
@app.post("/api/create-payment")
async def create_payment_endpoint(request: Request):
    try:
        data = await request.json()
        email, amount, credits_count = data.get("email"), data.get("amount"), data.get("credits")
        if not email or not amount or not credits_count:
            return {"success": False, "error": "Missing required fields"}

        if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
            return {"success": True, "confirmation_url": "https://yookassa.ru"}

        url = "https://api.yookassa.ru/v3/payments"
        payload = {
            "amount": {"value": f"{float(amount):.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": "https://babayagaland.ru/"},
            "description": f"Покупка {credits_count} кредитов ({email})",
            "metadata": {"email": email, "credits": int(credits_count)}
        }
        response = requests.post(url, json=payload, auth=HTTPBasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY), headers={"Idempotence-Key": str(uuid.uuid4())}, timeout=10)
        res_json = response.json()
        return {"success": True, "confirmation_url": res_json.get("confirmation", {}).get("confirmation_url")}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/yookassa-webhook")
@app.post("/api/yookassa-webhook")
async def yookassa_webhook(request: Request):
    try:
        event_json = await request.json()
        if event_json.get("event") == "payment.succeeded":
            meta = event_json.get("object", {}).get("metadata", {})
            email, add_cr = meta.get("email"), int(meta.get("credits", 0))
            if email and add_cr > 0:
                requests.post(GOOGLE_SCRIPT_URL, json={"action": "add", "email": email, "amount": add_cr}, timeout=25)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))