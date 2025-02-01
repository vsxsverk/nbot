import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import sqlite3
from datetime import datetime, timedelta
from telegram_bot_calendar import DetailedTelegramCalendar, LSTEP

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Подключение к SQLite
conn = sqlite3.connect('notes.db', check_same_thread=False)
cursor = conn.cursor()

# Создание новой таблицы entries
cursor.execute('''CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    entry_text TEXT NOT NULL,
    datetime TEXT NOT NULL,
    type TEXT NOT NULL -- 'note' или 'appointment'
)''')
conn.commit()

# Миграция данных из notes (если таблица существует)
try:
    cursor.execute("""
        INSERT INTO entries (admin_id, entry_text, datetime, type)
        SELECT admin_id, note_text, datetime, 'note'
        FROM notes
    """)
    conn.commit()
except sqlite3.OperationalError:
    pass  # Если таблица notes не существует, игнорируем ошибку

# Миграция данных из appointments (если таблица существует)
try:
    cursor.execute("""
        INSERT INTO entries (admin_id, entry_text, datetime, type)
        SELECT user_id, description, appointment_datetime, 'appointment'
        FROM appointments
    """)
    conn.commit()
except sqlite3.OperationalError:
    pass  # Если таблица appointments не существует, игнорируем ошибку

# Удаление старых таблиц (если они больше не нужны)
cursor.execute("DROP TABLE IF EXISTS notes")
cursor.execute("DROP TABLE IF EXISTS appointments")
conn.commit()

# Планировщик
scheduler = BackgroundScheduler()
scheduler.start()

# Вспомогательная функция для получения записей
def fetch_entries(start_date=None, end_date=None, entry_type=None):
    query = """
        SELECT id, entry_text, datetime, type 
        FROM entries
    """
    params = []
    if start_date and end_date:
        query += " WHERE datetime BETWEEN ? AND ?"
        params.extend([start_date.strftime('%Y-%m-%d %H:%M:%S'), end_date.strftime('%Y-%m-%d %H:%M:%S')])
    if entry_type:
        query += " AND type = ?" if "WHERE" in query else " WHERE type = ?"
        params.append(entry_type)
    
    cursor.execute(query, params)
    entries = cursor.fetchall()
    
    # Сортируем по времени
    entries.sort(key=lambda x: datetime.strptime(x[2], '%Y-%m-%d %H:%M:%S'))
    
    return entries

# Добавление записи (заметки или записи на приём)
async def add_entry_to_db(admin_id, entry_text, datetime_str, entry_type):
    try:
        # Преобразование даты в правильный формат для базы данных
        formatted_datetime = datetime.strptime(datetime_str, '%d.%m.%Y %H.%M').strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("""
            INSERT INTO entries (admin_id, entry_text, datetime, type)
            VALUES (?, ?, ?, ?)
        """, (admin_id, entry_text, formatted_datetime, entry_type))
        conn.commit()
        entry_id = cursor.lastrowid
        entry_time = datetime.strptime(datetime_str, '%d.%m.%Y %H.%M')
        scheduler.add_job(send_notification, DateTrigger(run_date=entry_time), args=[admin_id, entry_text, entry_id])
        logging.info(f"Запись добавлена: {entry_text} на {datetime_str} (тип: {entry_type})")
    except Exception as e:
        logging.error(f"Ошибка при добавлении записи: {e}", exc_info=True)
        raise

# Отправка уведомления всем пользователям
async def send_notification(admin_id, entry_text, entry_id):
    app = ApplicationBuilder().token("YOUR_TOKEN_HERE").build()
    cursor.execute("SELECT DISTINCT admin_id FROM entries")
    all_users = cursor.fetchall()
    for user in all_users:
        try:
            await app.bot.send_message(chat_id=user[0], text=f"Напоминание: {entry_text}")
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение пользователю {user[0]}: {e}", exc_info=True)
    # Удаление записи после отправки уведомления
    cursor.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()

# Выбор даты для добавления заметки
async def select_date_for_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    calendar, step = DetailedTelegramCalendar(locale='ru').build()
    await update.message.reply_text(f"Выберите {LSTEP[step]} для добавления заметки:", reply_markup=calendar)
    context.user_data['action'] = 'add_note'

