from datetime import datetime, timedelta
import gradio as gr
from huggingface_hub import InferenceClient
import os
import json
import logging
from symptom_classifier import define_specialist
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Конфигурация Google Calendar ---
SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_ID = os.environ.get('GOOGLE_CALENDAR_ID')

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
        return """Вы — система записи к врачу. Строго следуйте инструкциям из prompt-doctor.txt"""

SYSTEM_PROMPT = load_system_prompt()
client = InferenceClient(token=os.environ.get("HF_TOKEN"))

def format_prompt(message: str, history: list) -> str:
    prompt = f"{SYSTEM_PROMPT}\n\n"
    for user, assistant in history:
        prompt += f"Пользователь: {user}\nПомощник: {assistant}\n"
    prompt += f"Пользователь: {message}\nПомощник: "
    return prompt

def schedule_appointment(name: str, symptoms: str) -> str:
    try:
        specialist = define_specialist(symptoms)
        appointment_time = datetime.now() + timedelta(hours=3)
        
        service = get_calendar_service()
        event = {
            'summary': f'Прием: {name}, {specialist}',
            'description': f'Симптомы: {symptoms}. Требует подтверждения.',
            'start': {
                'dateTime': appointment_time.isoformat(),
                'timeZone': 'Europe/Moscow'
            },
            'end': {
                'dateTime': (appointment_time + timedelta(minutes=30)).isoformat(),
                'timeZone': 'Europe/Moscow'
            }
        }
        
        service.events().insert(
            calendarId=CALENDAR_ID,
            body=event
        ).execute()
        
        return f"Запись к {specialist} на {appointment_time.strftime('%d.%m.%Y %H:%M')} оформлена."
    except Exception as e:
        logger.error(f"Ошибка записи: {str(e)}")
        return "Ошибка при создании записи. Пожалуйста, попробуйте позже."

def generate_response(message: str, history: list):
    try:
        if "симптом" in message.lower() or "запись" in message.lower():
            if not hasattr(generate_response, "step"):
                generate_response.step = 0
            
            if generate_response.step == 0:
                generate_response.step = 1
                return "Пожалуйста, укажите ваше полное имя и опишите симптомы."
            
            if generate_response.step == 1:
                name, *symptoms = message.split(' ', 1)
                symptoms = symptoms[0] if symptoms else ""
                response = schedule_appointment(name.strip(), symptoms.strip())
                generate_response.step = 0
                return response
        
        prompt = format_prompt(message, history)
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
        yield "Произошла ошибка. Пожалуйста, попробуйте еще раз."

custom_theme = gr.themes.Default(
    primary_hue="blue",
    secondary_hue="teal",
    font=[gr.themes.GoogleFont("Open Sans")]
)

with gr.Blocks(theme=custom_theme) as app:
    gr.Markdown("# Doctor AI 🤖")
    gr.Markdown("Автоматизированная система записи к врачу")
    
    gr.ChatInterface(
        fn=generate_response,
        examples=[
            ["Записаться к врачу", "Пожалуйста, укажите ваше полное имя и опишите симптомы."],
            ["Иван Петров. Болит горло и температура", "Рекомендуем обратиться к терапевту..."]
        ]
    )

if __name__ == "__main__":
    app.launch(server_name="0.0.0.0", server_port=7860)
