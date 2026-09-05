import asyncio
import sqlite3
import hashlib
import aiohttp
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
import re
import ccxt
import logging

# ========== ОТКЛЮЧАЕМ ЛОГИ ДЛЯ ЧИСТОТЫ ==========
logging.basicConfig(level=logging.INFO)

# ========== КОНФИГ ==========
TOKEN = "8651064170:AAE_Y-GYtWhrMM9kncx5O2pVnDe25w2qmCQ"
ADMIN_ID = 5146620562  

# Цены
PRICE_YOOMONEY = 100  # 500 RUB
PRICE_USDT = "10 USDT"
PRICE_STARS = 80

# Реквизиты оплаты (ЗАМЕНИ НА СВОИ)
YOOMONEY_WALLET = "410011234567890"
USDT_ADDRESS = "TXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"

# Настройки Pocket Option
TIMEFRAME = "5m"
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# Bybit
bybit = ccxt.bybit({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

# Пары для анализа
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT']

# Демо и рефералка
DEMO_DAYS = 3
REFERRAL_BONUS = 20

# ========== БАЗА ДАННЫХ ==========
conn = sqlite3.connect("pocket_bot.db")
cursor = conn.cursor()

# Таблица пользователей
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    license_key TEXT,
    expire_date DATETIME,
    demo_start DATETIME,
    demo_used INTEGER DEFAULT 0,
    referrer_id INTEGER,
    balance REAL DEFAULT 0,
    is_admin INTEGER DEFAULT 0,
    is_blocked INTEGER DEFAULT 0,
    activated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")

# Таблица рефералов
cursor.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER,
    referred_id INTEGER,
    amount REAL,
    status TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")

# Таблица платежей
cursor.execute("""
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    currency TEXT,
    status TEXT,
    payment_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")

# Таблица сделок
cursor.execute("""
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    pair TEXT,
    direction TEXT,
    entry_price REAL,
    exit_price REAL,
    profit REAL,
    status TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")

# Таблица сигналов
cursor.execute("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    pair TEXT,
    direction TEXT,
    entry_price REAL,
    rsi REAL,
    sent_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Делаем пользователя ADMIN_ID администратором
cursor.execute("INSERT OR REPLACE INTO users (user_id, is_admin) VALUES (?, 1)", (ADMIN_ID,))
conn.commit()

# ========== ФУНКЦИИ ЛИЦЕНЗИЙ ==========
def generate_license(user_id):
    data = f"{user_id}_{datetime.now().timestamp()}"
    return hashlib.md5(data.encode()).hexdigest()[:16]

def is_admin(user_id):
    cursor.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result and result[0] == 1

def is_blocked(user_id):
    cursor.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result and result[0] == 1

def activate_demo(user_id):
    expire = datetime.now() + timedelta(days=DEMO_DAYS)
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, demo_start, demo_used, expire_date)
        VALUES (?, ?, ?, ?)
    """, (user_id, datetime.now(), 1, expire))
    conn.commit()
    return True

def activate_license(user_id, license_key, referrer_id=None):
    expire = datetime.now() + timedelta(days=30)
    
    cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result or not result[0]:
        if referrer_id:
            cursor.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_id, user_id))
    
    cursor.execute("""
        INSERT OR REPLACE INTO users (user_id, license_key, expire_date, demo_used)
        VALUES (?, ?, ?, 1)
    """, (user_id, license_key, expire))
    conn.commit()
    
    if referrer_id:
        bonus = PRICE_STARS * (REFERRAL_BONUS / 100)
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (bonus, referrer_id))
        cursor.execute("""
            INSERT INTO referrals (referrer_id, referred_id, amount, status)
            VALUES (?, ?, ?, 'pending')
        """, (referrer_id, user_id, bonus))
        conn.commit()
        asyncio.create_task(notify_referrer(referrer_id, user_id, bonus))
    
    return True

def check_access(user_id):
    if is_blocked(user_id):
        return False
    if is_admin(user_id):
        return True
    
    cursor.execute("SELECT expire_date, demo_used FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result:
        return False
    
    expire_date, demo_used = result
    
    if demo_used == 1 and expire_date:
        if expire_date and datetime.now() < datetime.strptime(expire_date, '%Y-%m-%d %H:%M:%S.%f'):
            return True
    
    if expire_date and datetime.now() < datetime.strptime(expire_date, '%Y-%m-%d %H:%M:%S.%f'):
        return True
    
    return False

def get_access_info(user_id):
    cursor.execute("""
        SELECT license_key, expire_date, demo_used, balance, is_admin
        FROM users WHERE user_id = ?
    """, (user_id,))
    result = cursor.fetchone()
    if not result:
        return None
    
    license_key, expire_date, demo_used, balance, admin = result
    
    access_type = "🔴 Нет доступа"
    days_left = 0
    
    if admin == 1:
        access_type = "👑 Администратор (бесплатно)"
        days_left = 999
    elif expire_date:
        expire = datetime.strptime(expire_date, '%Y-%m-%d %H:%M:%S.%f')
        days_left = (expire - datetime.now()).days
        if days_left > 0:
            access_type = "🟡 Демо-доступ" if demo_used == 1 else "🟢 Платная лицензия"
    
    return {
        'type': access_type,
        'days_left': days_left,
        'balance': balance or 0,
        'license_key': license_key,
        'is_admin': admin or 0
    }

async def notify_referrer(referrer_id, referred_id, bonus):
    text = f"🎉 <b>Новый реферал!</b>\n━━━━━━━━━━━━━━━━\n👤 Пользователь: <code>{referred_id}</code>\n💰 Бонус: <b>{bonus} Stars</b>"
    try:
        await bot.send_message(referrer_id, text, parse_mode='HTML')
    except:
        pass

def get_referral_stats(user_id):
    cursor.execute("""
        SELECT COUNT(*), SUM(amount) 
        FROM referrals WHERE referrer_id = ? AND status = 'pending'
    """, (user_id,))
    count, total = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
    total_refs = cursor.fetchone()[0]
    return {'total': total_refs, 'pending': count or 0, 'bonus': total or 0}

def get_referral_link(user_id):
    return f"https://t.me/{bot.username}?start={user_id}"

# ========== СИГНАЛЫ ==========
def get_pocket_signal(pair, user_id):
    if not check_access(user_id):
        return None
    
    try:
        ohlcv = bybit.fetch_ohlcv(pair, timeframe='5m', limit=50)
        if len(ohlcv) < 30:
            return None
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        close = df['close']
        
        rsi = RSIIndicator(close).rsi().iloc[-1]
        ema10 = EMAIndicator(close, window=10).ema_indicator().iloc[-1]
        ema30 = EMAIndicator(close, window=30).ema_indicator().iloc[-1]
        current_price = close.iloc[-1]
        
        signal_direction = None
        strength = 0
        
        if rsi < RSI_OVERSOLD and current_price > ema10:
            strength += 2
            signal_direction = "CALL"
        elif rsi > RSI_OVERBOUGHT and current_price < ema10:
            strength += 2
            signal_direction = "PUT"
        
        if signal_direction == "CALL" and ema10 > ema30:
            strength += 1
        elif signal_direction == "PUT" and ema10 < ema30:
            strength += 1
        
        if strength >= 2 and signal_direction:
            cursor.execute("""
                INSERT INTO signals (user_id, pair, direction, entry_price, rsi)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, pair, signal_direction, 
                  round(current_price, 4), round(rsi, 1)))
            conn.commit()
            
            return {
                'pair': pair.replace('/USDT', ''),
                'direction': signal_direction,
                'entry': round(current_price, 4),
                'rsi': round(rsi, 1),
                'strength': strength
            }
    except Exception as e:
        print(f"Ошибка сигнала {pair}: {e}")
        return None
    return None

# ========== НОВОСТИ ==========
async def get_economic_news():
    url = "https://ru.investing.com/rss/news.rss"
    important_news = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                text = await resp.text()
                matches = re.findall(r'<title>(.*?)<\/title>', text)
                for title in matches[:10]:
                    if any(keyword in title.lower() for keyword in 
                          ['fomc', 'нфр', 'cpi', 'ppi', 'безработиц', 'ставк', 'инфляци']):
                        important_news.append(title)
    except:
        pass
    return important_news[:3]

# ========== КЛАВИАТУРЫ ==========
def main_keyboard(user_id):
    has_access = check_access(user_id)
    
    buttons = [
        [InlineKeyboardButton(text="📊 Сигнал для Pocket Option", callback_data="get_signal")],
        [InlineKeyboardButton(text="📰 Новости", callback_data="get_news")],
        [InlineKeyboardButton(text="📈 Статистика", callback_data="get_stats")],
        [InlineKeyboardButton(text="📋 Мои сделки", callback_data="my_trades")],
        [InlineKeyboardButton(text="👥 Реферальная программа", callback_data="referral")],
    ]
    
    if not has_access:
        buttons.append([InlineKeyboardButton(text="💳 Купить доступ", callback_data="buy_access")])
        buttons.append([InlineKeyboardButton(text="🎁 Попробовать 3 дня", callback_data="try_demo")])
    else:
        buttons.append([InlineKeyboardButton(text="🔄 Продлить", callback_data="buy_access")])
    
    buttons.append([InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")])
    
    if is_admin(user_id):
        buttons.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_panel")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def payment_keyboard(payment_id):
    buttons = [
        [InlineKeyboardButton(text="💳 Юмани (500 ₽)", callback_data=f"pay_yoomoney_{payment_id}")],
        [InlineKeyboardButton(text="🪙 USDT (TRC20)", callback_data=f"pay_usdt_{payment_id}")],
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data=f"pay_stars_{payment_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    user_id = msg.from_user.id
    args = msg.text.split()
    
    if is_blocked(user_id):
        await msg.answer("⛔ Ваш аккаунт заблокирован!")
        return
    
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id != user_id:
            cursor.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_id, user_id))
            conn.commit()
            await msg.answer(f"🎉 Вы пришли по реферальной ссылке от <code>{referrer_id}</code>!", parse_mode='HTML')
    
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        is_admin_flag = 1 if user_id == ADMIN_ID else 0
        cursor.execute("INSERT INTO users (user_id, is_admin) VALUES (?, ?)", (user_id, is_admin_flag))
        conn.commit()
    
    info = get_access_info(user_id)
    
    admin_text = "\n👑 <b>Вы администратор! Бот для вас бесплатен.</b>\n" if is_admin(user_id) else ""
    
    text = (
        "🚀 <b>Pocket Option PRO Bot</b>\n\n"
        f"👤 Ваш ID: <code>{user_id}</code>\n"
        f"🔑 Статус: {info['type'] if info else '🔴 Нет доступа'}\n"
        f"📅 Дней осталось: {info['days_left'] if info else 0}\n"
        f"💰 Бонусный баланс: {info['balance'] if info else 0} Stars\n"
        f"{admin_text}"
        "\n📌 <b>Что умеет бот:</b>\n"
        "• Сигналы для Pocket Option (CALL/PUT)\n"
        "• Анализ через Bybit\n"
        "• Экономический календарь\n"
        "• Статистика сделок\n\n"
        f"📊 <b>Мониторим:</b> {', '.join([p.replace('/USDT', '') for p in PAIRS])}\n\n"
        "🎁 <b>Демо:</b> 3 дня бесплатно!\n"
        f"💰 <b>Цена:</b> {PRICE_YOOMONEY}₽ / {PRICE_USDT} / {PRICE_STARS} Stars\n"
        "🔄 Действует 30 дней\n\n"
        "🛡️ <b>Работает 24/7 на сервере</b>"
    )
    await msg.answer(text, parse_mode='HTML', reply_markup=main_keyboard(user_id))

@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if is_blocked(user_id):
        await callback.message.answer("⛔ Ваш аккаунт заблокирован!")
        await callback.answer()
        return
    
    data = callback.data
    
    if data == "admin_panel":
        if not is_admin(user_id):
            await callback.answer("⛔ Доступ запрещён", show_alert=True)
            return
        
        cursor.execute("""
            SELECT COUNT(*), 
                   SUM(CASE WHEN expire_date > datetime('now') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN demo_used = 1 AND expire_date > datetime('now') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN is_blocked = 1 THEN 1 ELSE 0 END)
            FROM users
        """)
        total, active, demo_active, blocked = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM referrals")
        refs = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(profit) FROM trades WHERE status='win'")
        total_profit = cursor.fetchone()[0] or 0
        
        text = (
            "👑 <b>Админ-панель</b>\n\n"
            f"👥 Всего пользователей: {total}\n"
            f"✅ Активных: {active}\n"
            f"🎁 Демо: {demo_active}\n"
            f"⛔ Заблокировано: {blocked}\n"
            f"👥 Рефералов: {refs}\n"
            f"💰 Общий профит: ${total_profit:.2f}\n\n"
            "📌 <b>Команды:</b>\n"
            "/add_license [user_id] — добавить лицензию\n"
            "/block [user_id] — заблокировать\n"
            "/unblock [user_id] — разблокировать\n"
            "/broadcast [текст] — рассылка\n"
            "/pay_referrals — выплатить рефералам"
        )
        await callback.message.answer(text, parse_mode='HTML')
        await callback.answer()
    
    elif data == "get_signal":
        if not check_access(user_id):
            await callback.message.answer(
                "❌ Нет доступа!\n🎁 Попробуй демо или купи подписку.",
                reply_markup=main_keyboard(user_id)
            )
            await callback.answer()
            return
        
        await callback.message.answer("🔍 Ищу сигнал для Pocket Option на Bybit...")
        
        for pair in PAIRS:
            signal = get_pocket_signal(pair, user_id)
            if signal:
                emoji = "🟢" if signal['direction'] == "CALL" else "🔴"
                text = (
                    f"📊 <b>СИГНАЛ ДЛЯ POCKET OPTION</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"💰 {signal['pair']}/USDT\n"
                    f"{emoji} <b>{signal['direction']}</b>\n"
                    f"💵 Цена входа: <b>${signal['entry']}</b>\n"
                    f"📉 RSI: {signal['rsi']} | Сила: {signal['strength']}/3\n"
                    f"📊 Источник: <b>Bybit</b>\n"
                    f"⏰ {datetime.now().strftime('%H:%M')}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"🔥 <b>Рекомендация:</b>\n"
                    f"• Экспирация: <b>5 минут</b>\n"
                    f"• Ставка: 3-5% от депозита\n"
                    f"• Тейк-профит: 80-85%\n\n"
                    f"<i>После сделки: /win [сумма] или /loss [сумма]</i>"
                )
                await callback.message.answer(text, parse_mode='HTML')
                break
        else:
            await callback.message.answer("❌ Сигналов сейчас нет. Попробуй через 5 минут.")
        
        await callback.answer()
    
    elif data == "try_demo":
        if is_admin(user_id):
            await callback.message.answer("👑 Вы администратор, бот бесплатен!")
            await callback.answer()
            return
        
        cursor.execute("SELECT demo_used FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        
        if result and result[0] == 1:
            info = get_access_info(user_id)
            if info and info['days_left'] > 0:
                await callback.message.answer(f"✅ Демо уже активно! Осталось {info['days_left']} дней")
            else:
                await callback.message.answer("❌ Демо истёк. Купи подписку!")
        else:
            activate_demo(user_id)
            await callback.message.answer(
                f"🎁 <b>Демо-доступ активирован на {DEMO_DAYS} дня!</b>\n\n"
                f"📅 Действует до: {(datetime.now() + timedelta(days=DEMO_DAYS)).strftime('%d.%m.%Y')}\n"
                f"📊 Используй 'Сигнал для Pocket Option'",
                parse_mode='HTML'
            )
        await callback.answer()
    
    elif data == "buy_access":
        if is_admin(user_id):
            await callback.message.answer("👑 Вы администратор, бот бесплатен!")
            await callback.answer()
            return
        
        payment_id = f"pay_{user_id}_{datetime.now().timestamp()}"
        cursor.execute("""
            INSERT INTO payments (user_id, amount, currency, status, payment_id)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, PRICE_YOOMONEY, "RUB", "pending", payment_id))
        conn.commit()
        
        text = (
            "💳 <b>Оплата доступа</b>\n\n"
            f"💰 Цена: {PRICE_YOOMONEY} ₽ / {PRICE_USDT} / {PRICE_STARS} Stars\n"
            f"🔄 30 дней\n"
            f"🎁 Рефералка: {REFERRAL_BONUS}%\n\n"
            "📌 <b>Выбери способ:</b>"
        )
        await callback.message.answer(text, parse_mode='HTML', reply_markup=payment_keyboard(payment_id))
        await callback.answer()
    
    elif data.startswith("pay_yoomoney_"):
        payment_id = data.replace("pay_yoomoney_", "")
        text = (
            "💳 <b>Оплата через Юмани</b>\n\n"
            f"💰 Сумма: {PRICE_YOOMONEY} ₽\n\n"
            "1️⃣ Перейди по ссылке:\n"
            f"<a href='https://yoomoney.ru/transfer?to={YOOMONEY_WALLET}&amount={PRICE_YOOMONEY}&comment={payment_id}'>Оплатить</a>\n\n"
            "2️⃣ Укажи комментарий:\n"
            f"<code>{payment_id}</code>\n\n"
            "3️⃣ Нажми 'Я оплатил'"
        )
        buttons = [[InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"confirm_payment_{payment_id}")]]
        await callback.message.answer(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
    
    elif data.startswith("pay_usdt_"):
        payment_id = data.replace("pay_usdt_", "")
        text = (
            "🪙 <b>Оплата USDT (TRC20)</b>\n\n"
            f"💰 Сумма: {PRICE_USDT}\n"
            f"🌐 Сеть: TRC20\n\n"
            "1️⃣ Отправь на адрес:\n"
            f"<code>{USDT_ADDRESS}</code>\n\n"
            "2️⃣ В комментарии укажи:\n"
            f"<code>{payment_id}</code>\n\n"
            "3️⃣ Нажми 'Я оплатил'"
        )
        buttons = [[InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"confirm_payment_{payment_id}")]]
        await callback.message.answer(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
    
    elif data.startswith("pay_stars_"):
        payment_id = data.replace("pay_stars_", "")
        text = (
            "⭐ <b>Оплата Stars</b>\n\n"
            f"💰 Сумма: {PRICE_STARS} Stars\n\n"
            "1️⃣ Отправь @PremiumBot запрос\n"
            "2️⃣ Введи 100 Stars\n"
            "3️⃣ Оплати\n"
            "4️⃣ Нажми 'Я оплатил'\n\n"
            f"📝 Код: <code>{payment_id}</code>"
        )
        buttons = [[InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"confirm_payment_{payment_id}")]]
        await callback.message.answer(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
    
    elif data.startswith("confirm_payment_"):
        payment_id = data.replace("confirm_payment_", "")
        
        cursor.execute("SELECT user_id, status FROM payments WHERE payment_id = ?", (payment_id,))
        result = cursor.fetchone()
        
        if not result:
            await callback.message.answer("❌ Платёж не найден")
            await callback.answer()
            return
        
        if result[1] == "confirmed":
            await callback.message.answer("✅ Уже подтверждён")
            await callback.answer()
            return
        
        user_id_pay = result[0]
        license_key = generate_license(user_id_pay)
        
        cursor.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id_pay,))
        ref_result = cursor.fetchone()
        referrer_id = ref_result[0] if ref_result else None
        
        activate_license(user_id_pay, license_key, referrer_id)
        cursor.execute("UPDATE payments SET status = 'confirmed' WHERE payment_id = ?", (payment_id,))
        conn.commit()
        
        await callback.message.answer(
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"🔑 Ключ: <code>{license_key}</code>\n"
            f"📅 Доступ на 30 дней\n"
            f"📊 Используй 'Сигнал для Pocket Option'",
            parse_mode='HTML',
            reply_markup=main_keyboard(user_id_pay)
        )
        await callback.answer()
    
    elif data == "referral":
        ref_link = get_referral_link(user_id)
        stats = get_referral_stats(user_id)
        info = get_access_info(user_id)
        
        text = (
            "👥 <b>Реферальная программа</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📊 Приведено: {stats['total']}\n"
            f"💰 Бонусов: {stats['bonus']} Stars\n"
            f"📊 Баланс: {info['balance'] if info else 0} Stars\n\n"
            f"🔗 <b>Твоя ссылка:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"💰 Бонус: {REFERRAL_BONUS}% от платежа друга"
        )
        await callback.message.answer(text, parse_mode='HTML')
        await callback.answer()
    
    elif data == "get_stats":
        cursor.execute("""
            SELECT COUNT(*), SUM(profit), 
                   SUM(CASE WHEN status='win' THEN 1 ELSE 0 END)
            FROM trades WHERE user_id = ? AND date(timestamp) = date('now')
        """, (user_id,))
        total, total_profit, wins = cursor.fetchone()
        
        if total and total > 0:
            winrate = round((wins / total) * 100, 1)
            text = (
                f"📈 <b>Статистика за сегодня:</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📊 Всего: {total}\n"
                f"✅ WIN: {wins}\n"
                f"❌ LOSS: {total - wins}\n"
                f"🎯 Винрейт: <b>{winrate}%</b>\n"
                f"💰 Профит: ${round(total_profit, 2)}"
            )
        else:
            text = "📭 Сегодня сделок нет"
        await callback.message.answer(text, parse_mode='HTML')
        await callback.answer()
    
    elif data == "my_trades":
        cursor.execute("""
            SELECT pair, direction, entry_price, profit, status
            FROM trades WHERE user_id = ? 
            ORDER BY id DESC LIMIT 5
        """, (user_id,))
        trades = cursor.fetchall()
        
        if trades:
            text = "📋 <b>Последние 5 сделок:</b>\n\n"
            for t in trades:
                emoji = "✅" if t[4] == 'win' else "❌"
                text += f"{emoji} {t[0]} | {t[1]} | Вход: {t[2]} | Профит: ${round(t[3], 2)}\n"
        else:
            text = "📭 Сделок нет"
        await callback.message.answer(text, parse_mode='HTML')
        await callback.answer()
    
    elif data == "get_news":
        news = await get_economic_news()
        if news:
            text = "📰 <b>Важные новости:</b>\n\n" + "\n".join(news)
        else:
            text = "❌ Новостей нет"
        await callback.message.answer(text, parse_mode='HTML')
        await callback.answer()
    
    elif data == "about":
        text = (
            "ℹ️ <b>О боте</b>\n\n"
            "🤖 <b>Версия:</b> 4.0 PO\n"
            "📊 <b>Для:</b> Pocket Option\n"
            "📊 <b>Источник:</b> Bybit\n"
            "📈 <b>Винрейт:</b> 60-70%\n\n"
            f"💰 <b>Цены:</b>\n"
            f"• Юмани: {PRICE_YOOMONEY} ₽\n"
            f"• USDT: {PRICE_USDT}\n"
            f"• Stars: {PRICE_STARS} Stars\n\n"
            "🎁 <b>Демо:</b> 3 дня\n"
            "👥 <b>Рефералка:</b> 20%\n\n"
            "🛡️ <b>Работает 24/7 на сервере</b>"
        )
        await callback.message.answer(text, parse_mode='HTML')
        await callback.answer()

# ===== КОМАНДЫ ПОЛЬЗОВАТЕЛЕЙ =====
@dp.message(Command("win"))
async def win_trade(msg: types.Message):
    user_id = msg.from_user.id
    args = msg.text.split()
    profit = float(args[1]) if len(args) > 1 else 10.0
    
    cursor.execute("SELECT pair, direction, entry_price FROM signals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
    signal = cursor.fetchone()
    
    if signal:
        cursor.execute("""
            INSERT INTO trades (user_id, pair, direction, entry_price, exit_price, profit, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, signal[0], signal[1], signal[2], signal[2] + profit/100, profit, 'win'))
        conn.commit()
        await msg.answer(f"✅ WIN! +${profit:.2f}", parse_mode='HTML')
    else:
        await msg.answer("❌ Нет активного сигнала")

@dp.message(Command("loss"))
async def loss_trade(msg: types.Message):
    user_id = msg.from_user.id
    args = msg.text.split()
    loss = float(args[1]) if len(args) > 1 else 10.0
    
    cursor.execute("SELECT pair, direction, entry_price FROM signals WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
    signal = cursor.fetchone()
    
    if signal:
        cursor.execute("""
            INSERT INTO trades (user_id, pair, direction, entry_price, exit_price, profit, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, signal[0], signal[1], signal[2], signal[2] - loss/100, -loss, 'loss'))
        conn.commit()
        await msg.answer(f"❌ LOSS! -${loss:.2f}", parse_mode='HTML')
    else:
        await msg.answer("❌ Нет активного сигнала")

@dp.message(Command("referral"))
async def referral_cmd(msg: types.Message):
    user_id = msg.from_user.id
    ref_link = get_referral_link(user_id)
    stats = get_referral_stats(user_id)
    info = get_access_info(user_id)
    
    text = (
        "👥 <b>Реферальная программа</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📊 Приведено: {stats['total']}\n"
        f"💰 Бонусов: {stats['bonus']} Stars\n"
        f"📊 Баланс: {info['balance'] if info else 0} Stars\n\n"
        f"🔗 <b>Твоя ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"💰 Бонус: {REFERRAL_BONUS}% от платежа друга"
    )
    await msg.answer(text, parse_mode='HTML')

@dp.message(Command("balance"))
async def balance_cmd(msg: types.Message):
    user_id = msg.from_user.id
    info = get_access_info(user_id)
    
    if info:
        text = f"💰 <b>Ваш баланс</b>\n━━━━━━━━━━━━━━━━\nStars: {info['balance']}\n👥 Приводи друзей и увеличивай баланс!"
    else:
        text = "❌ Пользователь не найден"
    
    await msg.answer(text, parse_mode='HTML')

# ===== АДМИН-КОМАНДЫ =====
@dp.message(Command("add_license"))
async def add_license(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer("❌ /add_license [user_id]")
        return
    user_id = int(args[1])
    license_key = generate_license(user_id)
    activate_license(user_id, license_key)
    await msg.answer(f"✅ Лицензия для {user_id} активирована")

@dp.message(Command("block"))
async def block_user(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer("❌ /block [user_id]")
        return
    user_id = int(args[1])
    cursor.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    await msg.answer(f"⛔ Пользователь {user_id} заблокирован")

@dp.message(Command("unblock"))
async def unblock_user(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer("❌ /unblock [user_id]")
        return
    user_id = int(args[1])
    cursor.execute("UPDATE users SET is_blocked = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    await msg.answer(f"✅ Пользователь {user_id} разблокирован")

@dp.message(Command("pay_referrals"))
async def pay_referrals(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    cursor.execute("SELECT referrer_id, SUM(amount) FROM referrals WHERE status = 'pending' GROUP BY referrer_id")
    pending = cursor.fetchall()
    if not pending:
        await msg.answer("❌ Нет выплат")
        return
    for referrer_id, amount in pending:
        cursor.execute("UPDATE referrals SET status = 'paid' WHERE referrer_id = ? AND status = 'pending'", (referrer_id,))
    conn.commit()
    await msg.answer("✅ Выплаты выполнены")

@dp.message(Command("broadcast"))
async def broadcast(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    text = msg.text.replace("/broadcast", "").strip()
    if not text:
        await msg.answer("❌ Введи текст")
        return
    cursor.execute("SELECT user_id FROM users WHERE is_blocked = 0")
    users = cursor.fetchall()
    sent = 0
    for user in users:
        try:
            await bot.send_message(user[0], f"📢 <b>Объявление</b>\n\n{text}", parse_mode='HTML')
            sent += 1
            await asyncio.sleep(0.1)
        except:
            pass
    await msg.answer(f"✅ Отправлено {sent} пользователям")

# ===== АВТО-СИГНАЛЫ =====
async def auto_signals():
    while True:
        now = datetime.now()
        wait_seconds = (5 - now.minute % 5) * 60 - now.second
        if wait_seconds < 10:
            wait_seconds += 300
        await asyncio.sleep(wait_seconds)
        
        cursor.execute("SELECT user_id FROM users WHERE (expire_date > datetime('now') OR is_admin = 1) AND is_blocked = 0")
        active_users = cursor.fetchall()
        
        for user_id in active_users:
            user_id = user_id[0]
            for pair in PAIRS:
                signal = get_pocket_signal(pair, user_id)
                if signal:
                    emoji = "🟢" if signal['direction'] == "CALL" else "🔴"
                    text = (
                        f"📊 <b>АВТО-СИГНАЛ PO</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"💰 {signal['pair']}/USDT\n"
                        f"{emoji} <b>{signal['direction']}</b>\n"
                        f"💵 Цена: <b>${signal['entry']}</b>\n"
                        f"📉 RSI: {signal['rsi']} | Сила: {signal['strength']}/3\n"
                        f"⏰ {datetime.now().strftime('%H:%M')}\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"🔥 Экспирация: 5 мин\n"
                        f"💡 Ставка: 3-5% депозита\n\n"
                        f"<i>/win или /loss</i>"
                    )
                    try:
                        await bot.send_message(user_id, text, parse_mode='HTML')
                    except:
                        pass
                    break

# ===== ЗАПУСК =====
async def main():
    print("=" * 50)
    print("🚀 БОТ ДЛЯ POCKET OPTION ЗАПУЩЕН НА RENDER!")
    print(f"👑 Админ: {ADMIN_ID} (бесплатно)")
    print(f"📊 Источник: Bybit")
    print(f"📊 Пары: {', '.join(PAIRS)}")
    print(f"💰 Цены: Юмани {PRICE_YOOMONEY}₽ | USDT {PRICE_USDT} | Stars {PRICE_STARS}")
    print(f"🎁 Демо: {DEMO_DAYS} дня")
    print(f"👥 Рефералка: {REFERRAL_BONUS}%")
    print("=" * 50)
    
    asyncio.create_task(auto_signals())
    await dp.start_polling(bot)

# Эта функция нужна для Render, чтобы он не ругался
def start():
    """Точка входа для Render"""
    asyncio.run(main())

if __name__ == "__main__":
    start()