# Обработка выбора даты через календарь
async def calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logging.info("CallbackQueryHandler triggered")
    if not query:
        logging.error("No callback query received")
        return
    await query.answer()
    logging.info(f"Callback data received: {query.data}")
    try:
        result, key, step = DetailedTelegramCalendar(locale='ru').process(query.data)
        if not result and key:
            logging.info(f"Processing calendar step: {LSTEP[step]} | Key: {key}")
            await query.message.edit_text(f"Выберите {LSTEP[step]}:", reply_markup=key)
        elif result:
            selected_date = result.strftime('%d.%m.%Y')
            action = context.user_data.get('action')
            logging.info(f"Selected date: {selected_date}, Action: {action}")
            if action == 'add_note':
                context.user_data['selected_date'] = selected_date
                context.user_data['awaiting_note'] = True
                await query.message.edit_text(f"Вы выбрали {selected_date}. Введите время (ЧЧ.ММ) и текст заметки через пробел.")
            elif action == 'view_notes':
                db_date_format = datetime.strptime(selected_date, '%d.%m.%Y').strftime('%Y-%m-%d')
                start_date = datetime.strptime(db_date_format, '%Y-%m-%d')
                end_date = start_date + timedelta(days=1)
                entries = fetch_entries(start_date, end_date)
                response = f"📝 *Записи на {selected_date}:*\n"
                if entries:
                    for entry in entries:
                        entry_type = "заметка" if entry[3] == "note" else "запись на приём"
                        response += f"ID: {entry[0]}, *{entry[2]}* ({entry_type}) - {entry[1]}\n"
                else:
                    response += "Нет записей."
                await query.message.edit_text(response, parse_mode=ParseMode.MARKDOWN)
            elif action == 'signup':
                context.user_data['selected_date'] = selected_date
                await query.message.edit_text(f"Вы выбрали дату {selected_date}. Введите время (ЧЧ.ММ) описание и номер телефона для обратной связи через пробел.")
                context.user_data['awaiting_signup'] = True
    except ValueError as e:
        logging.error(f"Ошибка в обработке данных календаря: {e}", exc_info=True)
        await query.message.reply_text("Произошла ошибка при выборе даты. Попробуйте снова.")
    except Exception as e:
        logging.error(f"Неизвестная ошибка: {e}", exc_info=True)
        await query.message.reply_text("Произошла неизвестная ошибка. Попробуйте позже.")

# Добавление записи на приём
async def signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        if context.user_data.get('awaiting_signup'):
            data = update.message.text.split(" ", 1)
            time_str = data[0].strip()
            description = data[1].strip() if len(data) > 1 else ""
            selected_date = context.user_data.get('selected_date')
            appointment_datetime = f"{selected_date} {time_str}"
            
            # Проверка на занятость времени
            cursor.execute("SELECT * FROM entries WHERE datetime = ? AND type = ?", (appointment_datetime, 'appointment'))
            existing_appointment = cursor.fetchone()
            if existing_appointment:
                await update.message.reply_text("Это время уже занято. Пожалуйста, выберите другое.")
                return
            
            # Добавляем запись в базу данных
            await add_entry_to_db(user_id, description, appointment_datetime, 'appointment')
            
            # Уведомление администраторов
            cursor.execute("SELECT user_id FROM users WHERE role = 'admin'")
            admins = cursor.fetchall()
            for admin in admins:
                try:
                    await context.bot.send_message(
                        chat_id=admin[0],
                        text=f"Новая запись на приём:\n"
                             f"Дата и время: {appointment_datetime}\n"
                             f"Описание: {description}\n"
                             f"Пользователь: {user_id}"
                    )
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление администратору {admin[0]}: {e}")
            
            await update.message.reply_text(f"Вы успешно записались на {appointment_datetime}.\nОписание: {description}")
            context.user_data['awaiting_signup'] = False
    except Exception as e:
        logging.error(f"Ошибка при записи на приём: {e}", exc_info=True)
        await update.message.reply_text("Произошла ошибка при записи. Попробуйте снова.")

