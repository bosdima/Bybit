import os
import sqlite3
import logging
import json
import re
from datetime import datetime
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')

# ID разрешенного пользователя и chat_id для уведомлений
ALLOWED_USER = "@bosdima"
ALLOWED_CHAT_ID = None

# Файл настроек
SETTINGS_FILE = 'bot_settings.json'
DB_FILE = 'dca_data.db'
DB_EXPORT_FILE = 'dca_data_export.json'

# Состояния
MAIN_MENU = 0
SETTINGS_MENU = 1
WAITING_PRICE = 2
WAITING_AMOUNT = 3
EDITING_PRICE = 4
EDITING_AMOUNT = 5
EDITING_DATE = 6
WAITING_ALERT_PERCENT = 7
WAITING_ALERT_INTERVAL = 8
WAITING_IMPORT_FILE = 9

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Глобальные настройки
bot_settings = {}

def load_settings():
    default_settings = {
        'alert_percent': 10.0,
        'alert_interval_minutes': 30,
        'notifications_enabled': True
    }
    
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                default_settings.update(loaded)
    except Exception as e:
        logger.error(f"Ошибка загрузки настроек: {e}")
    
    return default_settings

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")
        return False

def init_bybit():
    try:
        session = HTTP(testnet=False, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
        session.get_wallet_balance(accountType="UNIFIED")
        return session, True, "Bybit API работает"
    except Exception as e:
        return None, False, f"Ошибка Bybit API: {str(e)}"

def init_database():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS dca_purchases
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      date TEXT NOT NULL,
                      price REAL NOT NULL,
                      amount REAL NOT NULL,
                      coin TEXT DEFAULT 'TON')''')
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
        return False

def export_database():
    try:
        purchases = get_all_purchases()
        export_data = {
            'export_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_records': len(purchases),
            'purchases': [
                {
                    'id': p[0],
                    'date': p[1],
                    'price': p[2],
                    'amount': p[3],
                    'coin': p[4]
                } for p in purchases
            ]
        }
        
        with open(DB_EXPORT_FILE, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        
        return True, len(purchases)
    except Exception as e:
        logger.error(f"Ошибка экспорта базы: {e}")
        return False, 0

def import_database_from_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        purchases = data.get('purchases', [])
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM dca_purchases")
        
        for p in purchases:
            c.execute(
                "INSERT INTO dca_purchases (date, price, amount, coin) VALUES (?, ?, ?, ?)",
                (p['date'], p['price'], p['amount'], p['coin'])
            )
        
        conn.commit()
        conn.close()
        
        reindex_database()
        
        return True, len(purchases)
    except Exception as e:
        logger.error(f"Ошибка импорта базы: {e}")
        return False, str(e)

def reindex_database():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Получаем записи отсортированные по дате (и времени)
        c.execute("SELECT date, price, amount, coin FROM dca_purchases ORDER BY date ASC")
        records = c.fetchall()
        
        c.execute("DROP TABLE dca_purchases")
        
        c.execute('''CREATE TABLE dca_purchases
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      date TEXT NOT NULL,
                      price REAL NOT NULL,
                      amount REAL NOT NULL,
                      coin TEXT DEFAULT 'TON')''')
        
        for record in records:
            c.execute(
                "INSERT INTO dca_purchases (date, price, amount, coin) VALUES (?, ?, ?, ?)",
                record
            )
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Ошибка переиндексации: {e}")
        return False

def add_purchase(price, amount, coin='TON', date=None):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO dca_purchases (date, price, amount, coin) VALUES (?, ?, ?, ?)",
                  (date, price, amount, coin))
        conn.commit()
        purchase_id = c.lastrowid
        conn.close()
        
        # После добавления переиндексируем, чтобы ID шли по порядку дат
        reindex_database()
        
        return purchase_id
    except Exception as e:
        logger.error(f"Ошибка добавления покупки: {e}")
        return None

def get_all_purchases():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # Сортируем по дате для правильного отображения порядка
        c.execute("SELECT id, date, price, amount, coin FROM dca_purchases ORDER BY date ASC")
        purchases = c.fetchall()
        conn.close()
        return purchases
    except Exception as e:
        logger.error(f"Ошибка получения покупок: {e}")
        return []

def get_purchase_by_id(purchase_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT id, date, price, amount, coin FROM dca_purchases WHERE id=?", (purchase_id,))
        purchase = c.fetchone()
        conn.close()
        return purchase
    except Exception as e:
        logger.error(f"Ошибка получения покупки: {e}")
        return None

def update_purchase(purchase_id, date, price, amount):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE dca_purchases SET date=?, price=?, amount=? WHERE id=?",
                  (date, price, amount, purchase_id))
        conn.commit()
        conn.close()
        
        # После обновления даты переиндексируем, чтобы ID шли по порядку дат
        reindex_database()
        
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления покупки: {e}")
        return False

def delete_purchase(purchase_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM dca_purchases WHERE id=?", (purchase_id,))
        conn.commit()
        conn.close()
        reindex_database()
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления покупки: {e}")
        return False

def format_date(date_str):
    """Форматирует дату из YYYY-MM-DD HH:MM:SS в DD.MM.YYYY"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m.%Y")
    except:
        try:
            # Если только дата без времени
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.strftime("%d.%m.%Y")
        except:
            return date_str

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("💰 Мой Портфель"), KeyboardButton("📊 СТАТИСТИКА DCA")],
        [KeyboardButton("➕ Добавить покупку DCA"), KeyboardButton("✏️ Редактировать покупки")],
        [KeyboardButton("⚙️ Настройки")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_settings_keyboard():
    keyboard = [
        [KeyboardButton("📊 Процент для уведомления"), KeyboardButton("⏱ Частота проверки")],
        [KeyboardButton("🔔 Вкл/Выкл уведомления"), KeyboardButton("📋 Текущие настройки")],
        [KeyboardButton("📤 Экспорт базы"), KeyboardButton("📥 Импорт базы")],
        [KeyboardButton("🏠 Главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_edit_keyboard():
    keyboard = [
        [KeyboardButton("💰 Изменить цену"), KeyboardButton("📊 Изменить количество")],
        [KeyboardButton("📅 Изменить дату"), KeyboardButton("❌ Удалить покупку")],
        [KeyboardButton("🔙 Назад к списку"), KeyboardButton("🏠 Главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_cancel_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)

def get_confirm_keyboard():
    keyboard = [
        [KeyboardButton("✅ Да, удалить"), KeyboardButton("❌ Нет, отмена")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_purchases_list_keyboard(purchases):
    keyboard = []
    for p in purchases:
        # Форматируем дату в DD.MM.YYYY для отображения
        date_display = format_date(p[1])
        btn_text = f"ID{p[0]}: {date_display} - {p[3]:.4f} TON по {p[2]:.4f} USDT"
        keyboard.append([KeyboardButton(btn_text)])
    keyboard.append([KeyboardButton("🏠 Главное меню")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def check_user(update: Update) -> bool:
    global ALLOWED_CHAT_ID
    user = update.effective_user
    
    if ALLOWED_CHAT_ID is None and f"@{user.username}" == ALLOWED_USER:
        ALLOWED_CHAT_ID = update.effective_chat.id
        logger.info(f"Chat ID сохранен: {ALLOWED_CHAT_ID}")
    
    if f"@{user.username}" != ALLOWED_USER:
        await update.message.reply_text("⛔ Доступ запрещен")
        return False
    return True

# ============= ОБРАБОТЧИКИ =============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user(update):
        return MAIN_MENU
    
    bybit_session, bybit_status, bybit_message = init_bybit()
    if not bybit_status:
        await update.message.reply_text(f"❌ {bybit_message}")
        return MAIN_MENU
    
    context.bot_data['bybit'] = bybit_session
    
    await update.message.reply_text(
        f"✅ Бот запущен и работает!\n"
        f"Добро пожаловать, {ALLOWED_USER}\n"
        f"Выберите действие:",
        reply_markup=get_main_keyboard()
    )
    return MAIN_MENU

async def show_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        bybit = context.bot_data.get('bybit')
        if not bybit:
            bybit, status, msg = init_bybit()
            if not status:
                await update.message.reply_text(f"❌ {msg}")
                return MAIN_MENU
            context.bot_data['bybit'] = bybit
        
        resp = bybit.get_wallet_balance(accountType="UNIFIED")
        
        usdt_balance = 0
        all_balances = resp['result']['list'][0]['coin']
        
        for coin in all_balances:
            if coin['coin'] == 'USDT':
                usdt_balance = float(coin['walletBalance'])
                break
        
        portfolio_text = "💰 ДЕТАЛЬНЫЙ ПОРТФЕЛЬ\n\n"
        total_value = usdt_balance
        
        portfolio_text += f"📊 Общая стоимость: {total_value:.2f} USDT\n"
        portfolio_text += f"💵 Всего инвестировано: {total_value:.2f} USDT\n"
        portfolio_text += "══════════════════════════════\n"
        
        try:
            ticker_resp = bybit.get_tickers(category="spot", symbol="TONUSDT")
            ton_price = float(ticker_resp['result']['list'][0]['lastPrice'])
            
            for coin in all_balances:
                balance = float(coin['walletBalance'])
                if balance > 0 and coin['coin'] != 'USDT':
                    coin_name = coin['coin']
                    portfolio_text += f"\n{coin_name}\n"
                    portfolio_text += f"Количество монет: {balance:.4f}\n"
                    portfolio_text += f"Средняя цена: {ton_price:.4f} USDT\n"
            
            purchases = get_all_purchases()
            if purchases:
                total_amount = sum(p[3] for p in purchases)
                total_cost = sum(p[2] * p[3] for p in purchases)
                avg_price = total_cost / total_amount if total_amount > 0 else 0
                
                portfolio_text += "══════════════════════════════\n"
                portfolio_text += "📊 DCA Статистика:\n"
                portfolio_text += f"   TON в DCA: {total_amount:.4f}\n"
                portfolio_text += f"   Средняя цена DCA: {avg_price:.4f} USDT\n"
        except Exception as e:
            portfolio_text += f"\nОшибка: {str(e)}"
        
        await update.message.reply_text(portfolio_text, reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await update.message.reply_text(f"Ошибка получения портфеля: {str(e)}")
    
    return MAIN_MENU

async def show_dca_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        bybit = context.bot_data.get('bybit')
        if not bybit:
            bybit, status, msg = init_bybit()
            if not status:
                await update.message.reply_text(f"❌ {msg}")
                return MAIN_MENU
            context.bot_data['bybit'] = bybit
        
        ticker = bybit.get_tickers(category="spot", symbol="TONUSDT")
        current_price = float(ticker['result']['list'][0]['lastPrice'])
        
        purchases = get_all_purchases()
        if not purchases:
            await update.message.reply_text("Нет данных о покупках DCA", reply_markup=get_main_keyboard())
            return MAIN_MENU
        
        total_amount = sum(p[3] for p in purchases)
        total_cost = sum(p[2] * p[3] for p in purchases)
        avg_price = total_cost / total_amount if total_amount > 0 else 0
        current_value = total_amount * current_price
        pnl = current_value - total_cost
        pnl_percent = (pnl / total_cost * 100) if total_cost > 0 else 0
        
        start_date = min(datetime.strptime(p[1], "%Y-%m-%d %H:%M:%S") for p in purchases)
        
        # Получаем настройку процента для уведомлений
        target_percent = bot_settings.get('alert_percent', 10.0)
        
        # Определяем эмодзи в зависимости от знака PnL
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        pnl_sign = "+" if pnl >= 0 else ""
        
        text = f"📊 СТАТИСТИКА DCA\n\n"
        text += f"📅 Начало стратегии: {start_date.strftime('%d.%m.%Y')}\n"
        text += f"💰 Всего куплено: {total_amount:.4f} TON\n"
        text += f"💵 Средняя цена: {avg_price:.4f} USDT\n"
        text += f"📈 Текущая цена: {current_price:.4f} USDT\n"
        text += f"💵 Всего инвестировано: {total_cost:.2f} USDT\n"
        text += f"💰 Текущая стоимость: {current_value:.2f} USDT\n"
        text += f"{pnl_emoji} Текущий PnL: {pnl:.2f} USDT ({pnl_sign}{pnl_percent:.2f}%)\n"
        text += f"📊 Всего сделок: {len(purchases)}\n"
        
        # Рассчитываем цену для достижения целевого процента
        target_price = avg_price * (1 + target_percent / 100)
        target_value = total_amount * target_price
        target_pnl = target_value - total_cost
        
        # Рекомендации показываем всегда
        if pnl >= 0:
            # Прибыль - показываем фиксацию прибыли по текущей цене
            text += f"\n💡 Рекомендация: Прибыль {pnl_sign}{pnl_percent:.2f}%"
            text += f"\n\n📋 ДЛЯ ФИКСАЦИИ ПРИБЫЛИ:"
            text += f"\nПродать: {total_amount:.4f} TON"
            text += f"\nПо цене: {current_price:.4f} USDT"
            text += f"\nПолучите: {current_value:.2f} USDT"
            text += f"\nПрибыль: {pnl:.2f} USDT"
        else:
            # Убыток - показываем цену для выхода в ноль
            breakeven_price = total_cost / total_amount if total_amount > 0 else 0
            price_needed_percent = ((breakeven_price - current_price) / current_price * 100) if current_price > 0 else 0
            
            text += f"\n💡 Рекомендация: Убыток {pnl_percent:.2f}%"
            text += f"\n\n📋 ДЛЯ ВЫХОДА В НОЛЬ (БЕЗУБЫТОК):"
            text += f"\nПродать: {total_amount:.4f} TON"
            text += f"\nПо цене: {breakeven_price:.4f} USDT"
            text += f"\nПолучите: {total_cost:.2f} USDT (возврат инвестиций)"
            text += f"\nТекущий убыток: {abs(pnl):.2f} USDT"
            text += f"\n\n📈 Необходим рост цены: +{price_needed_percent:.2f}%"
            text += f"\nЦелевая цена: {breakeven_price:.4f} USDT"
        
        # Дополнительная рекомендация для достижения целевого процента из настроек
        text += f"\n\n🎯 ЦЕЛЕВАЯ ПРИБЫЛЬ {target_percent}%:"
        text += f"\nЕсли вы хотите, чтобы ваша позиция вышла на {target_percent}% прибыли,"
        text += f"\nвы должны продать {total_amount:.4f} TON"
        text += f"\nпо цене: {target_price:.4f} USDT"
        text += f"\nПолучите: {target_value:.2f} USDT"
        text += f"\nПрибыль составит: {target_pnl:.2f} USDT"
        
        # Показываем сколько еще нужно роста до целевой цены
        if current_price < target_price:
            growth_needed = ((target_price - current_price) / current_price * 100) if current_price > 0 else 0
            text += f"\n\n📊 До целевой цены осталось: +{growth_needed:.2f}%"
        
        await update.message.reply_text(text, reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await update.message.reply_text(f"Ошибка: {str(e)}")
    
    return MAIN_MENU

# ============= НАСТРОЙКИ =============

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ НАСТРОЙКИ\n\nВыберите параметр:",
        reply_markup=get_settings_keyboard()
    )
    return SETTINGS_MENU

async def show_current_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "✅ Включены" if bot_settings['notifications_enabled'] else "❌ Выключены"
    
    text = (
        f"📋 ТЕКУЩИЕ НАСТРОЙКИ\n\n"
        f"📊 Процент для уведомления: {bot_settings['alert_percent']}%\n"
        f"⏱ Частота проверки: {bot_settings['alert_interval_minutes']} минут\n"
        f"🔔 Уведомления: {status}"
    )
    
    await update.message.reply_text(text, reply_markup=get_settings_keyboard())
    return SETTINGS_MENU

async def toggle_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_settings['notifications_enabled'] = not bot_settings['notifications_enabled']
    save_settings(bot_settings)
    
    status = "✅ включены" if bot_settings['notifications_enabled'] else "❌ выключены"
    await update.message.reply_text(
        f"🔔 Уведомления {status}!",
        reply_markup=get_settings_keyboard()
    )
    return SETTINGS_MENU

async def set_alert_percent_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 Текущий процент: {bot_settings['alert_percent']}%\n\n"
        f"Введите новый процент (например: 5, 10, 15):",
        reply_markup=get_cancel_keyboard()
    )
    return WAITING_ALERT_PERCENT

async def set_alert_percent_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == "❌ Отмена":
        await update.message.reply_text("❌ Отменено", reply_markup=get_settings_keyboard())
        return SETTINGS_MENU
    
    try:
        new_percent = float(text.replace(',', '.'))
        
        if new_percent <= 0:
            await update.message.reply_text(
                "❌ Процент должен быть больше 0:",
                reply_markup=get_cancel_keyboard()
            )
            return WAITING_ALERT_PERCENT
        
        bot_settings['alert_percent'] = new_percent
        save_settings(bot_settings)
        
        await update.message.reply_text(
            f"✅ Процент изменен на {new_percent}%!",
            reply_markup=get_settings_keyboard()
        )
        return SETTINGS_MENU
        
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка! Введите число (например: 10):",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_ALERT_PERCENT

async def set_alert_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⏱ Текущая частота: {bot_settings['alert_interval_minutes']} минут\n\n"
        f"Введите новую частоту в минутах (например: 5, 15, 30, 60):",
        reply_markup=get_cancel_keyboard()
    )
    return WAITING_ALERT_INTERVAL

async def set_alert_interval_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == "❌ Отмена":
        await update.message.reply_text("❌ Отменено", reply_markup=get_settings_keyboard())
        return SETTINGS_MENU
    
    try:
        new_interval = int(float(text.replace(',', '.')))
        
        if new_interval < 1:
            await update.message.reply_text(
                "❌ Интервал должен быть не менее 1 минуты:",
                reply_markup=get_cancel_keyboard()
            )
            return WAITING_ALERT_INTERVAL
        
        bot_settings['alert_interval_minutes'] = new_interval
        save_settings(bot_settings)
        
        await update.message.reply_text(
            f"✅ Частота изменена на {new_interval} минут!\n⚠️ Перезапустите бота для применения.",
            reply_markup=get_settings_keyboard()
        )
        return SETTINGS_MENU
        
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка! Введите целое число (например: 30):",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_ALERT_INTERVAL

async def export_database_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    success, count = export_database()
    
    if success:
        # Отправляем сообщение с информацией
        await update.message.reply_text(
            f"✅ База экспортирована!\n📊 Записей: {count}",
            reply_markup=get_settings_keyboard()
        )
        # Отправляем файл
        try:
            with open(DB_EXPORT_FILE, 'rb') as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=DB_EXPORT_FILE),
                    caption="💾 Файл базы данных для скачивания"
                )
        except Exception as e:
            logger.error(f"Ошибка отправки файла: {e}")
            await update.message.reply_text(
                f"❌ Ошибка отправки файла: {str(e)}",
                reply_markup=get_settings_keyboard()
            )
    else:
        await update.message.reply_text("❌ Ошибка экспорта", reply_markup=get_settings_keyboard())
    
    return SETTINGS_MENU

async def import_database_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 ИМПОРТ БАЗЫ ДАННЫХ\n\n"
        "Отправьте файл базы данных (.json)\n"
        "⚠️ Все текущие записи будут заменены!",
        reply_markup=get_cancel_keyboard()
    )
    return WAITING_IMPORT_FILE

async def import_database_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверяем, есть ли документ
    if not update.message.document:
        await update.message.reply_text(
            "❌ Пожалуйста, отправьте файл .json",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_IMPORT_FILE
    
    document = update.message.document
    
    # Проверяем расширение файла
    if not document.file_name.endswith('.json'):
        await update.message.reply_text(
            "❌ Файл должен быть в формате .json",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_IMPORT_FILE
    
    try:
        # Скачиваем файл
        file = await context.bot.get_file(document.file_id)
        temp_file = f"temp_import_{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        await file.download_to_drive(temp_file)
        
        # Импортируем
        success, result = import_database_from_file(temp_file)
        
        # Удаляем временный файл
        if os.path.exists(temp_file):
            os.remove(temp_file)
        
        if success:
            await update.message.reply_text(
                f"✅ База импортирована!\n📊 Записей: {result}\n💾 Файл: {document.file_name}",
                reply_markup=get_settings_keyboard()
            )
            return SETTINGS_MENU
        else:
            await update.message.reply_text(
                f"❌ Ошибка импорта: {result}",
                reply_markup=get_settings_keyboard()
            )
            return SETTINGS_MENU
            
    except Exception as e:
        logger.error(f"Ошибка при получении файла: {e}")
        await update.message.reply_text(
            f"❌ Ошибка: {str(e)}",
            reply_markup=get_settings_keyboard()
        )
        return SETTINGS_MENU

async def import_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Импорт отменен",
        reply_markup=get_settings_keyboard()
    )
    return SETTINGS_MENU

# ============= РЕДАКТИРОВАНИЕ =============

async def show_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    purchases = get_all_purchases()
    
    if not purchases:
        await update.message.reply_text("Нет покупок для редактирования", reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    await update.message.reply_text(
        "Выберите покупку для редактирования:",
        reply_markup=get_purchases_list_keyboard(purchases)
    )
    return MAIN_MENU

async def edit_purchase_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if not text.startswith("ID"):
        return MAIN_MENU
    
    try:
        purchase_id = int(text.split(":")[0].replace("ID", ""))
        purchase = get_purchase_by_id(purchase_id)
        
        if not purchase:
            await update.message.reply_text("Покупка не найдена", reply_markup=get_main_keyboard())
            return MAIN_MENU
        
        context.user_data['editing_purchase_id'] = purchase_id
        
        # Форматируем дату в DD.MM.YYYY для отображения
        date_display = format_date(purchase[1])
        
        await update.message.reply_text(
            f"✏️ РЕДАКТИРОВАНИЕ ID: {purchase_id}\n\n"
            f"📅 Дата: {date_display}\n"
            f"💰 Цена: {purchase[2]:.4f} USDT\n"
            f"📊 Количество: {purchase[3]:.4f} TON\n\n"
            f"Выберите действие:",
            reply_markup=get_edit_keyboard()
        )
        return MAIN_MENU
        
    except (ValueError, IndexError):
        await update.message.reply_text("Ошибка выбора", reply_markup=get_main_keyboard())
        return MAIN_MENU

async def edit_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите новую цену в USDT (например: 2.5):",
        reply_markup=get_cancel_keyboard()
    )
    return EDITING_PRICE

async def edit_price_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == "❌ Отмена":
        await cancel_to_edit_menu(update, context)
        return MAIN_MENU
    
    try:
        new_price = float(text.replace(',', '.'))
        purchase_id = context.user_data.get('editing_purchase_id')
        
        if not purchase_id:
            await update.message.reply_text("Ошибка", reply_markup=get_main_keyboard())
            return MAIN_MENU
        
        purchase = get_purchase_by_id(purchase_id)
        if not purchase:
            await update.message.reply_text("Покупка не найдена", reply_markup=get_main_keyboard())
            return MAIN_MENU
        
        if update_purchase(purchase_id, purchase[1], new_price, purchase[3]):
            await update.message.reply_text(f"✅ Цена обновлена: {new_price:.4f} USDT")
        else:
            await update.message.reply_text("❌ Ошибка при обновлении")
        
        await show_purchase_after_edit(update, context, purchase_id)
        return MAIN_MENU
        
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка! Введите число:",
            reply_markup=get_cancel_keyboard()
        )
        return EDITING_PRICE

async def edit_amount_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите новое количество TON (например: 10.5):",
        reply_markup=get_cancel_keyboard()
    )
    return EDITING_AMOUNT

async def edit_amount_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == "❌ Отмена":
        await cancel_to_edit_menu(update, context)
        return MAIN_MENU
    
    try:
        new_amount = float(text.replace(',', '.'))
        purchase_id = context.user_data.get('editing_purchase_id')
        
        if not purchase_id:
            await update.message.reply_text("Ошибка", reply_markup=get_main_keyboard())
            return MAIN_MENU
        
        purchase = get_purchase_by_id(purchase_id)
        if not purchase:
            await update.message.reply_text("Покупка не найдена", reply_markup=get_main_keyboard())
            return MAIN_MENU
        
        if update_purchase(purchase_id, purchase[1], purchase[2], new_amount):
            await update.message.reply_text(f"✅ Количество обновлено: {new_amount:.4f} TON")
        else:
            await update.message.reply_text("❌ Ошибка при обновлении")
        
        await show_purchase_after_edit(update, context, purchase_id)
        return MAIN_MENU
        
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка! Введите число:",
            reply_markup=get_cancel_keyboard()
        )
        return EDITING_AMOUNT

def parse_date(date_str):
    date_str = date_str.strip()
    current_year = datetime.now().year
    
    patterns = [
        (r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        (r'^(\d{1,2})\.(\d{1,2})\.(\d{2})$', lambda m: (int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3)))),
        (r'^(\d{1,2})-(\d{1,2})-(\d{4})$', lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        (r'^(\d{1,2})-(\d{1,2})-(\d{2})$', lambda m: (int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3)))),
        (r'^(\d{1,2})\.(\d{1,2})$', lambda m: (int(m.group(1)), int(m.group(2)), current_year)),
        (r'^(\d{1,2})-(\d{1,2})$', lambda m: (int(m.group(1)), int(m.group(2)), current_year)),
    ]
    
    for pattern, extractor in patterns:
        match = re.match(pattern, date_str)
        if match:
            day, month, year = extractor(match)
            try:
                dt = datetime(year, month, day)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                raise ValueError(f"Некорректная дата: {day}.{month}.{year}")
    
    raise ValueError("Неподдерживаемый формат даты")

async def edit_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    purchase_id = context.user_data.get('editing_purchase_id')
    purchase = get_purchase_by_id(purchase_id)
    
    # Показываем текущую дату в формате DD.MM.YYYY
    current_date = format_date(purchase[1]) if purchase else "неизвестно"
    
    await update.message.reply_text(
        f"📅 Текущая дата: {current_date}\n\n"
        f"Введите новую дату в формате ДД.ММ.ГГГГ\n"
        f"(например: 01.01.2024 или 01.01.24 или 01.01):",
        reply_markup=get_cancel_keyboard()
    )
    return EDITING_DATE

async def edit_date_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == "❌ Отмена":
        await cancel_to_edit_menu(update, context)
        return MAIN_MENU
    
    try:
        new_date = parse_date(text)
        
        purchase_id = context.user_data.get('editing_purchase_id')
        if not purchase_id:
            await update.message.reply_text("Ошибка", reply_markup=get_main_keyboard())
            return MAIN_MENU
        
        purchase = get_purchase_by_id(purchase_id)
        if not purchase:
            await update.message.reply_text("Покупка не найдена", reply_markup=get_main_keyboard())
            return MAIN_MENU
        
        # Сохраняем время из старой даты
        old_time = purchase[1][11:] if len(purchase[1]) > 10 else "00:00:00"
        new_date_with_time = f"{new_date} {old_time}"
        
        if update_purchase(purchase_id, new_date_with_time, purchase[2], purchase[3]):
            # Форматируем для отображения
            display_date = format_date(new_date_with_time)
            await update.message.reply_text(f"✅ Дата обновлена: {display_date}")
        else:
            await update.message.reply_text("❌ Ошибка при обновлении")
        
        await show_purchase_after_edit(update, context, purchase_id)
        return MAIN_MENU
        
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Ошибка! {str(e)}\nПопробуйте снова:",
            reply_markup=get_cancel_keyboard()
        )
        return EDITING_DATE

async def delete_purchase_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ Удалить эту покупку?",
        reply_markup=get_confirm_keyboard()
    )
    return MAIN_MENU

async def delete_purchase_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text == "❌ Нет, отмена":
        purchase_id = context.user_data.get('editing_purchase_id')
        await show_purchase_after_edit(update, context, purchase_id)
        return MAIN_MENU
    
    if text == "✅ Да, удалить":
        purchase_id = context.user_data.get('editing_purchase_id')
        
        if purchase_id and delete_purchase(purchase_id):
            await update.message.reply_text(
                "✅ Покупка удалена! Нумерация обновлена.",
                reply_markup=get_main_keyboard()
            )
        else:
            await update.message.reply_text("❌ Ошибка при удалении", reply_markup=get_main_keyboard())
        
        context.user_data.pop('editing_purchase_id', None)
    
    return MAIN_MENU

async def show_purchase_after_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, purchase_id):
    purchase = get_purchase_by_id(purchase_id)
    
    if not purchase:
        await update.message.reply_text("Покупка не найдена", reply_markup=get_main_keyboard())
        return
    
    # Форматируем дату в DD.MM.YYYY
    date_display = format_date(purchase[1])
    
    await update.message.reply_text(
        f"✏️ РЕДАКТИРОВАНИЕ ID: {purchase_id}\n\n"
        f"📅 Дата: {date_display}\n"
        f"💰 Цена: {purchase[2]:.4f} USDT\n"
        f"📊 Количество: {purchase[3]:.4f} TON\n\n"
        f"Выберите действие:",
        reply_markup=get_edit_keyboard()
    )

async def cancel_to_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    purchase_id = context.user_data.get('editing_purchase_id')
    if purchase_id:
        await show_purchase_after_edit(update, context, purchase_id)
    else:
        await update.message.reply_text("❌ Отменено", reply_markup=get_main_keyboard())

# ============= ДОБАВЛЕНИЕ ПОКУПКИ =============

async def add_dca_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "➕ ДОБАВЛЕНИЕ ПОКУПКИ DCA\n\nВведите цену покупки TON в USDT\n(например: 2.5):",
        reply_markup=get_cancel_keyboard()
    )
    return WAITING_PRICE

async def add_dca_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == "❌ Отмена":
        await update.message.reply_text("❌ Отменено", reply_markup=get_main_keyboard())
        return MAIN_MENU
    
    try:
        price = float(text.replace(',', '.'))
        
        if price <= 0:
            await update.message.reply_text(
                "❌ Цена должна быть больше 0:",
                reply_markup=get_cancel_keyboard()
            )
            return WAITING_PRICE
        
        context.user_data['price'] = price
        
        await update.message.reply_text(
            f"✅ Цена {price} USDT принята!\n\nВведите количество TON\n(например: 10.5):",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_AMOUNT
        
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка! Введите число:",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_PRICE

async def add_dca_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text == "❌ Отмена":
        await update.message.reply_text("❌ Отменено", reply_markup=get_main_keyboard())
        context.user_data.clear()
        return MAIN_MENU
    
    try:
        amount = float(text.replace(',', '.'))
        
        if amount <= 0:
            await update.message.reply_text(
                "❌ Количество должно быть больше 0:",
                reply_markup=get_cancel_keyboard()
            )
            return WAITING_AMOUNT
        
        price = context.user_data.get('price')
        if not price:
            await update.message.reply_text("Ошибка! Начните заново /start", reply_markup=get_main_keyboard())
            return MAIN_MENU
        
        purchase_id = add_purchase(price, amount)
        
        if purchase_id:
            # Форматируем дату для отображения
            display_date = datetime.now().strftime('%d.%m.%Y')
            await update.message.reply_text(
                f"✅ ПОКУПКА ДОБАВЛЕНА!\n\n"
                f"ID: {purchase_id}\n"
                f"Цена: {price} USDT\n"
                f"Количество: {amount} TON\n"
                f"Дата: {display_date}"
            )
        else:
            await update.message.reply_text("❌ Ошибка сохранения")
        
        context.user_data.clear()
        
        await update.message.reply_text("Главное меню:", reply_markup=get_main_keyboard())
        return MAIN_MENU
        
    except ValueError:
        await update.message.reply_text(
            "❌ Ошибка! Введите число:",
            reply_markup=get_cancel_keyboard()
        )
        return WAITING_AMOUNT

# ============= ФОНОВАЯ ЗАДАЧА =============

async def check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_CHAT_ID, bot_settings
    
    try:
        if not bot_settings.get('notifications_enabled', True):
            return
        
        if ALLOWED_CHAT_ID is None:
            return
        
        bybit = context.bot_data.get('bybit')
        if not bybit:
            bybit, status, _ = init_bybit()
            if not status:
                return
            context.bot_data['bybit'] = bybit
        
        ticker = bybit.get_tickers(category="spot", symbol="TONUSDT")
        current_price = float(ticker['result']['list'][0]['lastPrice'])
        
        purchases = get_all_purchases()
        if not purchases:
            return
        
        total_amount = sum(p[3] for p in purchases)
        total_cost = sum(p[2] * p[3] for p in purchases)
        avg_price = total_cost / total_amount if total_amount > 0 else 0
        
        if avg_price <= 0:
            return
        
        price_change_percent = ((current_price - avg_price) / avg_price) * 100
        alert_percent = bot_settings.get('alert_percent', 10.0)
        
        if price_change_percent >= alert_percent:
            current_value = total_amount * current_price
            pnl = current_value - total_cost
            
            message = (
                f"🚨 ВНИМАНИЕ! Цена выросла на {price_change_percent:.2f}%\n\n"
                f"📊 Средняя цена покупки DCA: {avg_price:.4f} USDT\n"
                f"📈 Текущая цена: {current_price:.4f} USDT\n"
                f"💰 Количество монет: {total_amount:.4f} TON\n"
                f"💵 Прибыль: {pnl:.2f} USDT\n\n"
                f"📋 ДЛЯ ФИКСАЦИИ ПРИБЫЛИ:\n"
                f"Продать: {total_amount:.4f} TON\n"
                f"По цене: {current_price:.4f} USDT\n"
                f"Получите: {current_value:.2f} USDT"
            )
            
            try:
                await context.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=message)
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")
                
    except Exception as e:
        logger.error(f"Ошибка в check_price_alerts: {e}")

# ============= ОСНОВНАЯ ФУНКЦИЯ =============

def main():
    print("=" * 50)
    print("ЗАПУСК БОТА")
    print("=" * 50)
    
    global bot_settings
    bot_settings = load_settings()
    
    init_database()
    
    bybit_session, bybit_status, bybit_message = init_bybit()
    if not bybit_status:
        print(f"❌ {bybit_message}")
        return
    print("✅ Bybit API OK")
    
    if not TELEGRAM_TOKEN:
        print("❌ Нет TELEGRAM_TOKEN")
        return
    print("✅ Telegram токен OK")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.bot_data['bybit'] = bybit_session
    
    # Главный ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.Regex("^⚙️ Настройки$"), show_settings),
                MessageHandler(filters.Regex("^💰 Мой Портфель$"), show_portfolio),
                MessageHandler(filters.Regex("^📊 СТАТИСТИКА DCA$"), show_dca_stats),
                MessageHandler(filters.Regex("^➕ Добавить покупку DCA$"), add_dca_start),
                MessageHandler(filters.Regex("^✏️ Редактировать покупки$"), show_edit_menu),
                MessageHandler(filters.Regex("^ID\\d+:"), edit_purchase_selected),
                MessageHandler(filters.Regex("^💰 Изменить цену$"), edit_price_start),
                MessageHandler(filters.Regex("^📊 Изменить количество$"), edit_amount_start),
                MessageHandler(filters.Regex("^📅 Изменить дату$"), edit_date_start),
                MessageHandler(filters.Regex("^❌ Удалить покупку$"), delete_purchase_confirm),
                MessageHandler(filters.Regex("^✅ Да, удалить$"), delete_purchase_execute),
                MessageHandler(filters.Regex("^❌ Нет, отмена$"), delete_purchase_execute),
                MessageHandler(filters.Regex("^🔙 Назад к списку$"), show_edit_menu),
                MessageHandler(filters.Regex("^🏠 Главное меню$"), start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, start),
            ],
            SETTINGS_MENU: [
                MessageHandler(filters.Regex("^📊 Процент для уведомления$"), set_alert_percent_start),
                MessageHandler(filters.Regex("^⏱ Частота проверки$"), set_alert_interval_start),
                MessageHandler(filters.Regex("^🔔 Вкл/Выкл уведомления$"), toggle_notifications),
                MessageHandler(filters.Regex("^📋 Текущие настройки$"), show_current_settings),
                MessageHandler(filters.Regex("^📤 Экспорт базы$"), export_database_handler),
                MessageHandler(filters.Regex("^📥 Импорт базы$"), import_database_start),
                MessageHandler(filters.Regex("^🏠 Главное меню$"), start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, show_settings),
            ],
            WAITING_ALERT_PERCENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_alert_percent_save),
            ],
            WAITING_ALERT_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_alert_interval_save),
            ],
            WAITING_IMPORT_FILE: [
                MessageHandler(filters.Document.FileExtension("json"), import_database_receive),
                MessageHandler(filters.Regex("^❌ Отмена$"), import_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, import_database_receive),
            ],
            WAITING_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_dca_price),
            ],
            WAITING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_dca_amount),
            ],
            EDITING_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_price_save),
            ],
            EDITING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_amount_save),
            ],
            EDITING_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_date_save),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        name="main_conv",
        persistent=False,
    )
    
    application.add_handler(conv_handler)
    
    # Фоновая задача
    job_queue = application.job_queue
    if job_queue:
        interval_seconds = bot_settings.get('alert_interval_minutes', 30) * 60
        job_queue.run_repeating(check_price_alerts, interval=interval_seconds, first=10)
        print(f"✅ Фоновая задача запущена")
    
    print("=" * 50)
    print("🚀 Бот запущен!")
    print("=" * 50)
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()