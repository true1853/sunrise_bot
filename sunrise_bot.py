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

from config import BOT_TOKEN

#############################################
# Глобальные переменные и настройки
#############################################

global_location = None
subscribed_chats = {}
notified_events_global = {}
DATABASE_NAME = "global_settings.db"
REMINDER_OFFSET = 10  # базовое смещение – используется для уведомлений, далее будут 10, 30 и 60 мин

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
# Обработчики команд
#############################################

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет! 😀\n\n"
        "Команды:\n"
        "📍 /setlocation – установить локацию\n"
        "⏰ /times – время восхода/заката на сегодня и завтра (напоминания)\n"
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
    observer = Observer(latitude=lat, longitude=lon)
    try:
        # Получаем данные на сегодня
        s_today = sun(observer, date=now.date(), tzinfo=tz)
        # Получаем данные на завтра
        tomorrow_date = now.date() + timedelta(days=1)
        s_tomorrow = sun(observer, date=tomorrow_date, tzinfo=tz)
    except Exception as e:
        logging.exception("Ошибка расчёта")
        await update.message.reply_text("Ошибка расчёта времени ❌")
        return

    sunrise_today = s_today["sunrise"].strftime("%H:%M:%S")
    sunset_today = s_today["sunset"].strftime("%H:%M:%S")
    sunrise_tomorrow = s_tomorrow["sunrise"].strftime("%H:%M:%S")
    sunset_tomorrow = s_tomorrow["sunset"].strftime("%H:%M:%S")

    date_today_str = now.strftime("%Y-%m-%d")
    date_tomorrow_str = tomorrow_date.strftime("%Y-%m-%d")

    chat_id = update.effective_chat.id
    user = update.effective_user
    if chat_id not in subscribed_chats:
        subscribed_chats[chat_id] = {}
    subscribed_chats[chat_id][user.id] = user.first_name

    text = (f"— Сегодня ({date_today_str}) —\n"
            f"🌅 Восход: {sunrise_today}\n"
            f"🌇 Закат: {sunset_today}\n\n"
            f"— Завтра ({date_tomorrow_str}) —\n"
            f"🌅 Восход: {sunrise_tomorrow}\n"
            f"🌇 Закат: {sunset_tomorrow}")
    await update.message.reply_text(text)

#############################################
# Хелпер для отправки уведомлений
#############################################

async def send_notification(chat_id, msg, key):
    try:
        await application.bot.send_message(chat_id, msg, parse_mode="HTML")
        notified_events_global[key] = True
    except Exception as e:
        logging.exception("Ошибка уведомления в чате %s, пробую отправить без HTML", chat_id)
        try:
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
        observer = Observer(latitude=lat, longitude=lon)
        try:
            s = sun(observer, date=now.date(), tzinfo=tz)
        except Exception as e:
            logging.exception("Ошибка расчёта для уведомлений")
            return

        sunrise_dt = s["sunrise"]
        sunset_dt = s["sunset"]
        date_str = now.strftime("%Y-%m-%d")
        offsets = [10, 30, 60]  # уведомления за 10, 30 и 60 минут до события

        for chat_id, subs in subscribed_chats.items():
            mentions = " ".join([f"<a href='tg://user?id={uid}'>{name}</a>" for uid, name in subs.items()])
            for offset in offsets:
                # Уведомление для восхода
                sunrise_notif = sunrise_dt - timedelta(minutes=offset)
                key_sr = (chat_id, now.date(), "sunrise", offset)
                if key_sr not in notified_events_global:
                    if now >= sunrise_notif and now < sunrise_notif + timedelta(seconds=60):
                        msg = f"📅 {date_str}\n⏰ {offset} мин до восхода 🌅 {mentions}"
                        await send_notification(chat_id, msg, key_sr)
                # Уведомление для заката
                sunset_notif = sunset_dt - timedelta(minutes=offset)
                key_ss = (chat_id, now.date(), "sunset", offset)
                if key_ss not in notified_events_global:
                    if now >= sunset_notif and now < sunset_notif + timedelta(seconds=60):
                        msg = f"📅 {date_str}\n⏰ {offset} мин до заката 🌇 {mentions}"
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
        BotCommand("times", "⏰ Время (с данными на сегодня и завтра)"),
        BotCommand("test", "🧪 Тест уведомлений")
    ]
    await app.bot.set_my_commands(cmds)
    logging.info("Команды установлены.")

#############################################
# Команда тестирования уведомлений (/test)
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
    observer = Observer(latitude=lat, longitude=lon)
    try:
        s = sun(observer, date=now.date(), tzinfo=tz)
    except Exception as e:
        logging.exception("Ошибка расчёта для теста")
        await update.message.reply_text("Ошибка расчёта времени для теста ❌")
        return

    sunrise = s["sunrise"].strftime("%H:%M:%S")
    sunset = s["sunset"].strftime("%H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")
    test_msg_sr = f"[TEST] 📅 {date_str}\n⏰ 10 мин до восхода 🌅 (тестовое уведомление)"
    test_msg_ss = f"[TEST] 📅 {date_str}\n⏰ 10 мин до заката 🌇 (тестовое уведомление)"
    await update.message.reply_text(f"Тест: время восхода {sunrise}, время заката {sunset}")
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
