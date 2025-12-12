import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# === ЗАГРУЗКА ТОКЕНОВ (СМЕШАННЫЙ РЕЖИМ) ===
# BOT_TOKEN - ТОЛЬКО из переменных окружения хостинга
# FLYER_TOKEN - из .env файла или переменных окружения

BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Загружаем FLYER_TOKEN из .env файла (если есть)
FLYER_TOKEN = None
try:
    from dotenv import load_dotenv
    load_dotenv()  # Загружаем переменные из файла .env
    FLYER_TOKEN = os.environ.get("FLYER_TOKEN")
    print("✅ Файл .env успешно загружен для FLYER_TOKEN")
except ImportError:
    # Если нет python-dotenv, пробуем из переменных окружения
    FLYER_TOKEN = os.environ.get("FLYER_TOKEN")
    print("⚠️ Модуль python-dotenv не установлен, FLYER_TOKEN из окружения")

# Проверяем токен бота - ОБЯЗАТЕЛЬНО для работы
if not BOT_TOKEN:
    print("❌ ОШИБКА: Токен бота не найден в переменных окружения!")
    print("ℹ️ Для локального запуска добавьте BOT_TOKEN в .env файл")
    print("ℹ️ Для хостинга (Bothost): Settings → Environment Variables → BOT_TOKEN")
    exit(1)

print(f"✅ Бот инициализирован с токеном: {BOT_TOKEN[:10]}...")

# --- ОСТАЛЬНАЯ КОНФИГУРАЦИЯ ---
ADMINS = [6693423093]  # ID владельца (основного админа)
ADMINS_FILE = 'admins.txt'  # Файл для хранения списка админов
DB_FILE = 'users.db'

# ИНИЦИАЛИЗИРУЕМ ПЕРЕМЕННЫЕ FLYER
FLYER_API_ACTIVE = False
flyer = None

# Flyer токен - необязательный
if FLYER_TOKEN:
    print("✅ Flyer токен найден")
    FLYER_API_ACTIVE = True
else:
    print("⚠️ Flyer токен не найден. Бот будет работать без проверки подписок.")
    FLYER_API_ACTIVE = False

# Глобальный кэш админов для производительности
ADMINS_CACHE = None

# --- ИНИЦИАЛИЗАЦИЯ ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализация FlyerApi или заглушки
if FLYER_API_ACTIVE and FLYER_TOKEN:
    try:
        from flyerapi import Flyer 
        flyer = Flyer(FLYER_TOKEN)
        print("✅ FlyerApi успешно инициализирован")
    except ImportError:
        print("❌ Модуль flyerapi не установлен")
        FLYER_API_ACTIVE = False
        flyer = None
elif not FLYER_API_ACTIVE or flyer is None:
    # Заглушка для работы без Flyer
    class DummyFlyer:
        async def check(self, user_id):
            await asyncio.sleep(0.1) 
            return {'subscribed': True}
    flyer = DummyFlyer()
    print("ℹ️ Используется заглушка FlyerApi")

# --- ФУНКЦИИ ДЛЯ УПРАВЛЕНИЯ АДМИНАМИ ---
def load_admins():
    """Загружает список админов из файла"""
    global ADMINS_CACHE
    try:
        with open(ADMINS_FILE, 'r', encoding='utf-8') as f:
            admins = [int(line.strip()) for line in f if line.strip()]
            ADMINS_CACHE = list(set(admins))  # Убираем дубликаты и кэшируем
            return ADMINS_CACHE
    except FileNotFoundError:
        # Если файла нет, создаем его с основным админом
        with open(ADMINS_FILE, 'w', encoding='utf-8') as f:
            for admin_id in ADMINS:
                f.write(f"{admin_id}\n")
        ADMINS_CACHE = ADMINS.copy()
        return ADMINS_CACHE
    except Exception as e:
        logging.error(f"Ошибка загрузки админов: {e}")
        ADMINS_CACHE = ADMINS.copy()
        return ADMINS_CACHE

def save_admins(admins_list):
    """Сохраняет список админов в файл"""
    global ADMINS_CACHE
    try:
        with open(ADMINS_FILE, 'w', encoding='utf-8') as f:
            for admin_id in admins_list:
                f.write(f"{admin_id}\n")
        ADMINS_CACHE = admins_list.copy()
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения админов: {e}")
        return False

def add_admin(admin_id):
    """Добавляет администратора"""
    admins = load_admins()
    if admin_id not in admins:
        admins.append(admin_id)
        return save_admins(admins)
    return False

def remove_admin(admin_id):
    """Удаляет администратора"""
    admins = load_admins()
    if admin_id in admins and admin_id != ADMINS[0]:  # Нельзя удалить основного админа
        admins.remove(admin_id)
        return save_admins(admins)
    return False

def get_all_admins():
    """Получает список всех администраторов"""
    global ADMINS_CACHE
    if ADMINS_CACHE is None:
        return load_admins()
    return ADMINS_CACHE

def is_admin_filter(item: types.Message | types.CallbackQuery):
    user_id = item.from_user.id
    admins_list = get_all_admins()  # Используем кэшированную версию
    return user_id in admins_list

# --- МАШИНЫ СОСТОЯНИЙ (FSM) ---

class AdminStates(StatesGroup):
    changing_bonus = State()
    waiting_promo_code = State()
    waiting_promo_bonus = State()
    waiting_promo_limit = State()
    waiting_promo_code_for_delete = State()
    waiting_task_channel_id = State()
    waiting_task_description = State()
    waiting_task_reward = State()
    waiting_task_id_for_delete = State()
    waiting_admin_id_to_add = State()  # Новое состояние
    waiting_admin_id_to_remove = State()  # Новое состояние

class UserStates(StatesGroup):
    waiting_for_promocode = State()

# --- БАЗА ДАННЫХ (SQLite) ---

