from datetime import datetime, timedelta
import gradio as gr
from huggingface_hub import InferenceClient
import os
import json
import logging
import requests
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_fixed
from symptom_classifier import define_specialist
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Конфигурация ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID')
WEBHOOK_URL = os.environ.get('CONFIRMATION_WEBHOOK_URL')
MAX_RETRIES = 3

# --- Инициализация сервисов ---
def get_calendar_service():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ['GOOGLE_CREDS']),
        scopes=SCOPES
    )
    return build('calendar', 'v3', credentials=creds)

def load_system_prompt():
    try:
        with open("prompt-doctor.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.error(f"Error loading prompt: {e}")
        return "Вы — система записи к врачу. Строго следуйте инструкциям."

SYSTEM_PROMPT = load_system_prompt()
client = InferenceClient(token=os.environ.get("HF_TOKEN"))

# --- Вспомогательные функции ---
@retry(stop=stop_after_attempt(MAX_RETRIES), wait=wait_fixed(2))
def send_webhook_confirmation(event_id: str):
    """Отправка вебхука для подтверждения записи"""
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL не настроен, пропускаем подтверждение")
        return
    
    payload = {
        "event_id": event_id,
        "status": "pending_confirmation"
    }
    
    try:
        response = requests.post(
            WEBHOOK_URL,
            json=payload,
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"Webhook отправлен успешно для события {event_id}")
    except Exception as e:
        logger.error(f"Ошибка вебхука: {str(e)}")
        raise

def check_duplicate_event(name: str, time: datetime) -> bool:
    """Проверка дублирующих записей"""
    try:
        service = get_calendar_service()
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time.isoformat(),
            timeMax=(time + timedelta(minutes=30)).isoformat(),
            q=f"Прием: {name}",
            maxResults=1
        ).execute()
        
        return len(events_result.get('items', [])) > 0
    except HttpError as e:
        logger.error(f"Ошибка проверки дубликатов: {str(e)}")
        return False

# --- Основная логика ---
def schedule_appointment(name: str, symptoms: str) -> Optional[str]:
    try:
        if check_duplicate_event(name, datetime.now()):
            return "У вас уже есть активная запись. Пожалуйста, дождитесь подтверждения."
        
        specialist = define_specialist(symptoms)
        appointment_time = datetime.now() + timedelta(hours=3)
        
        service = get_calendar_service()
        event = {
            'summary': f'Прием: {name}, {specialist}',
            'description': f'Симптомы: {symptoms}',
            'start': {'dateTime': appointment_time.isoformat(), 'timeZone': 'Europe/Moscow'},
            'end': {'dateTime': (appointment_time + timedelta(minutes=30)).isoformat(), 'timeZone': 'Europe/Moscow'},
            'extendedProperties': {'private': {'requiresConfirmation': 'true'}}
        }
        
        created_event = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event
        ).execute()
        
        # Отправка вебхука для подтверждения
        send_webhook_confirmation(created_event['id'])
        
        return f"Предварительная запись к {specialist} на {appointment_time.strftime('%d.%m.%Y %H:%M')} создана. Ожидайте подтверждения."
    
    except HttpError as e:
        logger.error(f"Google API error: {str(e)}")
        return "Ошибка подключения к системе записи. Попробуйте позже."
    except Exception as e:
        logger.error(f"Общая ошибка: {str(e)}")
        return None

# --- Интерфейс Gradio ---
def generate_response(message: str, history: list):
    try:
        if "запись" in message.lower() or "симптом" in message.lower():
            if not hasattr(generate_response, "step"):
                generate_response.step = 0
            
            if generate_response.step == 0:
                generate_response.step = 1
                return "Пожалуйста, укажите ваше полное имя и опишите симптомы (через запятую)."
            
            if generate_response.step == 1:
                generate_response.step = 0
                parts = message.split(',', 1)
                if len(parts) < 2:
                    return "Неверный формат. Пожалуйста, укажите имя и симптомы через запятую."
                
                name, symptoms = parts[0].strip(), parts[1].strip()
                response = schedule_appointment(name, symptoms)
                return response or "Извините, произошла ошибка обработки запроса."
        
        prompt = f"{SYSTEM_PROMPT}\n\nДиалог:\n{history}\nПользователь: {message}\nПомощник:"
        stream = client.text_generation(
            prompt,
            max_new_tokens=512,
            temperature=0.3,
            stream=True
        )
        
        full_response = ""
        for token in stream:
            full_response += token
            yield full_response
            
    except Exception as e:
        logger.error(f"Ошибка генерации: {str(e)}")
        yield "Произошла внутренняя ошибка. Пожалуйста, повторите запрос."

custom_theme = gr.themes.Default(
    primary_hue="blue",
    secondary_hue="teal",
    font=[gr.themes.GoogleFont("Open Sans")]
)

with gr.Blocks(theme=custom_theme) as app:
    gr.Markdown("# Doctor AI 🤖")
    gr.Markdown("Автоматизированная система записи к врачу с подтверждением")
    
    gr.ChatInterface(
        fn=generate_response,
        examples=[
            ["Нужна запись к врачу", "Пожалуйста, укажите ваше полное имя и опишите симптомы..."],
            ["Иван Сидоров, головная боль и температура", "Предварительная запись к терапевту..."]
        ]
    )

if __name__ == "__main__":
    app.launch(server_name="0.0.0.0", server_port=7860)
