# main.py
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Set
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = "ВАШ_ТОКЕН_ТЕЛЕГРАМ_БОТА"
SHEETS_CREDENTIALS_PATH = "credentials.json"
SHEET_ID = "ID_ВАШЕГО_GOOGLE_SHEETS"  # Из URL sheets.google.com/spreadsheets/d/ID_ВАШЕГО/
SHEET_NAME = "Лист1"

CALENDAR_ID = "primary"  # или ID вашего календаря
CORPORATE_CHANNEL_ID = "@erkafarm_channel"  # ID канала или @username
TIMEZONE = pytz.timezone('Europe/Moscow')

# Слоты презентаций (среда 15:00-16:00)
SLOTS = ["15:00-15:10", "15:10-15:20", "15:20-15:30", "15:30-15:40", "15:40-15:50", "15:50-16:00"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class SignupStates(StatesGroup):
    waiting_slots = State()

class GoogleSheets:
    def __init__(self):
        self.service = self._get_sheets_service()
        self.spreadsheet_id = SHEET_ID
    
    def _get_sheets_service(self):
        creds = Credentials.from_service_account_file(
            SHEETS_CREDENTIALS_PATH,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        return build('sheets', 'v4', credentials=creds)
    
    async def add_signup(self, date: str, name: str, username: str, slots: List[str]):
        """Добавить запись в Google Sheets"""
        values = [[date, name, username, ", ".join(slots), "", "Запланировано"]]
        body = {'values': values}
        range_name = f'{SHEET_NAME}!A:F'
        
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
    
    async def get_upcoming_signups(self, days_ahead: int = 1) -> List[Dict]:
        """Получить заявки на завтра"""
        tomorrow = (datetime.now(TIMEZONE) + timedelta(days=days_ahead)).strftime('%Y-%m-%d')
        
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f'{SHEET_NAME}!A:F'
        ).execute()
        
        rows = result.get('values', [])
        if not rows:
            return []
        
        signups = []
        for row in rows[1:]:  # Пропускаем заголовки
            if len(row) >= 4 and row[0] == tomorrow:
                signups.append({
                    'name': row[1],
                    'username': row[2],
                    'slots': row[3].split(', '),
                    'user_id': None  # Telegram ID нужно хранить отдельно
                })
        return signups

class GoogleCalendar:
    def __init__(self):
        self.service = self._get_calendar_service()
    
    def _get_calendar_service(self):
        creds = Credentials.from_service_account_file(
            SHEETS_CREDENTIALS_PATH,
            scopes=['https://www.googleapis.com/auth/calendar']
        )
        return build('calendar', 'v3', credentials=creds)
    
    async def create_event(self, date_str: str, speakers: List[Dict]):
        """Создать событие в календаре"""
        date = datetime.strptime(date_str, '%Y-%m-%d')
        start_time = datetime(date.year, date.month, date.day, 15, 0, tzinfo=TIMEZONE)
        end_time = datetime(date.year, date.month, date.day, 16, 0, tzinfo=TIMEZONE)
        
        attendees = [f"Докладчики: {s['name']} ({s['username']})" for s in speakers]
        description = "\n".join(attendees)
        
        event = {
            'summary': f'Презентации сотрудников ({date_str})',
            'description': description,
            'start': {'dateTime': start_time.isoformat(), 'timeZone': 'Europe/Moscow'},
            'end': {'dateTime': end_time.isoformat(), 'timeZone': 'Europe/Moscow'},
        }
        
        self.service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

# Инициализация сервисов
sheets = GoogleSheets()
calendar = GoogleCalendar()

user_selections: Dict[int, Set[str]] = {}

@dp.message(Command("start", "signup"))
async def cmd_start(message: Message, state: FSMContext):
    """Старт записи на презентацию"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    row = []
    for i, slot in enumerate(SLOTS):
        row.append(InlineKeyboardButton(
            f"⏰ {slot}", 
            callback_data=f"slot_{slot}"
        ))
        if (i + 1) % 2 == 0:
            keyboard.inline_keyboard.append(row)
            row = []
    if row:
        keyboard.inline_keyboard.append(row)
    
    keyboard.inline_keyboard.append([
        InlineKeyboardButton("✅ Подтвердить выбор", callback_data="confirm_slots")
    ])
    
    await message.answer(
        "📅 **Запись на презентацию**\n"
        "🗓 *Среда 15:00-16:00*\n\n"
        "Выберите слоты (можно несколько):\n"
        "_Нажмите на слоты, затем 'Подтвердить'_",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(SignupStates.waiting_slots)

@dp.callback_query(F.data.startswith("slot_"))
async def select_slot(callback: CallbackQuery, state: FSMContext):
    """Выбор слота"""
    slot = callback.data.split("_", 1)[1]
    user_id = callback.from_user.id
    
    if user_id not in user_selections:
        user_selections[user_id] = set()
    
    if slot in user_selections[user_id]:
        user_selections[user_id].remove(slot)
        text = f"❌ Убрали слот {slot}"
    else:
        user_selections[user_id].add(slot)
        text = f"✅ Добавили слот {slot}"
    
    selected = ", ".join(sorted(user_selections[user_id])) or "не выбрано"
    await callback.answer(text, show_alert=True)
    
    # Обновляем клавиатуру
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    row = []
    for s in SLOTS:
        status = "✅" if s in user_selections[user_id] else "⏰"
        row.append(InlineKeyboardButton(
            f"{status} {s}", 
            callback_data=f"slot_{s}"
        ))
        if len(row) == 2:
            keyboard.inline_keyboard.append(row)
            row = []
    if row:
        keyboard.inline_keyboard.append(row)
    
    keyboard.inline_keyboard.append([
        InlineKeyboardButton("✅ Подтвердить выбор", callback_data="confirm_slots")
    ])
    
    await callback.message.edit_reply_markup(reply_markup=keyboard)

@dp.callback_query(F.data == "confirm_slots")
async def confirm_slots(callback: CallbackQuery, state: FSMContext):
    """Подтверждение слотов"""
    user_id = callback.from_user.id
    slots = user_selections.get(user_id, set())
    
    if not slots:
        await callback.answer("Выберите хотя бы один слот!", show_alert=True)
        return
    
    # Найти ближайшую среду
    today = datetime.now(TIMEZONE)
    days_ahead = 3 - today.weekday()  # Среда = 2
    if days_ahead <= 0:
        days_ahead += 7
    
    presentation_date = (today + timedelta(days=days_ahead)).strftime('%Y-%m-%d')
    
    name = callback.from_user.full_name
    username = callback.from_user.username or "без ника"
    
    try:
        # Сохраняем в Google Sheets
        await sheets.add_signup(presentation_date, name, username, list(slots))
        
        # Создаем событие в календаре
        # await calendar.create_event(presentation_date, [{"name": name, "username": username}])
        
        del user_selections[user_id]
        
        await callback.message.edit_text(
            f"🎉 **Запись подтверждена!**\n\n"
            f"📅 *{presentation_date}*\n"
            f"👤 {name} (@{username})\n"
            f"⏰ Слоты: {', '.join(sorted(slots))}\n\n"
            f"📋 Данные сохранены в Google Sheets\n"
            f"🔔 Напоминание придет за день до презентации",
            parse_mode="Markdown"
        )
        await callback.answer("Запись сохранена!")
        
    except Exception as e:
        logger.error(f"Error saving signup: {e}")
        await callback.answer("Ошибка сохранения. Попробуйте позже.", show_alert=True)

async def send_daily_reminders():
    """Ежедневный таск: напоминания за день до презентации (отправляется во вторник вечером)"""
    try:
        signups = await sheets.get_upcoming_signups(days_ahead=1)
        
        if not signups:
            return
        
        # Формируем сообщение для канала
        message = "📢 **Расписание презентаций на завтра**\n\n"
        for signup in signups:
            slots_text = " | ".join(signup['slots'])
            message += f"👤 {signup['name']} (@{signup['username']})\n⏰ {slots_text}\n\n"
        
        await bot.send_message(CORPORATE_CHANNEL_ID, message, parse_mode="Markdown")
        
        logger.info(f"Sent schedule to channel: {len(signups)} signups")
        
    except Exception as e:
        logger.error(f"Error in daily reminders: {e}")

async def main():
    """Запуск бота"""
    # Планировщик напоминаний (каждый вторник в 19:00)
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        send_daily_reminders,
        CronTrigger(day_of_week="tue", hour=19, minute=0),
        id="daily_reminders"
    )
    scheduler.start()
    
    logger.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
