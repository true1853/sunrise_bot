#!/usr/bin/env python3
import logging
import asyncio
import sqlite3
from datetime import datetime, timedelta, date
import pytz
from telegram import (
    Update, 
    BotCommand, 
    KeyboardButton, 
    ReplyKeyboardMarkup, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astral import Observer
from astral.sun import sun
from timezonefinder import TimezoneFinder

#############################################
# Глобальные переменные и настройки
#############################################

# Глобальная локация для всего приложения (формат: {"lat": float, "lon": float, "tz": str})
global_location = None

# Подписанные чаты – словарь: { chat_id: {user_id: first_name, ...} }
subscribed_chats = {}

# Словарь для отслеживания отправленных уведомлений: ключ (chat_id, дата, тип_события)
notified_events_global = {}

# Имя файла для хранения глобальных настроек (только локации)
DATABASE_NAME = "global_settings.db"

#############################################
# Функции работы с базой данных
#############################################

def init_db():
    """
    Инициализирует базу данных:
      – создаёт таблицу для глобальных настроек, если её нет;
      – пытается загрузить ранее сохранённую глобальную локацию.
    """
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS global_settings (
            id INTEGER PRIMARY KEY,
            lat REAL,
            lon REAL,
            tz TEXT
        )
    ''')
    conn.commit()
    cursor.execute("SELECT lat, lon, tz FROM global_settings WHERE id = 1")
    row = cursor.fetchone()
    if row:
        global global_location
        global_location = {"lat": row[0], "lon": row[1], "tz": row[2]}
        logging.info("Глобальная локация загружена из БД: %s", global_location)
    conn.close()

def save_global_location(lat: float, lon: float, tz: str):
    """Сохраняет глобальную локацию в базу данных."""
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM global_settings WHERE id = 1")
    if cursor.fetchone():
        cursor.execute("UPDATE global_settings SET lat = ?, lon = ?, tz = ? WHERE id = 1", (lat, lon, tz))
    else:
        cursor.execute("INSERT INTO global_settings (id, lat, lon, tz) VALUES (1, ?, ?, ?)", (lat, lon, tz))
    conn.commit()
    conn.close()

#############################################
# Настройка логирования
#############################################

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

#############################################
# Обработчики команд бота
#############################################

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветственное сообщение и список доступных команд."""
    await update.message.reply_text(
        "Привет! 😀\n"
        "Команды:\n"
        "/setlocation – установить глобальную локацию 📍\n"
        "/times – время рассвета/заката 🌅"
    )

async def setlocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Устанавливает глобальную локацию:
      – Если команда вызвана в группе, отправляет inline‑кнопку для перехода в ЛС.
      – В ЛС отправляет кнопку для запроса геолокации.
    """
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        me = await context.bot.get_me()
        bot_username = me.username
        url = f"https://t.me/{bot_username}?start=setlocation"
        inline_keyboard = InlineKeyboardMarkup.from_button(
            InlineKeyboardButton("Перейти в ЛС", url=url)
        )
        await update.message.reply_text(
            "Для установки глобальной локации напишите мне в ЛС 👤", 
            reply_markup=inline_keyboard
        )
        return

    # В ЛС устанавливаем флаг, чтобы обновить глобальную локацию при получении геолокации
    context.user_data["awaiting_global_location"] = True
    button = KeyboardButton("📍 Отправить локацию", request_location=True)
    keyboard = ReplyKeyboardMarkup([[button]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Нажмите кнопку для отправки вашей локации:", reply_markup=keyboard)

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает сообщение с локацией.
    Если получено в контексте установки глобальной локации, обновляет её и сохраняет в БД.
    """
    if update.message.location:
        if context.user_data.get("awaiting_global_location"):
            lat = update.message.location.latitude
            lon = update.message.location.longitude
            tf = TimezoneFinder()
            tz_str = tf.timezone_at(lng=lon, lat=lat) or "UTC"
            global global_location
            global_location = {"lat": lat, "lon": lon, "tz": tz_str}
            save_global_location(lat, lon, tz_str)
            context.user_data["awaiting_global_location"] = False
            await update.message.reply_text(f"Глобальная локация установлена: {lat}, {lon} (tz: {tz_str}) ✅")
        else:
            await update.message.reply_text("Локация получена, но не используется для установки глобальной локации.")

async def times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Вычисляет и выводит время рассвета и заката по глобальной локации с датой.
    Подписывает текущий чат на уведомления и сохраняет информацию о пользователе для упоминания.
    """
    if not global_location:
        await update.message.reply_text("Глобальная локация не установлена. Используйте /setlocation")
        return

    lat = global_location['lat']
    lon = global_location['lon']
    tz_str = global_location['tz']
    tz = pytz.timezone(tz_str)
    now = datetime.now(tz)
    observer = Observer(latitude=lat, longitude=lon)
    try:
        s = sun(observer, date=now.date(), tzinfo=tz)
    except Exception as e:
        logging.exception("Ошибка расчёта времени")
        await update.message.reply_text("Ошибка расчёта времени ❌")
        return

    sunrise = s["sunrise"].strftime("%H:%M:%S")
    sunset = s["sunset"].strftime("%H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")
    chat_id = update.effective_chat.id
    user = update.effective_user
    if chat_id not in subscribed_chats:
        subscribed_chats[chat_id] = {}
    subscribed_chats[chat_id][user.id] = user.first_name

    await update.message.reply_text(f"Дата: {date_str}\nРассвет 🌅: {sunrise}\nЗакат 🌇: {sunset}")

#############################################
# Планировщик уведомлений
#############################################

async def check_notifications():
    """
    Каждые 30 секунд проверяет, наступило ли время для отправки уведомлений
    (за 10 минут до рассвета/заката) и рассылает их по подписанным чатам.
    В уведомлении добавляется дата и упоминаются подписанные пользователи.
    """
    if not global_location:
        return
    lat = global_location['lat']
    lon = global_location['lon']
    tz_str = global_location['tz']
    tz = pytz.timezone(tz_str)
    now = datetime.now(tz)
    observer = Observer(latitude=lat, longitude=lon)
    try:
        s = sun(observer, date=now.date(), tzinfo=tz)
    except Exception as e:
        logging.exception("Ошибка расчёта времени для уведомлений")
        return

    sunrise_notification = s["sunrise"] - timedelta(minutes=10)
    sunset_notification = s["sunset"] - timedelta(minutes=10)
    date_str = now.strftime("%Y-%m-%d")

    for chat_id, subscribers in subscribed_chats.items():
        # Формируем строку упоминаний (HTML)
        mention_text = " ".join([f"<a href='tg://user?id={uid}'>{name}</a>" for uid, name in subscribers.items()]) if subscribers else ""
        key_sunrise = (chat_id, now.date(), "sunrise")
        if key_sunrise not in notified_events_global:
            if abs((now - sunrise_notification).total_seconds()) < 30:
                try:
                    msg_text = f"Дата: {date_str}\n10 мин до рассвета 🌅 {mention_text}"
                    await application.bot.send_message(chat_id, msg_text, parse_mode="HTML")
                    notified_events_global[key_sunrise] = True
                except Exception as e:
                    logging.exception("Ошибка уведомления рассвета для чата %s", chat_id)
        key_sunset = (chat_id, now.date(), "sunset")
        if key_sunset not in notified_events_global:
            if abs((now - sunset_notification).total_seconds()) < 30:
                try:
                    msg_text = f"Дата: {date_str}\n10 мин до заката 🌇 {mention_text}"
                    await application.bot.send_message(chat_id, msg_text, parse_mode="HTML")
                    notified_events_global[key_sunset] = True
                except Exception as e:
                    logging.exception("Ошибка уведомления заката для чата %s", chat_id)

def clear_notified_events():
    """Очищает записи об отправленных уведомлениях для предыдущих дней."""
    today = date.today()
    keys_to_remove = [k for k in notified_events_global if k[1] != today]
    for k in keys_to_remove:
        del notified_events_global[k]

async def start_scheduler():
    """Запускает APScheduler для периодической проверки уведомлений."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(check_notifications()), 'interval', seconds=30)
    scheduler.add_job(clear_notified_events, 'cron', hour=0, minute=1)
    scheduler.start()
    logging.info("Scheduler started.")

#############################################
# Установка меню команд бота
#############################################

async def set_bot_commands(app: Application) -> None:
    commands = [
        BotCommand("start", "Начать работу 😀"),
        BotCommand("setlocation", "Установить глобальную локацию 📍"),
        BotCommand("times", "Время рассвета/заката 🌅")
    ]
    await app.bot.set_my_commands(commands)
    logging.info("Bot commands set.")

#############################################
# Основная функция
#############################################

def main():
    global application
    init_db()
    application = Application.builder().token("7778834899:AAEs7eazNIyXw71cQ79nFUDj81gx9MnTfig").build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlocation", setlocation))
    application.add_handler(CommandHandler("times", times))
    application.add_handler(MessageHandler(filters.LOCATION, location_handler))
    loop = asyncio.get_event_loop()
    loop.create_task(start_scheduler())
    loop.create_task(set_bot_commands(application))
    application.run_polling()

if __name__ == '__main__':
    main()
