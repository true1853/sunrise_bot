#!/usr/bin/env python3
import logging
import asyncio
import sqlite3
import math
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
# Удалён импорт из astral (он больше не используется для расчёта)
#from astral import Observer
#from astral.sun import sun
from timezonefinder import TimezoneFinder

# Импорт токена из файла config.py
from config import BOT_TOKEN

#############################################
# Глобальные переменные и настройки
#############################################

# Глобальная локация для всего приложения (формат: {"lat": float, "lon": float, "tz": str})
global_location = None

# Подписанные чаты – словарь: { chat_id: {user_id: first_name, ...} }
subscribed_chats = {}

# Словарь для отслеживания отправленных уведомлений: ключ (chat_id, дата, тип_события)
notified_events_global = {}

DATABASE_NAME = "global_settings.db"
REMINDER_OFFSET = 10

#############################################
# Функции работы с базой данных
#############################################

def init_db():
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
        logging.info("Локация загружена: %s", global_location)
    conn.close()

def save_global_location(lat: float, lon: float, tz: str):
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
# Новый блок: Расчёт времени рассвета/заката по уравнению восхода
#############################################

def calculate_sun_times(cur_date: date, lat: float, lon: float, tzinfo):
    """
    Вычисляет время рассвета и заката для указанной даты и координат по классическому уравнению.
    Поправка: угол -0.83° учитывает атмосферную рефракцию и солнечный диск.
    """
    def deg2rad(d): 
        return math.radians(d)
    def rad2deg(r): 
        return math.degrees(r)
    
    # Вычисление Юлианской даты для 0 UTC данного дня
    year, month, day = cur_date.year, cur_date.month, cur_date.day
    if month <= 2:
        year -= 1
        month += 12
    A = year // 100
    B = 2 - A + (A // 4)
    J0 = int(365.25*(year+4716)) + int(30.6001*(month+1)) + day + B - 1524.5

    # Смещённое число дней
    n = J0 - 2451545.0 + 0.0008
    # Приблизительное время солнечного полудня (J*)
    J_star = n - (lon / 360)
    # Средняя аномалия Солнца
    M = (357.5291 + 0.98560028 * J_star) % 360
    M_rad = deg2rad(M)
    # Центрическая коррекция
    C = 1.9148 * math.sin(M_rad) + 0.0200 * math.sin(2 * M_rad) + 0.0003 * math.sin(3 * M_rad)
    # Экллиптическая долгота Солнца
    lambda_sun = (M + C + 180 + 102.9372) % 360
    lambda_rad = deg2rad(lambda_sun)
    # Точное время прохождения полудня
    J_transit = 2451545.0 + J_star + 0.0053 * math.sin(M_rad) - 0.0069 * math.sin(2 * lambda_rad)
    # Склонение Солнца
    delta = math.asin(math.sin(lambda_rad) * math.sin(deg2rad(23.44)))
    # Часовой угол (учитываем угол -0.83°)
    h0 = deg2rad(-0.83)
    lat_rad = deg2rad(lat)
    cos_omega = (math.sin(h0) - math.sin(lat_rad)*math.sin(delta)) / (math.cos(lat_rad)*math.cos(delta))
    cos_omega = max(min(cos_omega, 1), -1)
    omega = math.acos(cos_omega)
    omega_deg = rad2deg(omega)
    # Юлианские даты восхода и заката
    J_rise = J_transit - omega_deg/360.0
    J_set = J_transit + omega_deg/360.0

    def julian_to_datetime(j):
        timestamp = (j - 2440587.5) * 86400.0
        return datetime.utcfromtimestamp(timestamp).replace(tzinfo=pytz.utc)
    
    sunrise_utc = julian_to_datetime(J_rise)
    sunset_utc = julian_to_datetime(J_set)
    sunrise_local = sunrise_utc.astimezone(tzinfo)
    sunset_local = sunset_utc.astimezone(tzinfo)
    return sunrise_local, sunset_local

#############################################
# Обработчики команд
#############################################

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! 😀\n\n"
        "Команды:\n"
        "📍 /setlocation – установить локацию\n"
        f"⏰ /times – время рассвета/заката (напоминание за {REMINDER_OFFSET} мин)\n"
        "🧪 /test – тест уведомлений"
    )
    await update.message.reply_text(text)