# Просмотр всех записей (заметок и записей на приём)
async def view_entries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Проверяем, является ли пользователь администратором
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
    user_role = cursor.fetchone()
    if not (user_role and user_role[0] == 'admin'):
        await update.message.reply_text("У вас нет прав для просмотра записей.")
        return
    
    # Получаем все записи
    all_entries = fetch_entries()
    
    # Формируем ответ
    response = "📝 *Все записи:*\n"
    if all_entries:
        for entry in all_entries:
            entry_type = "заметка" if entry[3] == "note" else "запись на приём"
            response += f"ID: {entry[0]}, *{entry[2]}* ({entry_type}) - {entry[1]}\n"
    else:
        response += "Нет записей."
    
    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

# Просмотр записей за произвольный диапазон дат
async def view_entries_custom_range(update: Update, context: ContextTypes.DEFAULT_TYPE, days_back=0, days_forward=0):
    logging.info(f"Fetching entries from {days_back} days back to {days_forward} days forward")
    today = datetime.now()
    start_date = (today - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = (today + timedelta(days=days_forward)).replace(hour=23, minute=59, second=59, microsecond=999999)
    
    # Получаем записи
    all_entries = fetch_entries(start_date, end_date)
    
    # Формируем ответ
    response = f"📝 *Записи с {start_date.strftime('%d.%m.%Y')} по {end_date.strftime('%d.%m.%Y')}:*\n"
    if all_entries:
        for entry in all_entries:
            entry_type = "заметка" if entry[3] == "note" else "запись на приём"
            response += f"ID: {entry[0]}, *{entry[2]}* ({entry_type}) - {entry[1]}\n"
    else:
        response += "Нет записей."
    
    await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN)

# Назначение администратора
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (admin_id,))
    admin_role = cursor.fetchone()
    
    # Проверяем, является ли текущий пользователь администратором
    if not (admin_role and admin_role[0] == 'admin'):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    try:
        # Получаем аргумент (user_id или username)
        arg = context.args[0].strip()
        new_admin_id = None
        if arg.startswith("@"):  # Если передан username
            username = arg[1:]  # Убираем символ "@"
            # Находим user_id по username через Telegram API
            chat = await context.bot.get_chat(username)
            new_admin_id = chat.id
        else:  # Если передан user_id
            new_admin_id = int(arg)
        # Добавляем нового администратора в базу данных
        cursor.execute("INSERT OR REPLACE INTO users (user_id, role) VALUES (?, 'admin')", (new_admin_id,))
        conn.commit()
        await update.message.reply_text(f"Пользователь {arg} назначен администратором.")
    except IndexError:
        await update.message.reply_text("Использование: /add_admin <user_id или @username>")
    except ValueError:
        await update.message.reply_text("Неверный формат user_id. Убедитесь, что вы ввели число или корректный @username.")
    except Exception as e:
        logging.error(f"Ошибка при назначении администратора: {e}", exc_info=True)
        await update.message.reply_text("Произошла ошибка при назначении администратора.")

# Главное меню
async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Проверяем, существует ли пользователь в базе данных
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
    user_role = cursor.fetchone()
    
    # Если пользователя нет в базе данных, добавляем его с ролью 'user'
    if not user_role:
        cursor.execute("INSERT INTO users (user_id, role) VALUES (?, 'user')", (user_id,))
        conn.commit()
        logging.info(f"Новый пользователь добавлен: {user_id}")
    
    # Формируем клавиатуру в зависимости от роли
    keyboard = []
    if user_role and user_role[0] == 'admin':
        keyboard = [
            ["Добавить заметку", "Просмотреть заметки"],
            ["Записаться на приём"],  # Кнопка для админов
            ["Заметки за неделю", "Заметки за месяц"],
            ["Заметки на неделю вперёд", "Удалить заметку"],
            ["Редактировать заметку"]
        ]
    else:
        keyboard = [
            ["Записаться на приём"]  # Кнопка для обычных пользователей
        ]
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Обработчик выбора пользователя
async def handle_user_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text
    logging.info(f"User choice: {choice}")
    if choice == "Добавить заметку":
        await select_date_for_note(update, context)
    elif choice == "Просмотреть заметки":
        calendar, step = DetailedTelegramCalendar(locale='ru').build()
        await update.message.reply_text(f"Выберите {LSTEP[step]} для просмотра записей:", reply_markup=calendar)
        context.user_data['action'] = 'view_notes'
    elif choice == "Записаться на приём":
        calendar, step = DetailedTelegramCalendar(locale='ru').build()
        await update.message.reply_text(f"Выберите {LSTEP[step]} для записи на приём:", reply_markup=calendar)
        context.user_data['action'] = 'signup'
    elif choice == "Заметки за неделю":
        await view_entries_custom_range(update, context, days_back=7)
    elif choice == "Заметки за месяц":
        await view_entries_custom_range(update, context, days_back=30)
    elif choice == "Заметки на неделю вперёд":
        await view_entries_custom_range(update, context, days_forward=7)
    elif choice == "Удалить заметку":
        await update.message.reply_text("Введите ID записи для удаления.")
        context.user_data['awaiting_delete'] = True
    elif choice == "Редактировать заметку":
        await update.message.reply_text("Введите ID записи, новую дату (ДД.ММ.ГГГГ ЧЧ.ММ) и новый текст через пробел.")
        context.user_data['awaiting_edit'] = True

