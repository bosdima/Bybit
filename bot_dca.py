#!/usr/bin/env python3
"""
DCA Bybit Trading Bot - МАРТИНГЕЙЛ ЛЕСЕНКОЙ
Версия 5.4.1 (04.05.2026)
"""

import os
import sys
import asyncio
import logging
import json
import sqlite3
import re
import time
import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from colorama import init, Fore, Style
from logging.handlers import RotatingFileHandler

try:
    import pytz
except ImportError:
    os.system(f"{sys.executable} -m pip install pytz")
    import pytz

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from telegram.request import HTTPXRequest
from pybit.unified_trading import HTTP

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

init(autoreset=True)
load_dotenv()

# Настройка логов
log_handler = RotatingFileHandler("bot_errors.log", encoding='utf-8', maxBytes=200*1024, backupCount=2)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    handlers=[log_handler, logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AUTHORIZED_USER = os.getenv('AUTHORIZED_USER', '@bosdima')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
BYBIT_TESTNET_DEFAULT = os.getenv('BYBIT_TESTNET', 'false').lower() == 'true'

BOT_VERSION = "5.4.1 (04.05.2026)"
CONVERSATION_TIMEOUT = 180
MIN_ORDER_AMOUNT = 5.0

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

def get_moscow_time() -> datetime:
    return datetime.now(MOSCOW_TZ)

def get_moscow_time_naive() -> datetime:
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)

# Состояния
(
    SELECTING_ACTION,
    SET_SYMBOL,
    SET_SYMBOL_MANUAL,
    SET_AMOUNT,
    SET_PROFIT_PERCENT,
    SET_MAX_DROP,
    SET_SCHEDULE_TIME,
    SET_FREQUENCY_HOURS,
    MANAGE_ORDERS,
    EDIT_ORDER_PRICE,
    MANUAL_BUY_PRICE,
    MANUAL_BUY_AMOUNT,
    MANUAL_ADD_PRICE,
    MANUAL_ADD_AMOUNT,
    EDIT_PURCHASE_SELECT,
    EDIT_PRICE,
    EDIT_AMOUNT,
    EDIT_DATE,
    DELETE_CONFIRM,
    SETTINGS_MENU,
    NOTIFICATION_SETTINGS_MENU,
    WAITING_ALERT_PERCENT,
    WAITING_ALERT_INTERVAL,
    WAITING_IMPORT_FILE,
    SELECTING_SYMBOL,
    LADDER_MENU,
    SET_LADDER_DEPTH,
    SET_LADDER_BASE_AMOUNT,
    MANUAL_ADD_RECOMMENDATION,
    WAITING_ORDER_CHECK_INTERVAL,
    WAITING_ORDER_ID_TO_CANCEL,
    WAITING_SELL_CONFIRMATION,
    WAITING_CLEAR_STATS_CONFIRMATION,
    WAITING_PURCHASE_NOTIFY_TIME,
    AUTO_DCA_SETTINGS,
    SET_MANUAL_AMOUNT,
) = range(36)

DB_EXPORT_FILE = 'dca_data_export.json'
POPULAR_SYMBOLS = ["TONUSDT", "BTCUSDT", "ETHUSDT"]
MAX_DROP_DEPTH = 80

MAIN_MENU_BUTTONS = [
    "📊 Мой Портфель", "🚀 Запустить Авто DCA", "⏹ Остановить Авто DCA",
    "💰 Ручная покупка (лимит)", "📈 Статистика DCA", "➕ Добавить покупку вручную",
    "✏️ Редактировать покупки", "⚙️ Настройки", "📋 Статус бота",
    "📝 Управление ордерами", "✅ Отслеживание ордеров Вкл", "⏳ Отслеживание ордеров Выкл",
    "💰 Отслеживание продаж Вкл", "⏳ Отслеживание продаж Выкл", "🏠 Главное меню",
    "🔙 Назад в меню", "🔙 Назад в настройки", "🔙 Назад к списку", "❌ Отмена"
]

def format_price(price: float, decimals: int = 4) -> str:
    if price is None: return "N/A"
    return f"{price:.{decimals}f}"

def format_quantity(qty: float, decimals: int = 2) -> str:
    if qty is None: return "N/A"
    return f"{qty:.{decimals}f}"

def round_price_up(price: float) -> float:
    return math.ceil(price * 100) / 100

def calculate_current_drop(current_price: float, avg_price: float) -> float:
    if avg_price <= 0: return 0
    drop = ((avg_price - current_price) / avg_price) * 100
    return max(0, drop)