async def setlocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        me = await context.bot.get_me()
        bot_username = me.username
        url = f"https://t.me/{bot_username}?start=setlocation"
        inline_kb = InlineKeyboardMarkup.from_button(
            InlineKeyboardButton("👤 В ЛС", url=url)
        )
        await update.message.reply_text("Напиши в ЛС для установки локации.", reply_markup=inline_kb)
        return

    button = KeyboardButton("📍 Отправить локацию", request_location=True)
    kb = ReplyKeyboardMarkup([[button]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Отправь свою локацию:", reply_markup=kb)

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude
        tf = TimezoneFinder()
        tz_str = tf.timezone_at(lng=lon, lat=lat) or "UTC"
        global global_location
        global_location = {"lat": lat, "lon": lon, "tz": tz_str}
        save_global_location(lat, lon, tz_str)
        await update.message.reply_text(f"Локация: {lat}, {lon} (tz: {tz_str}) ✅")
    else:
        await update.message.reply_text("❌ Локация не получена.")

async def times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not global_location:
        await update.message.reply_text("Локация не установлена. Используй /setlocation")
        return

    lat = global_location['lat']
    lon = global_location['lon']
    tz_str = global_location['tz']
    tz = pytz.timezone(tz_str)
    now = datetime.now(tz)
    try:
        # Используем нашу функцию для расчёта рассвета/заката
        sunrise_dt, sunset_dt = calculate_sun_times(now.date(), lat, lon, tz)
    except Exception as e:
        logging.exception("Ошибка расчёта")
        await update.message.reply_text("Ошибка расчёта времени ❌")
        return

    sunrise = sunrise_dt.strftime("%H:%M:%S")
    sunset = sunset_dt.strftime("%H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")
    chat_id = update.effective_chat.id
    user = update.effective_user
    if chat_id not in subscribed_chats:
        subscribed_chats[chat_id] = {}
    subscribed_chats[chat_id][user.id] = user.first_name

    text = f"📅 {date_str}\n🌅 {sunrise}\n🌇 {sunset}"
    await update.message.reply_text(text)

#############################################
# Новый хелпер: Отправка уведомлений с обработкой ошибок в группах
#############################################

async def send_notification(chat_id, msg, key):
    try:
        await application.bot.send_message(chat_id, msg, parse_mode="HTML")
        notified_events_global[key] = True
    except Exception as e:
        logging.exception("Ошибка уведомления в чате %s, пробую отправить без HTML", chat_id)
        try:
            # Убираем HTML‑теги для групп
            plain_msg = msg.replace("<a href='tg://user?id=", "").replace("'>", " ").replace("</a>", "")
            await application.bot.send_message(chat_id, plain_msg)
            notified_events_global[key] = True
        except Exception as e2:
            logging.exception("Не удалось отправить уведомление в чате %s", chat_id)

#############################################
# Механизм уведомлений
#############################################

async def job_wrapper():
    try:
        await check_notifications()
    except Exception as e:
        logging.exception("Unhandled exception in job_wrapper: %s", e)

async def check_notifications():
    try:
        if not global_location:
            return
        lat = global_location['lat']
        lon = global_location['lon']
        tz_str = global_location['tz']
        tz = pytz.timezone(tz_str)
        now = datetime.now(tz)
        try:
            # Используем нашу функцию расчёта
            sunrise_dt, sunset_dt = calculate_sun_times(now.date(), lat, lon, tz)
        except Exception as e:
            logging.exception("Ошибка расчёта для уведомлений")
            return

        sunrise_notif = sunrise_dt - timedelta(minutes=REMINDER_OFFSET)
        sunset_notif = sunset_dt - timedelta(minutes=REMINDER_OFFSET)
        date_str = now.strftime("%Y-%m-%d")

        for chat_id, subs in subscribed_chats.items():
            mentions = " ".join([f"<a href='tg://user?id={uid}'>{name}</a>" for uid, name in subs.items()])
            key_sr = (chat_id, now.date(), "sunrise")
            if key_sr not in notified_events_global:
                if abs((now - sunrise_notif).total_seconds()) < 30:
                    msg = f"📅 {date_str}\n⏰ 10 мин до рассвета 🌅 {mentions}"
                    await send_notification(chat_id, msg, key_sr)
            key_ss = (chat_id, now.date(), "sunset")
            if key_ss not in notified_events_global:
                if abs((now - sunset_notif).total_seconds()) < 30:
                    msg = f"📅 {date_str}\n⏰ 10 мин до заката 🌇 {mentions}"
                    await send_notification(chat_id, msg, key_ss)
    except Exception as e:
        logging.exception("Unhandled exception in check_notifications: %s", e)

def clear_notified_events():
    today = date.today()
    keys = [k for k in notified_events_global if k[1] != today]
    for k in keys:
        del notified_events_global[k]

async def start_scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(job_wrapper()), 'interval', seconds=30)
    scheduler.add_job(clear_notified_events, 'cron', hour=0, minute=1)
    scheduler.start()
    logging.info("Scheduler запущен.")

async def set_bot_commands(app: Application) -> None:
    cmds = [
        BotCommand("start", "Начало 😀"),
        BotCommand("setlocation", "📍 Локация"),
        BotCommand("times", f"⏰ Время (напоминание за {REMINDER_OFFSET} мин)"),
        BotCommand("test", "🧪 Тест уведомлений")
    ]
    await app.bot.set_my_commands(cmds)
    logging.info("Команды установлены.")

#############################################
# Механизм тестирования (новая команда /test)
#############################################

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not global_location:
        await update.message.reply_text("Локация не установлена. Используй /setlocation")
        return

    lat = global_location['lat']
    lon = global_location['lon']
    tz_str = global_location['tz']
    tz = pytz.timezone(tz_str)
    now = datetime.now(tz)
    try:
        sunrise_dt, sunset_dt = calculate_sun_times(now.date(), lat, lon, tz)
    except Exception as e:
        logging.exception("Ошибка расчёта для теста")
        await update.message.reply_text("Ошибка расчёта времени для теста ❌")
        return

    sunrise = sunrise_dt.strftime("%H:%M:%S")
    sunset = sunset_dt.strftime("%H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")
    test_msg_sr = f"[TEST] 📅 {date_str}\n⏰ 10 мин до рассвета 🌅 (тестовое уведомление)"
    test_msg_ss = f"[TEST] 📅 {date_str}\n⏰ 10 мин до заката 🌇 (тестовое уведомление)"
    await update.message.reply_text(f"Тест: время рассвета {sunrise}, время заката {sunset}")
    await update.message.reply_text(test_msg_sr)
    await update.message.reply_text(test_msg_ss)

#############################################
# Основная функция
#############################################

def main():
    global application
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlocation", setlocation))
    application.add_handler(CommandHandler("times", times))
    application.add_handler(CommandHandler("test", test))
    application.add_handler(MessageHandler(filters.LOCATION, location_handler))
    loop = asyncio.get_event_loop()
    loop.create_task(start_scheduler())
    loop.create_task(set_bot_commands(application))
    application.run_polling()

if __name__ == '__main__':
    main()
