import os
import json
import base64
import uuid
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from requests.auth import HTTPBasicAuth
from google import genai
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="NeiroYaga Cake Layouts Service")

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

SINGLE_MODEL = 'gemini-3-pro-image'
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

def add_credits_to_sheets(email: str, credits_amount: int) -> bool:
    if not GOOGLE_SCRIPT_URL:
        return True
    try:
        payload = {"action": "deduct", "email": email, "cost": -abs(credits_amount)}
        res = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=25)
        return res.status_code == 200
    except Exception as e:
        print(f"❌ Ошибка добавления кредитов: {e}")
        return False

@app.get("/")
def root():
    return {"status": "ok", "service": "NeiroYaga Cake Layouts Service"}

@app.websocket("/ws/{client_email}")
async def websocket_endpoint(websocket: WebSocket, client_email: str):
    await websocket.accept()
    print(f"🎂 Кондитер {client_email} подключился!")
    
    init_result = check_and_deduct_credits(client_email, 0)
    initial_balance = init_result.get("credits", 0)
    
    await websocket.send_text(json.dumps({
        "type": "text",
        "credits": initial_balance,
        "text": "🎂 Приветствуем в студии «НейроЯга: Макеты для тортов»! Выбирай готовый шаблон или отправь референс."
    }))

    try:
        while True:
            raw_message = await websocket.receive_text()
            
            prompt = ""
            file_obj = None
            template_info = None
            
            try:
                data = json.loads(raw_message)
                if isinstance(data, dict):
                    prompt = data.get('prompt', '')
                    file_obj = data.get('file') 
                    template_info = data.get('template_info') 
            except json.JSONDecodeError:
                prompt = raw_message

            required_credits = CREDIT_COST
            credit_result = check_and_dedktop = check_and_deduct_credits(client_email, required_credits) if 'check_and_dedktop' not in globals() else check_and_deduct_credits(client_email, required_credits)
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
                "text": "🪄 НейроЯга верстает профессиональный макет для съедобной печати..."
            }))
            
            try:
                contents = []
                
                # Обработка картинки-референса
                if file_obj and isinstance(file_obj, dict) and file_obj.get('data'):
                    img_data = file_obj.get('data')
                    if ',' in img_data:
                        img_data = img_data.split(',')[1]
                    file_bytes = base64.b64decode(img_data)
                    mime_type = file_obj.get('mimeType') or 'image/jpeg'
                    
                    contents.append({
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": file_bytes
                        }
                    })
                
                # Строгий промпт именно для макета печати
                if template_info:
                    shape = template_info.get('shape', 'Круглый макет')
                    size = template_info.get('size', '16 см')
                    text_on_cake = template_info.get('text', '')
                    
                    final_prompt = (
                        f"Создай графический макет для съедобной пищевой печати на торт. "
                        f"Форма макета: {shape} (диаметр/сторона {size}). "
                        f"Текст на макете (обязательно четко пропиши его графически): '{text_on_cake}'. "
                        f"Пожелания по стилю и дизайну: {prompt}. "
                        f"Строгие требования: это макет для печати на сахарной бумаге, вид сверху строго перпендикулярно (flat lay), сбатная композиция, высокая четкость, аппетитные цвета."
                    )
                else:
                    final_prompt = (
                        f"Создай графический макет для съедобной пищевой печати на торт по описанию: {prompt}. "
                        f"Вид сверху, четкие границы круга или прямоугольника для печати, красивый декор, идеальное качество."
                    )
                
                contents.append(final_prompt)

                config_params = {
                    "system_instruction": "Ты — генератор макетов для съедобной кондитерской печати. Твоя единственная цель — выдать готовое графическое изображение (картинку) макета для торта. Картинка должна быть в высоком разрешении, с правильной центровкой и текстом."
                }
                
                response = client.models.generate_content(
                    model=SINGLE_MODEL,
                    contents=contents,
                    config=config_params
                )
                
                text_output = ""
                image_data = None
                mime_type = "image/jpeg"
                
                candidate = response.candidates[0] if response.candidates else None
                if candidate and candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        part_text = getattr(part, 'text', None)
                        if not part_text and isinstance(part, dict):
                            part_text = part.get('text')
                        if part_text:
                            text_output += ("\n" if text_output else "") + part_text
                        
                        inline = getattr(part, 'inline_data', None) or getattr(part, 'inlineData', None)
                        if not inline and isinstance(part, dict):
                            inline = part.get('inline_data') or part.get('inlineData')
                        
                        if inline:
                            raw_bytes = getattr(inline, 'data', None)
                            if not raw_bytes and isinstance(inline, dict):
                                raw_bytes = inline.get('data')
                            
                            if raw_bytes:
                                if isinstance(raw_bytes, bytes):
                                    image_data = base64.b64encode(raw_bytes).decode('utf-8')
                                elif isinstance(raw_bytes, str):
                                    image_data = raw_bytes
                                
                                mime_val = getattr(inline, 'mime_type', None) or getattr(inline, 'mimeType', None)
                                if not mime_val and isinstance(inline, dict):
                                    mime_val = mime_val or inline.get('mime_type') or inline.get('mimeType')
                                if mime_val:
                                    mime_type = mime_val

                if image_data:
                    response_payload = {
                        "type": "image",
                        "data": image_data,
                        "mimeType": mime_type,
                        "credits": current_balance,
                        "text": text_output if text_output else f"🎂 Макет для съедобной печати готов! (-{required_credits} кр.)"
                    }
                else:
                    # Возвращаем кредит, если картинка не сгенерировалась
                    add_credits_to_sheets(client_email, required_credits)
                    response_payload = {
                        "type": "text",
                        "credits": current_balance,
                        "text": text_output if text_output else "⚠️ Не удалось сформировать графический макет. Кредиты возвращены."
                    }
                
                await websocket.send_text(json.dumps(response_payload))

            except Exception as e:
                err_str = str(e)
                print(f"❌ Ошибка генерации: {err_str}")
                add_credits_to_sheets(client_email, required_credits)
                await websocket.send_text(json.dumps({"type": "text", "credits": current_balance, "text": f"Ошибка: {err_str}"}))
                
    except WebSocketDisconnect:
        print(f"Кондитер отключился.")

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
            "description": f"Покупка {credits_count} кредитов для НейроЯги ({email})",
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
                add_credits_to_sheets(email, add_cr)
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))