class Database:
    def __init__(self, db_file: str = "dca_bot.db"):
        self.db_file = db_file
        self.init_db()
    
    def init_db(self):
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dca_purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    amount_usdt REAL NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    multiplier REAL DEFAULT 1.0,
                    drop_percent REAL DEFAULT 0,
                    step_level INTEGER DEFAULT 0,
                    date TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    order_id TEXT
                )
            ''')
            
            cursor.execute("PRAGMA table_info(dca_purchases)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'order_id' not in columns:
                cursor.execute("ALTER TABLE dca_purchases ADD COLUMN order_id TEXT")
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sell_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    order_id TEXT NOT NULL UNIQUE,
                    quantity REAL NOT NULL,
                    target_price REAL NOT NULL,
                    profit_percent REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'active'
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS pending_sell_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    target_price REAL NOT NULL,
                    profit_percent REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'pending'
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS completed_sells (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    sell_price REAL NOT NULL,
                    profit_percent REAL NOT NULL,
                    profit_usdt REAL NOT NULL,
                    sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified BOOLEAN DEFAULT 0,
                    stats_cleared BOOLEAN DEFAULT 0
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    symbol TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dca_start (
                    id INTEGER PRIMARY KEY,
                    start_date TIMESTAMP,
                    symbol TEXT,
                    initial_price REAL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enabled BOOLEAN DEFAULT 1,
                    alert_percent REAL DEFAULT 10.0,
                    alert_interval_minutes INTEGER DEFAULT 30,
                    last_check TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ladder_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    max_depth REAL NOT NULL,
                    base_amount REAL NOT NULL,
                    max_amount REAL NOT NULL,
                    step_percent REAL DEFAULT 1.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sync_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    sync_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    purchases_deleted INTEGER DEFAULT 0,
                    details TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS executed_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    amount_usdt REAL NOT NULL,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    added_to_stats BOOLEAN DEFAULT 0,
                    skipped BOOLEAN DEFAULT 0,
                    notified_at TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            
            defaults = [
                ('symbol', 'TONUSDT'),
                ('invest_amount', '5.0'),
                ('manual_amount', '1.1'),
                ('profit_percent', '5'),
                ('max_drop_percent', '80'),
                ('max_multiplier', '3'),
                ('schedule_time', '09:00'),
                ('frequency_hours', '24'),
                ('price_alert_enabled', 'false'),
                ('dca_active', 'false'),
                ('last_purchase_price', '0'),
                ('initial_reference_price', '0'),
                ('last_purchase_time', '0'),
                ('ladder_base_amount', '5.0'),
                ('ladder_max_depth', '80'),
                ('ladder_max_amount', '15.0'),
                ('order_execution_notify', 'true'),
                ('order_check_interval_minutes', '5'),
                ('sell_tracking_enabled', 'true'),
                ('purchase_notify_enabled', 'true'),
                ('purchase_notify_time', '06:00'),
                ('last_order_check_time', ''),
                ('last_full_check_time', ''),
                ('last_sell_check_time', ''),
                ('last_purchase_notify_date', ''),
                ('first_order_date', ''),
                ('next_dca_purchase_time', ''),
                ('trading_mode', 'real'),
                ('last_daily_sync_time', ''),
            ]
            
            for key, value in defaults:
                cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
            
            cursor.execute('''
                INSERT OR IGNORE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check)
                VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)
            ''')
            
            conn.commit()
            conn.close()
            logger.info(f"Database initialized successfully")
        except Exception as e:
            logger.error(f"DB init error: {e}")
    
    def get_setting(self, key: str, default: str = '') -> str:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else default
        except Exception:
            return default
    
    def set_setting(self, key: str, value: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (key, value))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting {key}: {e}")
    
    def get_trading_mode(self) -> str:
        return self.get_setting('trading_mode', 'real')
    
    def set_trading_mode(self, mode: str):
        self.set_setting('trading_mode', mode)
    
    def is_demo_mode(self) -> bool:
        return self.get_trading_mode() == 'demo'
    
    def get_first_order_date(self) -> Optional[datetime]:
        date_str = self.get_setting('first_order_date', '')
        if date_str:
            try:
                return datetime.fromisoformat(date_str)
            except:
                return None
        return None
    
    def set_first_order_date(self, date: datetime):
        self.set_setting('first_order_date', date.isoformat())
    
    def update_first_order_date(self):
        purchases = self.get_purchases()
        if purchases:
            first_purchase = min(purchases, key=lambda x: x['date'])
            try:
                first_date = datetime.strptime(first_purchase['date'], "%Y-%m-%d %H:%M:%S")
                self.set_first_order_date(first_date)
            except Exception as e:
                logger.error(f"Error updating first order date: {e}")
        else:
            self.set_setting('first_order_date', '')
    
    def add_purchase(self, symbol: str, amount_usdt: float, price: float, 
                     quantity: float, multiplier: float = 1.0, drop_percent: float = 0,
                     step_level: int = 0, date: str = None, order_id: str = None):
        if date is None:
            date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO dca_purchases 
                (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, order_id))
            purchase_id = cursor.lastrowid
            conn.commit()
            conn.close()
            self.update_first_order_date()
            logger.info("Покупка добавлена")
            return purchase_id
        except Exception as e:
            logger.error(f"Error adding purchase: {e}")
            return None
    
    def get_purchases(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM dca_purchases WHERE symbol = ? ORDER BY date ASC', (symbol,))
            else:
                cursor.execute('SELECT * FROM dca_purchases ORDER BY date ASC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting purchases: {e}")
            return []
    
    def get_purchase_by_id(self, purchase_id: int) -> Optional[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dca_purchases WHERE id = ?', (purchase_id,))
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting purchase {purchase_id}: {e}")
            return None
    
    def delete_purchase(self, purchase_id: int) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE id = ?', (purchase_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            if success:
                self.update_first_order_date()
            return success
        except Exception as e:
            logger.error(f"Error deleting purchase {purchase_id}: {e}")
            return False
    
    def get_dca_stats(self, symbol: str) -> Dict:
        purchases = self.get_purchases(symbol)
        if not purchases:
            return None
        total_usdt = sum(p['amount_usdt'] for p in purchases)
        total_qty = sum(p['quantity'] for p in purchases)
        avg_price = total_usdt / total_qty if total_qty > 0 else 0
        return {
            'total_purchases': len(purchases),
            'total_usdt': total_usdt,
            'total_quantity': total_qty,
            'avg_price': avg_price,
        }
    
    def add_sell_order(self, symbol: str, order_id: str, quantity: float, target_price: float, profit_percent: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sell_orders (symbol, order_id, quantity, target_price, profit_percent)
                VALUES (?, ?, ?, ?, ?)
            ''', (symbol, order_id, quantity, target_price, profit_percent))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error adding sell order: {e}")
    
    def get_active_sell_orders(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM sell_orders WHERE symbol = ? AND status = "active" ORDER BY created_at DESC', (symbol,))
            else:
                cursor.execute('SELECT * FROM sell_orders WHERE status = "active" ORDER BY created_at DESC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting active sell orders: {e}")
            return []
    
    def update_sell_order_status(self, order_id: str, status: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE sell_orders SET status = ? WHERE order_id = ?', (status, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating order status: {e}")
    
    def add_completed_sell(self, symbol: str, order_id: str, quantity: float, 
                           sell_price: float, profit_percent: float, profit_usdt: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO completed_sells (symbol, order_id, quantity, sell_price, profit_percent, profit_usdt, notified, stats_cleared)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0)
            ''', (symbol, order_id, quantity, sell_price, profit_percent, profit_usdt))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error adding completed sell: {e}")
            return False
    
    def get_completed_sells_not_notified(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM completed_sells WHERE symbol = ? AND notified = 0 ORDER BY sold_at DESC', (symbol,))
            else:
                cursor.execute('SELECT * FROM completed_sells WHERE notified = 0 ORDER BY sold_at DESC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting completed sells: {e}")
            return []
    
    def mark_completed_sell_notified(self, sell_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET notified = 1 WHERE id = ?', (sell_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error marking sell notified: {e}")
    
    def clear_all_purchases(self, symbol: str) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE symbol = ?', (symbol,))
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            self.update_first_order_date()
            return deleted_count
        except Exception as e:
            logger.error(f"Error clearing purchases: {e}")
            return 0
    
    def get_sell_tracking_enabled(self) -> bool:
        return self.get_setting('sell_tracking_enabled', 'true') == 'true'
    
    def set_sell_tracking_enabled(self, enabled: bool):
        self.set_setting('sell_tracking_enabled', 'true' if enabled else 'false')
    
    def get_last_sell_check_time(self) -> Optional[datetime]:
        time_str = self.get_setting('last_sell_check_time', '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str)
            except:
                return None
        return None
    
    def set_last_sell_check_time(self, check_time: datetime):
        self.set_setting('last_sell_check_time', check_time.isoformat())
    
    def get_purchase_notify_enabled(self) -> bool:
        return self.get_setting('purchase_notify_enabled', 'true') == 'true'
    
    def set_purchase_notify_enabled(self, enabled: bool):
        self.set_setting('purchase_notify_enabled', 'true' if enabled else 'false')
    
    def get_purchase_notify_time(self) -> str:
        return self.get_setting('purchase_notify_time', '06:00')
    
    def set_purchase_notify_time(self, notify_time: str):
        self.set_setting('purchase_notify_time', notify_time)
    
    def get_last_purchase_notify_date(self) -> Optional[str]:
        return self.get_setting('last_purchase_notify_date', '')
    
    def set_last_purchase_notify_date(self, date_str: str):
        self.set_setting('last_purchase_notify_date', date_str)
    
    def get_manual_amount(self) -> float:
        return float(self.get_setting('manual_amount', '1.1'))
    
    def set_manual_amount(self, amount: float):
        self.set_setting('manual_amount', str(amount))
    
    def log_action(self, action: str, symbol: str = None, details: str = None):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO history (action, symbol, details) VALUES (?, ?, ?)', (action, symbol, details))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error logging action: {e}")
    
    def get_ladder_settings(self, symbol: str = None) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', 'TONUSDT')
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM ladder_settings WHERE symbol = ? ORDER BY created_at DESC LIMIT 1', (symbol,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return dict(row)
            else:
                return {
                    'symbol': symbol,
                    'max_depth': float(self.get_setting('ladder_max_depth', '80')),
                    'base_amount': float(self.get_setting('invest_amount', '5.0')),
                    'max_amount': float(self.get_setting('invest_amount', '5.0')) * 3,
                    'step_percent': 1.0,
                }
        except Exception as e:
            logger.error(f"Error getting ladder settings: {e}")
            return {
                'symbol': symbol,
                'max_depth': 80,
                'base_amount': 5.0,
                'max_amount': 15.0,
                'step_percent': 1.0,
            }
    
    def get_last_daily_sync_time(self) -> Optional[datetime]:
        time_str = self.get_setting('last_daily_sync_time', '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str)
            except:
                return None
        return None
    
    def set_last_daily_sync_time(self, sync_time: datetime):
        self.set_setting('last_daily_sync_time', sync_time.isoformat())
    
    def add_sync_history(self, symbol: str, purchases_deleted: int, details: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sync_history (symbol, purchases_deleted, details)
                VALUES (?, ?, ?)
            ''', (symbol, purchases_deleted, details))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error adding sync history: {e}")
    
    def add_executed_order(self, order_id: str, symbol: str, price: float, quantity: float, amount_usdt: float, executed_at: str = None) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO executed_orders (order_id, symbol, price, quantity, amount_usdt, executed_at, added_to_stats, skipped, notified_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, NULL)
            ''', (order_id, symbol, price, quantity, amount_usdt, executed_at))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error adding executed order: {e}")
            return False
    
    def mark_order_as_added(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET added_to_stats = 1, notified_at = CURRENT_TIMESTAMP WHERE order_id = ?', (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error marking order as added: {e}")
            return False
    
    def get_order_execution_notify(self) -> bool:
        return self.get_setting('order_execution_notify', 'true') == 'true'
    
    def set_order_execution_notify(self, enabled: bool):
        self.set_setting('order_execution_notify', 'true' if enabled else 'false')
    
    def get_order_check_interval(self) -> int:
        return int(self.get_setting('order_check_interval_minutes', '5'))
    
    def set_order_check_interval(self, minutes: int):
        self.set_setting('order_check_interval_minutes', str(minutes))
    
    def get_last_incremental_check_time(self) -> Optional[datetime]:
        time_str = self.get_setting('last_order_check_time', '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str)
            except:
                return None
        return None
    
    def set_last_incremental_check_time(self, check_time: Optional[datetime]):
        if check_time is None:
            self.set_setting('last_order_check_time', '')
        else:
            self.set_setting('last_order_check_time', check_time.isoformat())
    
    def reset_incremental_check_time(self):
        self.set_last_incremental_check_time(None)
        logger.info("Last incremental check time reset for full rescan")
    
    def get_authorized_user_id(self) -> Optional[int]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM bot_state WHERE key = "authorized_user_id"')
            row = cursor.fetchone()
            conn.close()
            return int(row[0]) if row else None
        except Exception:
            return None
    
    def set_authorized_user_id(self, user_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)', ('authorized_user_id', str(user_id)))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error saving authorized user id: {e}")


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.session = None
        self._price_cache = {}
        self._cache_time = {}
        self._cache_ttl = 5
        self._init_session()
    
    def _init_session(self):
        try:
            self.session = HTTP(testnet=self.testnet, api_key=self.api_key, api_secret=self.api_secret, recv_window=5000)
            logger.info(f"Bybit session initialized (testnet={self.testnet})")
        except Exception as e:
            logger.error(f"Session init error: {e}")
    
    async def get_symbol_price(self, symbol: str) -> Optional[float]:
        now = time.time()
        if symbol in self._cache_time and now - self._cache_time.get(symbol, 0) < self._cache_ttl:
            return self._price_cache.get(symbol)
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_tickers(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                price = float(response['result']['list'][0]['lastPrice'])
                self._price_cache[symbol] = price
                self._cache_time[symbol] = now
                return price
            return None
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None
    
    async def cancel_all_sell_orders(self, symbol: str) -> Tuple[int, List[str]]:
        try:
            open_orders = await self.get_open_orders(symbol)
            sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
            cancelled_ids = []
            for order in sell_orders:
                order_id = order.get('orderId')
                result = await self.cancel_order(symbol, order_id)
                if result['success']:
                    cancelled_ids.append(order_id)
                    logger.info(f"Cancelled sell order {order_id} for {symbol}")
                else:
                    logger.warning(f"Failed to cancel order {order_id}: {result.get('error')}")
            return len(cancelled_ids), cancelled_ids
        except Exception as e:
            logger.error(f"Error cancelling sell orders: {e}")
            return 0, []
    
    async def get_balance(self, coin: str = None) -> Dict:
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_wallet_balance(accountType="UNIFIED")
            if response['retCode'] == 0:
                result_list = response['result']['list']
                if result_list:
                    account_data = result_list[0]
                    coins = account_data.get('coin', [])
                    if coin:
                        for c in coins:
                            if c.get('coin') == coin:
                                wallet_balance = float(c.get('walletBalance', 0) or 0)
                                equity = float(c.get('equity', 0) or 0) or wallet_balance
                                locked = float(c.get('locked', 0) or 0)
                                available = wallet_balance - locked
                                usd_value = float(c.get('usdValue', 0) or 0)
                                return {'coin': coin, 'equity': equity, 'available': available, 'usdValue': usd_value}
                        return {'coin': coin, 'equity': 0, 'available': 0, 'usdValue': 0}
                    else:
                        return {'total_equity': float(account_data.get('totalEquity', 0) or 0), 'coins': coins}
            return {'error': 'Не удалось получить баланс'}
        except Exception as e:
            logger.error(f"Error in get_balance: {e}")
            return {'error': str(e)}
    
    async def get_open_orders(self, symbol: str = None) -> List[Dict]:
        try:
            if not self.session:
                self._init_session()
            params = {"category": "spot"}
            if symbol:
                params['symbol'] = symbol
            response = self.session.get_open_orders(**params)
            if response['retCode'] == 0:
                return response['result']['list']
            return []
        except Exception as e:
            logger.error(f"Error getting open orders: {e}")
            return []
    
    async def get_open_orders_by_side(self, symbol: str = None) -> Dict[str, List[Dict]]:
        orders = await self.get_open_orders(symbol)
        buy_orders = [o for o in orders if o.get('side') == 'Buy']
        sell_orders = [o for o in orders if o.get('side') == 'Sell']
        return {'buy': buy_orders, 'sell': sell_orders}
    
    async def get_order_history(self, symbol: str = None, limit: int = 500) -> List[Dict]:
        try:
            if not self.session:
                self._init_session()
            params = {"category": "spot", "limit": limit}
            if symbol:
                params['symbol'] = symbol
            response = self.session.get_order_history(**params)
            if response['retCode'] == 0:
                return response['result']['list']
            return []
        except Exception as e:
            logger.error(f"Error getting order history: {e}")
            return []
    
    async def get_instrument_info(self, symbol: str) -> Dict:
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_instruments_info(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                info = response['result']['list'][0]
                lot_size_filter = info.get('lotSizeFilter', {})
                price_filter = info.get('priceFilter', {})
                tick_size_str = price_filter.get('tickSize', '0.0001')
                tick_size = float(tick_size_str)
                return {
                    'min_qty': float(lot_size_filter.get('minOrderQty', 0.01)),
                    'min_amt': float(lot_size_filter.get('minOrderAmt', 5)),
                    'qty_step': float(lot_size_filter.get('qtyStep', 0.01)),
                    'tick_size': tick_size,
                }
            return {'min_qty': 0.01, 'min_amt': 5, 'qty_step': 0.01, 'tick_size': 0.0001}
        except Exception as e:
            logger.error(f"Error getting instrument info: {e}")
            return {'min_qty': 0.01, 'min_amt': 5, 'qty_step': 0.01, 'tick_size': 0.0001}
    
    def _round_price_by_tick(self, price: float, tick_size: float) -> float:
        if tick_size <= 0:
            return round(price, 4)
        rounded = math.floor(price / tick_size) * tick_size
        if rounded <= 0:
            rounded = tick_size
        return rounded
    
    async def get_all_executed_orders(self, symbol: str, from_date: datetime = None) -> List[Dict]:
        try:
            check_date = from_date if from_date else get_moscow_time_naive() - timedelta(days=90)
            orders = await self.get_order_history(symbol, limit=500)
            executed = []
            for order in orders:
                order_status = order.get('orderStatus', '')
                side = order.get('side', '')
                if order_status in ['Filled', 'PartiallyFilled'] and side == 'Buy':
                    created_time_str = order.get('createdTime', '')
                    if created_time_str:
                        try:
                            created_time_ms = int(created_time_str)
                            created_time = datetime.fromtimestamp(created_time_ms / 1000)
                            if created_time >= check_date:
                                avg_price = float(order.get('avgPrice', 0))
                                if avg_price == 0:
                                    avg_price = float(order.get('price', 0))
                                qty = float(order.get('cumExecQty', 0))
                                if qty == 0:
                                    qty = float(order.get('qty', 0))
                                amount_usdt = float(order.get('cumExecValue', 0))
                                if amount_usdt == 0 and avg_price > 0:
                                    amount_usdt = avg_price * qty
                                if qty > 0 and avg_price > 0:
                                    executed.append({
                                        'order_id': order.get('orderId'),
                                        'symbol': order.get('symbol'),
                                        'price': avg_price,
                                        'quantity': qty,
                                        'amount_usdt': amount_usdt,
                                        'executed_at': created_time,
                                    })
                        except Exception as e:
                            logger.error(f"Error parsing order time: {e}")
                            continue
            return executed
        except Exception as e:
            logger.error(f"Error getting executed orders: {e}")
            return []
    
    async def get_completed_sell_orders(self, symbol: str = None, from_date: datetime = None) -> List[Dict]:
        try:
            check_date = from_date if from_date else get_moscow_time_naive() - timedelta(days=90)
            orders = await self.get_order_history(symbol, limit=500)
            completed = []
            for order in orders:
                order_status = order.get('orderStatus', '')
                side = order.get('side', '')
                if order_status in ['Filled'] and side == 'Sell':
                    created_time_str = order.get('createdTime', '')
                    if created_time_str:
                        try:
                            created_time_ms = int(created_time_str)
                            created_time = datetime.fromtimestamp(created_time_ms / 1000)
                            if created_time >= check_date:
                                avg_price = float(order.get('avgPrice', 0))
                                if avg_price == 0:
                                    avg_price = float(order.get('price', 0))
                                qty = float(order.get('cumExecQty', 0))
                                if qty == 0:
                                    qty = float(order.get('qty', 0))
                                amount_usdt = float(order.get('cumExecValue', 0))
                                if amount_usdt == 0 and avg_price > 0:
                                    amount_usdt = avg_price * qty
                                if qty > 0 and avg_price > 0:
                                    completed.append({
                                        'order_id': order.get('orderId'),
                                        'symbol': order.get('symbol'),
                                        'sell_price': avg_price,
                                        'quantity': qty,
                                        'amount_usdt': amount_usdt,
                                        'executed_at': created_time,
                                    })
                        except Exception as e:
                            logger.error(f"Error parsing order time: {e}")
                            continue
            return completed
        except Exception as e:
            logger.error(f"Error getting completed sell orders: {e}")
            return []
    
    async def cancel_order(self, symbol: str, order_id: str) -> Dict:
        try:
            if not self.session:
                self._init_session()
            response = self.session.cancel_order(category="spot", symbol=symbol, orderId=order_id)
            if response['retCode'] == 0:
                return {'success': True}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        try:
            if not self.session:
                self._init_session()
            instrument_info = await self.get_instrument_info(symbol)
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            qty_step = instrument_info['qty_step']
            tick_size = instrument_info['tick_size']
            
            rounded_price = self._round_price_by_tick(price, tick_size)
            
            qty_decimal = Decimal(str(quantity))
            step_decimal = Decimal(str(qty_step))
            rounded_quantity = float((qty_decimal // step_decimal) * step_decimal)
            
            if rounded_quantity <= 0:
                rounded_quantity = min_qty
            
            if rounded_quantity < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty}'}
            
            order_value = rounded_quantity * rounded_price
            if order_value < min_amt:
                return {'success': False, 'error': 'min_amount_error', 'min_amt': min_amt}
            
            response = self.session.place_order(
                category="spot", symbol=symbol, side="Sell", orderType="Limit",
                qty=str(rounded_quantity), price=str(rounded_price), timeInForce="GTC"
            )
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'], 'quantity': rounded_quantity, 'price': rounded_price}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def place_limit_buy(self, symbol: str, price: float, amount_usdt: float, is_auto: bool = True) -> Dict:
        try:
            if not self.session:
                self._init_session()
            instrument_info = await self.get_instrument_info(symbol)
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            qty_step = instrument_info['qty_step']
            tick_size = instrument_info['tick_size']
            
            rounded_price = self._round_price_by_tick(price, tick_size)
            
            if not is_auto and amount_usdt < min_amt:
                return {'success': False, 'error': f'Сумма {amount_usdt:.2f} USDT меньше минимальной {min_amt} USDT'}
            
            if is_auto and amount_usdt < min_amt:
                amount_usdt = min_amt
                logger.warning(f"Авто DCA: сумма увеличена до минимальной {min_amt} USDT")
            
            quantity = amount_usdt / rounded_price
            qty_decimal = Decimal(str(quantity))
            step_decimal = Decimal(str(qty_step))
            quantity = float((qty_decimal // step_decimal) * step_decimal)
            
            if quantity < min_qty:
                quantity = min_qty
            
            order_value = quantity * rounded_price
            if order_value < min_amt:
                quantity = min_amt / rounded_price
                qty_decimal = Decimal(str(quantity))
                quantity = float((qty_decimal // step_decimal) * step_decimal)
                if quantity < min_qty:
                    quantity = min_qty
            
            response = self.session.place_order(
                category="spot", symbol=symbol, side="Buy", orderType="Limit",
                qty=str(quantity), price=str(rounded_price), timeInForce="GTC"
            )
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'], 'quantity': float(quantity), 'price': rounded_price, 'total_usdt': quantity * rounded_price}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}


class DCAStrategy:
    def __init__(self, db: Database, bybit: BybitClient):
        self.db = db
        self.bybit = bybit
    
    async def cancel_old_sell_orders(self, symbol: str) -> int:
        try:
            open_orders = await self.bybit.get_open_orders(symbol)
            sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
            if not sell_orders:
                return 0
            cancelled_count, cancelled_ids = await self.bybit.cancel_all_sell_orders(symbol)
            for order_id in cancelled_ids:
                self.db.update_sell_order_status(order_id, 'cancelled')
            if cancelled_count > 0:
                await asyncio.sleep(2)
            return cancelled_count
        except Exception as e:
            logger.error(f"Error cancelling old sell orders: {e}")
            return 0
    
    async def sync_purchases_with_exchange(self, symbol: str, user_id: int, bot) -> Dict:
        try:
            first_order_date = self.db.get_first_order_date()
            if first_order_date is None:
                first_order_date = get_moscow_time_naive() - timedelta(days=90)
            
            check_date = first_order_date - timedelta(days=1)
            exchange_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
            
            exchange_order_ids = set()
            for order in exchange_orders:
                if order.get('order_id'):
                    exchange_order_ids.add(order['order_id'])
            
            purchases = self.db.get_purchases(symbol)
            purchases_to_delete = []
            for purchase in purchases:
                purchase_order_id = purchase.get('order_id')
                if purchase_order_id and purchase_order_id not in exchange_order_ids:
                    purchases_to_delete.append(purchase)
            
            if not purchases_to_delete:
                return {'success': True, 'deleted_count': 0}
            
            deleted_count = 0
            for purchase in purchases_to_delete:
                if self.db.delete_purchase(purchase['id']):
                    deleted_count += 1
            
            if deleted_count > 0:
                self.db.add_sync_history(symbol, deleted_count, f"Удалено {deleted_count} покупок")
                if user_id:
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"🔄 *СИНХРОНИЗАЦИЯ*\n\n🗑 Удалено из статистики: `{deleted_count}` покупок\n✅ Статистика соответствует реальному балансу!",
                        parse_mode='Markdown'
                    )
                logger.info(f"Sync: removed {deleted_count} purchases for {symbol}")
            
            return {'success': True, 'deleted_count': deleted_count}
        except Exception as e:
            logger.error(f"Error syncing purchases with exchange: {e}")
            return {'success': False, 'error': str(e)}
    
    async def execute_scheduled_purchase(self, symbol: str, profit_percent: float) -> Dict:
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return {'success': False, 'error': 'Не удалось получить цену'}
        
        stats = self.db.get_dca_stats(symbol)
        settings = self.db.get_ladder_settings(symbol)
        base_amount = settings['base_amount']
        
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_amt = instrument_info['min_amt']
        tick_size = instrument_info['tick_size']
        
        if stats and stats['total_quantity'] > 0 and current_price > stats['avg_price']:
            return {'success': False, 'error': 'skip_price_above_avg'}
        
        if not stats or stats['total_quantity'] <= 0:
            amount_usdt = base_amount
            drop_percent = 0
        else:
            avg_price = stats['avg_price']
            if current_price < avg_price:
                current_drop = calculate_current_drop(current_price, avg_price)
                max_amount = settings['max_amount']
                amount_usdt = base_amount + (max_amount - base_amount) * (current_drop / settings['max_depth'])
                amount_usdt = min(amount_usdt, max_amount)
                drop_percent = current_drop
            else:
                amount_usdt = base_amount
                drop_percent = 0
        
        if amount_usdt < min_amt:
            amount_usdt = min_amt
        
        usdt_balance = await self.bybit.get_balance('USDT')
        available_usdt = usdt_balance.get('available', 0) if usdt_balance else 0
        
        if available_usdt < amount_usdt:
            return {'success': False, 'error': f'Недостаточно средств. Нужно {amount_usdt:.2f} USDT'}
        
        limit_price = current_price * 1.001
        limit_price = (math.floor(limit_price / tick_size) * tick_size) if tick_size > 0 else round(limit_price, 4)
        
        await self.cancel_old_sell_orders(symbol)
        result = await self.bybit.place_limit_buy(symbol, limit_price, amount_usdt, is_auto=True)
        
        if result['success']:
            current_date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
            self.db.add_purchase(
                symbol=symbol, amount_usdt=result['total_usdt'], price=result['price'],
                quantity=result['quantity'], drop_percent=drop_percent, order_id=result.get('order_id')
            )
            await asyncio.sleep(2)
            
            coin = symbol.replace('USDT', '')
            coin_balance = await self.bybit.get_balance(coin)
            actual_qty = coin_balance.get('available', 0) if coin_balance else 0
            quantity_for_sell = min(result['quantity'], actual_qty) if actual_qty > 0 else result['quantity']
            
            if quantity_for_sell > 0:
                target_price_sell = result['price'] * (1 + profit_percent / 100)
                sell_result = await self.bybit.place_limit_sell(symbol, quantity_for_sell, target_price_sell)
                if sell_result['success']:
                    self.db.add_sell_order(symbol, sell_result['order_id'], quantity_for_sell, target_price_sell, profit_percent)
        return result
    
    async def check_pending_sell_orders(self, symbol: str, user_id: int, bot) -> List[Dict]:
        return []
    
    async def check_and_update_sell_orders(self, symbol: str):
        active_orders = self.db.get_active_sell_orders(symbol)
        open_orders = await self.bybit.get_open_orders(symbol)
        open_order_ids = {o['orderId'] for o in open_orders}
        for order in active_orders:
            if order['order_id'] not in open_order_ids:
                self.db.update_sell_order_status(order['order_id'], 'completed')
    
    async def check_completed_sells(self, symbol: str, user_id: int, bot) -> List[Dict]:
        last_check = self.db.get_last_sell_check_time()
        if last_check is None:
            last_check = get_moscow_time_naive() - timedelta(days=30)
        check_date = last_check - timedelta(hours=24)
        
        all_completed = await self.bybit.get_completed_sell_orders(symbol, from_date=check_date)
        self.db.set_last_sell_check_time(get_moscow_time_naive())
        
        already_processed = self.db.get_completed_sells_not_notified(symbol)
        processed_order_ids = set([s['order_id'] for s in already_processed])
        
        new_completed = []
        for sell in all_completed:
            if sell['order_id'] in processed_order_ids:
                continue
            stats = self.db.get_dca_stats(symbol)
            if stats and stats['total_quantity'] > 0:
                avg_price = stats['avg_price']
                profit_percent = ((sell['sell_price'] - avg_price) / avg_price) * 100
                profit_usdt = (sell['sell_price'] - avg_price) * sell['quantity']
            else:
                profit_percent = 0
                profit_usdt = 0
            self.db.add_completed_sell(symbol, sell['order_id'], sell['quantity'], sell['sell_price'], profit_percent, profit_usdt)
            new_completed.append(sell)
        
        for sell in new_completed:
            profit_emoji = "🟢" if sell['profit_usdt'] >= 0 else "🔴"
            msg = (f"💰 *СДЕЛКА ПРОДАНА!*\n\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"📊 Количество: `{format_quantity(sell['quantity'], 2)}`\n"
                   f"💰 Цена продажи: `{format_price(sell['sell_price'], 4)}` USDT\n"
                   f"{profit_emoji} Прибыль: `{sell['profit_usdt']:.2f}` USDT\n\n"
                   f"❗ *Очистить статистику DCA?*")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, очистить", callback_data=f"do_clear_stats_{symbol}"),
                InlineKeyboardButton("❌ Нет", callback_data=f"skip_clear_stats_{symbol}")
            ]])
            try:
                await bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown', reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
        return new_completed
    
    def calculate_target_info(self, stats: Dict, profit_percent: float) -> Dict:
        if not stats or stats['total_quantity'] <= 0:
            return None
        total_qty = stats['total_quantity']
        avg_price = stats['avg_price']
        target_price = avg_price * (1 + profit_percent / 100)
        target_value = total_qty * target_price
        total_cost = stats['total_usdt']
        target_profit = target_value - total_cost
        return {
            'target_price': target_price,
            'target_value': target_value,
            'target_profit': target_profit,
            'total_qty': total_qty,
            'avg_price': avg_price,
        }
    
    async def auto_check_and_notify(self, symbol: str, user_id: int, bot) -> Dict:
        return {'type': 'incremental', 'count': 0, 'orders': []}
    
    async def place_full_sell_order(self, update, symbol: str, profit_percent: float, auto_cancel_old: bool = True) -> Dict:
        try:
            stats = self.db.get_dca_stats(symbol)
            if not stats or stats['total_quantity'] <= 0:
                return {'success': False, 'error': 'Нет купленных активов'}
            
            coin = symbol.replace('USDT', '')
            balance_info = await self.bybit.get_balance(coin)
            available_qty = balance_info.get('available', 0)
            
            if available_qty <= 0:
                return {'success': False, 'error': f'Доступный баланс {coin} равен 0'}
            
            instrument_info = await self.bybit.get_instrument_info(symbol)
            tick_size = instrument_info['tick_size']
            qty_step = instrument_info['qty_step']
            min_qty = instrument_info['min_qty']
            
            avg_price = stats['avg_price']
            raw_target_price = avg_price * (1 + profit_percent / 100)
            rounded_price = (math.floor(raw_target_price / tick_size) * tick_size) if tick_size > 0 else round_price_up(raw_target_price)
            
            qty_decimal = Decimal(str(available_qty))
            step_decimal = Decimal(str(qty_step))
            sell_qty = float((qty_decimal // step_decimal) * step_decimal)
            
            if sell_qty < min_qty:
                return {'success': False, 'error': f'Доступное количество меньше минимального'}
            
            result = await self.bybit.place_limit_sell(symbol, sell_qty, rounded_price)
            if result['success']:
                self.db.add_sell_order(symbol, result['order_id'], sell_qty, rounded_price, profit_percent)
                warning_msg = ""
                if sell_qty < stats['total_quantity']:
                    diff = stats['total_quantity'] - sell_qty
                    warning_msg = f"\n⚠️ Продано только {format_quantity(sell_qty, 2)} из {format_quantity(stats['total_quantity'], 2)} {coin}."
                
                return {
                    'success': True,
                    'order_id': result['order_id'],
                    'quantity': sell_qty,
                    'price': rounded_price,
                    'profit_percent': profit_percent,
                    'warning': warning_msg
                }
            else:
                return {'success': False, 'error': result.get('error', 'Ошибка создания ордера')}
        except Exception as e:
            logger.error(f"Error placing full sell order: {e}")
            return {'success': False, 'error': str(e)}


class FastDCABot:
    def __init__(self):
        self.db = Database()
        self.bybit = None
        self.strategy = None
        self.bybit_initialized = False
        self.import_waiting = False
        self.scheduler_running = False
        self.background_tasks = []
        
        request_kwargs = {'connect_timeout': 60.0, 'read_timeout': 60.0, 'write_timeout': 60.0, 'pool_timeout': 60.0}
        request = HTTPXRequest(**request_kwargs)
        builder = Application.builder().token(TELEGRAM_TOKEN).request(request)
        self.application = builder.build()
        
        self.authorized_user_id = self.db.get_authorized_user_id()
        
        self.setup_handlers()
    
    def _init_bybit(self):
        if not self.bybit_initialized and BYBIT_API_KEY and BYBIT_API_SECRET:
            try:
                testnet = self.db.is_demo_mode()
                self.bybit = BybitClient(BYBIT_API_KEY, BYBIT_API_SECRET, testnet)
                self.strategy = DCAStrategy(self.db, self.bybit)
                self.bybit_initialized = True
                logger.info(f"Bybit client initialized (demo={testnet})")
            except Exception as e:
                logger.error(f"Bybit init error: {e}")
    
    def get_main_keyboard(self):
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        dca_button = "⏹ Остановить Авто DCA" if is_active else "🚀 Запустить Авто DCA"
        keyboard = [
            [KeyboardButton("📊 Мой Портфель"), KeyboardButton(dca_button)],
            [KeyboardButton("💰 Ручная покупка"), KeyboardButton("📈 Статистика DCA")],
            [KeyboardButton("➕ Добавить покупку"), KeyboardButton("✏️ Редактировать покупки")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("📋 Статус бота")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_settings_keyboard(self):
        mode = self.db.get_trading_mode()
        mode_button = "🌐 Режим: Демо" if mode == 'demo' else "🌐 Режим: Обычный"
        manual_amount = self.db.get_manual_amount()
        keyboard = [
            [KeyboardButton("🪙 Выбор токена"), KeyboardButton("🚀 Настройки Авто DCA")],
            [KeyboardButton("📊 Процент прибыли"), KeyboardButton("🪜 Лестница Мартингейла")],
            [KeyboardButton("💵 Сумма ручного ордера"), KeyboardButton("⚙️ Настройки отслеживания")],
            [KeyboardButton("🔔 Уведомления"), KeyboardButton(mode_button)],
            [KeyboardButton("📤 Экспорт базы"), KeyboardButton("📥 Импорт базы")],
            [KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_tracking_settings_keyboard(self):
        current_status = self.db.get_order_execution_notify()
        sell_tracking = self.db.get_sell_tracking_enabled()
        tracking_button = "✅ Отслеживание Вкл" if current_status else "❌ Отслеживание Выкл"
        sell_tracking_button = "💰 Отслеживание продаж Вкл" if sell_tracking else "⏳ Отслеживание продаж Выкл"
        keyboard = [
            [KeyboardButton(tracking_button)],
            [KeyboardButton(sell_tracking_button)],
            [KeyboardButton("🔄 Синхронизация с биржей")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_ladder_settings_keyboard(self):
        keyboard = [
            [KeyboardButton("📉 Глубина просадки"), KeyboardButton("💵 Базовая сумма")],
            [KeyboardButton("📋 Текущие настройки"), KeyboardButton("🔄 Сбросить лестницу")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_symbol_selection_keyboard(self):
        keyboard = [[KeyboardButton(s)] for s in POPULAR_SYMBOLS]
        keyboard.append([KeyboardButton("✏️ Ввести свой")])
        keyboard.append([KeyboardButton("❌ Отмена")])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_edit_purchases_keyboard(self):
        keyboard = [
            [KeyboardButton("💰 Изменить цену"), KeyboardButton("📊 Изменить количество")],
            [KeyboardButton("📅 Изменить дату"), KeyboardButton("❌ Удалить покупку")],
            [KeyboardButton("🔙 Назад к списку"), KeyboardButton("🏠 Главное меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_confirm_delete_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("✅ Да, удалить"), KeyboardButton("❌ Нет, отмена")]], resize_keyboard=True)
    
    def get_cancel_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    
    def get_auto_dca_keyboard(self):
        schedule_time = self.db.get_setting('schedule_time', '09:00')
        invest_amount = self.db.get_setting('invest_amount', '5.0')
        keyboard = [
            [KeyboardButton(f"💵 Сумма ({invest_amount} USDT)")],
            [KeyboardButton(f"⏰ Время ({schedule_time})")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_purchase_notify_settings_keyboard(self):
        enabled = self.db.get_purchase_notify_enabled()
        notify_time = self.db.get_purchase_notify_time()
        status_button = "🔔 Уведомления Вкл" if enabled else "🔕 Уведомления Выкл"
        keyboard = [
            [KeyboardButton(status_button)],
            [KeyboardButton(f"⏰ Время ({notify_time})")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    async def _check_user_fast(self, update: Update) -> bool:
        user = update.effective_user
        username = f"@{user.username}" if user.username else f"ID:{user.id}"
        if self.authorized_user_id is None:
            if username == AUTHORIZED_USER:
                self.authorized_user_id = user.id
                self.db.set_authorized_user_id(user.id)
                return True
        elif user.id == self.authorized_user_id:
            return True
        await update.message.reply_text("⛔ Доступ запрещен")
        return False
    
    async def _reset_bot_state(self, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        self.import_waiting = False
    
    def _calculate_next_purchase_time(self) -> datetime:
        schedule_time_str = self.db.get_setting('schedule_time', '09:00')
        frequency_hours = int(self.db.get_setting('frequency_hours', '24'))
        schedule_hour, schedule_minute = map(int, schedule_time_str.split(':'))
        now = get_moscow_time()
        next_time = now.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
        while next_time <= now:
            next_time += timedelta(hours=frequency_hours)
        return next_time
    
    async def cmd_start_fast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        mode = self.db.get_trading_mode()
        mode_text = "Демо-режим" if mode == 'demo' else "Обычный режим"
        await update.message.reply_text(
            f"👋 Привет!\n\n🤖 DCA Bybit Bot\n📌 Версия: {BOT_VERSION}\n🌐 Режим: {mode_text}\n\nГлавное меню:",
            reply_markup=self.get_main_keyboard()
        )
    
    async def manual_sync_exchange(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return NOTIFICATION_SETTINGS_MENU
        
        await update.message.reply_text("🔄 *Синхронизация...*", parse_mode='Markdown')
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ API не инициализирован")
            return NOTIFICATION_SETTINGS_MENU
        
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        result = await self.strategy.sync_purchases_with_exchange(symbol, self.authorized_user_id, self.application.bot)
        
        if result['success']:
            if result['deleted_count'] > 0:
                await update.message.reply_text(f"✅ Удалено {result['deleted_count']} покупок", reply_markup=self.get_tracking_settings_keyboard())
            else:
                await update.message.reply_text("✅ Расхождений не найдено", reply_markup=self.get_tracking_settings_keyboard())
        else:
            await update.message.reply_text(f"❌ Ошибка: {result.get('error')}", reply_markup=self.get_tracking_settings_keyboard())
        
        return NOTIFICATION_SETTINGS_MENU
    
    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ API не инициализирован")
            return
        
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        coin = symbol.replace('USDT', '')
        coin_balance = await self.bybit.get_balance(coin)
        current_price = await self.bybit.get_symbol_price(symbol)
        
        message = f"📊 *Мой Портфель*\n\n"
        if coin_balance and 'equity' in coin_balance:
            equity = coin_balance['equity']
            message += f"🪙 {coin}: `{format_quantity(equity, 2)}`\n"
            message += f"💰 Цена: `{format_price(current_price, 4)}` USDT\n"
        
        stats = self.db.get_dca_stats(symbol)
        if stats:
            message += f"\n📊 Статистика DCA:\n"
            message += f"💰 Вложено: `{stats['total_usdt']:.2f}` USDT\n"
            message += f"📈 Средняя цена: `{format_price(stats['avg_price'], 4)}` USDT"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def show_dca_stats_detailed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        stats = self.db.get_dca_stats(symbol)
        if not stats:
            await update.message.reply_text("📈 Нет данных", parse_mode='Markdown')
            return
        
        profit_percent = float(self.db.get_setting('profit_percent', '5'))
        target_info = self.strategy.calculate_target_info(stats, profit_percent)
        
        text = f"📊 *Статистика DCA*\n\n"
        text += f"🪙 Токен: `{symbol}`\n"
        text += f"💰 Куплено: `{format_quantity(stats['total_quantity'], 2)}`\n"
        text += f"💵 Инвестировано: `{stats['total_usdt']:.2f}` USDT\n"
        text += f"📈 Средняя цена: `{format_price(stats['avg_price'], 4)}` USDT\n"
        
        if target_info:
            text += f"\n🎯 *Цель {profit_percent}%:*\n"
            text += f"💰 Цена продажи: `{format_price(target_info['target_price'], 4)}` USDT\n"
            text += f"📊 Прибыль: `{target_info['target_profit']:.2f}` USDT"
        
        await update.message.reply_text(text, parse_mode='Markdown')
    
    async def show_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        mode = self.db.get_trading_mode()
        mode_text = "Демо-режим" if mode == 'demo' else "Обычный режим"
        
        message = f"📋 *Статус бота*\n\n"
        message += f"🌐 Режим: {mode_text}\n"
        message += f"🤖 DCA: {'✅ Активен' if is_active else '⏹ Остановлен'}\n"
        message += f"🪙 Токен: `{symbol}`\n"
        message += f"🕐 Время: `{get_moscow_time().strftime('%H:%M')}` (МСК)"
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def toggle_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ API не инициализирован")
            return
        
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        if is_active:
            self.db.set_setting('dca_active', 'false')
            await update.message.reply_text("⏹ DCA остановлен", reply_markup=self.get_main_keyboard())
        else:
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            current_price = await self.bybit.get_symbol_price(symbol)
            if not current_price:
                await update.message.reply_text("❌ Не удалось получить цену")
                return
            
            self.db.set_setting('dca_active', 'true')
            next_time = self._calculate_next_purchase_time()
            self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
            
            await update.message.reply_text(
                f"✅ DCA запущен!\n\n🪙 {symbol}\n💰 Цена: {format_price(current_price, 4)} USDT\n⏰ Следующая покупка: {next_time.strftime('%d.%m.%Y %H:%M')}",
                reply_markup=self.get_main_keyboard()
            )
    
    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        await update.message.reply_text("⚙️ *Настройки*", reply_markup=self.get_settings_keyboard(), parse_mode='Markdown')
        return SELECTING_ACTION
    
    async def tracking_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        await update.message.reply_text("⚙️ *Настройки отслеживания*", reply_markup=self.get_tracking_settings_keyboard(), parse_mode='Markdown')
        return NOTIFICATION_SETTINGS_MENU
    
    async def back_to_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ *Настройки*", reply_markup=self.get_settings_keyboard(), parse_mode='Markdown')
        return ConversationHandler.END
    
    async def back_to_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("Главное меню:", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await update.message.reply_text("Используйте кнопки", reply_markup=self.get_main_keyboard())
    
    async def toggle_trading_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_trading_mode()
        new_mode = 'demo' if current == 'real' else 'real'
        self.db.set_trading_mode(new_mode)
        self.bybit_initialized = False
        self._init_bybit()
        mode_text = "Демо-режим" if new_mode == 'demo' else "Обычный режим"
        await update.message.reply_text(f"✅ Режим: {mode_text}", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def toggle_order_execution(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_order_execution_notify()
        new_status = not current
        self.db.set_order_execution_notify(new_status)
        status_text = "✅ Включено" if new_status else "⏹ Выключено"
        await update.message.reply_text(f"📋 Отслеживание: {status_text}", reply_markup=self.get_tracking_settings_keyboard())
    
    async def toggle_sell_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_sell_tracking_enabled()
        new_status = not current
        self.db.set_sell_tracking_enabled(new_status)
        status_text = "✅ Включено" if new_status else "⏹ Выключено"
        await update.message.reply_text(f"💰 Отслеживание продаж: {status_text}", reply_markup=self.get_tracking_settings_keyboard())
    
    async def ladder_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🪜 *Лестница Мартингейла*", reply_markup=self.get_ladder_settings_keyboard(), parse_mode='Markdown')
        return LADDER_MENU
    
    async def show_ladder_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📋 Текущие настройки", reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    async def reset_ladder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        self.db.clear_all_purchases(symbol)
        await update.message.reply_text("🔄 Статистика очищена", reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    async def set_ladder_max_depth_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📉 Введите глубину просадки (30-95%):", reply_markup=self.get_cancel_keyboard())
        return SET_LADDER_DEPTH
    
    async def set_ladder_max_depth_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ Глубина установлена", reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    async def set_ladder_base_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💵 Введите базовую сумму (мин 5 USDT):", reply_markup=self.get_cancel_keyboard())
        return SET_LADDER_BASE_AMOUNT
    
    async def set_ladder_base_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ Сумма установлена", reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    async def auto_dca_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🚀 *Настройки Авто DCA*", reply_markup=self.get_auto_dca_keyboard(), parse_mode='Markdown')
        return AUTO_DCA_SETTINGS
    
    async def set_amount_start_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💵 Введите сумму для Авто DCA (мин 5 USDT):", reply_markup=self.get_cancel_keyboard())
        return SET_AMOUNT
    
    async def set_amount_done_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        try:
            amount = float(text)
            if amount < 5:
                raise ValueError("Минимальная сумма 5 USDT")
            self.db.set_setting('invest_amount', str(amount))
            await update.message.reply_text(f"✅ Сумма изменена на {amount} USDT", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        except ValueError:
            await update.message.reply_text("❌ Некорректная сумма. Введите число больше 5", reply_markup=self.get_cancel_keyboard())
            return SET_AMOUNT
    
    async def set_time_start_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏰ Введите время (ЧЧ:ММ):", reply_markup=self.get_cancel_keyboard())
        return SET_SCHEDULE_TIME
    
    async def set_time_done_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        time_str = update.message.text.strip()
        if time_str in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        try:
            datetime.strptime(time_str, "%H:%M")
            self.db.set_setting('schedule_time', time_str)
            await update.message.reply_text(f"✅ Время изменено на {time_str}", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        except ValueError:
            await update.message.reply_text("❌ Некорректный формат. Используйте ЧЧ:ММ", reply_markup=self.get_cancel_keyboard())
            return SET_SCHEDULE_TIME
    
    async def purchase_notify_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔔 *Уведомления*", reply_markup=self.get_purchase_notify_settings_keyboard(), parse_mode='Markdown')
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def toggle_purchase_notify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_purchase_notify_enabled()
        new_status = not current
        self.db.set_purchase_notify_enabled(new_status)
        status_text = "🔔 Включены" if new_status else "🔕 Выключены"
        await update.message.reply_text(f"Уведомления: {status_text}", reply_markup=self.get_purchase_notify_settings_keyboard())
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def set_purchase_notify_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏰ Введите время (ЧЧ:ММ):", reply_markup=self.get_cancel_keyboard())
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def set_purchase_notify_time_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_purchase_notify_settings_keyboard())
            return WAITING_PURCHASE_NOTIFY_TIME
        try:
            datetime.strptime(text, "%H:%M")
            self.db.set_purchase_notify_time(text)
            await update.message.reply_text(f"✅ Время: {text}", reply_markup=self.get_purchase_notify_settings_keyboard())
            return WAITING_PURCHASE_NOTIFY_TIME
        except ValueError:
            await update.message.reply_text("❌ Неверный формат", reply_markup=self.get_cancel_keyboard())
            return WAITING_PURCHASE_NOTIFY_TIME
    
    async def back_to_settings_from_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ *Настройки*", reply_markup=self.get_settings_keyboard(), parse_mode='Markdown')
        return ConversationHandler.END
    
    async def set_manual_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💵 Введите сумму:", reply_markup=self.get_cancel_keyboard())
        return SET_MANUAL_AMOUNT
    
    async def set_manual_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            amount = float(text)
            if amount < 1.1:
                raise ValueError
            self.db.set_manual_amount(amount)
            await update.message.reply_text(f"✅ Сумма: {amount} USDT", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        except ValueError:
            await update.message.reply_text("❌ Некорректная сумма", reply_markup=self.get_cancel_keyboard())
            return SET_MANUAL_AMOUNT
    
    async def set_profit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📊 Введите процент прибыли:", reply_markup=self.get_cancel_keyboard())
        return SET_PROFIT_PERCENT
    
    async def set_profit_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            percent = float(text)
            self.db.set_setting('profit_percent', str(percent))
            await update.message.reply_text(f"✅ Процент: {percent}%", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_symbol_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🪙 Выберите токен:", reply_markup=self.get_symbol_selection_keyboard())
        return SELECTING_SYMBOL
    
    async def process_symbol_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        if text == "✏️ Ввести свой":
            await update.message.reply_text("✏️ Введите символ (TONUSDT):", reply_markup=self.get_cancel_keyboard())
            return SET_SYMBOL_MANUAL
        self.db.set_setting('symbol', text)
        await update.message.reply_text(f"✅ Токен: {text}", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_symbol_manual(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = update.message.text.upper().strip()
        self.db.set_setting('symbol', symbol)
        await update.message.reply_text(f"✅ Токен: {symbol}", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def handle_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏳ Экспорт...")
        success, count, file_path = self.db.export_database()
        if success:
            await update.message.reply_text(f"✅ Экспортировано: {count} записей")
        else:
            await update.message.reply_text(f"❌ Ошибка: {file_path}")
    
    async def handle_import_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.import_waiting = True
        await update.message.reply_text("📥 Отправьте файл .json", reply_markup=self.get_cancel_keyboard())
    
    async def handle_import_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.import_waiting:
            await update.message.reply_text("Сначала нажмите '📥 Импорт базы'")
            return
        try:
            file = await context.bot.get_file(update.message.document.file_id)
            temp_file = f"temp_import_{int(time.time())}.json"
            await file.download_to_drive(temp_file)
            success, message = self.db.import_database(temp_file)
            os.remove(temp_file)
            self.import_waiting = False
            if success:
                await update.message.reply_text(f"✅ {message}", reply_markup=self.get_main_keyboard())
            else:
                await update.message.reply_text(f"❌ Ошибка: {message}", reply_markup=self.get_main_keyboard())
        except Exception as e:
            self.import_waiting = False
            await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=self.get_main_keyboard())
    
    async def handle_import_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.import_waiting = False
        await update.message.reply_text("❌ Импорт отменен", reply_markup=self.get_main_keyboard())
    
    async def manual_buy_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💰 Введите цену:", reply_markup=self.get_cancel_keyboard())
        return MANUAL_BUY_PRICE
    
    async def manual_buy_price_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        context.user_data['manual_price'] = float(text)
        await update.message.reply_text("💰 Введите сумму в USDT:", reply_markup=self.get_cancel_keyboard())
        return MANUAL_BUY_AMOUNT
    
    async def manual_buy_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        
        self._init_bybit()
        price = context.user_data.get('manual_price')
        amount = float(text)
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        
        result = await self.bybit.place_limit_buy(symbol, price, amount, is_auto=False)
        if result['success']:
            profit_percent = float(self.db.get_setting('profit_percent', '5'))
            target_price = price * (1 + profit_percent / 100)
            self.db.add_purchase(symbol, amount, price, result['quantity'], order_id=result.get('order_id'))
            
            await asyncio.sleep(2)
            coin_balance = await self.bybit.get_balance(symbol.replace('USDT', ''))
            actual_qty = coin_balance.get('available', 0)
            quantity_for_sell = min(result['quantity'], actual_qty) if actual_qty > 0 else result['quantity']
            
            if quantity_for_sell > 0:
                await self.bybit.place_limit_sell(symbol, quantity_for_sell, target_price)
            
            await update.message.reply_text(f"✅ Ордер создан! {result['quantity']} по {price}", reply_markup=self.get_main_keyboard())
        else:
            await update.message.reply_text(f"❌ Ошибка: {result.get('error')}", reply_markup=self.get_main_keyboard())
        
        return ConversationHandler.END
    
    async def manual_add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("➕ Введите цену:", reply_markup=self.get_cancel_keyboard())
        return MANUAL_ADD_PRICE
    
    async def manual_add_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        context.user_data['manual_price'] = float(text)
        await update.message.reply_text("💰 Введите количество:", reply_markup=self.get_cancel_keyboard())
        return MANUAL_ADD_AMOUNT
    
    async def manual_add_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        
        price = context.user_data.get('manual_price')
        quantity = float(text)
        amount_usdt = price * quantity
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        
        self.db.add_purchase(symbol, amount_usdt, price, quantity)
        await update.message.reply_text(f"✅ Покупка добавлена: {quantity} по {price}", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def edit_purchases_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        purchases = self.db.get_purchases(symbol)
        if not purchases:
            await update.message.reply_text("Нет покупок", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        
        keyboard = []
        for p in purchases[:10]:
            keyboard.append([KeyboardButton(f"ID{p['id']}: {p['price']} @ {p['quantity']}")])
        keyboard.append([KeyboardButton("🏠 Главное меню")])
        
        await update.message.reply_text("✏️ Выберите покупку:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return EDIT_PURCHASE_SELECT
    
    async def edit_purchase_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if text == "🏠 Главное меню":
            await self.back_to_main(update, context)
            return ConversationHandler.END
        
        import re
        match = re.search(r'ID(\d+)', text)
        if match:
            purchase_id = int(match.group(1))
            context.user_data['editing_purchase_id'] = purchase_id
            purchase = self.db.get_purchase_by_id(purchase_id)
            if purchase:
                await update.message.reply_text(
                    f"✏️ ID: {purchase_id}\n💰 Цена: {purchase['price']}\n📊 Количество: {purchase['quantity']}",
                    reply_markup=self.get_edit_purchases_keyboard()
                )
                return EDIT_PURCHASE_SELECT
        
        await update.message.reply_text("❌ Неверный выбор", reply_markup=self.get_edit_purchases_keyboard())
        return EDIT_PURCHASE_SELECT
    
    async def edit_price_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💰 Введите новую цену:", reply_markup=self.get_cancel_keyboard())
        return EDIT_PRICE
    
    async def edit_price_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            new_price = float(text)
            purchase_id = context.user_data.get('editing_purchase_id')
            purchase = self.db.get_purchase_by_id(purchase_id)
            if purchase:
                new_amount = new_price * purchase['quantity']
                self.db.update_purchase(purchase_id, price=new_price, amount_usdt=new_amount)
                await update.message.reply_text(f"✅ Цена обновлена: {new_price}")
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        except ValueError:
            await update.message.reply_text("❌ Ошибка", reply_markup=self.get_cancel_keyboard())
            return EDIT_PRICE
    
    async def edit_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📊 Введите новое количество:", reply_markup=self.get_cancel_keyboard())
        return EDIT_AMOUNT
    
    async def edit_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            new_qty = float(text)
            purchase_id = context.user_data.get('editing_purchase_id')
            purchase = self.db.get_purchase_by_id(purchase_id)
            if purchase:
                new_amount = purchase['price'] * new_qty
                self.db.update_purchase(purchase_id, quantity=new_qty, amount_usdt=new_amount)
                await update.message.reply_text(f"✅ Количество обновлено: {new_qty}")
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        except ValueError:
            await update.message.reply_text("❌ Ошибка", reply_markup=self.get_cancel_keyboard())
            return EDIT_AMOUNT
    
    async def edit_date_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📅 Введите новую дату (ДД.ММ.ГГГГ):", reply_markup=self.get_cancel_keyboard())
        return EDIT_DATE
    
    async def edit_date_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        purchase_id = context.user_data.get('editing_purchase_id')
        purchase = self.db.get_purchase_by_id(purchase_id)
        if purchase:
            old_time = purchase['date'][11:] if len(purchase['date']) > 10 else "00:00:00"
            new_date_with_time = f"{text} {old_time}"
            self.db.update_purchase(purchase_id, date=new_date_with_time)
            await update.message.reply_text(f"✅ Дата обновлена: {text}")
        await self.show_purchase_after_edit(update, context, purchase_id)
        return EDIT_PURCHASE_SELECT
    
    async def delete_purchase_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚠️ *Удалить покупку?*", reply_markup=self.get_confirm_delete_keyboard(), parse_mode='Markdown')
        return DELETE_CONFIRM
    
    async def delete_purchase_execute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if text == "✅ Да, удалить":
            purchase_id = context.user_data.get('editing_purchase_id')
            if purchase_id and self.db.delete_purchase(purchase_id):
                await update.message.reply_text("✅ Покупка удалена", reply_markup=self.get_main_keyboard())
            else:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
            context.user_data.pop('editing_purchase_id', None)
            return ConversationHandler.END
        else:
            purchase_id = context.user_data.get('editing_purchase_id')
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
    
    async def show_purchase_after_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE, purchase_id):
        purchase = self.db.get_purchase_by_id(purchase_id)
        if purchase:
            await update.message.reply_text(
                f"✏️ ID: {purchase_id}\n💰 Цена: {purchase['price']}\n📊 Количество: {purchase['quantity']}",
                reply_markup=self.get_edit_purchases_keyboard()
            )
        else:
            await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_edit_purchases_keyboard())
    
    async def cancel_to_edit_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        purchase_id = context.user_data.get('editing_purchase_id')
        if purchase_id:
            await self.show_purchase_after_edit(update, context, purchase_id)
        else:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_edit_purchases_keyboard())
    
    async def handle_order_execution_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        
        if data.startswith("do_clear_stats_"):
            symbol = data.replace("do_clear_stats_", "")
            deleted = self.db.clear_all_purchases(symbol)
            await query.edit_message_text(f"✅ Статистика очищена! Удалено {deleted} покупок")
        elif data.startswith("skip_clear_stats_"):
            await query.edit_message_text("⏭ Очистка отложена")
        else:
            await query.edit_message_text("✅ Готово")
    
    def setup_handlers(self):
        logger.info("Setting up handlers...")
        
        self.application.add_handler(CommandHandler("start", self.cmd_start_fast))
        self.application.add_handler(CallbackQueryHandler(self.handle_order_execution_callback))
        
        self.application.add_handler(MessageHandler(filters.Regex('^(📊 Мой Портфель)$'), self.show_portfolio))
        self.application.add_handler(MessageHandler(filters.Regex('^(🚀 Запустить Авто DCA|⏹ Остановить Авто DCA)$'), self.toggle_dca))
        self.application.add_handler(MessageHandler(filters.Regex('^(📈 Статистика DCA)$'), self.show_dca_stats_detailed))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 Статус бота)$'), self.show_status))
        self.application.add_handler(MessageHandler(filters.Regex('^(⚙️ Настройки)$'), self.settings_menu))
        self.application.add_handler(MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.back_to_main))
        
        self.application.add_handler(MessageHandler(filters.Regex('^(📤 Экспорт базы)$'), self.handle_export))
        self.application.add_handler(MessageHandler(filters.Regex('^(📥 Импорт базы)$'), self.handle_import_start))
        self.application.add_handler(MessageHandler(filters.Regex('^❌ Отмена$'), self.handle_import_cancel))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_import_file))
        
        tracking_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(⚙️ Настройки отслеживания)$'), self.tracking_settings)],
            states={
                NOTIFICATION_SETTINGS_MENU: [
                    MessageHandler(filters.Regex('^(✅ Отслеживание Вкл|❌ Отслеживание Выкл)$'), self.toggle_order_execution),
                    MessageHandler(filters.Regex('^(💰 Отслеживание продаж)'), self.toggle_sell_tracking),
                    MessageHandler(filters.Regex('^(🔄 Синхронизация с биржей)$'), self.manual_sync_exchange),
                    MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings),
                ]
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="tracking_conv", persistent=False
        )
        self.application.add_handler(tracking_conv)
        
        main_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(⚙️ Настройки)$'), self.settings_menu)],
            states={
                SELECTING_ACTION: [
                    MessageHandler(filters.Regex('^(🪙 Выбор токена)$'), self.set_symbol_start),
                    MessageHandler(filters.Regex('^(🚀 Настройки Авто DCA)$'), self.auto_dca_settings_menu),
                    MessageHandler(filters.Regex('^(📊 Процент прибыли)$'), self.set_profit_start),
                    MessageHandler(filters.Regex('^(🪜 Лестница Мартингейла)$'), self.ladder_settings_menu),
                    MessageHandler(filters.Regex('^(💵 Сумма ручного ордера)$'), self.set_manual_amount_start),
                    MessageHandler(filters.Regex('^(⚙️ Настройки отслеживания)$'), self.tracking_settings),
                    MessageHandler(filters.Regex('^(🔔 Уведомления)$'), self.purchase_notify_settings),
                    MessageHandler(filters.Regex('^🌐 Режим: (Обычный|Демо)$'), self.toggle_trading_mode),
                    MessageHandler(filters.Regex('^(📤 Экспорт базы)$'), self.handle_export),
                    MessageHandler(filters.Regex('^(📥 Импорт базы)$'), self.handle_import_start),
                    MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.back_to_main),
                ],
                SELECTING_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_symbol_selection)],
                SET_SYMBOL_MANUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_symbol_manual)],
                SET_PROFIT_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_profit_done)],
                SET_MANUAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_manual_amount_done)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="main_conv", persistent=False
        )
        self.application.add_handler(main_conv)
        
        ladder_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🪜 Лестница Мартингейла)$'), self.ladder_settings_menu)],
            states={
                LADDER_MENU: [
                    MessageHandler(filters.Regex('^(📉 Глубина просадки)$'), self.set_ladder_max_depth_start),
                    MessageHandler(filters.Regex('^(💵 Базовая сумма)$'), self.set_ladder_base_amount_start),
                    MessageHandler(filters.Regex('^(📋 Текущие настройки)$'), self.show_ladder_settings),
                    MessageHandler(filters.Regex('^(🔄 Сбросить лестницу)$'), self.reset_ladder),
                    MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings),
                ],
                SET_LADDER_DEPTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_max_depth_save)],
                SET_LADDER_BASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_base_amount_save)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="ladder_conv", persistent=False
        )
        self.application.add_handler(ladder_conv)
        
        auto_dca_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🚀 Настройки Авто DCA)$'), self.auto_dca_settings_menu)],
            states={
                AUTO_DCA_SETTINGS: [
                    MessageHandler(filters.Regex(r'^💵'), self.set_amount_start_auto),
                    MessageHandler(filters.Regex(r'^⏰'), self.set_time_start_auto),
                    MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings),
                ],
                SET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_amount_done_auto)],
                SET_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_time_done_auto)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="auto_dca_conv", persistent=False
        )
        self.application.add_handler(auto_dca_conv)
        
        notify_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🔔 Уведомления)$'), self.purchase_notify_settings)],
            states={
                WAITING_PURCHASE_NOTIFY_TIME: [
                    MessageHandler(filters.Regex('^(🔔 Уведомления Вкл|🔕 Уведомления Выкл)$'), self.toggle_purchase_notify),
                    MessageHandler(filters.Regex('^(⏰ Время)'), self.set_purchase_notify_time_start),
                    MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings_from_purchase),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_purchase_notify_time_done)
                ]
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="notify_conv", persistent=False
        )
        self.application.add_handler(notify_conv)
        
        edit_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(✏️ Редактировать покупки)$'), self.edit_purchases_list)],
            states={
                EDIT_PURCHASE_SELECT: [
                    MessageHandler(filters.Regex('^(💰 Изменить цену)$'), self.edit_price_start),
                    MessageHandler(filters.Regex('^(📊 Изменить количество)$'), self.edit_amount_start),
                    MessageHandler(filters.Regex('^(📅 Изменить дату)$'), self.edit_date_start),
                    MessageHandler(filters.Regex('^(❌ Удалить покупку)$'), self.delete_purchase_confirm),
                    MessageHandler(filters.Regex('^(🔙 Назад к списку)$'), self.edit_purchases_list),
                    MessageHandler(filters.Regex('^(🏠 Главное меню)$'), self.back_to_main),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_purchase_selected)
                ],
                EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_price_save)],
                EDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_amount_save)],
                EDIT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_date_save)],
                DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.delete_purchase_execute)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="edit_conv", persistent=False
        )
        self.application.add_handler(edit_conv)
        
        manual_limit_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(💰 Ручная покупка)$'), self.manual_buy_start)],
            states={
                MANUAL_BUY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_price_done)],
                MANUAL_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_amount_done)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="manual_buy_conv", persistent=False
        )
        self.application.add_handler(manual_limit_conv)
        
        manual_add_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(➕ Добавить покупку)$'), self.manual_add_start)],
            states={
                MANUAL_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_price)],
                MANUAL_ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_amount)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="manual_add_conv", persistent=False
        )
        self.application.add_handler(manual_add_conv)
        
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_unknown))
        
        logger.info("Handlers setup completed")
    
    async def dca_scheduler_loop(self):
        logger.info("DCA scheduler loop started")
        while self.scheduler_running:
            try:
                await asyncio.sleep(30)
                if self.db.get_setting('dca_active', 'false') != 'true':
                    continue
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    continue
                
                now = get_moscow_time()
                next_purchase_str = self.db.get_setting('next_dca_purchase_time', '')
                if not next_purchase_str:
                    next_time = self._calculate_next_purchase_time()
                    self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
                    continue
                
                try:
                    next_time = datetime.fromisoformat(next_purchase_str)
                except:
                    next_time = self._calculate_next_purchase_time()
                    self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
                    continue
                
                if now >= next_time:
                    symbol = self.db.get_setting('symbol', 'TONUSDT')
                    profit_percent = float(self.db.get_setting('profit_percent', '5'))
                    result = await self.strategy.execute_scheduled_purchase(symbol, profit_percent)
                    
                    if result.get('success') and self.authorized_user_id:
                        msg = f"🪜 *АВТО DCA ПОКУПКА*\n\n🪙 {symbol}\n💰 Сумма: {result.get('total_usdt', 0):.2f} USDT\n💵 Цена: {result.get('price', 0):.4f} USDT"
                        try:
                            await self.application.bot.send_message(chat_id=self.authorized_user_id, text=msg, parse_mode='Markdown')
                        except:
                            pass
                    
                    frequency_hours = int(self.db.get_setting('frequency_hours', '24'))
                    next_time = next_time + timedelta(hours=frequency_hours)
                    while next_time <= now:
                        next_time += timedelta(hours=frequency_hours)
                    self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
                
                await self.strategy.check_and_update_sell_orders(self.db.get_setting('symbol', 'TONUSDT'))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DCA scheduler error: {e}")
                await asyncio.sleep(60)
    
    async def sell_checker_loop(self):
        logger.info("Sell checker loop started")
        await asyncio.sleep(60)
        while self.scheduler_running:
            try:
                if not self.db.get_sell_tracking_enabled():
                    await asyncio.sleep(3600)
                    continue
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    await asyncio.sleep(3600)
                    continue
                symbol = self.db.get_setting('symbol', 'TONUSDT')
                if self.authorized_user_id:
                    await self.strategy.check_completed_sells(symbol, self.authorized_user_id, self.application.bot)
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Sell checker error: {e}")
                await asyncio.sleep(60)
    
    async def pending_sell_checker_loop(self):
        logger.info("Pending sell checker loop started")
        await asyncio.sleep(120)
        while self.scheduler_running:
            try:
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    await asyncio.sleep(1800)
                    continue
                symbol = self.db.get_setting('symbol', 'TONUSDT')
                if self.authorized_user_id:
                    await self.strategy.check_pending_sell_orders(symbol, self.authorized_user_id, self.application.bot)
                await asyncio.sleep(1800)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pending sell checker error: {e}")
                await asyncio.sleep(60)
    
    async def order_checker_loop(self):
        logger.info("Order checker loop started")
        await asyncio.sleep(30)
        while self.scheduler_running:
            try:
                interval_minutes = self.db.get_order_check_interval()
                if not self.db.get_order_execution_notify():
                    await asyncio.sleep(interval_minutes * 60)
                    continue
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    await asyncio.sleep(interval_minutes * 60)
                    continue
                symbol = self.db.get_setting('symbol', 'TONUSDT')
                if self.authorized_user_id:
                    await self.strategy.auto_check_and_notify(symbol, self.authorized_user_id, self.application.bot)
                await asyncio.sleep(interval_minutes * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Order checker error: {e}")
                await asyncio.sleep(60)
    
    async def daily_sync_loop(self):
        logger.info("Daily sync loop started")
        await asyncio.sleep(60)
        while self.scheduler_running:
            try:
                now = get_moscow_time()
                last_sync = self.db.get_last_daily_sync_time()
                
                if last_sync is None or now.date() > last_sync.date():
                    if now.hour >= 19:
                        logger.info(f"Running daily sync at {now.strftime('%H:%M')}")
                        if not self.bybit_initialized:
                            self._init_bybit()
                        if self.bybit_initialized and self.authorized_user_id:
                            symbol = self.db.get_setting('symbol', 'TONUSDT')
                            await self.application.bot.send_message(chat_id=self.authorized_user_id, text="🔄 *ЕЖЕДНЕВНАЯ СИНХРОНИЗАЦИЯ*\nЗапущена сверка с биржей...", parse_mode='Markdown')
                            result = await self.strategy.sync_purchases_with_exchange(symbol, self.authorized_user_id, self.application.bot)
                            if result['success']:
                                if result['deleted_count'] > 0:
                                    await self.application.bot.send_message(chat_id=self.authorized_user_id, text=f"✅ Синхронизация завершена!\n🗑 Удалено {result['deleted_count']} покупок", parse_mode='Markdown')
                                else:
                                    await self.application.bot.send_message(chat_id=self.authorized_user_id, text="✅ Синхронизация завершена!\n📊 Расхождений не найдено", parse_mode='Markdown')
                            else:
                                await self.application.bot.send_message(chat_id=self.authorized_user_id, text=f"❌ Ошибка синхронизации: {result.get('error')}", parse_mode='Markdown')
                        self.db.set_last_daily_sync_time(now)
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Daily sync error: {e}")
                await asyncio.sleep(3600)
    
    async def purchase_notify_loop(self):
        logger.info("Purchase notify loop started")
        await asyncio.sleep(10)
        while self.scheduler_running:
            try:
                if not self.db.get_purchase_notify_enabled():
                    await asyncio.sleep(60)
                    continue
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    await asyncio.sleep(60)
                    continue
                
                now = get_moscow_time()
                notify_time_str = self.db.get_purchase_notify_time()
                last_notify_date = self.db.get_last_purchase_notify_date()
                current_date_str = now.strftime("%Y-%m-%d")
                
                should_notify = False
                notify_hour, notify_minute = map(int, notify_time_str.split(':'))
                if now.hour == notify_hour and now.minute >= notify_minute and now.minute < notify_minute + 5:
                    if last_notify_date != current_date_str:
                        should_notify = True
                
                if should_notify and self.authorized_user_id:
                    symbol = self.db.get_setting('symbol', 'TONUSDT')
                    current_price = await self.bybit.get_symbol_price(symbol)
                    if current_price:
                        manual_amount = self.db.get_manual_amount()
                        msg = f"🔔 *ЕЖЕДНЕВНОЕ УВЕДОМЛЕНИЕ*\n\n💰 {symbol}: {format_price(current_price, 4)} USDT\n💡 Сумма для ручного ордера: {manual_amount:.2f} USDT"
                        try:
                            await self.application.bot.send_message(chat_id=self.authorized_user_id, text=msg, parse_mode='Markdown')
                            self.db.set_last_purchase_notify_date(current_date_str)
                        except Exception as e:
                            logger.error(f"Error sending notification: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Purchase notify error: {e}")
            await asyncio.sleep(60)
    
    async def post_init(self, application):
        self.scheduler_running = True
        self.background_tasks = [
            asyncio.create_task(self.dca_scheduler_loop()),
            asyncio.create_task(self.order_checker_loop()),
            asyncio.create_task(self.sell_checker_loop()),
            asyncio.create_task(self.pending_sell_checker_loop()),
            asyncio.create_task(self.purchase_notify_loop()),
            asyncio.create_task(self.daily_sync_loop()),
        ]
        logger.info("Bot initialized, all loops started")
    
    async def shutdown(self, application):
        logger.info("Shutting down...")
        self.scheduler_running = False
        for task in self.background_tasks:
            if not task.done():
                task.cancel()
        await asyncio.sleep(2)
        logger.info("Shutdown complete")
    
    def run(self):
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"{Fore.CYAN}🚀 ЗАПУСК DCA BYBIT BOT")
        print(f"{Fore.CYAN}Версия: {BOT_VERSION}")
        print(f"{Fore.CYAN}{'='*60}")
        
        if not TELEGRAM_TOKEN:
            print(f"{Fore.RED}❌ TELEGRAM_BOT_TOKEN не найден!")
            return
        
        print(f"{Fore.GREEN}✅ Токен загружен")
        print(f"{Fore.WHITE}👤 Пользователь: {AUTHORIZED_USER}")
        print(f"{Fore.CYAN}{'='*60}\n")
        
        self.application.post_init = self.post_init
        self.application.shutdown = self.shutdown
        
        try:
            self.application.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0, timeout=60)
        except Exception as e:
            logger.error(f"Failed to start: {e}")
            print(f"{Fore.RED}❌ Ошибка: {e}")


if __name__ == "__main__":
    try:
        import colorama
    except ImportError:
        print("Устанавливаю colorama...")
        os.system(f"{sys.executable} -m pip install colorama")
        import colorama
    bot = FastDCABot()
    bot.run()