# Запасной вход: если Railway запускает "uvicorn main:app", подхватится актуальный main_cake.py
from main_cake import app  # noqa: F401
