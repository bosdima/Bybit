#!/usr/bin/env python3
"""
DCA Bybit Trading Bot - МАРТИНГЕЙЛ ЛЕСЕНКОЙ
Непрерывный расчёт коэффициента на каждый процент падения
Версия 3.4.4 (13.04.2026)
ИСПРАВЛЕНО: Глобальный конфликт кнопки "Отмена", зацикливание состояний, таймауты диалогов
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
from typing import Dict, List, Optional, Tuple
from colorama import init, Fore, Style

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

# Для Windows
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

init(autoreset=True)
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot_errors.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Константы
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AUTHORIZED_USER = os.getenv('AUTHORIZED_USER', '@bosdima')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
BYBIT_TESTNET = os.getenv('BYBIT_TESTNET', 'false').lower() == 'true'

BOT_VERSION = "3.4.4 (13.04.2026)"
CONVERSATION_TIMEOUT = 180  # 3 минуты

# Состояния (34)
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
) = range(34)

DB_EXPORT_FILE = 'dca_data_export.json'
POPULAR_SYMBOLS = ["TONUSDT", "BTCUSDT", "ETHUSDT"]
MAX_DROP_DEPTH = 80

MAIN_MENU_BUTTONS = [
    "📊 Мой Портфель", "🚀 Запустить Авто DCA", "⏹ Остановить Авто DCA",
    "💰 Ручная покупка (лимит)", "📈 Статистика DCA", "➕ Добавить покупку вручную",
    "✏️ Редактировать покупки", "⚙️ Настройки", "📋 Статус бота",
    "📝 Управление ордерами", "🏠 Главное меню", "🔙 Назад в меню"
]

# ============= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =============
def format_price(price: float, decimals: int = 4) -> str:
    if price is None: return "N/A"
    return f"{price:.{decimals}f}"

def format_quantity(qty: float, decimals: int = 2) -> str:
    if qty is None: return "N/A"
    return f"{qty:.{decimals}f}"

def round_price_up(price: float) -> float:
    return math.ceil(price * 100) / 100

def round_quantity_for_sell(quantity: float, min_qty: float = 0.01) -> float:
    rounded = math.floor(quantity * 100) / 100
    if rounded < min_qty: rounded = min_qty
    return rounded

def get_ladder_levels(drop_percent: float, max_depth: float = MAX_DROP_DEPTH) -> Tuple[int, float]:
    if drop_percent <= 0: return 0, 0.0
    effective_drop = min(drop_percent, max_depth)
    ratio = min((effective_drop / max_depth) * 3.0, 3.0)
    return int(effective_drop), ratio

def get_amount_by_drop(drop_percent: float, base_amount: float, max_amount: float, max_depth: float = MAX_DROP_DEPTH) -> float:
    if drop_percent <= 0: return base_amount
    effective_drop = min(drop_percent, max_depth)
    fraction = effective_drop / max_depth
    amount = base_amount + (max_amount - base_amount) * fraction
    return min(amount, max_amount)

def calculate_current_drop(current_price: float, avg_price: float) -> float:
    if avg_price <= 0: return 0
    return max(0, ((avg_price - current_price) / avg_price) * 100)

# ============= БАЗА ДАННЫХ =============
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='executed_orders'")
            table_exists = cursor.fetchone()
            
            if not table_exists:
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
            else:
                cursor.execute("PRAGMA table_info(executed_orders)")
                columns = [col[1] for col in cursor.fetchall()]
                if 'skipped' not in columns:
                    cursor.execute("ALTER TABLE executed_orders ADD COLUMN skipped BOOLEAN DEFAULT 0")
                if 'notified_at' not in columns:
                    cursor.execute("ALTER TABLE executed_orders ADD COLUMN notified_at TIMESTAMP")
                if 'added_to_stats' not in columns:
                    cursor.execute("ALTER TABLE executed_orders ADD COLUMN added_to_stats BOOLEAN DEFAULT 0")
            
            defaults = [
                ('symbol', 'TONUSDT'),
                ('invest_amount', '1.1'),
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
                ('ladder_base_amount', '1.1'),
                ('ladder_max_depth', '80'),
                ('ladder_max_amount', '3.3'),
                ('order_execution_notify', 'true'),
                ('order_check_interval_minutes', '60'),
                ('sell_tracking_enabled', 'true'),
                ('purchase_notify_enabled', 'true'),
                ('purchase_notify_time', '06:00'),
                ('last_order_check_time', ''),
                ('last_full_check_time', ''),
                ('last_sell_check_time', ''),
                ('last_purchase_notify_date', ''),
                ('first_order_date', ''),
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
                     step_level: int = 0, date: str = None):
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO dca_purchases 
                (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date))
            purchase_id = cursor.lastrowid
            conn.commit()
            conn.close()
            self.update_first_order_date()
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
    
    def update_purchase(self, purchase_id: int, **kwargs) -> bool:
        allowed_fields = ['symbol', 'amount_usdt', 'price', 'quantity', 'multiplier', 'drop_percent', 'step_level', 'date']
        updates = []
        values = []
        for key, value in kwargs.items():
            if key in allowed_fields:
                updates.append(f"{key} = ?")
                values.append(value)
        if not updates:
            return False
        values.append(purchase_id)
        query = f"UPDATE dca_purchases SET {', '.join(updates)} WHERE id = ?"
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute(query, values)
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            if success:
                self.update_first_order_date()
            return success
        except Exception as e:
            logger.error(f"Error updating purchase {purchase_id}: {e}")
            return False
    
    def delete_purchase(self, purchase_id: int) -> bool:
        try:
            purchase = self.get_purchase_by_id(purchase_id)
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE id = ?', (purchase_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            if success and purchase:
                self.reset_executed_order_status(purchase['price'], purchase['quantity'], purchase['symbol'])
            if success:
                self.update_first_order_date()
            return success
        except Exception as e:
            logger.error(f"Error deleting purchase {purchase_id}: {e}")
            return False
    
    def reset_executed_order_status(self, price: float, quantity: float, symbol: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE executed_orders 
                SET added_to_stats = 0, skipped = 0, notified_at = NULL 
                WHERE symbol = ? AND ABS(price - ?) < 0.0001 AND ABS(quantity - ?) < 0.0001
            ''', (symbol, price, quantity))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error resetting executed order status: {e}")
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
            try:
                cursor.execute('''
                    INSERT INTO sell_orders (symbol, order_id, quantity, target_price, profit_percent)
                    VALUES (?, ?, ?, ?, ?)
                ''', (symbol, order_id, quantity, target_price, profit_percent))
                conn.commit()
            except sqlite3.IntegrityError:
                cursor.execute('''
                    UPDATE sell_orders SET target_price = ?, profit_percent = ?, status = 'active'
                    WHERE order_id = ?
                ''', (target_price, profit_percent, order_id))
                conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error adding sell order: {e}")
    
    def add_pending_sell_order(self, symbol: str, quantity: float, target_price: float, profit_percent: float) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO pending_sell_orders (symbol, quantity, target_price, profit_percent, status)
                VALUES (?, ?, ?, ?, 'pending')
            ''', (symbol, quantity, target_price, profit_percent))
            order_id = cursor.lastrowid
            conn.commit()
            conn.close()
            logger.info(f"Added pending sell order for {symbol}: {quantity} @ {target_price}")
            return order_id
        except Exception as e:
            logger.error(f"Error adding pending sell order: {e}")
            return 0
    
    def get_pending_sell_orders(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM pending_sell_orders WHERE symbol = ? AND status = "pending" ORDER BY created_at ASC', (symbol,))
            else:
                cursor.execute('SELECT * FROM pending_sell_orders WHERE status = "pending" ORDER BY created_at ASC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting pending sell orders: {e}")
            return []
    
    def update_pending_sell_order_status(self, order_id: int, status: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE pending_sell_orders SET status = ? WHERE id = ?', (status, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating pending sell order: {e}")
    
    def delete_pending_sell_order(self, order_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM pending_sell_orders WHERE id = ?', (order_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error deleting pending sell order: {e}")
    
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
    
    def delete_sell_order(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM sell_orders WHERE order_id = ?', (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error deleting sell order: {e}")
            return False
    
    def update_order_price(self, order_id: str, new_price: float, new_profit_percent: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE sell_orders SET target_price = ?, profit_percent = ? WHERE order_id = ?', 
                          (new_price, new_profit_percent, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating order price: {e}")
    
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
    
    def mark_completed_sell_stats_cleared(self, sell_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET stats_cleared = 1 WHERE id = ?', (sell_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error marking sell stats cleared: {e}")
    
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
        return self.get_setting('purchase_notify_enabled', 'false') == 'true'
    
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
    
    def log_action(self, action: str, symbol: str = None, details: str = None):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO history (action, symbol, details) VALUES (?, ?, ?)', (action, symbol, details))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error logging action: {e}")
    
    def set_dca_start(self, symbol: str, initial_price: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_start')
            cursor.execute('INSERT INTO dca_start (id, start_date, symbol, initial_price) VALUES (1, CURRENT_TIMESTAMP, ?, ?)', 
                          (symbol, initial_price))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting dca start: {e}")
    
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
                    'base_amount': float(self.get_setting('ladder_base_amount', '1.1')),
                    'max_amount': float(self.get_setting('ladder_max_amount', '3.3')),
                }
        except Exception as e:
            logger.error(f"Error getting ladder settings: {e}")
            return {
                'symbol': symbol,
                'max_depth': 80,
                'base_amount': 1.1,
                'max_amount': 3.3,
            }
    
    def save_ladder_settings(self, settings: Dict):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM ladder_settings WHERE symbol = ?', (settings['symbol'],))
            cursor.execute('''
                INSERT INTO ladder_settings 
                (symbol, max_depth, base_amount, max_amount)
                VALUES (?, ?, ?, ?)
            ''', (
                settings['symbol'],
                settings['max_depth'],
                settings['base_amount'],
                settings['max_amount'],
            ))
            conn.commit()
            conn.close()
            
            self.set_setting('ladder_max_depth', str(settings['max_depth']))
            self.set_setting('ladder_base_amount', str(settings['base_amount']))
            self.set_setting('ladder_max_amount', str(settings['max_amount']))
        except Exception as e:
            logger.error(f"Error saving ladder settings: {e}")
    
    def calculate_ladder_purchase(self, current_price: float, symbol: str = None) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', 'TONUSDT')
        
        stats = self.get_dca_stats(symbol)
        if not stats or stats['total_quantity'] <= 0:
            return {
                'should_buy': True,
                'step_level': 0,
                'amount_usdt': self.get_ladder_settings(symbol)['base_amount'],
                'target_price': current_price,
                'drop_percent': 0,
                'reason': 'Первая покупка'
            }
        
        settings = self.get_ladder_settings(symbol)
        avg_price = stats['avg_price']
        current_drop = calculate_current_drop(current_price, avg_price)
        
        purchases = self.get_purchases(symbol)
        max_purchased_drop = max([p.get('drop_percent', 0) for p in purchases], default=0)
        
        if current_drop > max_purchased_drop + 0.01:
            amount_usdt = get_amount_by_drop(current_drop, settings['base_amount'], settings['max_amount'], settings['max_depth'])
            
            if current_drop >= settings['max_depth']:
                return {
                    'should_buy': False,
                    'step_level': int(current_drop),
                    'amount_usdt': amount_usdt,
                    'target_price': current_price,
                    'reason': f'Достигнута максимальная глубина ({settings["max_depth"]}%)'
                }
            
            return {
                'should_buy': True,
                'step_level': int(current_drop),
                'amount_usdt': amount_usdt,
                'target_price': current_price,
                'drop_percent': current_drop,
                'current_drop': current_drop,
                'reason': f'Падение {current_drop:.1f}% от средней цены (превышает {max_purchased_drop:.1f}%)'
            }
        
        next_drop = max_purchased_drop + 1.0
        next_price = avg_price * (1 - next_drop / 100)
        
        return {
            'should_buy': False,
            'step_level': 0,
            'amount_usdt': 0,
            'target_price': next_price,
            'current_drop': current_drop,
            'next_drop': next_drop,
            'reason': f'Ждем падения до {next_drop:.1f}% ({format_price(next_price)}) от средней цены {format_price(avg_price)}'
        }
    
    def get_recommendation_for_current_drop(self, current_price: float, symbol: str = None) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', 'TONUSDT')
        
        stats = self.get_dca_stats(symbol)
        settings = self.get_ladder_settings(symbol)
        
        if not stats or stats['total_quantity'] <= 0:
            return {
                'success': True,
                'drop_percent': 0,
                'ratio': 0,
                'amount_usdt': settings['base_amount'],
                'level': 0,
                'avg_price': 0,
                'is_first': True
            }
        
        avg_price = stats['avg_price']
        drop_percent = calculate_current_drop(current_price, avg_price)
        
        amount = get_amount_by_drop(drop_percent, settings['base_amount'], settings['max_amount'], settings['max_depth'])
        level, ratio = get_ladder_levels(drop_percent, settings['max_depth'])
        
        return {
            'success': True,
            'drop_percent': drop_percent,
            'ratio': ratio,
            'amount_usdt': amount,
            'level': level,
            'avg_price': avg_price,
            'current_drop': drop_percent,
            'is_first': False
        }
    
    def get_ladder_summary(self, symbol: str = None, current_price: float = None) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', 'TONUSDT')
        
        settings = self.get_ladder_settings(symbol)
        stats = self.get_dca_stats(symbol)
        avg_price = stats['avg_price'] if stats else 0
        
        purchases = self.get_purchases(symbol)
        
        levels = {}
        for p in purchases:
            drop = int(p.get('drop_percent', 0))
            if drop not in levels:
                levels[drop] = []
            levels[drop].append(p)
        
        max_depth_int = int(settings['max_depth'])
        steps = []
        
        for drop_percent in range(0, max_depth_int + 1, 1):
            level, ratio = get_ladder_levels(drop_percent, settings['max_depth'])
            amount = get_amount_by_drop(drop_percent, settings['base_amount'], settings['max_amount'], settings['max_depth'])
            
            if drop_percent in levels:
                step_purchases = levels[drop_percent]
                total_amount = sum(p['amount_usdt'] for p in step_purchases)
                total_qty = sum(p['quantity'] for p in step_purchases)
                step_avg_price = total_amount / total_qty if total_qty > 0 else 0
                steps.append({
                    'step': drop_percent,
                    'drop_percent': drop_percent,
                    'ratio': ratio,
                    'price': step_avg_price,
                    'amount': amount,
                    'quantity': total_qty,
                    'status': 'completed'
                })
            else:
                target_price = avg_price * (1 - drop_percent / 100) if avg_price > 0 else 0
                steps.append({
                    'step': drop_percent,
                    'drop_percent': drop_percent,
                    'ratio': ratio,
                    'price': target_price,
                    'amount': amount,
                    'quantity': 0,
                    'status': 'pending'
                })
        
        max_purchase_drop = max([p.get('drop_percent', 0) for p in purchases], default=0)
        current_drop = 0
        if current_price and avg_price > 0:
            current_drop = calculate_current_drop(current_price, avg_price)
        
        return {
            'symbol': symbol,
            'avg_price': avg_price,
            'step_percent': 1,
            'max_depth': settings['max_depth'],
            'base_amount': settings['base_amount'],
            'max_amount': settings['max_amount'],
            'current_step': int(max_purchase_drop),
            'max_purchase_drop': max_purchase_drop,
            'current_drop': current_drop,
            'steps': steps
        }
    
    def reset_ladder(self, symbol: str = None):
        if symbol is None:
            symbol = self.get_setting('symbol', 'TONUSDT')
        self.clear_all_purchases(symbol)
    
    def add_executed_order(self, order_id: str, symbol: str, price: float, quantity: float, amount_usdt: float, executed_at: str = None) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            if executed_at:
                cursor.execute('''
                    INSERT OR IGNORE INTO executed_orders (order_id, symbol, price, quantity, amount_usdt, executed_at, added_to_stats, skipped, notified_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, NULL)
                ''', (order_id, symbol, price, quantity, amount_usdt, executed_at))
            else:
                cursor.execute('''
                    INSERT OR IGNORE INTO executed_orders (order_id, symbol, price, quantity, amount_usdt, added_to_stats, skipped, notified_at)
                    VALUES (?, ?, ?, ?, ?, 0, 0, NULL)
                ''', (order_id, symbol, price, quantity, amount_usdt))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error adding executed order: {e}")
            return False
    
    def is_order_notified(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM executed_orders WHERE order_id = ? AND (added_to_stats = 1 OR skipped = 1)', (order_id,))
            exists = cursor.fetchone() is not None
            conn.close()
            return exists
        except Exception as e:
            logger.error(f"Error checking order notified: {e}")
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
    
    def mark_order_as_skipped(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET skipped = 1, notified_at = CURRENT_TIMESTAMP WHERE order_id = ?', (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error marking order as skipped: {e}")
            return False
    
    def get_order_execution_notify(self) -> bool:
        return self.get_setting('order_execution_notify', 'true') == 'true'
    
    def set_order_execution_notify(self, enabled: bool):
        self.set_setting('order_execution_notify', 'true' if enabled else 'false')
    
    def get_order_check_interval(self) -> int:
        return int(self.get_setting('order_check_interval_minutes', '60'))
    
    def set_order_check_interval(self, minutes: int):
        self.set_setting('order_check_interval_minutes', str(minutes))
    
    def get_last_full_check_time(self) -> Optional[datetime]:
        time_str = self.get_setting('last_full_check_time', '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str)
            except:
                return None
        return None
    
    def set_last_full_check_time(self, check_time: datetime):
        self.set_setting('last_full_check_time', check_time.isoformat())
    
    def get_last_incremental_check_time(self) -> Optional[datetime]:
        time_str = self.get_setting('last_order_check_time', '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str)
            except:
                return None
        return None
    
    def set_last_incremental_check_time(self, check_time: datetime):
        self.set_setting('last_order_check_time', check_time.isoformat())
    
    def export_database(self) -> Tuple[bool, int, str]:
        try:
            purchases = self.get_purchases()
            sell_orders = self.get_active_sell_orders()
            pending_sells = self.get_pending_sell_orders()
            completed_sells = self.get_completed_sells_not_notified()
            settings = {}
            
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM settings')
            for key, value in cursor.fetchall():
                settings[key] = value
            
            cursor.execute('SELECT enabled, alert_percent, alert_interval_minutes FROM notifications WHERE id = 1')
            notification_row = cursor.fetchone()
            notifications = {
                'enabled': bool(notification_row[0]) if notification_row else True,
                'alert_percent': notification_row[1] if notification_row else 10.0,
                'alert_interval_minutes': notification_row[2] if notification_row else 30
            }
            
            cursor.execute('SELECT start_date, symbol, initial_price FROM dca_start WHERE id = 1')
            dca_start_row = cursor.fetchone()
            dca_start = {
                'start_date': dca_start_row[0] if dca_start_row else None,
                'symbol': dca_start_row[1] if dca_start_row else None,
                'initial_price': dca_start_row[2] if dca_start_row else None
            } if dca_start_row else None
            
            cursor.execute('SELECT * FROM ladder_settings')
            ladder_rows = cursor.fetchall()
            ladder_settings = []
            for row in ladder_rows:
                ladder_settings.append({
                    'id': row[0],
                    'symbol': row[1],
                    'max_depth': row[2],
                    'base_amount': row[3],
                    'max_amount': row[4],
                    'created_at': row[5]
                })
            
            cursor.execute('SELECT * FROM executed_orders')
            executed_rows = cursor.fetchall()
            executed_orders = []
            for row in executed_rows:
                if len(row) >= 10:
                    executed_orders.append({
                        'id': row[0],
                        'order_id': row[1],
                        'symbol': row[2],
                        'price': row[3],
                        'quantity': row[4],
                        'amount_usdt': row[5],
                        'executed_at': row[6],
                        'added_to_stats': row[7],
                        'skipped': row[8] if len(row) > 8 else 0,
                        'notified_at': row[9] if len(row) > 9 else None
                    })
                else:
                    executed_orders.append({
                        'id': row[0],
                        'order_id': row[1],
                        'symbol': row[2],
                        'price': row[3],
                        'quantity': row[4],
                        'amount_usdt': row[5],
                        'executed_at': row[6],
                        'added_to_stats': row[7] if len(row) > 7 else 0,
                        'skipped': 0,
                        'notified_at': None
                    })
            
            conn.close()
            
            export_data = {
                'export_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'version': BOT_VERSION,
                'purchases': purchases,
                'sell_orders': sell_orders,
                'pending_sell_orders': pending_sells,
                'completed_sells': completed_sells,
                'settings': settings,
                'notifications': notifications,
                'dca_start': dca_start,
                'ladder_settings': ladder_settings,
                'executed_orders': executed_orders
            }
            
            with open(DB_EXPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)
            
            return True, len(purchases), DB_EXPORT_FILE
        except Exception as e:
            logger.error(f"Error exporting database: {e}")
            return False, 0, str(e)
    
    def import_database(self, file_path: str) -> Tuple[bool, str]:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            
            cursor.execute("PRAGMA foreign_keys = OFF")
            
            cursor.execute("DELETE FROM dca_purchases")
            cursor.execute("DELETE FROM sell_orders")
            cursor.execute("DELETE FROM pending_sell_orders")
            cursor.execute("DELETE FROM completed_sells")
            cursor.execute("DELETE FROM settings")
            cursor.execute("DELETE FROM dca_start")
            cursor.execute("DELETE FROM ladder_settings")
            cursor.execute("DELETE FROM executed_orders")
            cursor.execute("DELETE FROM history")
            cursor.execute("DELETE FROM notifications")
            
            purchases_imported = 0
            for purchase in data.get('purchases', []):
                try:
                    cursor.execute('''
                        INSERT INTO dca_purchases 
                        (id, symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        purchase.get('id'),
                        purchase.get('symbol', 'TONUSDT'),
                        purchase.get('amount_usdt', 0),
                        purchase.get('price', 0),
                        purchase.get('quantity', 0),
                        purchase.get('multiplier', 1.0),
                        purchase.get('drop_percent', 0),
                        purchase.get('step_level', 0),
                        purchase.get('date', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        purchase.get('created_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    ))
                    purchases_imported += 1
                except Exception as e:
                    logger.warning(f"Error importing purchase: {e}")
                    continue
            
            orders_imported = 0
            for order in data.get('sell_orders', []):
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO sell_orders 
                        (id, symbol, order_id, quantity, target_price, profit_percent, created_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        order.get('id'),
                        order.get('symbol', 'TONUSDT'),
                        order.get('order_id', f"imported_{order.get('id', 0)}"),
                        order.get('quantity', 0),
                        order.get('target_price', 0),
                        order.get('profit_percent', 5),
                        order.get('created_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        order.get('status', 'active')
                    ))
                    orders_imported += 1
                except Exception as e:
                    logger.warning(f"Error importing order: {e}")
                    continue
            
            for pending in data.get('pending_sell_orders', []):
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO pending_sell_orders 
                        (id, symbol, quantity, target_price, profit_percent, created_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        pending.get('id'),
                        pending.get('symbol', 'TONUSDT'),
                        pending.get('quantity', 0),
                        pending.get('target_price', 0),
                        pending.get('profit_percent', 5),
                        pending.get('created_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        pending.get('status', 'pending')
                    ))
                except Exception as e:
                    logger.warning(f"Error importing pending order: {e}")
                    continue
            
            for sell in data.get('completed_sells', []):
                try:
                    cursor.execute('''
                        INSERT INTO completed_sells 
                        (id, symbol, order_id, quantity, sell_price, profit_percent, profit_usdt, sold_at, notified, stats_cleared)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        sell.get('id'),
                        sell.get('symbol', 'TONUSDT'),
                        sell.get('order_id'),
                        sell.get('quantity', 0),
                        sell.get('sell_price', 0),
                        sell.get('profit_percent', 0),
                        sell.get('profit_usdt', 0),
                        sell.get('sold_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        sell.get('notified', 0),
                        sell.get('stats_cleared', 0)
                    ))
                except Exception as e:
                    logger.warning(f"Error importing completed sell: {e}")
                    continue
            
            for key, value in data.get('settings', {}).items():
                try:
                    cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (key, value))
                except Exception:
                    pass
            
            dca_start = data.get('dca_start')
            if dca_start and dca_start.get('start_date'):
                try:
                    cursor.execute('INSERT OR REPLACE INTO dca_start (id, start_date, symbol, initial_price) VALUES (1, ?, ?, ?)',
                                  (dca_start['start_date'], dca_start.get('symbol', 'TONUSDT'), dca_start.get('initial_price', 0)))
                except Exception:
                    pass
            
            notifications = data.get('notifications', {})
            if notifications:
                try:
                    cursor.execute('''
                        INSERT OR REPLACE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check)
                        VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)
                    ''', (1 if notifications.get('enabled', True) else 0, notifications.get('alert_percent', 10.0), notifications.get('alert_interval_minutes', 30)))
                except Exception as e:
                    logger.warning(f"Error importing notifications: {e}")
            else:
                cursor.execute('''
                    INSERT OR IGNORE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check)
                    VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)
                ''')
            
            for ladder in data.get('ladder_settings', []):
                try:
                    cursor.execute('''
                        INSERT OR REPLACE INTO ladder_settings 
                        (id, symbol, max_depth, base_amount, max_amount, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        ladder.get('id'),
                        ladder.get('symbol', 'TONUSDT'),
                        ladder.get('max_depth', 80),
                        ladder.get('base_amount', 1.1),
                        ladder.get('max_amount', 3.3),
                        ladder.get('created_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    ))
                except Exception as e:
                    logger.warning(f"Error importing ladder: {e}")
                    continue
            
            for executed in data.get('executed_orders', []):
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO executed_orders 
                        (id, order_id, symbol, price, quantity, amount_usdt, executed_at, added_to_stats, skipped, notified_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        executed.get('id'),
                        executed.get('order_id'),
                        executed.get('symbol', 'TONUSDT'),
                        executed.get('price', 0),
                        executed.get('quantity', 0),
                        executed.get('amount_usdt', 0),
                        executed.get('executed_at', datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        executed.get('added_to_stats', 0),
                        executed.get('skipped', 0),
                        executed.get('notified_at')
                    ))
                except Exception as e:
                    logger.warning(f"Error importing executed order: {e}")
                    continue
            
            cursor.execute("PRAGMA foreign_keys = ON")
            conn.commit()
            conn.close()
            
            self.update_first_order_date()
            return True, f"Импортировано: {purchases_imported} покупок, {orders_imported} ордеров"
        except Exception as e:
            logger.error(f"Error importing database: {e}")
            return False, str(e)

# ============= BYBIT КЛИЕНТ =============
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
            logger.info("Bybit session initialized")
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
            params = {"category": "spot", "openOnly": 0}
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
    
    async def get_sell_orders(self, symbol: str = None) -> List[Dict]:
        orders = await self.get_open_orders(symbol)
        return [o for o in orders if o.get('side') == 'Sell']
    
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
                return {
                    'min_qty': float(lot_size_filter.get('minOrderQty', 0.01)),
                    'min_amt': float(lot_size_filter.get('minOrderAmt', 10)),
                    'qty_step': float(lot_size_filter.get('qtyStep', 0.01)),
                    'tick_size': float(price_filter.get('tickSize', 0.0001)),
                    'base_precision': int(lot_size_filter.get('basePrecision', 2)),
                }
            return {'min_qty': 0.01, 'min_amt': 10, 'qty_step': 0.01, 'tick_size': 0.0001, 'base_precision': 2}
        except Exception as e:
            logger.error(f"Error getting instrument info: {e}")
            return {'min_qty': 0.01, 'min_amt': 10, 'qty_step': 0.01, 'tick_size': 0.0001, 'base_precision': 2}
    
    async def get_all_executed_orders(self, symbol: str, from_date: datetime = None) -> List[Dict]:
        try:
            check_date = from_date if from_date else datetime.now() - timedelta(days=90)
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
                                        'order_status': order_status
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
            check_date = from_date if from_date else datetime.now() - timedelta(days=90)
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
    
    async def amend_order_price(self, symbol: str, order_id: str, new_price: float) -> Dict:
        try:
            if not self.session:
                self._init_session()
            response = self.session.amend_order(category="spot", symbol=symbol, orderId=order_id, price=str(new_price))
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
            
            rounded_quantity = math.floor(quantity / qty_step) * qty_step
            if rounded_quantity <= 0:
                rounded_quantity = min_qty
            
            if rounded_quantity < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty} {symbol.replace("USDT", "")}'}
            
            order_value = rounded_quantity * price
            if order_value < min_amt:
                return {'success': False, 'error': 'min_amount_error', 'min_amt': min_amt, 'order_value': order_value, 'quantity': rounded_quantity, 'price': price}
            
            response = self.session.place_order(
                category="spot", symbol=symbol, side="Sell", orderType="Limit", qty=str(rounded_quantity), price=str(price), timeInForce="GTC"
            )
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'], 'quantity': rounded_quantity, 'price': price}
            if response['retCode'] == 170140:
                return {'success': False, 'error': 'min_amount_error', 'min_amt': min_amt, 'order_value': order_value, 'quantity': rounded_quantity, 'price': price}
            return {'success': False, 'error': f"{response['retMsg']} (Код: {response['retCode']})"}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def place_market_buy(self, symbol: str, amount_usdt: float) -> Dict:
        try:
            if not self.session:
                self._init_session()
            instrument_info = await self.get_instrument_info(symbol)
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            qty_step = instrument_info['qty_step']
            if amount_usdt < min_amt:
                return {'success': False, 'error': f'Минимальная сумма: {min_amt} USDT'}
            price = await self.get_symbol_price(symbol)
            if not price:
                return {'success': False, 'error': 'Не удалось получить цену'}
            quantity = amount_usdt / price
            quantity = math.floor(quantity / qty_step) * qty_step
            if quantity < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty}'}
            response = self.session.place_order(category="spot", symbol=symbol, side="Buy", orderType="Market", qty=str(quantity))
            if response['retCode'] == 0:
                order_id = response['result']['orderId']
                await asyncio.sleep(1)
                order_details = self.session.get_order_history(category="spot", orderId=order_id)
                avg_price = price
                if order_details['retCode'] == 0 and order_details['result']['list']:
                    avg_price = float(order_details['result']['list'][0].get('avgPrice', price))
                return {'success': True, 'order_id': order_id, 'quantity': float(quantity), 'price': avg_price, 'total_usdt': amount_usdt}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def place_limit_buy(self, symbol: str, price: float, amount_usdt: float) -> Dict:
        try:
            if not self.session:
                self._init_session()
            instrument_info = await self.get_instrument_info(symbol)
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            qty_step = instrument_info['qty_step']
            if amount_usdt < min_amt:
                return {'success': False, 'error': f'Минимальная сумма: {min_amt} USDT'}
            quantity = amount_usdt / price
            quantity = math.floor(quantity / qty_step) * qty_step
            if quantity < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty}'}
            response = self.session.place_order(category="spot", symbol=symbol, side="Buy", orderType="Limit", qty=str(quantity), price=str(price), timeInForce="GTC")
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'], 'quantity': float(quantity), 'price': price, 'total_usdt': amount_usdt}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}

# ============= СТРАТЕГИЯ DCA =============
class DCAStrategy:
    def __init__(self, db: Database, bybit: BybitClient):
        self.db = db
        self.bybit = bybit
    
    async def execute_ladder_purchase(self, symbol: str, profit_percent: float) -> Dict:
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return {'success': False, 'error': 'Не удалось получить цену'}
        
        ladder_info = self.db.calculate_ladder_purchase(current_price, symbol)
        if not ladder_info['should_buy']:
            return {'success': False, 'error': ladder_info['reason']}
        
        amount_usdt = ladder_info['amount_usdt']
        drop_percent = ladder_info.get('drop_percent', 0)
        step_level = ladder_info['step_level']
        
        usdt_balance = await self.bybit.get_balance('USDT')
        available_usdt = usdt_balance.get('available', 0) if usdt_balance else 0
        
        if available_usdt < amount_usdt:
            return {'success': False, 'error': f'Недостаточно средств. Нужно {amount_usdt:.2f} USDT'}
        
        result = await self.bybit.place_market_buy(symbol, amount_usdt)
        
        if result['success']:
            current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.db.add_purchase(symbol=symbol, amount_usdt=result['total_usdt'], price=result['price'],
                                quantity=result['quantity'], multiplier=1.0, drop_percent=drop_percent,
                                step_level=step_level, date=current_date)
            self.db.set_setting('last_purchase_price', str(result['price']))
            self.db.set_setting('last_purchase_time', str(datetime.now().timestamp()))
            
            target_price_sell = result['price'] * (1 + profit_percent / 100)
            sell_result = await self.bybit.place_limit_sell(symbol, result['quantity'], target_price_sell)
            
            if sell_result['success']:
                self.db.add_sell_order(symbol=symbol, order_id=sell_result['order_id'],
                                      quantity=result['quantity'], target_price=target_price_sell,
                                      profit_percent=profit_percent)
                result['sell_order_id'] = sell_result['order_id']
                result['target_price'] = target_price_sell
            elif sell_result.get('error') == 'min_amount_error':
                pending_id = self.db.add_pending_sell_order(
                    symbol=symbol,
                    quantity=result['quantity'],
                    target_price=target_price_sell,
                    profit_percent=profit_percent
                )
                result['pending_order_id'] = pending_id
                result['sell_warning'] = f"Сумма ордера ({sell_result['order_value']:.2f} USDT) меньше минимальной ({sell_result['min_amt']} USDT). Ордер отложен до достижения нужной цены."
            else:
                result['sell_warning'] = sell_result.get('error', 'Не удалось создать ордер на продажу')
            
            result['step_level'] = step_level
            result['amount_usdt'] = amount_usdt
            result['drop_percent'] = drop_percent
            
            self.db.log_action('LADDER_PURCHASE', symbol, f"Уровень {drop_percent:.1f}%: {result['total_usdt']:.2f} USDT")
        
        return result
    
    async def check_pending_sell_orders(self, symbol: str, user_id: int, bot) -> List[Dict]:
        pending_orders = self.db.get_pending_sell_orders(symbol)
        executed_orders = []
        if not pending_orders:
            return []
        
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return []
        
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_amt = instrument_info['min_amt']
        
        for order in pending_orders:
            if current_price >= order['target_price']:
                new_target_price = current_price * (1 + order['profit_percent'] / 100)
                rounded_price = round_price_up(new_target_price)
                order_value = order['quantity'] * rounded_price
                
                if order_value >= min_amt:
                    result = await self.bybit.place_limit_sell(symbol, order['quantity'], rounded_price)
                    if result['success']:
                        self.db.add_sell_order(
                            symbol=symbol,
                            order_id=result['order_id'],
                            quantity=result['quantity'],
                            target_price=rounded_price,
                            profit_percent=order['profit_percent']
                        )
                        self.db.delete_pending_sell_order(order['id'])
                        executed_orders.append({
                            'id': order['id'],
                            'quantity': order['quantity'],
                            'target_price': rounded_price,
                            'profit_percent': order['profit_percent']
                        })
                        msg = (f"✅ *ОТЛОЖЕННЫЙ ОРДЕР ВЫПОЛНЕН!*\n\n"
                               f"🪙 Токен: `{symbol}`\n"
                               f"📊 Количество: `{format_quantity(order['quantity'], 2)}`\n"
                               f"💰 Цена продажи: `{format_price(rounded_price, 4)}` USDT\n"
                               f"📈 Целевая прибыль: `{order['profit_percent']}%`\n\n"
                               f"💵 Сумма ордера: `{order_value:.2f}` USDT\n\n"
                               f"✅ Ордер успешно выставлен!")
                        try:
                            await bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown')
                        except Exception as e:
                            logger.error(f"Error sending pending order notification: {e}")
                    else:
                        logger.warning(f"Failed to execute pending order {order['id']}: {result.get('error')}")
        return executed_orders
    
    async def check_and_update_sell_orders(self, symbol: str):
        active_orders = self.db.get_active_sell_orders(symbol)
        open_orders = await self.bybit.get_open_orders(symbol)
        open_order_ids = {o['orderId'] for o in open_orders}
        for order in active_orders:
            if order['order_id'] not in open_order_ids:
                self.db.update_sell_order_status(order['order_id'], 'completed')
                self.db.log_action('SELL_COMPLETED', symbol, f"Продано по {format_price(order['target_price'])}")
    
    async def check_completed_sells(self, symbol: str, user_id: int, bot) -> List[Dict]:
        last_check = self.db.get_last_sell_check_time()
        first_order_date = self.db.get_first_order_date()
        if first_order_date is None:
            first_order_date = datetime.now() - timedelta(days=30)
        check_date = last_check if last_check and last_check > first_order_date else first_order_date
        check_date = check_date - timedelta(hours=24)
        
        all_completed = await self.bybit.get_completed_sell_orders(symbol, from_date=check_date)
        self.db.set_last_sell_check_time(datetime.now())
        
        already_processed = self.db.get_completed_sells_not_notified(symbol)
        processed_order_ids = set([s['order_id'] for s in already_processed])
        active_sell_orders = self.db.get_active_sell_orders(symbol)
        active_order_ids = {o['order_id'] for o in active_sell_orders}
        new_completed = []
        
        for sell in all_completed:
            if sell['order_id'] in processed_order_ids:
                continue
            was_our_order = sell['order_id'] in active_order_ids
            if not was_our_order:
                continue
            stats = self.db.get_dca_stats(symbol)
            if stats and stats['total_quantity'] > 0:
                avg_price = stats['avg_price']
                profit_percent = ((sell['sell_price'] - avg_price) / avg_price) * 100
                profit_usdt = (sell['sell_price'] - avg_price) * sell['quantity']
            else:
                profit_percent = 0
                profit_usdt = 0
            sell_id = self.db.add_completed_sell(
                symbol=symbol,
                order_id=sell['order_id'],
                quantity=sell['quantity'],
                sell_price=sell['sell_price'],
                profit_percent=profit_percent,
                profit_usdt=profit_usdt
            )
            new_completed.append({
                'id': sell_id,
                'order_id': sell['order_id'],
                'quantity': sell['quantity'],
                'sell_price': sell['sell_price'],
                'amount_usdt': sell['amount_usdt'],
                'executed_at': sell['executed_at'],
                'profit_percent': profit_percent,
                'profit_usdt': profit_usdt
            })
            self.db.update_sell_order_status(sell['order_id'], 'completed')
        
        for sell in new_completed:
            profit_emoji = "🟢" if sell['profit_usdt'] >= 0 else "🔴"
            profit_color = "+" if sell['profit_usdt'] >= 0 else ""
            msg = (f"💰 *СДЕЛКА ПРОДАНА!*\n\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"📊 Количество: `{format_quantity(sell['quantity'], 2)}`\n"
                   f"💰 Цена продажи: `{format_price(sell['sell_price'], 4)}` USDT\n"
                   f"💵 Сумма: `{sell['amount_usdt']:.2f}` USDT\n"
                   f"{profit_emoji} Прибыль: `{profit_color}{sell['profit_usdt']:.2f}` USDT (`{profit_color}{sell['profit_percent']:.2f}%`)\n"
                   f"🕐 Время: `{sell['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                   f"❗ *Очистить статистику DCA по этому токену?*\n"
                   f"После очистки начнется новый цикл накопления.")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, очистить статистику", callback_data=f"confirm_clear_stats_{symbol}_{sell['id']}"),
                 InlineKeyboardButton("❌ Нет, оставить", callback_data=f"skip_clear_stats_{symbol}_{sell['id']}")]
            ])
            try:
                await bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown', reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
        return new_completed
    
    async def get_recommended_purchase(self, symbol: str) -> Dict:
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return {'success': False, 'error': 'Не удалось получить цену'}
        ladder_info = self.db.calculate_ladder_purchase(current_price, symbol)
        if ladder_info['should_buy']:
            return {'success': True, 'should_buy': True, 'amount_usdt': ladder_info['amount_usdt'],
                   'step_level': ladder_info['step_level'], 'target_price': ladder_info['target_price'],
                   'drop_percent': ladder_info.get('drop_percent', 0), 'reason': ladder_info['reason'],
                   'current_price': current_price, 'current_drop': ladder_info.get('current_drop', 0)}
        else:
            return {'success': True, 'should_buy': False, 'reason': ladder_info['reason'],
                   'current_price': current_price, 'next_buy_price': ladder_info['target_price'],
                   'next_drop': ladder_info.get('next_drop', 0), 'current_drop': ladder_info.get('current_drop', 0)}
    
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
            'profit_percent': profit_percent
        }
    
    async def check_new_orders_incremental(self, symbol: str, user_id: int, bot) -> List[Dict]:
        last_check = self.db.get_last_incremental_check_time()
        first_order_date = self.db.get_first_order_date()
        if first_order_date is None:
            first_order_date = datetime.now() - timedelta(days=30)
        check_date = last_check if last_check and last_check > first_order_date else first_order_date
        all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
        self.db.set_last_incremental_check_time(datetime.now())
        
        purchases = self.db.get_purchases(symbol)
        added_orders = set()
        for p in purchases:
            added_orders.add(f"{round(p['price'], 4)}_{round(p['quantity'], 6)}")
        
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT order_id, added_to_stats, skipped FROM executed_orders WHERE symbol = ?', (symbol,))
            executed_records = cursor.fetchall()
        except Exception as e:
            executed_records = []
        conn.close()
        
        processed_order_ids = set()
        for record in executed_records:
            added_to_stats = record[1] if len(record) > 1 else 0
            skipped = record[2] if len(record) > 2 else 0
            if added_to_stats == 1 or skipped == 1:
                processed_order_ids.add(record[0])
        
        new_orders = []
        for order in all_orders:
            if order['order_id'] in processed_order_ids:
                continue
            if f"{round(order['price'], 4)}_{round(order['quantity'], 6)}" in added_orders:
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                continue
            if order['executed_at'] > check_date:
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                new_orders.append(order)
        
        for order in new_orders:
            msg = (f"✅ *ОРДЕР ИСПОЛНЕН!*\n\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"💰 Цена: `{format_price(order['price'], 4)}` USDT\n"
                   f"📊 Количество: `{format_quantity(order['quantity'], 6)}`\n"
                   f"💵 Сумма: `{order['amount_usdt']:.2f}` USDT\n"
                   f"🕐 Время: `{order['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                   f"❗ *Добавить в статистику покупок?*")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Добавить", callback_data=f"add_order_{order['order_id']}"),
                                             InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_order_{order['order_id']}")]])
            try:
                await bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown', reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
        return new_orders
    
    async def full_check_missing_orders(self, symbol: str, user_id: int, bot) -> List[Dict]:
        first_order_date = self.db.get_first_order_date()
        if first_order_date is None:
            first_order_date = datetime.now() - timedelta(days=90)
        check_date = first_order_date - timedelta(days=1)
        all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
        
        purchases = self.db.get_purchases(symbol)
        added_orders = set()
        for p in purchases:
            added_orders.add(f"{round(p['price'], 4)}_{round(p['quantity'], 6)}")
        
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT order_id, added_to_stats, skipped FROM executed_orders WHERE symbol = ?', (symbol,))
            executed_records = cursor.fetchall()
        except Exception as e:
            executed_records = []
        conn.close()
        
        processed_order_ids = set()
        for record in executed_records:
            added_to_stats = record[1] if len(record) > 1 else 0
            skipped = record[2] if len(record) > 2 else 0
            if added_to_stats == 1 or skipped == 1:
                processed_order_ids.add(record[0])
        
        missing_orders = []
        for order in all_orders:
            if order['order_id'] in processed_order_ids:
                continue
            if f"{round(order['price'], 4)}_{round(order['quantity'], 6)}" in added_orders:
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                continue
            existing = False
            for record in executed_records:
                if record[0] == order['order_id']:
                    existing = True
                    break
            if not existing:
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
            missing_orders.append(order)
        
        for order in missing_orders:
            msg = (f"✅ *ОРДЕР ИСПОЛНЕН!*\n\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"💰 Цена: `{format_price(order['price'], 4)}` USDT\n"
                   f"📊 Количество: `{format_quantity(order['quantity'], 6)}`\n"
                   f"💵 Сумма: `{order['amount_usdt']:.2f}` USDT\n"
                   f"🕐 Время: `{order['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                   f"❗ *Добавить в статистику покупок?*")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Добавить", callback_data=f"add_order_{order['order_id']}"),
                                             InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_order_{order['order_id']}")]])
            try:
                await bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown', reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
        
        self.db.set_last_full_check_time(datetime.now())
        return missing_orders
    
    async def auto_check_and_notify(self, symbol: str, user_id: int, bot) -> Dict:
        last_full_check = self.db.get_last_full_check_time()
        now = datetime.now()
        need_full_check = False
        if last_full_check is None:
            need_full_check = True
        else:
            last_check_date = last_full_check.date()
            today = now.date()
            if last_check_date < today:
                if now.hour >= 19:
                    need_full_check = True
            elif last_check_date == today and last_full_check.hour < 19 and now.hour >= 19:
                need_full_check = True
        
        if need_full_check:
            missing_orders = await self.full_check_missing_orders(symbol, user_id, bot)
            return {'type': 'full', 'count': len(missing_orders), 'orders': missing_orders}
        else:
            new_orders = await self.check_new_orders_incremental(symbol, user_id, bot)
            return {'type': 'incremental', 'count': len(new_orders), 'orders': new_orders}
    
    async def force_check_executed_orders(self, symbol: str, bot, user_id: int) -> Dict:
        first_order_date = self.db.get_first_order_date()
        if first_order_date is None:
            first_order_date = datetime.now() - timedelta(days=90)
        check_date = first_order_date - timedelta(days=1)
        all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
        purchases = self.db.get_purchases(symbol)
        added_orders = set()
        for p in purchases:
            added_orders.add(f"{round(p['price'], 4)}_{round(p['quantity'], 6)}")
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT order_id, added_to_stats, skipped, price, quantity FROM executed_orders WHERE symbol = ?', (symbol,))
            executed_records = cursor.fetchall()
        except Exception as e:
            executed_records = []
        conn.close()
        processed_order_ids = set()
        for record in executed_records:
            added_to_stats = record[1] if len(record) > 1 else 0
            skipped = record[2] if len(record) > 2 else 0
            if added_to_stats == 1 or skipped == 1:
                processed_order_ids.add(record[0])
        missing_orders = []
        already_added = []
        for order in all_orders:
            if order['order_id'] in processed_order_ids:
                already_added.append(order)
                continue
            if f"{round(order['price'], 4)}_{round(order['quantity'], 6)}" in added_orders:
                already_added.append(order)
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
            else:
                existing = False
                for record in executed_records:
                    if record[0] == order['order_id']:
                        existing = True
                        break
                if not existing:
                    self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                missing_orders.append(order)
        notified_count = 0
        for order in missing_orders:
            if notified_count >= 10:
                break
            msg = (f"✅ *ОРДЕР ИСПОЛНЕН!*\n\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"💰 Цена: `{format_price(order['price'], 4)}` USDT\n"
                   f"📊 Количество: `{format_quantity(order['quantity'], 6)}`\n"
                   f"💵 Сумма: `{order['amount_usdt']:.2f}` USDT\n"
                   f"🕐 Время: `{order['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                   f"❗ *Добавить в статистику покупок?*")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Добавить", callback_data=f"add_order_{order['order_id']}"),
                                             InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_order_{order['order_id']}")]])
            try:
                await bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown', reply_markup=keyboard)
                notified_count += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
        return {'total_found': len(all_orders), 'already_added': len(already_added), 'missing': missing_orders, 'check_date': check_date, 'notified_count': notified_count}
    
    async def force_check_completed_sells(self, symbol: str, bot, user_id: int) -> Dict:
        first_order_date = self.db.get_first_order_date()
        if first_order_date is None:
            first_order_date = datetime.now() - timedelta(days=90)
        check_date = first_order_date - timedelta(days=1)
        all_completed = await self.bybit.get_completed_sell_orders(symbol, from_date=check_date)
        already_processed = self.db.get_completed_sells_not_notified(symbol)
        processed_order_ids = set([s['order_id'] for s in already_processed])
        active_sell_orders = self.db.get_active_sell_orders(symbol)
        active_order_ids = {o['order_id'] for o in active_sell_orders}
        missing_sells = []
        for sell in all_completed:
            if sell['order_id'] in processed_order_ids:
                continue
            was_our_order = sell['order_id'] in active_order_ids
            if not was_our_order:
                continue
            stats = self.db.get_dca_stats(symbol)
            if stats and stats['total_quantity'] > 0:
                avg_price = stats['avg_price']
                profit_percent = ((sell['sell_price'] - avg_price) / avg_price) * 100
                profit_usdt = (sell['sell_price'] - avg_price) * sell['quantity']
            else:
                profit_percent = 0
                profit_usdt = 0
            sell_id = self.db.add_completed_sell(symbol=symbol, order_id=sell['order_id'], quantity=sell['quantity'], sell_price=sell['sell_price'], profit_percent=profit_percent, profit_usdt=profit_usdt)
            missing_sells.append({'id': sell_id, 'order_id': sell['order_id'], 'quantity': sell['quantity'], 'sell_price': sell['sell_price'], 'amount_usdt': sell['amount_usdt'], 'executed_at': sell['executed_at'], 'profit_percent': profit_percent, 'profit_usdt': profit_usdt})
            self.db.update_sell_order_status(sell['order_id'], 'completed')
        for sell in missing_sells:
            profit_emoji = "🟢" if sell['profit_usdt'] >= 0 else "🔴"
            profit_color = "+" if sell['profit_usdt'] >= 0 else ""
            msg = (f"💰 *СДЕЛКА ПРОДАНА!*\n\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"📊 Количество: `{format_quantity(sell['quantity'], 2)}`\n"
                   f"💰 Цена продажи: `{format_price(sell['sell_price'], 4)}` USDT\n"
                   f"💵 Сумма: `{sell['amount_usdt']:.2f}` USDT\n"
                   f"{profit_emoji} Прибыль: `{profit_color}{sell['profit_usdt']:.2f}` USDT (`{profit_color}{sell['profit_percent']:.2f}%`)\n"
                   f"🕐 Время: `{sell['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                   f"❗ *Очистить статистику DCA по этому токену?*\n"
                   f"После очистки начнется новый цикл накопления.")
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да, очистить статистику", callback_data=f"confirm_clear_stats_{symbol}_{sell['id']}"),
                                             InlineKeyboardButton("❌ Нет, оставить", callback_data=f"skip_clear_stats_{symbol}_{sell['id']}")]])
            try:
                await bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown', reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Error sending notification: {e}")
        return {'total_found': len(all_completed), 'already_processed': len(already_processed), 'missing': missing_sells, 'check_date': check_date}
    
    async def place_full_sell_order(self, update, symbol: str, profit_percent: float, auto_cancel_old: bool = True) -> Dict:
        try:
            stats = self.db.get_dca_stats(symbol)
            if not stats or stats['total_quantity'] <= 0:
                return {'success': False, 'error': 'Нет купленных активов для продажи'}
            target_info = self.calculate_target_info(stats, profit_percent)
            if not target_info:
                return {'success': False, 'error': 'Не удалось рассчитать целевую цену'}
            raw_price = target_info['target_price']
            rounded_price = round_price_up(raw_price)
            coin = symbol.replace('USDT', '')
            total_quantity = stats['total_quantity']
            if total_quantity <= 0:
                return {'success': False, 'error': f'Количество {coin} равно 0'}
            instrument_info = await self.bybit.get_instrument_info(symbol)
            qty_step = instrument_info['qty_step']
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            rounded_quantity = math.floor(total_quantity / qty_step) * qty_step
            if rounded_quantity <= 0:
                rounded_quantity = min_qty
            order_value = rounded_quantity * rounded_price
            if order_value < min_amt:
                pending_id = self.db.add_pending_sell_order(symbol=symbol, quantity=rounded_quantity, target_price=rounded_price, profit_percent=profit_percent)
                required_price = min_amt / rounded_quantity
                msg = (f"⏳ *ОРДЕР ОТЛОЖЕН*\n\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"📊 Количество: `{format_quantity(rounded_quantity, 2)}` {coin}\n"
                       f"💰 Целевая цена: `{format_price(rounded_price, 4)}` USDT\n"
                       f"📈 Целевая прибыль: `{profit_percent}%`\n\n"
                       f"⚠️ *Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)*\n\n"
                       f"🔄 Ордер будет автоматически выставлен, когда цена достигнет или превысит:\n"
                       f"💰 `{format_price(rounded_price, 4)}` USDT\n\n"
                       f"📈 ИЛИ когда цена поднимется до `{format_price(required_price, 4)}` USDT\n"
                       f"(при которой сумма ордера достигнет минимальной)\n\n"
                       f"✅ Ордер сохранен и будет проверяться автоматически.")
                if update and hasattr(update, 'message'):
                    await update.message.reply_text(msg, parse_mode='Markdown')
                return {'success': False, 'pending': True, 'pending_id': pending_id, 'error': 'min_amount_error', 'message': msg}
            open_orders = await self.bybit.get_open_orders(symbol)
            existing_sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
            if existing_sell_orders and auto_cancel_old:
                if update and hasattr(update, 'message'):
                    await update.message.reply_text("🔄 Обнаружены старые ордера на продажу. Отменяю их...")
                cancelled_count, cancelled_ids = await self.bybit.cancel_all_sell_orders(symbol)
                if cancelled_count > 0:
                    for order_id in cancelled_ids:
                        self.db.update_sell_order_status(order_id, 'cancelled')
                    if update and hasattr(update, 'message'):
                        await update.message.reply_text(f"✅ Отменено {cancelled_count} старых ордеров. Выставляю новый...")
                else:
                    if update and hasattr(update, 'message'):
                        await update.message.reply_text("⚠️ Не удалось отменить старые ордера. Попробуйте позже.")
                    return {'success': False, 'error': 'Не удалось отменить старые ордера'}
            result = await self.bybit.place_limit_sell(symbol, rounded_quantity, rounded_price)
            if result['success']:
                self.db.add_sell_order(symbol=symbol, order_id=result['order_id'], quantity=rounded_quantity, target_price=rounded_price, profit_percent=profit_percent)
                self.db.log_action('FULL_SELL_ORDER', symbol, f"Ордер на продажу {rounded_quantity:.2f} {coin} по {rounded_price:.4f} USDT")
                return {'success': True, 'order_id': result['order_id'], 'quantity': rounded_quantity, 'price': rounded_price, 'raw_price': raw_price, 'profit_percent': profit_percent}
            elif result.get('error') == 'min_amount_error':
                pending_id = self.db.add_pending_sell_order(symbol=symbol, quantity=rounded_quantity, target_price=rounded_price, profit_percent=profit_percent)
                required_price = min_amt / rounded_quantity
                msg = (f"⏳ *ОРДЕР ОТЛОЖЕН*\n\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"📊 Количество: `{format_quantity(rounded_quantity, 2)}` {coin}\n"
                       f"💰 Целевая цена: `{format_price(rounded_price, 4)}` USDT\n"
                       f"📈 Целевая прибыль: `{profit_percent}%`\n\n"
                       f"⚠️ *Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)*\n\n"
                       f"🔄 Ордер будет автоматически выставлен, когда цена достигнет или превысит:\n"
                       f"💰 `{format_price(rounded_price, 4)}` USDT\n\n"
                       f"📈 ИЛИ когда цена поднимется до `{format_price(required_price, 4)}` USDT\n"
                       f"(при которой сумма ордера достигнет минимальной)\n\n"
                       f"✅ Ордер сохранен и будет проверяться автоматически.")
                if update and hasattr(update, 'message'):
                    await update.message.reply_text(msg, parse_mode='Markdown')
                return {'success': False, 'pending': True, 'pending_id': pending_id, 'error': result.get('error'), 'message': msg}
            else:
                return {'success': False, 'error': result.get('error', 'Ошибка создания ордера')}
        except Exception as e:
            logger.error(f"Error placing full sell order: {e}")
            return {'success': False, 'error': str(e)}

# ============= ОСНОВНОЙ КЛАСС БОТА =============
class FastDCABot:
    def __init__(self):
        self.db = Database()
        self.bybit = None
        self.strategy = None
        self.bybit_initialized = False
        self.import_waiting = False
        
        request_kwargs = {'connect_timeout': 60.0, 'read_timeout': 60.0, 'write_timeout': 60.0, 'pool_timeout': 60.0}
        request = HTTPXRequest(**request_kwargs)
        builder = Application.builder().token(TELEGRAM_TOKEN).request(request)
        self.application = builder.build()
        
        self.scheduler_running = False
        self.authorized_user_id = None
        
        self.setup_handlers()
    
    def _init_bybit(self):
        if not self.bybit_initialized and BYBIT_API_KEY and BYBIT_API_SECRET:
            try:
                self.bybit = BybitClient(BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET)
                self.strategy = DCAStrategy(self.db, self.bybit)
                self.bybit_initialized = True
                logger.info("Bybit client initialized")
            except Exception as e:
                logger.error(f"Bybit init error: {e}")
    
    # ============= КЛАВИАТУРЫ =============
    def get_main_keyboard(self):
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        dca_button = "⏹ Остановить Авто DCA" if is_active else "🚀 Запустить Авто DCA"
        keyboard = [
            [KeyboardButton("📊 Мой Портфель"), KeyboardButton(dca_button)],
            [KeyboardButton("💰 Ручная покупка (лимит)"), KeyboardButton("📈 Статистика DCA")],
            [KeyboardButton("➕ Добавить покупку вручную"), KeyboardButton("✏️ Редактировать покупки")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("📝 Управление ордерами")],
            [KeyboardButton("📋 Статус бота")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_order_management_keyboard(self):
        keyboard = [
            [KeyboardButton("📋 Список открытых ордеров"), KeyboardButton("❌ Удалить ордер")],
            [KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_tracking_settings_keyboard(self):
        current_status = self.db.get_order_execution_notify()
        sell_tracking = self.db.get_sell_tracking_enabled()
        current_interval = self.db.get_order_check_interval()
        tracking_button = "✅ Отслеживание ордеров Вкл" if current_status else "❌ Отслеживание ордеров Выкл"
        sell_tracking_button = "💰 Отслеживание продаж Вкл" if sell_tracking else "⏳ Отслеживание продаж Выкл"
        keyboard = [
            [KeyboardButton(tracking_button)],
            [KeyboardButton(sell_tracking_button)],
            [KeyboardButton(f"⏱ Интервал проверки Ордеров {current_interval} мин")],
            [KeyboardButton("🔍 Тест отслеживания")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_purchase_notify_settings_keyboard(self):
        enabled = self.db.get_purchase_notify_enabled()
        notify_time = self.db.get_purchase_notify_time()
        status_button = "🔔 Уведомления Вкл" if enabled else "🔕 Уведомления Выкл"
        keyboard = [
            [KeyboardButton(status_button)],
            [KeyboardButton(f"⏰ Время уведомления ({notify_time})")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_cancel_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    
    def get_sell_confirmation_keyboard(self):
        return ReplyKeyboardMarkup([
            [KeyboardButton("✅ Да, выставить ордер на продажу")],
            [KeyboardButton("❌ Нет, отмена")]
        ], resize_keyboard=True)
    
    def get_settings_keyboard(self):
        keyboard = [
            [KeyboardButton("🪙 Выбор токена"), KeyboardButton("💵 Сумма покупки")],
            [KeyboardButton("📊 Процент прибыли"), KeyboardButton("📉 Настройки падения")],
            [KeyboardButton("⏰ Время покупки"), KeyboardButton("🔄 Частота покупки")],
            [KeyboardButton("🪜 Настройка лестницы"), KeyboardButton("⚙️ Настройки отслеживания")],
            [KeyboardButton("🔔 Уведомления о покупке"), KeyboardButton("📤 Экспорт базы")],
            [KeyboardButton("📥 Импорт базы"), KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_ladder_settings_keyboard(self):
        keyboard = [
            [KeyboardButton("📉 Глубина просадки (%)"), KeyboardButton("💵 Базовая сумма")],
            [KeyboardButton("📋 Текущие настройки"), KeyboardButton("🔄 Сбросить лестницу")],
            [KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_symbol_selection_keyboard(self):
        keyboard = []
        for symbol in POPULAR_SYMBOLS:
            keyboard.append([KeyboardButton(symbol)])
        keyboard.append([KeyboardButton("✏️ Ввести свой токен")])
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
    
    def get_purchases_list_keyboard(self, purchases):
        keyboard = []
        for p in purchases:
            try:
                date_display = datetime.strptime(p['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
            except:
                date_display = p['date'][:10] if p['date'] else "N/A"
            btn_text = f"ID{p['id']}: {date_display} - {format_quantity(p['quantity'], 2)} по {format_price(p['price'], 4)}"
            keyboard.append([KeyboardButton(btn_text)])
        keyboard.append([KeyboardButton("🏠 Главное меню")])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_manual_buy_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    
    # ============= ПРОВЕРКА ПОЛЬЗОВАТЕЛЯ И СБРОС СОСТОЯНИЯ =============
    async def _check_user_fast(self, update: Update) -> bool:
        user = update.effective_user
        username = f"@{user.username}" if user.username else f"ID:{user.id}"
        if self.authorized_user_id is None:
            if username == AUTHORIZED_USER:
                self.authorized_user_id = user.id
                return True
        elif user.id == self.authorized_user_id:
            return True
        await update.message.reply_text("⛔ Доступ запрещен")
        return False
    
    async def _reset_bot_state(self, context: ContextTypes.DEFAULT_TYPE):
        """Полный сброс состояния бота"""
        context.user_data.clear()
        self.import_waiting = False
        for conv_name in ['manual_add_conversation', 'manual_buy_conversation', 'main_conversation', 
                         'ladder_conversation', 'edit_purchases_conversation', 'tracking_conversation',
                         'cancel_order_conversation', 'purchase_notify_conversation']:
            try:
                conv_handler = getattr(self.application, conv_name, None)
                if conv_handler and hasattr(conv_handler, '_conversations'):
                    for chat_id in list(conv_handler._conversations.keys()):
                        conv_handler._conversations.pop(chat_id, None)
            except Exception:
                pass
    
    async def return_to_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("🏠 Главное меню:", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    # ============= ОБРАБОТЧИКИ КОМАНД =============
    async def cmd_start_fast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        await update.message.reply_text(
            f"👋 Привет, {update.effective_user.first_name}!\n\n"
            f"🤖 DCA Bybit Bot (Мартингейл лесенкой)\n"
            f"📌 Версия: {BOT_VERSION}\n\n"
            f"Главное меню:",
            reply_markup=self.get_main_keyboard()
        )
    
    async def cancel_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Универсальный обработчик кнопки '❌ Отмена' в любом диалоге"""
        await self._reset_bot_state(context)
        await update.message.reply_text("❌ Действие отменено", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    # ============= ОСНОВНЫЕ ФУНКЦИИ =============
    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        try:
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            coin = symbol.replace('USDT', '')
            coin_balance = await self.bybit.get_balance(coin)
            usdt_balance = await self.bybit.get_balance('USDT')
            current_price = await self.bybit.get_symbol_price(symbol)
            message = f"📊 *Мой Портфель*\n\n"
            if usdt_balance and 'equity' in usdt_balance:
                available_usdt = usdt_balance.get('available', usdt_balance.get('equity', 0))
                message += f"💵 USDT доступно: `{available_usdt:.2f}`\n\n"
            if coin_balance and 'equity' in coin_balance:
                equity = coin_balance['equity']
                available = coin_balance.get('available', 0)
                usd_value = coin_balance.get('usdValue', 0)
                if usd_value == 0 and current_price and equity > 0:
                    usd_value = equity * current_price
                dca_stats = self.db.get_dca_stats(symbol)
                avg_price = dca_stats['avg_price'] if dca_stats else 0
                if avg_price > 0 and current_price and equity > 0:
                    pnl_percent = ((current_price - avg_price) / avg_price * 100)
                    pnl_usd = (current_price - avg_price) * equity
                else:
                    pnl_percent = 0
                    pnl_usd = 0
                emoji = "🟢" if pnl_percent >= 0 else "🔴"
                message += f"🪙 *{coin}*\n"
                message += f"Количество: `{format_quantity(equity, 2)}`\n"
                message += f"Доступно: `{format_quantity(available, 2)}`\n"
                message += f"Стоимость: `{usd_value:.2f}` USDT\n"
                message += f"Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                if avg_price > 0:
                    message += f"Средняя цена входа: `{format_price(avg_price, 4)}` USDT\n"
                    message += f"{emoji} PnL: `{pnl_percent:+.2f}%` ({pnl_usd:+.2f} USDT)\n\n"
            await update.message.reply_text(message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error in show_portfolio: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def show_dca_stats_detailed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        try:
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            coin = symbol.replace('USDT', '')
            stats = self.db.get_dca_stats(symbol)
            current_price = await self.bybit.get_symbol_price(symbol)
            profit_percent = float(self.db.get_setting('profit_percent', '5'))
            if not stats:
                await update.message.reply_text("📈 *Статистика DCA*\n\nПокупок пока нет.", parse_mode='Markdown')
                return
            total_amount = stats['total_quantity']
            total_cost = stats['total_usdt']
            avg_price = stats['avg_price']
            current_value = total_amount * current_price if current_price else 0
            pnl = current_value - total_cost
            pnl_percent = (pnl / total_cost * 100) if total_cost > 0 else 0
            target_info = self.strategy.calculate_target_info(stats, profit_percent)
            text = f"📊 *ДЕТАЛЬНАЯ СТАТИСТИКА DCA*\n\n"
            text += f"🪙 Токен: `{symbol}`\n"
            text += f"💰 Куплено: `{format_quantity(total_amount, 2)}` {coin}\n"
            text += f"💵 Инвестировано: `{total_cost:.2f}` USDT\n"
            text += f"📈 Средняя цена входа: `{format_price(avg_price, 4)}` USDT\n"
            if current_price:
                current_drop = calculate_current_drop(current_price, avg_price)
                text += f"\n📊 *ТЕКУЩАЯ СИТУАЦИЯ*\n"
                text += f"📉 Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                text += f"📉 Падение от средней цены: `{current_drop:.1f}%`\n"
                text += f"💰 Текущая стоимость: `{current_value:.2f}` USDT\n"
                emoji = "📈" if pnl >= 0 else "📉"
                text += f"{emoji} Текущий PnL: `{pnl:.2f}` USDT ({pnl_percent:+.2f}%)\n"
            if target_info:
                rounded_target = round_price_up(target_info['target_price'])
                text += f"\n🎯 *ЦЕЛЕВАЯ ПРИБЫЛЬ {profit_percent}%:*\n"
                text += f"Нужно продать: `{format_quantity(target_info['total_qty'], 2)}` {coin}\n"
                text += f"Цена продажи (расчетная): `{format_price(target_info['target_price'], 4)}` USDT\n"
                text += f"Цена продажи (с округлением вверх): `{format_price(rounded_target, 4)}` USDT\n"
                text += f"Получите: `{target_info['target_value']:.2f}` USDT\n"
                text += f"Прибыль: `{target_info['target_profit']:.2f}` USDT\n"
                if current_price:
                    increase_needed = ((rounded_target - current_price) / current_price * 100)
                    text += f"Нужен рост: `{increase_needed:+.2f}%` от текущей цены"
            ladder_settings = self.db.get_ladder_settings(symbol)
            if ladder_settings:
                text += f"\n\n🪜 *ЛЕСТНИЦА*\n"
                text += f"Глубина просадки: `{ladder_settings['max_depth']}%`\n"
                text += f"Базовая сумма: `{ladder_settings['base_amount']}` USDT\n"
                text += f"Максимальная сумма: `{ladder_settings['max_amount']}` USDT"
            await update.message.reply_text(text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error in show_dca_stats_detailed: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def show_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        invest_amount = float(self.db.get_setting('invest_amount', '1.1'))
        ladder_settings = self.db.get_ladder_settings(symbol)
        order_execution = self.db.get_order_execution_notify()
        sell_tracking = self.db.get_sell_tracking_enabled()
        purchase_notify = self.db.get_purchase_notify_enabled()
        purchase_notify_time = self.db.get_purchase_notify_time()
        order_interval = self.db.get_order_check_interval()
        last_full_check = self.db.get_last_full_check_time()
        first_order_date = self.db.get_first_order_date()
        message = f"📋 *Статус бота*\n\n"
        message += f"🤖 Статус: {'✅ Активен' if is_active else '⏹ Остановлен'}\n"
        message += f"🪙 Токен: `{symbol}`\n"
        message += f"💵 Сумма покупки: `{invest_amount}` USDT\n"
        message += f"📈 Цель: `{self.db.get_setting('profit_percent', '5')}%`\n"
        message += f"📋 Отслеживание ордеров: {'✅ Вкл' if order_execution else '⏹ Выкл'}\n"
        message += f"💰 Отслеживание продаж: {'✅ Вкл' if sell_tracking else '⏹ Выкл'}\n"
        message += f"🔔 Уведомления о покупке: {'✅ Вкл' if purchase_notify else '⏹ Выкл'} ({purchase_notify_time})\n"
        message += f"🕐 Интервал проверки: `{order_interval}` мин\n"
        if first_order_date:
            message += f"📅 Первый ордер: `{first_order_date.strftime('%d.%m.%Y %H:%M')}`\n"
        if last_full_check:
            message += f"📅 Последняя полная проверка: `{last_full_check.strftime('%d.%m.%Y %H:%M')}`\n"
        message += f"\n🪜 *Лестница:*\n"
        message += f"Глубина просадки: `{ladder_settings['max_depth']}%`\n"
        message += f"Базовая сумма: `{ladder_settings['base_amount']}` USDT\n"
        message += f"Макс. сумма: `{ladder_settings['max_amount']}` USDT\n"
        stats = self.db.get_dca_stats(symbol)
        if stats:
            message += f"\n📊 Всего покупок: `{stats['total_purchases']}`\n💰 Вложено: `{stats['total_usdt']:.2f}` USDT"
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def toggle_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
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
            stats = self.db.get_dca_stats(symbol)
            avg_price = stats['avg_price'] if stats else 0
            if avg_price > 0:
                await update.message.reply_text(f"🪜 Расчет лестницы от средней цены: {format_price(avg_price, 4)} USDT")
            else:
                await update.message.reply_text(f"🪜 Первая покупка будет по текущей цене: {format_price(current_price, 4)} USDT")
            self.db.set_setting('dca_active', 'true')
            self.db.set_setting('initial_reference_price', str(current_price))
            self.db.set_dca_start(symbol, current_price)
            invest_amount = float(self.db.get_setting('invest_amount', '1.1'))
            ladder_settings = self.db.get_ladder_settings(symbol)
            await update.message.reply_text(
                f"✅ DCA запущен!\n\n"
                f"🪙 {symbol}\n"
                f"💰 Средняя цена: {format_price(avg_price, 4) if avg_price > 0 else '—'} USDT\n"
                f"💵 Базовая сумма: {invest_amount} USDT\n"
                f"📉 Макс. просадка: {ladder_settings['max_depth']}%",
                reply_markup=self.get_main_keyboard()
            )
    
    # ============= НАСТРОЙКИ (ГЛАВНЫЙ ДИАЛОГ) =============
    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        invest_amount = self.db.get_setting('invest_amount', '1.1')
        profit_percent = self.db.get_setting('profit_percent', '5')
        schedule_time = self.db.get_setting('schedule_time', '09:00')
        frequency_hours = self.db.get_setting('frequency_hours', '24')
        await update.message.reply_text(
            f"⚙️ *Настройки*\n\n"
            f"🪙 Токен: `{symbol}`\n"
            f"💵 Сумма: `{invest_amount}` USDT\n"
            f"📈 Прибыль: `{profit_percent}%`\n"
            f"⏰ Время: `{schedule_time}`\n"
            f"🔄 Частота: `{frequency_hours}`ч\n\n"
            f"Выберите параметр для изменения:",
            reply_markup=self.get_settings_keyboard(),
            parse_mode='Markdown'
        )
        return SELECTING_ACTION
    
    async def set_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"💵 Введите сумму (текущая: {self.db.get_setting('invest_amount', '1.1')}):\n*Это базовая сумма для лестницы*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_AMOUNT
    
    async def set_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.cancel_action(update, context)
        try:
            amount = float(text)
            if amount < 1:
                raise ValueError
            self.db.set_setting('invest_amount', str(amount))
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            ladder = self.db.get_ladder_settings(symbol)
            ladder['base_amount'] = amount
            ladder['max_amount'] = amount * 3
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Сумма изменена на {amount} USDT\n🪜 Базовая сумма лестницы обновлена", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_profit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"📊 Введите процент прибыли (текущий: {self.db.get_setting('profit_percent', '5')}%):", reply_markup=self.get_cancel_keyboard())
        return SET_PROFIT_PERCENT
    
    async def set_profit_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.cancel_action(update, context)
        try:
            percent = float(text)
            if percent < 0.1:
                raise ValueError
            self.db.set_setting('profit_percent', str(percent))
            await update.message.reply_text(f"✅ Процент изменен на {percent}%", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_drop_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"📉 Введите макс. падение % и множитель (текущие: {self.db.get_setting('max_drop_percent', '80')}% x{self.db.get_setting('max_multiplier', '3')}):\nНапример: 80 3", reply_markup=self.get_cancel_keyboard())
        return SET_MAX_DROP
    
    async def set_drop_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.cancel_action(update, context)
        try:
            parts = text.split()
            if len(parts) != 2:
                raise ValueError
            max_drop = float(parts[0])
            max_mult = float(parts[1])
            if max_drop < 30 or max_drop > 95 or max_mult < 1.5 or max_mult > 10:
                raise ValueError
            self.db.set_setting('max_drop_percent', str(max_drop))
            self.db.set_setting('max_multiplier', str(max_mult))
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            ladder = self.db.get_ladder_settings(symbol)
            ladder['max_depth'] = max_drop
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Настройки обновлены: {max_drop}% x{max_mult}\n🪜 Глубина просадки лестницы обновлена", reply_markup=self.get_settings_keyboard())
        except Exception:
            await update.message.reply_text("❌ Ошибка формата. Используйте: 80 3", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"⏰ Введите время (текущее: {self.db.get_setting('schedule_time', '09:00')}, формат ЧЧ:ММ):", reply_markup=self.get_cancel_keyboard())
        return SET_SCHEDULE_TIME
    
    async def set_time_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        time_str = update.message.text.strip()
        if time_str == "❌ Отмена":
            return await self.cancel_action(update, context)
        try:
            datetime.strptime(time_str, "%H:%M")
            self.db.set_setting('schedule_time', time_str)
            await update.message.reply_text(f"✅ Время изменено на {time_str}", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректный формат. Используйте ЧЧ:ММ", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_frequency_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"🔄 Введите частоту в часах (текущая: {self.db.get_setting('frequency_hours', '24')}):", reply_markup=self.get_cancel_keyboard())
        return SET_FREQUENCY_HOURS
    
    async def set_frequency_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.cancel_action(update, context)
        try:
            hours = int(text)
            if hours < 1 or hours > 720:
                raise ValueError
            self.db.set_setting('frequency_hours', str(hours))
            await update.message.reply_text(f"✅ Частота изменена на {hours} часов", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Введите число от 1 до 720", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_symbol_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        await update.message.reply_text(f"🪙 Выберите токен или введите свой\nТекущий: {self.db.get_setting('symbol', 'TONUSDT')}", reply_markup=self.get_symbol_selection_keyboard())
        return SELECTING_SYMBOL
    
    async def process_symbol_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.cancel_action(update, context)
        if text == "✏️ Ввести свой токен":
            await update.message.reply_text("✏️ Введите символ токена (например: TONUSDT):", reply_markup=self.get_cancel_keyboard())
            return SET_SYMBOL_MANUAL
        if text in POPULAR_SYMBOLS:
            return await self._validate_and_set_symbol(update, text)
        else:
            await update.message.reply_text("❌ Неверный выбор.", reply_markup=self.get_symbol_selection_keyboard())
            return SELECTING_SYMBOL
    
    async def set_symbol_manual(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = update.message.text.upper().strip()
        if symbol == "❌ Отмена":
            return await self.cancel_action(update, context)
        return await self._validate_and_set_symbol(update, symbol)
    
    async def _validate_and_set_symbol(self, update: Update, symbol: str) -> int:
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        price = await self.bybit.get_symbol_price(symbol)
        if not price:
            await update.message.reply_text(f"❌ Символ {symbol} не найден.", reply_markup=self.get_symbol_selection_keyboard())
            return SELECTING_SYMBOL
        self.db.set_setting('symbol', symbol)
        self.db.set_setting('initial_reference_price', str(price))
        await update.message.reply_text(f"✅ Символ изменен на {symbol}\n💰 Текущая цена: {format_price(price, 4)} USDT", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    # ============= ДОБАВЛЕНИЕ ПОКУПКИ ВРУЧНУЮ =============
    async def manual_add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        current_price = await self.bybit.get_symbol_price(symbol)
        stats = self.db.get_dca_stats(symbol)
        ladder_settings = self.db.get_ladder_settings(symbol)
        recommendation = self.db.get_recommendation_for_current_drop(current_price, symbol)
        msg = f"➕ *Добавление покупки вручную*\n\n"
        msg += f"💰 Текущая цена {symbol}: `{format_price(current_price, 4)}` USDT\n\n"
        if stats and stats['avg_price'] > 0:
            current_drop = calculate_current_drop(current_price, stats['avg_price'])
            msg += f"📉 Средняя цена: `{format_price(stats['avg_price'], 4)}` USDT\n"
            msg += f"📉 Падение от средней цены: `{current_drop:.1f}%`\n\n"
        if recommendation['success']:
            if recommendation['is_first']:
                msg += f"🟢 *ПЕРВАЯ ПОКУПКА*\n💰 Рекомендуемая сумма: `{recommendation['amount_usdt']:.2f}` USDT\n\n"
            else:
                msg += f"🟢 *РЕКОМЕНДАЦИЯ ПО МАРТИНГЕЙЛУ:*\n📉 Уровень падения: `{recommendation['drop_percent']:.1f}%`\n📊 Коэффициент: `{recommendation['ratio']:.4f}`\n💰 Рекомендуемая сумма покупки: `{recommendation['amount_usdt']:.2f}` USDT\n\n"
        msg += f"Введите цену покупки (USDT):"
        await update.message.reply_text(msg, reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return MANUAL_ADD_PRICE
    
    async def manual_add_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.cancel_action(update, context)
        try:
            price_str = text.replace(',', '.').strip()
            price = float(price_str)
            if price <= 0:
                raise ValueError("Цена должна быть положительной")
            context.user_data['manual_price'] = price
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            stats = self.db.get_dca_stats(symbol)
            ladder_settings = self.db.get_ladder_settings(symbol)
            if stats and stats['avg_price'] > 0:
                drop_percent = calculate_current_drop(price, stats['avg_price'])
                recommendation = self.db.get_recommendation_for_current_drop(price, symbol)
                suggested_amount = recommendation.get('amount_usdt', 1.1) if recommendation['success'] else 1.1
                await update.message.reply_text(
                    f"✅ Цена {format_price(price, 4)} USDT\n"
                    f"📉 Падение от средней цены ({format_price(stats['avg_price'], 4)}): `{drop_percent:.1f}%`\n\n"
                    f"💰 Введите количество монет (в {symbol.replace('USDT', '')}):\n"
                    f"*Рекомендуемая сумма:* {suggested_amount:.2f} USDT\n"
                    f"*Минимальное количество:* 0.000001",
                    reply_markup=self.get_cancel_keyboard(),
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    f"✅ Цена {format_price(price, 4)} USDT\n\n"
                    f"💰 Введите количество монет (в {symbol.replace('USDT', '')}):\n"
                    f"*Рекомендуемая сумма:* {ladder_settings['base_amount']:.2f} USDT\n"
                    f"*Минимальное количество:* 0.000001",
                    reply_markup=self.get_cancel_keyboard(),
                    parse_mode='Markdown'
                )
            return MANUAL_ADD_AMOUNT
        except ValueError as e:
            await update.message.reply_text(f"❌ Ошибка! Введите корректную цену.\nПример: 2.35 или 2,35\n\nОшибка: {str(e)}", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_PRICE
    
    async def manual_add_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.cancel_action(update, context)
        try:
            quantity_str = text.replace(',', '.').strip()
            quantity = float(quantity_str)
            if quantity <= 0:
                raise ValueError("Количество должно быть положительным")
            price = context.user_data.get('manual_price')
            if not price:
                await self._reset_bot_state(context)
                await update.message.reply_text("❌ Ошибка: цена не найдена. Попробуйте заново.", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            amount_usdt = price * quantity
            stats = self.db.get_dca_stats(symbol)
            drop_percent = 0
            step_level = 0
            if stats and stats['avg_price'] > 0:
                drop_percent = calculate_current_drop(price, stats['avg_price'])
                step_level = int(drop_percent)
            purchase_id = self.db.add_purchase(symbol=symbol, amount_usdt=amount_usdt, price=price, quantity=quantity, multiplier=1.0, drop_percent=drop_percent, step_level=step_level, date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            if purchase_id:
                msg = f"✅ *Покупка добавлена!*\n\n🆔 ID: `{purchase_id}`\n💰 Цена: `{format_price(price, 4)}` USDT\n📊 Количество: `{format_quantity(quantity, 2)}`\n💵 Сумма: `{amount_usdt:.2f}` USDT"
                if drop_percent > 0:
                    msg += f"\n📉 Падение от средней цены: `{drop_percent:.1f}%`"
                await update.message.reply_text(msg, reply_markup=self.get_main_keyboard(), parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Ошибка сохранения в базу данных", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        except ValueError as e:
            await update.message.reply_text(f"❌ Ошибка! Введите корректное количество.\nПример: 10.5 или 10,5\n\nОшибка: {str(e)}", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_AMOUNT
    
    # ============= ОБРАБОТЧИК НЕИЗВЕСТНЫХ КОМАНД =============
    async def handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        await update.message.reply_text("Используйте кнопки меню", reply_markup=self.get_main_keyboard())
    
    # ============= НАСТРОЙКА ОБРАБОТЧИКОВ =============
    def setup_handlers(self):
        logger.info("Setting up handlers...")
        self.application.add_handler(CommandHandler("start", self.cmd_start_fast))
        self.application.add_handler(CallbackQueryHandler(self.handle_order_execution_callback, pattern='^(add_order_|skip_order_|clear_stats_|skip_clear_|do_clear_|cancel_clear_|confirm_clear_stats_|skip_clear_stats_)'))
        
        # Глобальные команды, не входящие в диалоги
        self.application.add_handler(MessageHandler(filters.Regex('^(📊 Мой Портфель)$'), self.show_portfolio))
        self.application.add_handler(MessageHandler(filters.Regex('^(🚀 Запустить Авто DCA|⏹ Остановить Авто DCA)$'), self.toggle_dca))
        self.application.add_handler(MessageHandler(filters.Regex('^(📈 Статистика DCA)$'), self.show_dca_stats_detailed))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 Статус бота)$'), self.show_status))
        self.application.add_handler(MessageHandler(filters.Regex('^(📝 Управление ордерами)$'), self.orders_menu))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 Список открытых ордеров)$'), self.show_open_orders))
        self.application.add_handler(MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.return_to_main_menu))
        self.application.add_handler(MessageHandler(filters.Regex('^(✅ Да, выставить ордер на продажу|❌ Нет, отмена)$'), self.handle_sell_confirmation))
        
        # Диалог добавления покупки вручную
        manual_add_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(➕ Добавить покупку вручную)$'), self.manual_add_start)],
            states={
                MANUAL_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_price)],
                MANUAL_ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_amount)],
            },
            fallbacks=[
                MessageHandler(filters.Regex('^❌ Отмена$'), self.cancel_action),
                CommandHandler("cancel", self.cancel_action)
            ],
            conversation_timeout=CONVERSATION_TIMEOUT,
            name="manual_add_conversation", persistent=False
        )
        self.application.add_handler(manual_add_conv)
        
        # Главный диалог настроек (вход по кнопке "⚙️ Настройки")
        main_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(⚙️ Настройки)$'), self.settings_menu)],
            states={
                SELECTING_ACTION: [
                    MessageHandler(filters.Regex('^(🪙 Выбор токена)$'), self.set_symbol_start),
                    MessageHandler(filters.Regex('^(💵 Сумма покупки)$'), self.set_amount_start),
                    MessageHandler(filters.Regex('^(📊 Процент прибыли)$'), self.set_profit_start),
                    MessageHandler(filters.Regex('^(📉 Настройки падения)$'), self.set_drop_start),
                    MessageHandler(filters.Regex('^(⏰ Время покупки)$'), self.set_time_start),
                    MessageHandler(filters.Regex('^(🔄 Частота покупки)$'), self.set_frequency_start),
                    MessageHandler(filters.Regex('^(🪜 Настройка лестницы)$'), self.ladder_settings_menu),
                    MessageHandler(filters.Regex('^(⚙️ Настройки отслеживания)$'), self.tracking_settings),
                    MessageHandler(filters.Regex('^(🔔 Уведомления о покупке)$'), self.purchase_notify_settings),
                    MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.return_to_main_menu),
                ],
                SELECTING_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_symbol_selection)],
                SET_SYMBOL_MANUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_symbol_manual)],
                SET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_amount_done)],
                SET_PROFIT_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_profit_done)],
                SET_MAX_DROP: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_drop_done)],
                SET_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_time_done)],
                SET_FREQUENCY_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_frequency_done)],
            },
            fallbacks=[
                MessageHandler(filters.Regex('^❌ Отмена$'), self.cancel_action),
                CommandHandler("cancel", self.cancel_action)
            ],
            conversation_timeout=CONVERSATION_TIMEOUT,
            name="main_conversation", persistent=False
        )
        self.application.add_handler(main_conv)
        
        # ВАЖНО: этот обработчик должен быть ПОСЛЕДНИМ
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_unknown))
        logger.info("Handlers setup completed")
    
    # ============= ЗАПУСК =============
    def run(self):
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"{Fore.CYAN}🚀 ЗАПУСК DCA BYBIT BOT (МАРТИНГЕЙЛ ЛЕСТНИЦОЙ)")
        print(f"{Fore.CYAN}Версия: {BOT_VERSION}")
        print(f"{Fore.CYAN}{'='*60}")
        if not TELEGRAM_TOKEN:
            print(f"{Fore.RED}❌ TELEGRAM_BOT_TOKEN не найден!")
            return
        print(f"{Fore.GREEN}✅ Токен: {TELEGRAM_TOKEN[:10]}...{TELEGRAM_TOKEN[-5:]}")
        print(f"{Fore.WHITE}👤 Пользователь: {AUTHORIZED_USER}")
        print(f"{Fore.WHITE}🌐 Testnet: {'Да' if BYBIT_TESTNET else 'Нет'}")
        print(f"{Fore.WHITE}💾 База данных: dca_bot.db (данные сохраняются)")
        print(f"{Fore.CYAN}{'='*60}\n")
        self.application.post_init = self.post_init
        try:
            self.application.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0, timeout=60)
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            print(f"{Fore.RED}❌ Ошибка: {e}")

if __name__ == "__main__":
    try:
        import colorama
    except ImportError:
        os.system(f"{sys.executable} -m pip install colorama")
        import colorama
    bot = FastDCABot()
    bot.run()