def execute_db(query, params=(), fetch_one=False, fetch_all=False):
    """Синхронное выполнение запроса к БД"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        conn.commit()
        if fetch_one:
            return cursor.fetchone()
        if fetch_all:
            return cursor.fetchall()

        if query.strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
            return cursor.rowcount

        return None

    except Exception as e:
        logging.error(f"DB Error: {e} in query: {query}")
        return None
    finally:
        conn.close()

def init_db():
    """Инициализация базы данных"""
    queries = [
        '''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            referrer_id INTEGER,
            referrals_count INTEGER DEFAULT 0,
            reg_date TEXT,
            status TEXT DEFAULT 'Активен'
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            bonus INTEGER NOT NULL,
            max_uses INTEGER NOT NULL,
            current_uses INTEGER DEFAULT 0
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS user_promocodes (
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            activation_date TEXT,
            PRIMARY KEY (user_id, code)
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            description TEXT NOT NULL,
            reward INTEGER NOT NULL,
            is_active INTEGER DEFAULT 1
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS user_tasks (
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            completion_date TEXT,
            PRIMARY KEY (user_id, task_id)
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            request_date TEXT,
            status TEXT DEFAULT 'Ожидание'
        )
        '''
    ]

    for q in queries:
        execute_db(q)

    execute_db('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', ('referral_bonus', '5'))

# --- DB GET/SET HELPERS ---

def get_setting(key):
    res = execute_db('SELECT value FROM settings WHERE key = ?', (key,), fetch_one=True)
    return int(res[0]) if res and key == 'referral_bonus' else (res[0] if res else None)

def update_setting(key, value):
    execute_db('REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))

def get_referral_bonus():
    return get_setting('referral_bonus') or 5

def get_user(user_id):
    return execute_db('SELECT user_id, username, balance, referrer_id, referrals_count, reg_date, status FROM users WHERE user_id = ?', (user_id,), fetch_one=True)

def update_user_balance(user_id, amount):
    execute_db('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))

def get_user_balance(user_id):
    res = execute_db('SELECT balance FROM users WHERE user_id = ?', (user_id,), fetch_one=True)
    return res[0] if res else 0

def add_user(user_id, username, referrer_id=None):
    existing_user = get_user(user_id)
    if existing_user:
        return False

    reg_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_db(
        'INSERT INTO users (user_id, username, referrer_id, reg_date) VALUES (?, ?, ?, ?)',
        (user_id, username, referrer_id, reg_date)
    )

    if referrer_id:
        referral_bonus = get_referral_bonus()
        update_user_balance(referrer_id, referral_bonus)
        execute_db(
            'UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id = ?',
            (referrer_id,)
        )
    return True

# --- DB PROMOCODES / TASKS / WITHDRAWALS ---

def get_promocode(code):
    return execute_db('SELECT code, bonus, max_uses, current_uses FROM promocodes WHERE code = ?', (code,), fetch_one=True)

def check_user_promocode(user_id, code):
    return execute_db('SELECT * FROM user_promocodes WHERE user_id = ? AND code = ?', (user_id, code), fetch_one=True)

def activate_promocode_in_db(user_id, code, bonus):
    try:
        activation_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_db('INSERT INTO user_promocodes (user_id, code, activation_date) VALUES (?, ?, ?)',
                    (user_id, code, activation_date))
        execute_db('UPDATE promocodes SET current_uses = current_uses + 1 WHERE code = ?', (code,))
        update_user_balance(user_id, bonus)
        return True
    except Exception as e:
        logging.error(f"Ошибка активации промокода {code} для user {user_id}: {e}")
        return False

def create_promocode_in_db(code, bonus, max_uses):
    try:
        execute_db('INSERT INTO promocodes (code, bonus, max_uses) VALUES (?, ?, ?)', (code, bonus, max_uses))
        return True
    except Exception:
        return False

def delete_promocode_from_db(code):
    return execute_db('DELETE FROM promocodes WHERE code = ?', (code,))

def get_all_promocodes():
    return execute_db('SELECT code, bonus, max_uses, current_uses FROM promocodes', fetch_all=True)

def get_active_tasks_for_user(user_id):
    return execute_db('''
        SELECT t.task_id, t.channel_id, t.description, t.reward
        FROM tasks t
        LEFT JOIN user_tasks ut ON t.task_id = ut.task_id AND ut.user_id = ?
        WHERE t.is_active = 1 AND ut.user_id IS NULL
    ''', (user_id,), fetch_all=True)

def get_all_tasks():
    return execute_db('SELECT task_id, channel_id, description, reward FROM tasks', fetch_all=True)

def add_task_to_db(channel_id, description, reward):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO tasks (channel_id, description, reward) VALUES (?, ?, ?)', (channel_id, description, reward))
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return task_id

def delete_task_from_db(task_id):
    deleted_count = execute_db('DELETE FROM tasks WHERE task_id = ?', (task_id,))
    return deleted_count

def create_withdrawal_request(user_id, amount):
    request_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO withdrawals (user_id, amount, request_date) VALUES (?, ?, ?)',
        (user_id, amount, request_date)
    )
    request_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return request_id

def get_pending_withdrawals():
    return execute_db('SELECT id, user_id, amount, request_date FROM withdrawals WHERE status = "Ожидание"', fetch_all=True)

def get_withdrawal_request(request_id):
    return execute_db('SELECT id, user_id, amount, request_date, status FROM withdrawals WHERE id = ?', (request_id,), fetch_one=True)

def get_user_withdrawals(user_id):
    return execute_db(
        'SELECT id, amount, request_date, status FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 10',
        (user_id,),
        fetch_all=True
    )

def update_withdrawal_status(request_id, status):
    execute_db('UPDATE withdrawals SET status = ? WHERE id = ?', (status, request_id))

def get_top_users_by_balance(limit=10):
    return execute_db('SELECT user_id, username, balance FROM users ORDER BY balance DESC LIMIT ?', (limit,), fetch_all=True)

def get_top_users_by_referrals(limit=10):
    return execute_db('SELECT user_id, username, referrals_count FROM users ORDER BY referrals_count DESC LIMIT ?', (limit,), fetch_all=True)

# --- КЛАВИАТУРЫ ---

def admin_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔑 Управление промокодами", callback_data="admin_promo_menu")
    builder.button(text="📝 Управление заданиями", callback_data="admin_tasks_menu")
    builder.button(text="💸 Заявки на вывод", callback_data="admin_withdrawals_menu")
    builder.button(text="⭐ Изменить бонус за реферала", callback_data="admin_change_bonus")
    builder.button(text="👥 Управление администраторами", callback_data="admin_manage_admins")
    builder.adjust(1)
    return builder.as_markup()

def promo_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать промокод", callback_data="admin_create_promo")
    builder.button(text="📜 Список промокодов", callback_data="admin_list_promo")
    builder.button(text="🗑 Удалить промокод", callback_data="admin_delete_promo")
    builder.button(text="⬅️ Назад в админ-панель", callback_data="admin_manage")
    builder.adjust(1)
    return builder.as_markup()

def tasks_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить задание", callback_data="admin_create_task")
    builder.button(text="📜 Список заданий", callback_data="admin_list_tasks")
    builder.button(text="🗑 Удалить задание", callback_data="admin_delete_task")
    builder.button(text="⬅️ Назад в админ-панель", callback_data="admin_manage")
    builder.adjust(1)
    return builder.as_markup()

def admin_management_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Список администраторов", callback_data="admin_list_admins")
    builder.button(text="➕ Добавить администратора", callback_data="admin_add_admin")
    builder.button(text="➖ Удалить администратора", callback_data="admin_remove_admin")
    builder.button(text="⬅️ Назад в админ-панель", callback_data="admin_manage")
    builder.adjust(1)
    return builder.as_markup()

def withdrawal_action_keyboard(request_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"approve_withdraw_{request_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_withdraw_{request_id}")
    return builder.as_markup()

def main_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.row(types.KeyboardButton(text="✨ Заработать звёзды"))
    builder.row(types.KeyboardButton(text="👤 Профиль"), types.KeyboardButton(text="❄️ Вывести звёзды"))
    builder.row(types.KeyboardButton(text="📝 Задания"))
    builder.row(types.KeyboardButton(text="🏆 ТОПЫ"))
    builder.row(types.KeyboardButton(text="Наш канал"))
    return builder.as_markup(resize_keyboard=True)

def sub_check_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Наш канал", url="https://t.me/+h0sjTepJRVs5NDMy")
    builder.button(text="Я подписался /check", callback_data="check_sub")
    builder.adjust(1)
    return builder.as_markup()

def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Мои заявки на вывод", callback_data="my_withdraws")
    builder.button(text="🔑 Активировать промокод", callback_data="activate_promo")
    builder.adjust(1)
    return builder.as_markup()

# --- ФУНКЦИИ ПРОВЕРКИ И ВСТУПЛЕНИЯ ---

async def check_user_subscription(user_id: int) -> bool:
    """Проверяет подписку пользователя через FlyerApi."""
    global FLYER_API_ACTIVE, flyer

    if not FLYER_API_ACTIVE or flyer is None:
        logging.warning("FlyerAPI не активен, проверка подписки пропущена.")
        return True

    try:
        result = await flyer.check(user_id)
        return result.get('subscribed', False) if isinstance(result, dict) else bool(result)
    except Exception as e:
        logging.error(f"Ошибка проверки подписки через FlyerApi для {user_id}: {e}")
        # В случае ошибки API разрешаем доступ ТОЛЬКО администраторам
        return user_id in get_all_admins()

# --- ХЕНДЛЕРЫ: СТАРТ и ГЛАВНОЕ МЕНЮ ---

@dp.message(CommandStart())
async def command_start_handler(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username if message.from_user.username else str(user_id)

    referrer_id = None
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id == user_id:
            referrer_id = None

    is_new = add_user(user_id, username, referrer_id)

    if is_new and referrer_id:
        try:
            await bot.send_message(referrer_id, f"🎉 У вас новый реферал: @{username}! Начислено +{get_referral_bonus()} звёзд.")
        except Exception:
            pass

    # Проверяем подписку
    is_subscribed = await check_user_subscription(user_id)
    admins_list = get_all_admins()

    if is_subscribed or user_id in admins_list:
        await message.answer("Добро пожаловать в главное меню!", reply_markup=main_menu_keyboard())
    else:
        await message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )

@dp.callback_query(F.data == "check_sub")
async def check_subscription_callback(call: types.CallbackQuery):
    user_id = call.from_user.id

    is_subscribed = await check_user_subscription(user_id)

    if is_subscribed:
        try:
            await bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        await call.message.answer("✅ Подписка подтверждена! Добро пожаловать.", reply_markup=main_menu_keyboard())
    else:
        await call.answer("❌ Вы не подписались на все каналы!", show_alert=True)

@dp.message(F.text == "Назад")
@dp.message(Command("check"))
async def go_back(message: types.Message, state: FSMContext):
    # Проверяем подписку
    is_subscribed = await check_user_subscription(message.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and message.from_user.id not in admins_list:
        await message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    await state.clear()
    await message.answer("Главное меню", reply_markup=main_menu_keyboard())

@dp.message(F.text == "👤 Профиль")
async def profile(message: types.Message):
    # Проверяем подписку
    is_subscribed = await check_user_subscription(message.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and message.from_user.id not in admins_list:
        await message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    user_data = get_user(message.from_user.id)
    if not user_data:
        await message.answer("Профиль не найден. Нажмите /start")
        return

    uid, uname, balance, ref_id, refs_count, r_date, status = user_data
    referral_bonus = get_referral_bonus()

    # Исправляем проблему с именем пользователя
    username_display = f"@{uname}" if uname and uname != "None" else f"ID: {uid}"

    inviter_text = "Неизвестно"
    if ref_id:
        inviter_data = get_user(ref_id)
        if inviter_data and inviter_data[1] and inviter_data[1] != "None":
            inviter_text = f"@{inviter_data[1]}"
        elif inviter_data:
            inviter_text = f"ID: {ref_id}"

    # Создаем текст с HTML разметкой
    text = (
        f"<b>🔷 Профиль пользователя 🔷</b>\n"
        f"__________________________\n\n"
        f"🆔 <b>ID:</b> <code>{uid}</code>\n"
        f"👤 <b>Имя:</b> {username_display}\n"
        f"💰 <b>Баланс:</b> {balance} ✨\n\n"
        f"🎁 <b>Бонус за 1 реферала:</b> {referral_bonus} ✨\n"
        f"👨‍💻 <b>Пригласивший:</b> {inviter_text}\n"
        f"📊 <b>Рефералов:</b> {refs_count}\n\n"
        f"🗓 <b>Регистрация:</b> {r_date}\n"
        f"🔓 <b>Статус:</b> {status}\n"
        f"__________________________"
    )

    try:
        await message.answer(text, parse_mode="HTML", reply_markup=profile_keyboard())
    except TelegramBadRequest as e:
        if "can't parse entities" in str(e):
            # Если HTML не работает, отправляем без разметки
            plain_text = (
                f"🔷 Профиль пользователя 🔷\n"
                f"__________________________\n\n"
                f"🆔 ID: {uid}\n"
                f"👤 Имя: {username_display}\n"
                f"💰 Баланс: {balance} ✨\n\n"
                f"🎁 Бонус за 1 реферала: {referral_bonus} ✨\n"
                f"👨‍💻 Пригласивший: {inviter_text}\n"
                f"📊 Рефералов: {refs_count}\n\n"
                f"🗓 Регистрация: {r_date}\n"
                f"🔓 Статус: {status}\n"
                f"__________________________"
            )
            await message.answer(plain_text, parse_mode=None, reply_markup=profile_keyboard())
        else:
            raise e

@dp.message(F.text == "📝 Задания")
async def tasks(message: types.Message):
    # Проверяем подписку
    is_subscribed = await check_user_subscription(message.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and message.from_user.id not in admins_list:
        await message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    user_id = message.from_user.id
    active_tasks = get_active_tasks_for_user(user_id)

    if not active_tasks:
        text = "<b>📝 Активные задания</b>\n\nНет доступных заданий. Зайдите позже!"
        await message.answer(text, parse_mode="HTML")
        return

    await message.answer("<b>📝 Активные задания</b>\n__________________________", parse_mode="HTML")

    for i, (task_id, channel_id, desc, reward) in enumerate(active_tasks):
        channel_link_name = channel_id.lstrip('@')
        task_text = f"<b>{i+1}. Подписка на <a href='https://t.me/{channel_link_name}'>Канал</a></b>\n"
        task_text += f"   Описание: {desc}\n"
        task_text += f"   Награда: <b>{reward} ✨</b>"

        builder = InlineKeyboardBuilder()
        builder.button(text=f"✅ Проверить подписку ({reward} ⭐)", callback_data=f"check_task_{task_id}")

        await message.answer(task_text, parse_mode="HTML", reply_markup=builder.as_markup())

@dp.message(F.text == "✨ Заработать звёзды")
async def earn_stars(message: types.Message):
    # Проверяем подписку
    is_subscribed = await check_user_subscription(message.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and message.from_user.id not in admins_list:
        await message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    user_id = message.from_user.id
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    referral_link = f"https://t.me/{bot_username}?start={user_id}"
    referral_bonus = get_referral_bonus()

    text = (
        "<b>✨ Приглашай друзей и получай звёзды! ✨</b>\n\n"
        f"Твоя реферальная ссылка:\n<code>{referral_link}</code>\n\n"
        f"🔑 Копируй ссылку выше, и отправляй друзьям, за каждого друга ты будешь получать <b>{referral_bonus}</b> звезды, чем больше пригласишь, тем больше получишь!"
    )

    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "🏆 ТОПЫ")
async def top_menu(message: types.Message):
    # Проверяем подписку
    is_subscribed = await check_user_subscription(message.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and message.from_user.id not in admins_list:
        await message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    text = "Какой топ ты хочешь увидеть?\n\nВыберите тип топа ниже."

    builder = InlineKeyboardBuilder()
    builder.button(text="🏆 Топ по балансу", callback_data="top_balance")
    builder.button(text="🏆 Топ по рефералам", callback_data="top_refs")
    builder.adjust(2)

    await message.answer(text, parse_mode=None, reply_markup=builder.as_markup())

@dp.message(F.text == "Наш канал")
async def our_channel(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="Перейти на канал", url="https://t.me/+h0sjTepJRVs5NDMy")
    await message.answer("Наш канал:", reply_markup=builder.as_markup())

@dp.message(F.text == "❄️ Вывести звёзды")
async def start_withdrawal(message: types.Message):
    # Проверяем подписку
    is_subscribed = await check_user_subscription(message.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and message.from_user.id not in admins_list:
        await message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    user_balance = get_user_balance(message.from_user.id)

    builder = InlineKeyboardBuilder()
    amounts = [15, 25, 50, 100, 200, 300, 500, 700, 1000]
    for amount in amounts:
        builder.button(text=f"{amount} ⭐", callback_data=f"withdraw_amount_{amount}")
    builder.adjust(3)

    text = (
        f"<b>💸 Обмен звёзд</b>\n"
        f"Ваш баланс: <b>{user_balance} ✨</b>\n\n"
        "Выберите количество звёзд для вывода:"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())

# --- ХЕНДЛЕРЫ: АДМИН-ПАНЕЛЬ ---

@dp.message(Command("admin"), is_admin_filter)
async def admin_command(message: types.Message, state: FSMContext):
    await state.clear()
    bonus = get_referral_bonus()
    text = (
        "<b>⚙️ Панель администратора</b>\n\n"
        f"Текущий бонус за реферала: <b>{bonus} ✨</b>\n"
        "Выберите действие ниже:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=admin_menu_keyboard())

# --- FSM: Создание промокода ---

@dp.callback_query(F.data == "admin_create_promo", is_admin_filter)
async def start_create_promo(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminStates.waiting_promo_code)
    await call.message.edit_text("✍️ Введите <b>текст промокода</b> (например, STARTSUMMER):", parse_mode="HTML")

@dp.message(AdminStates.waiting_promo_code, is_admin_filter)
async def process_promo_code(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    if get_promocode(code):
        return await message.answer("❌ Промокод с таким кодом уже существует. Введите другой:")

    await state.update_data(code=code)
    await state.set_state(AdminStates.waiting_promo_bonus)
    await message.answer("✍️ Код принят. Введите <b>бонус</b> (количество звёзд):", parse_mode="HTML")

@dp.message(AdminStates.waiting_promo_bonus, is_admin_filter)
async def process_promo_bonus(message: types.Message, state: FSMContext):
    try:
        bonus = int(message.text.strip())
        if bonus <= 0:
            raise ValueError

        await state.update_data(bonus=bonus)
        await state.set_state(AdminStates.waiting_promo_limit)
        await message.answer("✍️ Бонус принят. Введите <b>лимит активаций</b> (0 или -1 для бесконечного лимита):", parse_mode="HTML")

    except ValueError:
        await message.answer("❌ <b>Ошибка!</b> Бонус должен быть целым положительным числом. Повторите ввод:", parse_mode="HTML")

@dp.message(AdminStates.waiting_promo_limit, is_admin_filter)
async def process_promo_limit(message: types.Message, state: FSMContext):
    try:
        limit = int(message.text.strip())
        if limit < -1: raise ValueError

        data = await state.get_data()
        code = data['code']
        bonus = data['bonus']

        max_uses = limit if limit > 0 else 0
        create_promocode_in_db(code, bonus, max_uses)

        await message.answer(
            f"✅ <b>Промокод создан!</b>\nКод: {code}\nБонус: <b>{bonus} ✨</b>\nЛимит: <b>{max_uses}</b>",
            parse_mode="HTML", reply_markup=main_menu_keyboard()
        )
        await state.clear()

    except ValueError:
        await message.answer("❌ <b>Ошибка!</b> Лимит должен быть целым числом (0 для бесконечного). Повторите ввод:", parse_mode="HTML")

# --- FSM: Удаление промокода ---

@dp.callback_query(F.data == "admin_delete_promo", is_admin_filter)
async def start_delete_promo(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminStates.waiting_promo_code_for_delete)
    await call.message.edit_text(
        "✍️ Введите <b>код промокода</b>, который вы хотите удалить (например, STARTSUMMER).",
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_promo_code_for_delete, is_admin_filter)
async def process_delete_promo(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    deleted_count = delete_promocode_from_db(code)

    await state.clear()

    if deleted_count and deleted_count > 0:
        await message.answer(
            f"✅ <b>Промокод '{code}' успешно удален!</b>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
    else:
        await message.answer(
            f"❌ <b>Ошибка!</b> Промокод с кодом '{code}' не найден.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )

# --- FSM: Создание задания ---

@dp.callback_query(F.data == "admin_create_task", is_admin_filter)
async def start_create_task(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminStates.waiting_task_channel_id)
    await call.message.edit_text("✍️ Введите <b>ID канала</b> для проверки подписки (например, @channel_name или полную ссылку):", parse_mode="HTML")

@dp.message(AdminStates.waiting_task_channel_id, is_admin_filter)
async def process_task_channel_id(message: types.Message, state: FSMContext):
    channel_id = message.text.strip().replace('https://t.me/', '@').replace('t.me/', '@')

    if not channel_id.startswith('@'):
        return await message.answer("❌ Неверный формат ID канала. Введите, пожалуйста, @channel_name или ссылку.")

    await state.update_data(channel_id=channel_id)
    await state.set_state(AdminStates.waiting_task_description)
    await message.answer("✍️ ID канала принят. Теперь введите <b>описание задания</b> (что нужно сделать):", parse_mode="HTML")

@dp.message(AdminStates.waiting_task_description, is_admin_filter)
async def process_task_description(message: types.Message, state: FSMContext):
    description = message.text.strip()

    await state.update_data(description=description)
    await state.set_state(AdminStates.waiting_task_reward)
    await message.answer("✍️ Описание принято. Теперь введите <b>вознаграждение</b> (количество звёзд):", parse_mode="HTML")

@dp.message(AdminStates.waiting_task_reward, is_admin_filter)
async def process_task_reward(message: types.Message, state: FSMContext):
    try:
        reward = int(message.text.strip())
        if reward <= 0: raise ValueError

        data = await state.get_data()
        channel_id = data['channel_id']
        description = data['description']

        task_id = add_task_to_db(channel_id, description, reward)

        await message.answer(
            f"✅ <b>Задание добавлено!</b> (ID: {task_id})\nКанал: <code>{channel_id}</code>\nОписание: {description}\nНаграда: <b>{reward} ✨</b>",
            parse_mode="HTML", reply_markup=main_menu_keyboard()
        )
        await state.clear()

    except ValueError:
        await message.answer("❌ <b>Ошибка!</b> Вознаграждение должно быть целым числом больше нуля. Повторите ввод:", parse_mode="HTML")

# --- FSM: Удаление задания ---

@dp.callback_query(F.data == "admin_delete_task", is_admin_filter)
async def start_delete_task(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminStates.waiting_task_id_for_delete)
    await call.message.edit_text(
        "✍️ Введите <b>ID задания</b>, которое вы хотите удалить. ID можно посмотреть в 'Списке заданий'.",
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_task_id_for_delete, is_admin_filter)
async def process_delete_task(message: types.Message, state: FSMContext):
    try:
        task_id = int(message.text.strip())

        deleted_count = delete_task_from_db(task_id)

        await state.clear()

        if deleted_count and deleted_count > 0:
            await message.answer(
                f"✅ <b>Задание ID {task_id} успешно удалено!</b>",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
        else:
            await message.answer(
                f"❌ <b>Ошибка!</b> Задание с ID {task_id} не найдено. Попробуйте снова или проверьте список.",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )

    except ValueError:
        await message.answer("❌ <b>Ошибка!</b> ID задания должен быть целым числом. Повторите ввод ID:", parse_mode="HTML")

# --- FSM: Смена бонуса ---

@dp.callback_query(F.data == "admin_change_bonus", is_admin_filter)
async def start_change_bonus(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminStates.changing_bonus)
    await call.message.edit_text("✍️ Введите новое количество звёзд, начисляемых за одного реферала (целое число):", parse_mode="HTML")

@dp.message(AdminStates.changing_bonus, is_admin_filter)
async def process_new_bonus(message: types.Message, state: FSMContext):
    try:
        new_bonus = int(message.text.strip())
        if new_bonus < 0: raise ValueError

        update_setting('referral_bonus', new_bonus)
        await state.clear()

        text = (
            f"✅ <b>Бонус успешно обновлен!</b>\n"
            f"Новое количество звёзд за реферала: <b>{new_bonus} ✨</b>"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu_keyboard())
    except ValueError:
        await message.answer("❌ <b>Ошибка ввода!</b> Введите, пожалуйста, целое положительное число.", parse_mode="HTML")

# --- FSM: Активация промокода (Пользователь) ---

@dp.callback_query(F.data == "activate_promo")
async def start_activate_promo(call: types.CallbackQuery, state: FSMContext):
    # Проверяем подписку
    is_subscribed = await check_user_subscription(call.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and call.from_user.id not in admins_list:
        await call.answer("❌ Вы не подписаны на обязательные каналы!", show_alert=True)
        await call.message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    await call.answer()
    await state.set_state(UserStates.waiting_for_promocode)
    await call.message.answer(
        "<b>🔑 Введите промокод:</b>\n"
        "Если вы передумали, нажмите 'Назад'.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardBuilder().add(types.KeyboardButton(text="Назад")).as_markup(resize_keyboard=True)
    )

@dp.message(UserStates.waiting_for_promocode)
async def process_promocode_activation(message: types.Message, state: FSMContext):
    if message.text == "Назад":
        await state.clear()
        await message.answer("Главное меню", reply_markup=main_menu_keyboard())
        return

    code = message.text.strip().upper()
    user_id = message.from_user.id

    promo_data = get_promocode(code)

    if not promo_data:
        return await message.answer("❌ Промокод не найден.")

    code, bonus, max_uses, current_uses = promo_data

    if max_uses > 0 and current_uses >= max_uses:
        return await message.answer("❌ Срок действия промокода истёк (достигнут лимит активаций).")

    if check_user_promocode(user_id, code):
        return await message.answer("❌ Вы уже активировали этот промокод.")

    if activate_promocode_in_db(user_id, code, bonus):
        await message.answer(
            f"🎉 <b>Промокод активирован!</b>\n"
            f"Начислено <b>{bonus} ✨</b> на ваш баланс.",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
        await state.clear()
    else:
        await message.answer("❌ Произошла ошибка при активации. Попробуйте позже.", reply_markup=main_menu_keyboard())
        await state.clear()

# --- FSM: Вывод средств (Пользователь) ---

@dp.callback_query(F.data.startswith("withdraw_amount_"))
async def create_withdrawal_request_callback(call: types.CallbackQuery):
    # Проверяем подписку
    is_subscribed = await check_user_subscription(call.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and call.from_user.id not in admins_list:
        await call.answer("❌ Вы не подписаны на обязательные каналы!", show_alert=True)
        await call.message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    amount = int(call.data.split('_')[2])
    user_id = call.from_user.id

    await call.answer()

    user_balance = get_user_balance(user_id)

    if amount > user_balance:
        await call.message.edit_text(
            f"<b>💸 Обмен звёзд</b>\nВаш баланс: <b>{user_balance} ✨</b>\n\n"
            f"❌ Недостаточно звёзд для вывода <b>{amount} ✨</b>!",
            parse_mode="HTML"
        )
        return

    # Убавляем баланс (синхронная функция, без await!)
    update_user_balance(user_id, -amount)
    request_id = create_withdrawal_request(user_id, amount)

    await call.message.edit_text(
        f"✅ <b>Заявка на вывод создана!</b> (ID: {request_id})\n"
        f"Сумма: <b>{amount} ✨</b>\n\n"
        "Ожидайте проверки администратором. Статус можно проверить в Профиле.",
        parse_mode="HTML",
    )

    user_info = call.from_user
    for admin_id in get_all_admins():
        try:
            await bot.send_message(
                admin_id,
                f"🔔 <b>НОВАЯ ЗАЯВКА НА ВЫВОД!</b>\n"
                f"ID: {request_id}\n"
                f"Пользователь: @{user_info.username} ({user_id})\n"
                f"Сумма: <b>{amount} ✨</b>",
                parse_mode="HTML",
                reply_markup=withdrawal_action_keyboard(request_id)
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить админа {admin_id}: {e}")

# --- CALLBACK ХЕНДЛЕРЫ ДЛЯ АДМИНКИ ---

@dp.callback_query(F.data.startswith("admin_"), is_admin_filter)
async def admin_callback_handler(call: types.CallbackQuery, state: FSMContext):
    action = call.data

    if action == "admin_manage":
        await state.clear()
        await admin_command(call.message, state)
        await call.answer()

    elif action == "admin_promo_menu":
        text = "<b>🔑 Управление промокодами</b>\n\nВыберите действие:"
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=promo_menu_keyboard())
        await call.answer()

    elif action == "admin_tasks_menu":
        text = "<b>📝 Управление заданиями</b>\n\nВыберите действие:"
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=tasks_menu_keyboard())
        await call.answer()

    elif action == "admin_list_promo":
        promos = get_all_promocodes()
        text = "<b>📜 Активные промокоды</b>\n__________________________\n\n"
        if not promos:
            text += "Список пуст."
        else:
            for code, bonus, max_uses, current_uses in promos:
                limit_text = f"Лимит: {current_uses}/{max_uses}" if max_uses > 0 else "Лимит: ∞"
                text += f"<b>{code}</b> (Бонус: {bonus} ✨)\n{limit_text}\n\n"

        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_promo_menu")
        builder.button(text="🗑 Удалить промокод", callback_data="admin_delete_promo")
        builder.adjust(1)
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await call.answer()

    elif action == "admin_list_tasks":
        tasks_data = get_all_tasks()

        text = "<b>📜 Список всех заданий</b>\n__________________________\n\n"
        if not tasks_data:
            text += "Список заданий пуст."
        else:
            for task_id, channel_id, desc, reward in tasks_data:
                text += f"🆔 <b>{task_id}</b> (Канал: <code>{channel_id}</code>)\n"
                text += f"{desc}\nНаграда: {reward} ✨\n\n"

        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_tasks_menu")
        builder.button(text="🗑 Удалить задание", callback_data="admin_delete_task")
        builder.adjust(1)
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await call.answer()

    elif action == "admin_manage_admins":
        text = "<b>👥 Управление администраторами</b>\n\nВыберите действие:"
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=admin_management_keyboard())
        await call.answer()

    elif action == "admin_list_admins":
        admins_list = get_all_admins()
        text = "<b>📋 Список администраторов</b>\n\n"

        for i, admin_id in enumerate(admins_list, 1):
            try:
                user = await bot.get_chat(admin_id)
                username = f"@{user.username}" if user.username else user.full_name
                is_owner = "👑" if admin_id == ADMINS[0] else "👤"
                text += f"{i}. {is_owner} <code>{admin_id}</code> - {username}\n"
            except Exception:
                text += f"{i}. <code>{admin_id}</code> - Неизвестный пользователь\n"

        text += f"\nВсего администраторов: <b>{len(admins_list)}</b>"

        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_manage_admins")
        builder.adjust(1)
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await call.answer()

    elif action == "admin_add_admin":
        await call.answer()
        await state.set_state(AdminStates.waiting_admin_id_to_add)
        await call.message.edit_text(
            "✍️ Введите <b>ID пользователя</b>, которого хотите добавить в администраторы.\n\n"
            "ID можно получить с помощью бота @userinfobot или другого бота для получения ID.",
            parse_mode="HTML"
        )

    elif action == "admin_remove_admin":
        await call.answer()
        await state.set_state(AdminStates.waiting_admin_id_to_remove)

        admins_list = get_all_admins()
        text = "<b>➖ Удаление администратора</b>\n\n"
        text += "Список текущих администраторов:\n"

        for i, admin_id in enumerate(admins_list, 1):
            try:
                user = await bot.get_chat(admin_id)
                username = f"@{user.username}" if user.username else user.full_name
                is_owner = "👑" if admin_id == ADMINS[0] else "👤"
                text += f"{i}. {is_owner} <code>{admin_id}</code> - {username}\n"
            except Exception:
                text += f"{i}. <code>{admin_id}</code> - Неизвестный пользователь\n"

        text += "\n✍️ Введите <b>ID администратора</b>, которого хотите удалить (кроме основного владельца):"

        await call.message.edit_text(text, parse_mode="HTML")

    elif action == "admin_withdrawals_menu":
        await display_pending_withdrawals(call.message)
        await call.answer()

# Обработчик для добавления админа
@dp.message(AdminStates.waiting_admin_id_to_add, is_admin_filter)
async def process_add_admin(message: types.Message, state: FSMContext):
    try:
        new_admin_id = int(message.text.strip())

        if new_admin_id == message.from_user.id:
            await message.answer("❌ <b>Ошибка!</b> Вы не можете добавить самого себя (вы уже администратор).", parse_mode="HTML")
            await state.clear()
            return

        if add_admin(new_admin_id):
            try:
                user = await bot.get_chat(new_admin_id)
                username = f"@{user.username}" if user.username else user.full_name

                await message.answer(
                    f"✅ <b>Администратор успешно добавлен!</b>\n"
                    f"ID: <code>{new_admin_id}</code>\n"
                    f"Имя: {username}",
                    parse_mode="HTML",
                    reply_markup=admin_management_keyboard()
                )

                # Уведомляем нового админа
                try:
                    await bot.send_message(
                        new_admin_id,
                        f"🎉 <b>Вам предоставлены права администратора!</b>\n\n"
                        f"Теперь вы можете управлять ботом @{(await bot.get_me()).username}\n"
                        f"Используйте команду /admin для доступа к панели управления.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            except Exception:
                await message.answer(
                    f"✅ <b>Администратор успешно добавлен!</b>\n"
                    f"ID: <code>{new_admin_id}</code>",
                    parse_mode="HTML",
                    reply_markup=admin_management_keyboard()
                )
        else:
            await message.answer(
                f"❌ <b>Ошибка!</b> Пользователь с ID <code>{new_admin_id}</code> уже является администратором.",
                parse_mode="HTML",
                reply_markup=admin_management_keyboard()
            )

        await state.clear()

    except ValueError:
        await message.answer("❌ <b>Ошибка!</b> ID должен быть числом. Повторите ввод:", parse_mode="HTML")

# Обработчик для удаления админа
@dp.message(AdminStates.waiting_admin_id_to_remove, is_admin_filter)
async def process_remove_admin(message: types.Message, state: FSMContext):
    try:
        admin_id_to_remove = int(message.text.strip())

        if admin_id_to_remove == ADMINS[0]:
            await message.answer(
                "❌ <b>Ошибка!</b> Нельзя удалить основного владельца бота.",
                parse_mode="HTML",
                reply_markup=admin_management_keyboard()
            )
            await state.clear()
            return

        if admin_id_to_remove == message.from_user.id:
            await message.answer(
                "❌ <b>Ошибка!</b> Вы не можете удалить самого себя.",
                parse_mode="HTML",
                reply_markup=admin_management_keyboard()
            )
            await state.clear()
            return

        if remove_admin(admin_id_to_remove):
            try:
                user = await bot.get_chat(admin_id_to_remove)
                username = f"@{user.username}" if user.username else user.full_name

                await message.answer(
                    f"✅ <b>Администратор успешно удален!</b>\n"
                    f"ID: <code>{admin_id_to_remove}</code>\n"
                    f"Имя: {username}",
                    parse_mode="HTML",
                    reply_markup=admin_management_keyboard()
                )

                # Уведомляем удаленного админа
                try:
                    await bot.send_message(
                        admin_id_to_remove,
                        f"🔒 <b>Ваши права администратора отозваны!</b>\n\n"
                        f"Вы больше не имеете доступа к панели управления ботом @{(await bot.get_me()).username}.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            except Exception:
                await message.answer(
                    f"✅ <b>Администратор успешно удален!</b>\n"
                    f"ID: <code>{admin_id_to_remove}</code>",
                    parse_mode="HTML",
                    reply_markup=admin_management_keyboard()
                )
        else:
            await message.answer(
                f"❌ <b>Ошибка!</b> Пользователь с ID <code>{admin_id_to_remove}</code> не найден в списке администраторов.",
                parse_mode="HTML",
                reply_markup=admin_management_keyboard()
            )

        await state.clear()

    except ValueError:
        await message.answer("❌ <b>Ошибка!</b> ID должен быть числом. Повторите ввод:", parse_mode="HTML")

async def display_pending_withdrawals(message: types.Message):
    withdrawals = get_pending_withdrawals()
    text = "<b>💸 Заявки на вывод (Ожидание)</b>\n__________________________\n\n"

    builder = InlineKeyboardBuilder()

    if not withdrawals:
        text += "Нет ожидающих заявок."
    else:
        for req_id, user_id, amount, date in withdrawals:
            text += f"<b>ID {req_id}</b> | {amount} ✨ | User: <code>{user_id}</code>\n"
            text += f"Дата: {date.split()[0]}\n\n"
            builder.button(text=f"Обработать {req_id}", callback_data=f"show_withdraw_{req_id}")

    builder.button(text="⬅️ Назад", callback_data="admin_manage")
    builder.adjust(1)

    await message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("show_withdraw_"), is_admin_filter)
async def show_withdrawal_details(call: types.CallbackQuery):
    request_id = int(call.data.split('_')[2])
    request = get_withdrawal_request(request_id)

    if not request or request[4] != 'Ожидание':
        await call.answer("Заявка не найдена или уже обработана.", show_alert=True)
        await display_pending_withdrawals(call.message)
        return

    req_id, user_id, amount, date, status = request
    user_data = get_user(user_id)
    username = user_data[1] if user_data else "Неизвестно"

    text = (
        f"<b>📝 Детали заявки ID {req_id}</b>\n"
        f"__________________________\n"
        f"👤 Пользователь: @{username} (<code>{user_id}</code>)\n"
        f"💰 Сумма: <b>{amount} ✨</b>\n"
        f"🗓 Дата: {date}\n"
        f"🔓 Статус: {status}\n\n"
        f"<b>Действия администратора:</b>"
    )

    await call.message.edit_text(text, parse_mode="HTML", reply_markup=withdrawal_action_keyboard(req_id))
    await call.answer()

@dp.callback_query(F.data.startswith(("approve_withdraw_", "reject_withdraw_")), is_admin_filter)
async def process_withdrawal_action(call: types.CallbackQuery):
    action, req_id_str = call.data.split('_withdraw_')
    req_id = int(req_id_str)

    request = get_withdrawal_request(req_id)
    if not request or request[4] != 'Ожидание':
        await call.answer("Заявка уже обработана.", show_alert=True)
        await display_pending_withdrawals(call.message)
        return

    req_id, user_id, amount, date, status = request

    if action == "approve":
        new_status = "Одобрено"
        update_withdrawal_status(req_id, new_status)

        try:
            await bot.send_message(
                user_id,
                f"✅ <b>Ваша заявка на вывод ID {req_id} ({amount} ✨) одобрена!</b> Администратор свяжется с вами для перевода.",
                parse_mode="HTML"
            )
        except Exception:
            pass

        await call.answer(f"Заявка ID {req_id} одобрена.", show_alert=True)

    elif action == "reject":
        new_status = "Отклонено"
        update_withdrawal_status(req_id, new_status)

        # Возвращаем средства (синхронная функция, без await!)
        update_user_balance(user_id, amount)

        try:
            await bot.send_message(
                user_id,
                f"❌ <b>Ваша заявка на вывод ID {req_id} ({amount} ✨) отклонена.</b> Средства возвращены на ваш баланс.",
                parse_mode="HTML"
            )
        except Exception:
            pass

        await call.answer(f"Заявка ID {req_id} отклонена. Средства возвращены.", show_alert=True)

    await display_pending_withdrawals(call.message)

# --- УНИВЕРСАЛЬНЫЕ CALLBACK ХЕНДЛЕРЫ (Пользователь) ---

@dp.callback_query(F.data.startswith(("check_task_", "top_", "my_withdraws")))
async def user_callback_handler(call: types.CallbackQuery):
    # Проверяем подписку
    is_subscribed = await check_user_subscription(call.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and call.from_user.id not in admins_list:
        await call.answer("❌ Вы не подписаны на обязательные каналы!", show_alert=True)
        await call.message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    data = call.data
    user_id = call.from_user.id

    if data.startswith("check_task_"):
        task_id = int(data.split('_')[2])

        task_data = execute_db('SELECT channel_id, reward FROM tasks WHERE task_id = ?', (task_id,), fetch_one=True)

        if not task_data:
            return await call.answer("❌ Задание не найдено или удалено.", show_alert=True)

        channel_id, reward = task_data

        # Проверка подписки на канал (стандартная проверка Telegram)
        try:
            member = await bot.get_chat_member(channel_id, user_id)
            is_subscribed = member.status in ['member', 'administrator', 'creator']
        except Exception as e:
            logging.error(f"Ошибка проверки подписки на канал {channel_id}: {e}")
            return await call.answer(f"❌ Не удалось проверить подписку. Убедитесь, что бот является администратором канала {channel_id} (если это частный канал) и/или что канал существует.", show_alert=True)

        if is_subscribed:
            if execute_db('SELECT * FROM user_tasks WHERE user_id = ? AND task_id = ?', (user_id, task_id), fetch_one=True):
                await call.answer("✅ Вы уже выполняли это задание.", show_alert=True)
                return

            execute_db('INSERT INTO user_tasks (user_id, task_id, completion_date) VALUES (?, ?, ?)',
                        (user_id, task_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            # Начисляем награду (синхронная функция, без await!)
            update_user_balance(user_id, reward)

            await call.message.edit_text(
                f"🎉 <b>Задание выполнено!</b>\nНачислено <b>{reward} ✨</b> на ваш баланс.",
                parse_mode="HTML",
                reply_markup=None
            )
            await call.answer(f"Начислено {reward} звёзд!", show_alert=True)
        else:
            await call.answer(f"❌ Вы не подписались на канал {channel_id}!", show_alert=True)

    elif data.startswith("top_"):
        if data == "top_balance":
            top_data = get_top_users_by_balance(10)
            text = format_top_list(top_data, "ТОП по балансу (звёзды)", "✨", 2)
        elif data == "top_refs":
            top_data = get_top_users_by_referrals(10)
            text = format_top_list(top_data, "ТОП по рефералам", "👥", 2)

        builder = InlineKeyboardBuilder()
        builder.button(text="🏆 Топ по балансу", callback_data="top_balance")
        builder.button(text="🏆 Топ по рефералам", callback_data="top_refs")
        builder.adjust(2)

        await call.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
        await call.answer()

    elif data == "my_withdraws":
        withdrawals = get_user_withdrawals(user_id)

        text = "<b>📄 Ваши последние заявки на вывод</b>\n__________________________\n\n"

        if not withdrawals:
            text += "У вас еще нет заявок на вывод."
        else:
            for req_id, amount, date, status in withdrawals:
                status_emoji = "⏳" if status == "Ожидание" else ("✅" if status == "Одобрено" else "❌")
                text += (
                    f"<b>{req_id}.</b> {amount} ✨\n"
                    f"  Статус: {status_emoji} <b>{status}</b>\n"
                    f"  Дата: {date.split()[0]}\n\n"
                )

        await call.message.edit_text(text, parse_mode="HTML", reply_markup=profile_keyboard())
        await call.answer()

def format_top_list(top_data, title, unit, count_index):
    if not top_data:
        return f"<b>🏆 {title}</b>\n\nСписок пока пуст."

    text = f"<b>🏆 {title}</b>\n"
    text += "__________________________\n\n"

    for i, row in enumerate(top_data):
        user_id, username, count = row[0], row[1], row[count_index]
        medal = ""
        if i == 0:
            medal = "🥇"
        elif i == 1:
            medal = "🥈"
        elif i == 2:
            medal = "🥉"
        else:
            medal = f"#{i+1}"

        name = f"@{username}" if username and username != "None" else f"ID: <code>{user_id}</code>"

        text += f"{medal} {name} — <b>{count}</b> {unit}\n"

    text += "\n__________________________"
    return text

# --- УНИВЕРСАЛЬНЫЙ ХЕНДЛЕР ---

@dp.message()
async def handle_unhandled_messages(message: types.Message, state: FSMContext):
    # Проверяем подписку для всех сообщений
    is_subscribed = await check_user_subscription(message.from_user.id)
    admins_list = get_all_admins()

    if not is_subscribed and message.from_user.id not in admins_list:
        await message.answer(
            "🔒 <b>Доступ закрыт!</b>\n\nПожалуйста, подпишитесь на наш канал для использования бота.",
            parse_mode="HTML",
            reply_markup=sub_check_keyboard()
        )
        return

    current_state = await state.get_state()

    if current_state:
        if not message.text:
            await message.answer("💬 Извините, я жду текстового ввода.")
        return

    if current_state is None:
        if message.text:
            await message.answer(
                "🤖 Извините, я не понял эту команду. Используйте кнопки меню или команду /start.",
                reply_markup=main_menu_keyboard()
            )
        else:
            await message.answer("💬 Извините, я пока могу обрабатывать только текстовые сообщения и нажатия кнопок.")

# --- ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ---

async def main() -> None:
    # Загружаем админов при запуске
    admins = load_admins()
    print(f"Загружено администраторов: {len(admins)}")

    # Инициализируем БД
    init_db()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())



