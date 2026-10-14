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

MODEL_MAPPING = {
    'flash': 'gemini-3.8-flash',
    'layout_gen': 'gemini-3.1-flash-image',     # Модель для генерации картинки макета
    'layout_pro': 'gemini-3-pro-image'          # Pro-модель
}

CREDIT_COSTS = {
    'flash': 1,
    'layout_gen': 5,    # 5 кредитов за макет
    'layout_pro': 10    # 10 кредитов за Pro
}

def check_and_deduct_credits(email: str, cost: int) -> dict:
    if not GOOGLE_SCRIPT_URL:
        return {"success": True, "credits": 999}

    try:
        payload = {
            "action": "deduct",
            "email": email,
            "cost": cost
        }
        response = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=25)
        if response.status_code != 200:
            return {"success": False, "credits": 0}
        
        res_data = response.json()
        return {
            "success": res_data.get("success", False),
            "credits": res_data.get("credits", 0)
        }
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
        "text": "🎂 Приветствуем в студии «НейроЯга: Макеты для тортов»! Выбирай готовый шаблон в галерее сверху или опиши свой макет."
    }))

    try:
        while True:
            raw_message = await websocket.receive_text()
            
            prompt = ""
            model_key = 'layout_gen'
            file_obj = None
            template_info = None
            
            try:
                data = json.loads(raw_message)
                if isinstance(data, dict):
                    prompt = data.get('prompt', '')
                    model_key = data.get('modelKey', 'layout_gen')
                    file_obj = data.get('file') 
                    template_info = data.get('template_info') 
            except json.JSONDecodeError:
                prompt = raw_message

            required_credits = CREDIT_COSTS.get(model_key, 5)
            
            credit_result = check_and_deduct_credits(client_email, required_credits)
            can_proceed = credit_result.get("success", False)
            current_balance = credit_result.get("credits", 0)

            if not can_proceed:
                error_payload = json.dumps({
                    "type": "text", 
                    "credits": current_balance,
                    "text": f"💎 Недостаточно кредитов! Требуется: {required_credits} кр., а на балансе: {current_balance} кр."
                })
                await websocket.send_text(error_payload)
                continue
            
            await websocket.send_text(json.dumps({
                "type": "text", 
                "credits": current_balance,
                "text": "🪄 Создаем макет для печати... Готовим цвета и правильные пропорции!"
            }))
            
            try:
                contents = []
                
                if file_obj and file_obj.get('data'):
                    file_bytes = base64.b64decode(file_obj['data'])
                    contents.append({
                        "inline_data": {
                            "mime_type": file_obj.get('mimeType', 'image/jpeg'),
                            "data": file_bytes
                        }
                    })
                
                final_prompt = prompt
                if template_info:
                    shape = template_info.get('shape', 'Круглый макет')
                    size = template_info.get('size', 'стандарт')
                    text_on_cake = template_info.get('text', '')
                    
                    final_prompt = (
                        f"Создай профессиональный макет для съедобной печати на торт. "
                        f"Форма и пропорции: {shape}, размеры: {size}. "
                        f"Текст на макете: '{text_on_cake}'. "
                        f"Пожелания кондитера: {prompt}. "
                        f"Требования: идеальная центровка, высокое качество, "
                        f"яркие и аппетитные цвета для пищевой печати на сахарной или вафельной бумаге."
                    )
                
                if final_prompt:
                    contents.append(final_prompt)
                elif not contents:
                    contents.append("Создай круглый макет для печати на торт диаметром 16 см.")

                resolved_model = MODEL_MAPPING.get(model_key, 'gemini-3.1-flash-image')

                config_params = {
                    "system_instruction": "Ты — арт-директор кондитерской студии. Создаешь макеты для тортов."
                }
                
                response = client.models.generate_content(
                    model=resolved_model,
                    contents=contents,
                    config=config_params
                )
                
                text_output = ""
                image_data = None
                mime_type = "image/jpeg"
                
                candidate = response.candidates[0] if response.candidates else None
                if candidate and candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if getattr(part, 'text', None):
                            text_output += ("\n" if text_output else "") + part.text
                        
                        inline = getattr(part, 'inline_data', None) or getattr(part, 'inlineData', None)
                        if inline and getattr(inline, 'data', None):
                            raw_bytes = inline.data
                            image_data = base64.b64encode(raw_bytes).decode('utf-8') if isinstance(raw_bytes, bytes) else str(raw_bytes)
                            mime_type = getattr(inline, 'mime_type', None) or getattr(inline, 'mimeType', None) or "image/jpeg"
                
                if image_data:
                    response_payload = {
                        "type": "image",
                        "data": image_data,
                        "mimeType": mime_type,
                        "credits": current_balance,
                        "text": text_output if text_output else f"🎂 Макет для печати готов! (-{required_credits} кр.)"
                    }
                else:
                    response_payload = {
                        "type": "text",
                        "credits": current_balance,
                        "text": text_output if text_output else "Не удалось сгенерировать макет..."
                    }
                
                await websocket.send_text(json.dumps(response_payload))

            except Exception as e:
                err_str = str(e)
                print(f"❌ Ошибка генерации: {err_str}")
                await websocket.send_text(json.dumps({"type": "text", "credits": current_balance, "text": f"Ошибка: {err_str}"}))
            
    except WebSocketDisconnect:
        print(f"Кондитер отключился.")

# Авторизация и ЮKassa
@app.post("/api/auth")
async def authenticate_user(request: Request):
    if not GOOGLE_SCRIPT_URL: return {"success": True}
    try:
        data = await request.json()
        response = requests.post(GOOGLE_SCRIPT_URL, json={"action": "auth", "email": data.get("email"), "password": data.get("password")}, timeout=25)
        return response.json() if response.status_code == 200 else {"success": False}
    except Exception as e: return {"success": False, "error": str(e)}

@app.post("/api/create-payment")
async def create_payment(request: Request):
    try:
        data = await request.json()
        email, amount, credits = data.get("email"), data.get("amount"), data.get("credits")
        if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY: return {"success": True, "confirmation_url": "https://yookassa.ru"}

        url = "https://api.yookassa.ru/v3/payments"
        payload = {
            "amount": {"value": f"{float(amount):.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": "https://babayagaland.ru/cakes"},
            "description": f"Покупка {credits} кредитов для макетов ({email})",
            "metadata": {"email": email, "credits": int(credits)}
        }
        response = requests.post(url, json=payload, auth=HTTPBasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY), headers={"Idempotence-Key": str(uuid.uuid4())}, timeout=10)
        return {"success": True, "confirmation_url": response.json().get("confirmation", {}).get("confirmation_url")}
    except Exception as e: return {"success": False, "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_cake:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))