# Обработчик текстовых сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"Handling message: {update.message.text}")
    if context.user_data.get('awaiting_note'):
        context.user_data['awaiting_note'] = False
        try:
            date = context.user_data.get('selected_date')
            data = update.message.text.split(" ", 1)
            time_str = data[0].strip()
            note_text = data[1].strip()
            datetime_str = f"{date} {time_str}"
            datetime.strptime(datetime_str, '%d.%m.%Y %H.%M')
            await add_entry_to_db(update.effective_user.id, note_text, datetime_str, 'note')
            await update.message.reply_text("Заметка успешно добавлена!")
        except (IndexError, ValueError) as e:
            logging.error(f"Ошибка при сохранении заметки: {e}", exc_info=True)
            await update.message.reply_text("Ошибка! Убедитесь, что время указано в формате ЧЧ.ММ, и текст заметки разделен пробелом.")
    elif context.user_data.get('awaiting_signup'):  # Обработка записи на приём
        await signup(update, context)
    elif context.user_data.get('awaiting_delete'):
        context.user_data['awaiting_delete'] = False
        try:
            entry_id = int(update.message.text.strip())
            cursor.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            conn.commit()
            await update.message.reply_text("Запись успешно удалена!")
        except ValueError as e:
            logging.error(f"Ошибка при удалении записи: {e}", exc_info=True)
            await update.message.reply_text("Ошибка! Убедитесь, что вы указали корректный ID записи.")
    elif context.user_data.get('awaiting_edit'):
        context.user_data['awaiting_edit'] = False
        try:
            data = update.message.text.split(" ", 2)
            entry_id = int(data[0].strip())
            new_datetime = data[1].strip()
            new_text = data[2].strip()
            datetime.strptime(new_datetime, '%d.%m.%Y %H.%M')
            # Преобразование даты в правильный формат для базы данных
            formatted_datetime = datetime.strptime(new_datetime, '%d.%m.%Y %H.%M').strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("UPDATE entries SET datetime = ?, entry_text = ? WHERE id = ?", 
                           (formatted_datetime, new_text, entry_id))
            conn.commit()
            await update.message.reply_text("Запись успешно обновлена!")
        except (IndexError, ValueError) as e:
            logging.error(f"Ошибка при редактировании записи: {e}", exc_info=True)
            await update.message.reply_text("Ошибка! Убедитесь, что вы указали корректный ID, новую дату и текст записи через пробел.")

if __name__ == "__main__":
    app = ApplicationBuilder().token("7851502628:AAHp_88IjQ86VgD6YzHVTM3ju-lpUjPeLcg").build()
    app.add_handler(CommandHandler("start", start_menu))
    app.add_handler(CommandHandler("add_admin", add_admin))  # Команда для назначения администраторов
    app.add_handler(CallbackQueryHandler(calendar_callback, pattern=".*"))
    app.add_handler(MessageHandler(filters.Regex("^(Добавить заметку|Просмотреть заметки|Записаться на приём|Заметки за неделю|Заметки за месяц|Заметки на неделю вперёд|Удалить заметку|Редактировать заметку)$"), handle_user_choice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logging.info("Бот запущен.")
    app.run_polling()