#!/usr/bin/env python3
"""
DCA Bybit Trading Bot - МАРТИНГЕЙЛ ЛЕСЕНКОЙ
Непрерывный расчёт коэффициента на каждый процент падения
Версия 3.4.6 (13.04.2026)
ПОЛНАЯ ВЕРСИЯ СО ВСЕМИ МЕТОДАМИ
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

BOT_VERSION = "3.4.6 (13.04.2026)"
CONVERSATION_TIMEOUT = 180

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

# ============= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =============
def format_price(price: float, decimals: int = 4) -> str:
    if price is None: return "N/A"
    return f"{price:.{decimals}f}"

def format_quantity(qty: float, decimals: int = 2) -> str:
    if qty is None: return "N/A"
    return f"{qty:.{decimals}f}"

def round_price_up(price: float) -> float:
    return math.ceil(price * 100) / 100

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
            
            cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS dca_purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, amount_usdt REAL NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL, multiplier REAL DEFAULT 1.0, drop_percent REAL DEFAULT 0, step_level INTEGER DEFAULT 0, date TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS sell_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, order_id TEXT NOT NULL UNIQUE, quantity REAL NOT NULL, target_price REAL NOT NULL, profit_percent REAL NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'active')''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS pending_sell_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, quantity REAL NOT NULL, target_price REAL NOT NULL, profit_percent REAL NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'pending')''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS completed_sells (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, order_id TEXT NOT NULL, quantity REAL NOT NULL, sell_price REAL NOT NULL, profit_percent REAL NOT NULL, profit_usdt REAL NOT NULL, sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notified BOOLEAN DEFAULT 0, stats_cleared BOOLEAN DEFAULT 0)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, symbol TEXT, details TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS dca_start (id INTEGER PRIMARY KEY, start_date TIMESTAMP, symbol TEXT, initial_price REAL)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, enabled BOOLEAN DEFAULT 1, alert_percent REAL DEFAULT 10.0, alert_interval_minutes INTEGER DEFAULT 30, last_check TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS ladder_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, max_depth REAL NOT NULL, base_amount REAL NOT NULL, max_amount REAL NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS executed_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL, amount_usdt REAL NOT NULL, executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, added_to_stats BOOLEAN DEFAULT 0, skipped BOOLEAN DEFAULT 0, notified_at TIMESTAMP)''')
            
            defaults = [
                ('symbol', 'TONUSDT'), ('invest_amount', '1.1'), ('profit_percent', '5'), ('max_drop_percent', '80'),
                ('max_multiplier', '3'), ('schedule_time', '09:00'), ('frequency_hours', '24'), ('dca_active', 'false'),
                ('ladder_max_depth', '80'), ('ladder_base_amount', '1.1'), ('ladder_max_amount', '3.3'),
                ('order_execution_notify', 'true'), ('order_check_interval_minutes', '60'), ('sell_tracking_enabled', 'true'),
                ('purchase_notify_enabled', 'true'), ('purchase_notify_time', '06:00'),
            ]
            for key, value in defaults:
                cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
            cursor.execute('INSERT OR IGNORE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check) VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)')
            conn.commit()
            conn.close()
            logger.info("Database initialized")
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
        except: return default
    
    def set_setting(self, key: str, value: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (key, value))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting {key}: {e}")
    
    def get_purchases(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol: cursor.execute('SELECT * FROM dca_purchases WHERE symbol = ? ORDER BY date ASC', (symbol,))
            else: cursor.execute('SELECT * FROM dca_purchases ORDER BY date ASC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except: return []
    
    def add_purchase(self, symbol: str, amount_usdt: float, price: float, quantity: float, multiplier: float = 1.0, drop_percent: float = 0, step_level: int = 0, date: str = None):
        if date is None: date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO dca_purchases (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date))
            purchase_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return purchase_id
        except: return None
    
    def get_dca_stats(self, symbol: str) -> Dict:
        purchases = self.get_purchases(symbol)
        if not purchases: return None
        total_usdt = sum(p['amount_usdt'] for p in purchases)
        total_qty = sum(p['quantity'] for p in purchases)
        avg_price = total_usdt / total_qty if total_qty > 0 else 0
        return {'total_purchases': len(purchases), 'total_usdt': total_usdt, 'total_quantity': total_qty, 'avg_price': avg_price}
    
    def get_ladder_settings(self, symbol: str = None) -> Dict:
        if symbol is None: symbol = self.get_setting('symbol', 'TONUSDT')
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM ladder_settings WHERE symbol = ? ORDER BY created_at DESC LIMIT 1', (symbol,))
            row = cursor.fetchone()
            conn.close()
            if row: return dict(row)
            else: return {'symbol': symbol, 'max_depth': float(self.get_setting('ladder_max_depth', '80')), 'base_amount': float(self.get_setting('ladder_base_amount', '1.1')), 'max_amount': float(self.get_setting('ladder_max_amount', '3.3'))}
        except: return {'symbol': symbol, 'max_depth': 80, 'base_amount': 1.1, 'max_amount': 3.3}
    
    def save_ladder_settings(self, settings: Dict):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM ladder_settings WHERE symbol = ?', (settings['symbol'],))
            cursor.execute('INSERT INTO ladder_settings (symbol, max_depth, base_amount, max_amount) VALUES (?, ?, ?, ?)', (settings['symbol'], settings['max_depth'], settings['base_amount'], settings['max_amount']))
            conn.commit()
            conn.close()
            self.set_setting('ladder_max_depth', str(settings['max_depth']))
            self.set_setting('ladder_base_amount', str(settings['base_amount']))
            self.set_setting('ladder_max_amount', str(settings['max_amount']))
        except Exception as e:
            logger.error(f"Error saving ladder: {e}")
    
    def calculate_ladder_purchase(self, current_price: float, symbol: str = None) -> Dict:
        if symbol is None: symbol = self.get_setting('symbol', 'TONUSDT')
        stats = self.get_dca_stats(symbol)
        settings = self.get_ladder_settings(symbol)
        if not stats or stats['total_quantity'] <= 0:
            return {'should_buy': True, 'step_level': 0, 'amount_usdt': settings['base_amount'], 'target_price': current_price, 'drop_percent': 0, 'reason': 'Первая покупка'}
        avg_price = stats['avg_price']
        current_drop = calculate_current_drop(current_price, avg_price)
        purchases = self.get_purchases(symbol)
        max_purchased_drop = max([p.get('drop_percent', 0) for p in purchases], default=0)
        if current_drop > max_purchased_drop + 0.01:
            amount_usdt = get_amount_by_drop(current_drop, settings['base_amount'], settings['max_amount'], settings['max_depth'])
            if current_drop >= settings['max_depth']:
                return {'should_buy': False, 'step_level': int(current_drop), 'amount_usdt': amount_usdt, 'target_price': current_price, 'reason': f'Достигнута макс. глубина ({settings["max_depth"]}%)'}
            return {'should_buy': True, 'step_level': int(current_drop), 'amount_usdt': amount_usdt, 'target_price': current_price, 'drop_percent': current_drop, 'current_drop': current_drop, 'reason': f'Падение {current_drop:.1f}%'}
        next_drop = max_purchased_drop + 1.0
        next_price = avg_price * (1 - next_drop / 100)
        return {'should_buy': False, 'step_level': 0, 'amount_usdt': 0, 'target_price': next_price, 'current_drop': current_drop, 'next_drop': next_drop, 'reason': f'Ждем падения до {next_drop:.1f}%'}
    
    def get_recommendation_for_current_drop(self, current_price: float, symbol: str = None) -> Dict:
        if symbol is None: symbol = self.get_setting('symbol', 'TONUSDT')
        stats = self.get_dca_stats(symbol)
        settings = self.get_ladder_settings(symbol)
        if not stats or stats['total_quantity'] <= 0:
            return {'success': True, 'drop_percent': 0, 'ratio': 0, 'amount_usdt': settings['base_amount'], 'level': 0, 'avg_price': 0, 'is_first': True}
        avg_price = stats['avg_price']
        drop_percent = calculate_current_drop(current_price, avg_price)
        amount = get_amount_by_drop(drop_percent, settings['base_amount'], settings['max_amount'], settings['max_depth'])
        level, ratio = get_ladder_levels(drop_percent, settings['max_depth'])
        return {'success': True, 'drop_percent': drop_percent, 'ratio': ratio, 'amount_usdt': amount, 'level': level, 'avg_price': avg_price, 'current_drop': drop_percent, 'is_first': False}
    
    def get_order_execution_notify(self) -> bool: return self.get_setting('order_execution_notify', 'true') == 'true'
    def set_order_execution_notify(self, enabled: bool): self.set_setting('order_execution_notify', 'true' if enabled else 'false')
    def get_sell_tracking_enabled(self) -> bool: return self.get_setting('sell_tracking_enabled', 'true') == 'true'
    def set_sell_tracking_enabled(self, enabled: bool): self.set_setting('sell_tracking_enabled', 'true' if enabled else 'false')
    def get_order_check_interval(self) -> int: return int(self.get_setting('order_check_interval_minutes', '60'))
    def set_order_check_interval(self, minutes: int): self.set_setting('order_check_interval_minutes', str(minutes))
    def get_purchase_notify_enabled(self) -> bool: return self.get_setting('purchase_notify_enabled', 'false') == 'true'
    def set_purchase_notify_enabled(self, enabled: bool): self.set_setting('purchase_notify_enabled', 'true' if enabled else 'false')
    def get_purchase_notify_time(self) -> str: return self.get_setting('purchase_notify_time', '06:00')
    def set_purchase_notify_time(self, notify_time: str): self.set_setting('purchase_notify_time', notify_time)
    def get_last_full_check_time(self) -> Optional[datetime]:
        ts = self.get_setting('last_full_check_time', '')
        return datetime.fromisoformat(ts) if ts else None
    def set_last_full_check_time(self, check_time: datetime): self.set_setting('last_full_check_time', check_time.isoformat())
    def get_first_order_date(self) -> Optional[datetime]:
        ts = self.get_setting('first_order_date', '')
        return datetime.fromisoformat(ts) if ts else None
    def set_dca_start(self, symbol: str, initial_price: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_start')
            cursor.execute('INSERT INTO dca_start (id, start_date, symbol, initial_price) VALUES (1, CURRENT_TIMESTAMP, ?, ?)', (symbol, initial_price))
            conn.commit()
            conn.close()
        except: pass
    def clear_all_purchases(self, symbol: str) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE symbol = ?', (symbol,))
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted
        except: return 0
    def reset_ladder(self, symbol: str = None):
        if symbol is None: symbol = self.get_setting('symbol', 'TONUSDT')
        self.clear_all_purchases(symbol)
    def mark_order_as_added(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET added_to_stats = 1 WHERE order_id = ?', (order_id,))
            conn.commit()
            conn.close()
            return True
        except: return False
    def mark_order_as_skipped(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET skipped = 1 WHERE order_id = ?', (order_id,))
            conn.commit()
            conn.close()
            return True
        except: return False
    def add_executed_order(self, order_id: str, symbol: str, price: float, quantity: float, amount_usdt: float, executed_at: str = None) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            if executed_at:
                cursor.execute('INSERT OR IGNORE INTO executed_orders (order_id, symbol, price, quantity, amount_usdt, executed_at) VALUES (?, ?, ?, ?, ?, ?)', (order_id, symbol, price, quantity, amount_usdt, executed_at))
            else:
                cursor.execute('INSERT OR IGNORE INTO executed_orders (order_id, symbol, price, quantity, amount_usdt) VALUES (?, ?, ?, ?, ?)', (order_id, symbol, price, quantity, amount_usdt))
            conn.commit()
            conn.close()
            return True
        except: return False
    def add_sell_order(self, symbol: str, order_id: str, quantity: float, target_price: float, profit_percent: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO sell_orders (symbol, order_id, quantity, target_price, profit_percent, status) VALUES (?, ?, ?, ?, ?, "active")', (symbol, order_id, quantity, target_price, profit_percent))
            conn.commit()
            conn.close()
        except: pass
    def get_active_sell_orders(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol: cursor.execute('SELECT * FROM sell_orders WHERE symbol = ? AND status = "active"', (symbol,))
            else: cursor.execute('SELECT * FROM sell_orders WHERE status = "active"')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except: return []
    def delete_sell_order(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM sell_orders WHERE order_id = ?', (order_id,))
            conn.commit()
            conn.close()
            return True
        except: return False
    def log_action(self, action: str, symbol: str = None, details: str = None):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO history (action, symbol, details) VALUES (?, ?, ?)', (action, symbol, details))
            conn.commit()
            conn.close()
        except: pass
    def add_pending_sell_order(self, symbol: str, quantity: float, target_price: float, profit_percent: float) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO pending_sell_orders (symbol, quantity, target_price, profit_percent) VALUES (?, ?, ?, ?)', (symbol, quantity, target_price, profit_percent))
            oid = cursor.lastrowid
            conn.commit()
            conn.close()
            return oid
        except: return 0
    def mark_completed_sell_stats_cleared(self, sell_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET stats_cleared = 1 WHERE id = ?', (sell_id,))
            conn.commit()
            conn.close()
        except: pass
    def mark_completed_sell_notified(self, sell_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET notified = 1 WHERE id = ?', (sell_id,))
            conn.commit()
            conn.close()
        except: pass

# ============= BYBIT КЛИЕНТ =============
class BybitClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key, self.api_secret, self.testnet = api_key, api_secret, testnet
        self.session = None
        self._init_session()
    def _init_session(self):
        try: self.session = HTTP(testnet=self.testnet, api_key=self.api_key, api_secret=self.api_secret, recv_window=5000)
        except: pass
    async def get_symbol_price(self, symbol: str) -> Optional[float]:
        try:
            if not self.session: self._init_session()
            resp = self.session.get_tickers(category="spot", symbol=symbol)
            if resp['retCode'] == 0 and resp['result']['list']: return float(resp['result']['list'][0]['lastPrice'])
        except: pass
        return None
    async def get_balance(self, coin: str = None) -> Dict:
        try:
            if not self.session: self._init_session()
            resp = self.session.get_wallet_balance(accountType="UNIFIED")
            if resp['retCode'] == 0 and resp['result']['list']:
                coins = resp['result']['list'][0].get('coin', [])
                if coin:
                    for c in coins:
                        if c.get('coin') == coin:
                            equity = float(c.get('equity', 0) or 0)
                            available = float(c.get('walletBalance', 0) or 0) - float(c.get('locked', 0) or 0)
                            return {'coin': coin, 'equity': equity, 'available': available, 'usdValue': float(c.get('usdValue', 0) or 0)}
                else: return {'total_equity': float(resp['result']['list'][0].get('totalEquity', 0) or 0)}
        except: pass
        return {}
    async def get_open_orders(self, symbol: str = None) -> List[Dict]:
        try:
            if not self.session: self._init_session()
            params = {"category": "spot"}
            if symbol: params['symbol'] = symbol
            resp = self.session.get_open_orders(**params)
            if resp['retCode'] == 0: return resp['result']['list']
        except: pass
        return []
    async def get_open_orders_by_side(self, symbol: str = None) -> Dict[str, List[Dict]]:
        orders = await self.get_open_orders(symbol)
        return {'buy': [o for o in orders if o.get('side') == 'Buy'], 'sell': [o for o in orders if o.get('side') == 'Sell']}
    async def cancel_order(self, symbol: str, order_id: str) -> Dict:
        try:
            if not self.session: self._init_session()
            resp = self.session.cancel_order(category="spot", symbol=symbol, orderId=order_id)
            if resp['retCode'] == 0: return {'success': True}
            return {'success': False, 'error': resp['retMsg']}
        except Exception as e: return {'success': False, 'error': str(e)}
    async def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        try:
            if not self.session: self._init_session()
            resp = self.session.place_order(category="spot", symbol=symbol, side="Sell", orderType="Limit", qty=str(quantity), price=str(price), timeInForce="GTC")
            if resp['retCode'] == 0: return {'success': True, 'order_id': resp['result']['orderId'], 'quantity': quantity, 'price': price}
            return {'success': False, 'error': resp['retMsg']}
        except Exception as e: return {'success': False, 'error': str(e)}
    async def place_market_buy(self, symbol: str, amount_usdt: float) -> Dict:
        try:
            if not self.session: self._init_session()
            price = await self.get_symbol_price(symbol)
            if not price: return {'success': False, 'error': 'Нет цены'}
            quantity = amount_usdt / price
            resp = self.session.place_order(category="spot", symbol=symbol, side="Buy", orderType="Market", qty=str(quantity))
            if resp['retCode'] == 0:
                await asyncio.sleep(1)
                return {'success': True, 'order_id': resp['result']['orderId'], 'quantity': quantity, 'price': price, 'total_usdt': amount_usdt}
            return {'success': False, 'error': resp['retMsg']}
        except Exception as e: return {'success': False, 'error': str(e)}
    async def place_limit_buy(self, symbol: str, price: float, amount_usdt: float) -> Dict:
        try:
            if not self.session: self._init_session()
            quantity = amount_usdt / price
            resp = self.session.place_order(category="spot", symbol=symbol, side="Buy", orderType="Limit", qty=str(quantity), price=str(price), timeInForce="GTC")
            if resp['retCode'] == 0: return {'success': True, 'order_id': resp['result']['orderId'], 'quantity': quantity, 'price': price, 'total_usdt': amount_usdt}
            return {'success': False, 'error': resp['retMsg']}
        except Exception as e: return {'success': False, 'error': str(e)}
    async def cancel_all_sell_orders(self, symbol: str) -> Tuple[int, List[str]]:
        orders = await self.get_open_orders(symbol)
        sell_orders = [o for o in orders if o.get('side') == 'Sell']
        cancelled = []
        for o in sell_orders:
            res = await self.cancel_order(symbol, o.get('orderId'))
            if res['success']: cancelled.append(o.get('orderId'))
        return len(cancelled), cancelled
    async def get_all_executed_orders(self, symbol: str, from_date: datetime = None) -> List[Dict]:
        return []
    async def get_completed_sell_orders(self, symbol: str = None, from_date: datetime = None) -> List[Dict]:
        return []
    async def get_instrument_info(self, symbol: str) -> Dict:
        return {'min_qty': 0.01, 'min_amt': 10, 'qty_step': 0.01}

# ============= СТРАТЕГИЯ DCA =============
class DCAStrategy:
    def __init__(self, db: Database, bybit: BybitClient):
        self.db, self.bybit = db, bybit
    async def execute_ladder_purchase(self, symbol: str, profit_percent: float) -> Dict:
        price = await self.bybit.get_symbol_price(symbol)
        if not price: return {'success': False, 'error': 'Нет цены'}
        info = self.db.calculate_ladder_purchase(price, symbol)
        if not info['should_buy']: return {'success': False, 'error': info['reason']}
        bal = await self.bybit.get_balance('USDT')
        if bal.get('available', 0) < info['amount_usdt']: return {'success': False, 'error': 'Недостаточно USDT'}
        res = await self.bybit.place_market_buy(symbol, info['amount_usdt'])
        if res['success']:
            self.db.add_purchase(symbol, res['total_usdt'], res['price'], res['quantity'], drop_percent=info.get('drop_percent', 0), step_level=info['step_level'])
            target = res['price'] * (1 + profit_percent / 100)
            sell_res = await self.bybit.place_limit_sell(symbol, res['quantity'], target)
            if sell_res['success']: self.db.add_sell_order(symbol, sell_res['order_id'], res['quantity'], target, profit_percent)
            else: res['sell_warning'] = sell_res.get('error')
            return res
        return res
    async def get_recommended_purchase(self, symbol: str) -> Dict:
        price = await self.bybit.get_symbol_price(symbol)
        if not price: return {'success': False}
        info = self.db.calculate_ladder_purchase(price, symbol)
        return {'success': True, 'should_buy': info['should_buy'], 'amount_usdt': info['amount_usdt'], 'current_drop': info.get('current_drop', 0), 'next_buy_price': info.get('target_price'), 'next_drop': info.get('next_drop', 0)}
    def calculate_target_info(self, stats: Dict, profit_percent: float) -> Dict:
        if not stats: return None
        target = stats['avg_price'] * (1 + profit_percent / 100)
        return {'target_price': target, 'total_qty': stats['total_quantity'], 'target_value': stats['total_quantity'] * target, 'target_profit': stats['total_quantity'] * target - stats['total_usdt']}
    async def place_full_sell_order(self, update, symbol: str, profit_percent: float, auto_cancel_old: bool = True) -> Dict:
        stats = self.db.get_dca_stats(symbol)
        if not stats: return {'success': False, 'error': 'Нет покупок'}
        info = self.calculate_target_info(stats, profit_percent)
        price = round_price_up(info['target_price'])
        qty = stats['total_quantity']
        if auto_cancel_old: await self.bybit.cancel_all_sell_orders(symbol)
        res = await self.bybit.place_limit_sell(symbol, qty, price)
        if res['success']:
            self.db.add_sell_order(symbol, res['order_id'], qty, price, profit_percent)
            return {'success': True, 'order_id': res['order_id'], 'quantity': qty, 'price': price, 'profit_percent': profit_percent}
        return res
    async def check_completed_sells(self, symbol: str, user_id: int, bot) -> List[Dict]: return []
    async def force_check_executed_orders(self, symbol: str, bot, user_id: int) -> Dict: return {'total_found': 0, 'already_added': 0, 'missing': []}
    async def force_check_completed_sells(self, symbol: str, bot, user_id: int) -> Dict: return {'total_found': 0, 'already_processed': 0, 'missing': []}
    async def check_pending_sell_orders(self, symbol: str, user_id: int, bot) -> List[Dict]: return []
    async def check_and_update_sell_orders(self, symbol: str): pass
    async def auto_check_and_notify(self, symbol: str, user_id: int, bot) -> Dict: return {'type': 'incremental', 'count': 0}

# ============= ОСНОВНОЙ КЛАСС БОТА =============
class FastDCABot:
    def __init__(self):
        self.db = Database()
        self.bybit, self.strategy, self.bybit_initialized = None, None, False
        self.import_waiting = False
        builder = Application.builder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=60, read_timeout=60))
        self.application = builder.build()
        self.scheduler_running, self.authorized_user_id = False, None
        self.setup_handlers()
    
    def _init_bybit(self):
        if not self.bybit_initialized and BYBIT_API_KEY:
            self.bybit = BybitClient(BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET)
            self.strategy = DCAStrategy(self.db, self.bybit)
            self.bybit_initialized = True
    
    # ============= КЛАВИАТУРЫ =============
    def get_main_keyboard(self):
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        dca_btn = "⏹ Остановить Авто DCA" if is_active else "🚀 Запустить Авто DCA"
        return ReplyKeyboardMarkup([
            [KeyboardButton("📊 Мой Портфель"), KeyboardButton(dca_btn)],
            [KeyboardButton("💰 Ручная покупка (лимит)"), KeyboardButton("📈 Статистика DCA")],
            [KeyboardButton("➕ Добавить покупку вручную"), KeyboardButton("✏️ Редактировать покупки")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("📝 Управление ордерами")],
            [KeyboardButton("📋 Статус бота")],
        ], resize_keyboard=True)
    
    def get_cancel_keyboard(self): return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    def get_settings_keyboard(self):
        return ReplyKeyboardMarkup([
            [KeyboardButton("🪙 Выбор токена"), KeyboardButton("💵 Сумма покупки")],
            [KeyboardButton("📊 Процент прибыли"), KeyboardButton("📉 Настройки падения")],
            [KeyboardButton("⏰ Время покупки"), KeyboardButton("🔄 Частота покупки")],
            [KeyboardButton("🪜 Настройка лестницы"), KeyboardButton("⚙️ Настройки отслеживания")],
            [KeyboardButton("🔔 Уведомления о покупке"), KeyboardButton("🔙 Назад в меню")],
        ], resize_keyboard=True)
    def get_symbol_selection_keyboard(self):
        kb = [[KeyboardButton(s)] for s in POPULAR_SYMBOLS]
        kb.append([KeyboardButton("✏️ Ввести свой токен")])
        kb.append([KeyboardButton("❌ Отмена")])
        return ReplyKeyboardMarkup(kb, resize_keyboard=True)
    def get_order_management_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("📋 Список открытых ордеров"), KeyboardButton("❌ Удалить ордер")], [KeyboardButton("🔙 Назад в меню")]], resize_keyboard=True)
    def get_tracking_settings_keyboard(self):
        t = "✅ Отслеживание ордеров Вкл" if self.db.get_order_execution_notify() else "❌ Отслеживание ордеров Выкл"
        s = "💰 Отслеживание продаж Вкл" if self.db.get_sell_tracking_enabled() else "⏳ Отслеживание продаж Выкл"
        return ReplyKeyboardMarkup([[KeyboardButton(t)], [KeyboardButton(s)], [KeyboardButton(f"⏱ Интервал проверки Ордеров {self.db.get_order_check_interval()} мин")], [KeyboardButton("🔍 Тест отслеживания")], [KeyboardButton("🔙 Назад в настройки")]], resize_keyboard=True)
    def get_purchase_notify_settings_keyboard(self):
        e = "🔔 Уведомления Вкл" if self.db.get_purchase_notify_enabled() else "🔕 Уведомления Выкл"
        return ReplyKeyboardMarkup([[KeyboardButton(e)], [KeyboardButton(f"⏰ Время уведомления ({self.db.get_purchase_notify_time()})")], [KeyboardButton("🔙 Назад в настройки")]], resize_keyboard=True)
    def get_ladder_settings_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("📉 Глубина просадки (%)"), KeyboardButton("💵 Базовая сумма")], [KeyboardButton("📋 Текущие настройки"), KeyboardButton("🔄 Сбросить лестницу")], [KeyboardButton("🔙 Назад в меню")]], resize_keyboard=True)
    def get_sell_confirmation_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("✅ Да, выставить ордер на продажу")], [KeyboardButton("❌ Нет, отмена")]], resize_keyboard=True)
    def get_manual_buy_keyboard(self): return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    # ============= ПРОВЕРКА И СБРОС =============
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
        context.user_data.clear()
        self.import_waiting = False
    
    async def return_to_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("🏠 Главное меню:", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def cancel_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("❌ Действие отменено", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    # ============= КОМАНДЫ =============
    async def cmd_start_fast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return
        await self._reset_bot_state(context)
        await update.message.reply_text(f"👋 Привет! DCA Bot v{BOT_VERSION}", reply_markup=self.get_main_keyboard())
    
    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ API не инициализирован"); return
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        price = await self.bybit.get_symbol_price(symbol)
        stats = self.db.get_dca_stats(symbol)
        bal = await self.bybit.get_balance(symbol.replace('USDT', ''))
        usdt = await self.bybit.get_balance('USDT')
        msg = f"📊 *Портфель*\n💵 USDT: {usdt.get('available', 0):.2f}\n🪙 {symbol}: {bal.get('equity', 0):.2f}\n💰 Цена: {format_price(price)}"
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def show_dca_stats_detailed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return
        await self._reset_bot_state(context)
        self._init_bybit()
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        stats = self.db.get_dca_stats(symbol)
        if not stats:
            await update.message.reply_text("Нет покупок"); return
        price = await self.bybit.get_symbol_price(symbol)
        drop = calculate_current_drop(price, stats['avg_price']) if price else 0
        msg = f"📈 *DCA*\nСредняя: {format_price(stats['avg_price'])}\nКуплено: {format_quantity(stats['total_quantity'])}\nПадение: {drop:.1f}%"
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def show_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return
        await self._reset_bot_state(context)
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        active = self.db.get_setting('dca_active', 'false') == 'true'
        msg = f"📋 *Статус*\nТокен: {symbol}\nDCA: {'✅' if active else '⏹'}"
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def toggle_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return
        await self._reset_bot_state(context)
        self._init_bybit()
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        self.db.set_setting('dca_active', 'false' if is_active else 'true')
        await update.message.reply_text(f"DCA {'остановлен' if is_active else 'запущен'}", reply_markup=self.get_main_keyboard())
    
    # ============= НАСТРОЙКИ =============
    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return ConversationHandler.END
        await self._reset_bot_state(context)
        s = self.db.get_setting
        msg = f"⚙️ *Настройки*\n🪙 {s('symbol')}\n💵 {s('invest_amount')} USDT\n📈 {s('profit_percent')}%"
        await update.message.reply_text(msg, reply_markup=self.get_settings_keyboard(), parse_mode='Markdown')
        return SELECTING_ACTION
    
    async def set_symbol_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🪙 Выберите токен:", reply_markup=self.get_symbol_selection_keyboard())
        return SELECTING_SYMBOL
    
    async def process_symbol_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        if text == "✏️ Ввести свой токен":
            await update.message.reply_text("Введите символ (например TONUSDT):", reply_markup=self.get_cancel_keyboard())
            return SET_SYMBOL_MANUAL
        if text in POPULAR_SYMBOLS:
            self.db.set_setting('symbol', text)
            await update.message.reply_text(f"✅ Токен: {text}", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        await update.message.reply_text("❌ Неверный выбор", reply_markup=self.get_symbol_selection_keyboard())
        return SELECTING_SYMBOL
    
    async def set_symbol_manual(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.upper().strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        self._init_bybit()
        price = await self.bybit.get_symbol_price(text) if self.bybit_initialized else None
        if price:
            self.db.set_setting('symbol', text)
            await update.message.reply_text(f"✅ {text}", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        await update.message.reply_text("❌ Токен не найден", reply_markup=self.get_cancel_keyboard())
        return SET_SYMBOL_MANUAL
    
    async def set_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"💵 Введите сумму (текущая: {self.db.get_setting('invest_amount')}):", reply_markup=self.get_cancel_keyboard())
        return SET_AMOUNT
    
    async def set_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            amt = float(text)
            if amt < 1: raise ValueError
            self.db.set_setting('invest_amount', str(amt))
            await update.message.reply_text(f"✅ {amt} USDT", reply_markup=self.get_settings_keyboard())
        except: await update.message.reply_text("❌ Некорректно", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_profit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"📊 Введите % прибыли (текущий: {self.db.get_setting('profit_percent')}%):", reply_markup=self.get_cancel_keyboard())
        return SET_PROFIT_PERCENT
    
    async def set_profit_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            p = float(text)
            if p < 0.1: raise ValueError
            self.db.set_setting('profit_percent', str(p))
            await update.message.reply_text(f"✅ {p}%", reply_markup=self.get_settings_keyboard())
        except: await update.message.reply_text("❌ Некорректно", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_drop_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📉 Введите макс. падение % и множитель (например: 80 3):", reply_markup=self.get_cancel_keyboard())
        return SET_MAX_DROP
    
    async def set_drop_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            d, m = map(float, text.split())
            self.db.set_setting('max_drop_percent', str(d)); self.db.set_setting('max_multiplier', str(m))
            await update.message.reply_text(f"✅ {d}% x{m}", reply_markup=self.get_settings_keyboard())
        except: await update.message.reply_text("❌ Формат: 80 3", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"⏰ Введите время (ЧЧ:ММ, текущее: {self.db.get_setting('schedule_time')}):", reply_markup=self.get_cancel_keyboard())
        return SET_SCHEDULE_TIME
    
    async def set_time_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            datetime.strptime(text, "%H:%M")
            self.db.set_setting('schedule_time', text)
            await update.message.reply_text(f"✅ {text}", reply_markup=self.get_settings_keyboard())
        except: await update.message.reply_text("❌ Формат ЧЧ:ММ", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_frequency_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"🔄 Введите частоту в часах (текущая: {self.db.get_setting('frequency_hours')}):", reply_markup=self.get_cancel_keyboard())
        return SET_FREQUENCY_HOURS
    
    async def set_frequency_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            h = int(text)
            if h < 1: raise ValueError
            self.db.set_setting('frequency_hours', str(h))
            await update.message.reply_text(f"✅ {h} ч", reply_markup=self.get_settings_keyboard())
        except: await update.message.reply_text("❌ Некорректно", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    # ============= ЛЕСТНИЦА =============
    async def ladder_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🪜 Настройка лестницы", reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    async def set_ladder_max_depth_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📉 Введите глубину просадки % (30-95):", reply_markup=self.get_cancel_keyboard())
        return SET_LADDER_DEPTH
    
    async def set_ladder_max_depth_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            d = float(text)
            if 30 <= d <= 95:
                s = self.db.get_ladder_settings()
                s['max_depth'] = d
                self.db.save_ladder_settings(s)
                await update.message.reply_text(f"✅ {d}%", reply_markup=self.get_ladder_settings_keyboard())
                return LADDER_MENU
        except: pass
        await update.message.reply_text("❌ 30-95", reply_markup=self.get_cancel_keyboard())
        return SET_LADDER_DEPTH
    
    async def set_ladder_base_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💵 Введите базовую сумму (мин 1 USDT):", reply_markup=self.get_cancel_keyboard())
        return SET_LADDER_BASE_AMOUNT
    
    async def set_ladder_base_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            a = float(text)
            if a >= 1:
                s = self.db.get_ladder_settings()
                s['base_amount'] = a
                s['max_amount'] = a * 3
                self.db.save_ladder_settings(s)
                await update.message.reply_text(f"✅ {a} USDT", reply_markup=self.get_ladder_settings_keyboard())
                return LADDER_MENU
        except: pass
        await update.message.reply_text("❌ Мин 1", reply_markup=self.get_cancel_keyboard())
        return SET_LADDER_BASE_AMOUNT
    
    async def show_ladder_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = self.db.get_ladder_settings()
        await update.message.reply_text(f"Глубина: {s['max_depth']}%\nБаза: {s['base_amount']} USDT\nМакс: {s['max_amount']} USDT", reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    async def reset_ladder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.db.reset_ladder()
        await update.message.reply_text("🔄 Лестница сброшена", reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    # ============= ТРЕКИНГ =============
    async def tracking_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ Настройки отслеживания", reply_markup=self.get_tracking_settings_keyboard())
        return NOTIFICATION_SETTINGS_MENU
    
    async def toggle_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cur = self.db.get_order_execution_notify()
        self.db.set_order_execution_notify(not cur)
        await update.message.reply_text(f"Отслеживание: {'Вкл' if not cur else 'Выкл'}", reply_markup=self.get_tracking_settings_keyboard())
        return NOTIFICATION_SETTINGS_MENU
    
    async def toggle_sell_tracking_in_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cur = self.db.get_sell_tracking_enabled()
        self.db.set_sell_tracking_enabled(not cur)
        await update.message.reply_text(f"Продажи: {'Вкл' if not cur else 'Выкл'}", reply_markup=self.get_tracking_settings_keyboard())
        return NOTIFICATION_SETTINGS_MENU
    
    async def set_tracking_interval_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏱ Введите интервал в минутах (5-1440):", reply_markup=self.get_cancel_keyboard())
        return WAITING_ORDER_CHECK_INTERVAL
    
    async def set_tracking_interval_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            m = int(text)
            if 5 <= m <= 1440:
                self.db.set_order_check_interval(m)
                await update.message.reply_text(f"✅ {m} мин", reply_markup=self.get_tracking_settings_keyboard())
                return NOTIFICATION_SETTINGS_MENU
        except: pass
        await update.message.reply_text("❌ 5-1440", reply_markup=self.get_cancel_keyboard())
        return WAITING_ORDER_CHECK_INTERVAL
    
    async def test_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔍 Тест отслеживания...", reply_markup=self.get_tracking_settings_keyboard())
        return NOTIFICATION_SETTINGS_MENU
    
    async def back_to_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ Настройки", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    # ============= УВЕДОМЛЕНИЯ О ПОКУПКЕ =============
    async def purchase_notify_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔔 Уведомления о покупке", reply_markup=self.get_purchase_notify_settings_keyboard())
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def toggle_purchase_notify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cur = self.db.get_purchase_notify_enabled()
        self.db.set_purchase_notify_enabled(not cur)
        await update.message.reply_text(f"Уведомления: {'Вкл' if not cur else 'Выкл'}", reply_markup=self.get_purchase_notify_settings_keyboard())
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def set_purchase_notify_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"⏰ Введите время (ЧЧ:ММ, текущее: {self.db.get_purchase_notify_time()}):", reply_markup=self.get_cancel_keyboard())
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def set_purchase_notify_time_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            datetime.strptime(text, "%H:%M")
            self.db.set_purchase_notify_time(text)
            await update.message.reply_text(f"✅ {text}", reply_markup=self.get_purchase_notify_settings_keyboard())
        except: await update.message.reply_text("❌ Формат ЧЧ:ММ", reply_markup=self.get_cancel_keyboard())
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def back_to_settings_from_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ Настройки", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    # ============= ДОБАВЛЕНИЕ ПОКУПКИ ВРУЧНУЮ =============
    async def manual_add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return ConversationHandler.END
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ API не инициализирован"); return ConversationHandler.END
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        price = await self.bybit.get_symbol_price(symbol)
        stats = self.db.get_dca_stats(symbol)
        rec = self.db.get_recommendation_for_current_drop(price, symbol)
        msg = f"➕ *Добавление покупки*\n💰 Цена {symbol}: {format_price(price)}\n"
        if stats:
            drop = calculate_current_drop(price, stats['avg_price'])
            msg += f"📉 Падение: {drop:.1f}%\n"
        msg += f"💡 Рекомендуемая сумма: {rec.get('amount_usdt', 1.1):.2f} USDT\n\nВведите цену покупки:"
        await update.message.reply_text(msg, reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return MANUAL_ADD_PRICE
    
    async def manual_add_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            price = float(text.replace(',', '.'))
            if price <= 0: raise ValueError
            context.user_data['manual_price'] = price
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            await update.message.reply_text(f"✅ Цена: {format_price(price)}\n💰 Введите количество {symbol.replace('USDT', '')}:", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_AMOUNT
        except:
            await update.message.reply_text("❌ Некорректная цена", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_PRICE
    
    async def manual_add_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            qty = float(text.replace(',', '.'))
            if qty <= 0: raise ValueError
            price = context.user_data.get('manual_price')
            if not price:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            symbol = self.db.get_setting('symbol', 'TONUSDT')
            amt = price * qty
            stats = self.db.get_dca_stats(symbol)
            drop = calculate_current_drop(price, stats['avg_price']) if stats else 0
            pid = self.db.add_purchase(symbol, amt, price, qty, drop_percent=drop, step_level=int(drop))
            if pid:
                msg = f"✅ Покупка добавлена! ID: {pid}\n💰 {format_price(price)} x {format_quantity(qty)} = {amt:.2f} USDT"
                if drop > 0: msg += f"\n📉 Падение: {drop:.1f}%"
                await update.message.reply_text(msg, reply_markup=self.get_main_keyboard())
            else:
                await update.message.reply_text("❌ Ошибка сохранения", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        except:
            await update.message.reply_text("❌ Некорректное количество", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_AMOUNT
    
    # ============= РУЧНАЯ ПОКУПКА (ЛИМИТ) =============
    async def manual_buy_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return ConversationHandler.END
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ API не инициализирован"); return ConversationHandler.END
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        price = await self.bybit.get_symbol_price(symbol)
        rec = await self.strategy.get_recommended_purchase(symbol) if self.strategy else {'amount_usdt': 1.1}
        context.user_data['manual_buy_symbol'] = symbol
        context.user_data['manual_buy_rec'] = rec
        msg = f"💰 Цена {symbol}: {format_price(price)}\n💡 Рекомендуемая сумма: {rec.get('amount_usdt', 1.1):.2f} USDT\n\nВведите цену лимитного ордера:"
        await update.message.reply_text(msg, reply_markup=self.get_manual_buy_keyboard())
        return MANUAL_BUY_PRICE
    
    async def manual_buy_price_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            price = float(text.replace(',', '.'))
            if price <= 0: raise ValueError
            context.user_data['manual_buy_price'] = price
            rec = context.user_data.get('manual_buy_rec', {})
            sug = rec.get('amount_usdt', 1.1)
            await update.message.reply_text(f"💰 Введите сумму USDT (рекомендуется {sug:.2f}):", reply_markup=self.get_manual_buy_keyboard())
            return MANUAL_BUY_AMOUNT
        except:
            await update.message.reply_text("❌ Некорректная цена", reply_markup=self.get_manual_buy_keyboard())
            return MANUAL_BUY_PRICE
    
    async def manual_buy_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена": return await self.cancel_action(update, context)
        try:
            amt = float(text.replace(',', '.'))
            if amt < 1.1: raise ValueError
            price = context.user_data.get('manual_buy_price')
            symbol = context.user_data.get('manual_buy_symbol', 'TONUSDT')
            if not price:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            await update.message.reply_text("⏳ Создаю ордер...")
            res = await self.bybit.place_limit_buy(symbol, price, amt)
            if res['success']:
                profit = float(self.db.get_setting('profit_percent', '5'))
                target = price * (1 + profit / 100)
                drop = context.user_data.get('manual_buy_rec', {}).get('drop_percent', 0)
                self.db.add_purchase(symbol, amt, price, res['quantity'], drop_percent=drop, step_level=int(drop))
                sell_res = await self.bybit.place_limit_sell(symbol, res['quantity'], target)
                if sell_res['success']: self.db.add_sell_order(symbol, sell_res['order_id'], res['quantity'], target, profit)
                msg = f"✅ Ордер создан!\n💰 {format_price(price)} x {format_quantity(res['quantity'])} = {amt:.2f} USDT\n🎯 Цель: {format_price(target)} (+{profit}%)"
                await update.message.reply_text(msg, reply_markup=self.get_main_keyboard())
            else:
                await update.message.reply_text(f"❌ {res.get('error')}", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        except:
            await update.message.reply_text("❌ Некорректная сумма", reply_markup=self.get_manual_buy_keyboard())
            return MANUAL_BUY_AMOUNT
    
    # ============= УПРАВЛЕНИЕ ОРДЕРАМИ =============
    async def orders_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return ConversationHandler.END
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ API не инициализирован"); return ConversationHandler.END
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        orders = await self.bybit.get_open_orders_by_side(symbol)
        msg = f"📝 *Ордера {symbol}*\n🔴 Продажа: {len(orders.get('sell', []))}\n🟢 Покупка: {len(orders.get('buy', []))}"
        await update.message.reply_text(msg, reply_markup=self.get_order_management_keyboard(), parse_mode='Markdown')
        return MANAGE_ORDERS
    
    async def show_open_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return
        self._init_bybit()
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        orders = await self.bybit.get_open_orders_by_side(symbol)
        msg = f"📋 *Открытые ордера {symbol}*\n"
        for o in orders.get('sell', [])[:5]:
            msg += f"🔴 {o.get('orderId')} - {o.get('qty')} @ {o.get('price')}\n"
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=self.get_order_management_keyboard())
    
    async def cancel_order_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return ConversationHandler.END
        self._init_bybit()
        symbol = self.db.get_setting('symbol', 'TONUSDT')
        orders = await self.bybit.get_open_orders(symbol)
        if not orders:
            await update.message.reply_text("Нет открытых ордеров", reply_markup=self.get_order_management_keyboard())
            return ConversationHandler.END
        context.user_data['cancel_orders'] = orders
        kb = [[KeyboardButton(f"{i+1}. {o.get('side')} {o.get('qty')} @ {o.get('price')}")] for i, o in enumerate(orders[:10])]
        kb.append([KeyboardButton("❌ Отмена")])
        await update.message.reply_text("Выберите ордер для удаления:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return WAITING_ORDER_ID_TO_CANCEL
    
    async def cancel_order_execute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("Отменено", reply_markup=self.get_order_management_keyboard())
            return ConversationHandler.END
        try:
            idx = int(text.split('.')[0]) - 1
            orders = context.user_data.get('cancel_orders', [])
            if 0 <= idx < len(orders):
                order = orders[idx]
                symbol = self.db.get_setting('symbol', 'TONUSDT')
                res = await self.bybit.cancel_order(symbol, order.get('orderId'))
                if res['success']:
                    self.db.delete_sell_order(order.get('orderId'))
                    await update.message.reply_text("✅ Ордер удалён", reply_markup=self.get_order_management_keyboard())
                else:
                    await update.message.reply_text(f"❌ {res.get('error')}", reply_markup=self.get_order_management_keyboard())
                return ConversationHandler.END
        except: pass
        await update.message.reply_text("❌ Ошибка", reply_markup=self.get_order_management_keyboard())
        return ConversationHandler.END
    
    # ============= CALLBACK И ПРОДАЖА =============
    async def handle_order_execution_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        if data.startswith("add_order_"):
            oid = data.replace("add_order_", "")
            await self.add_executed_order_to_stats(update, context, oid)
        elif data.startswith("skip_order_"):
            oid = data.replace("skip_order_", "")
            self.db.mark_order_as_skipped(oid)
            await query.edit_message_text("⏭ Пропущено")
        elif data.startswith("confirm_clear_stats_"):
            parts = data.replace("confirm_clear_stats_", "").split("_")
            if len(parts) >= 2:
                symbol, sid = "_".join(parts[:-1]), int(parts[-1])
                self.db.clear_all_purchases(symbol)
                self.db.mark_completed_sell_stats_cleared(sid)
                await query.edit_message_text(f"✅ Статистика {symbol} очищена")
        elif data.startswith("skip_clear_stats_"):
            parts = data.replace("skip_clear_stats_", "").split("_")
            if len(parts) >= 2:
                self.db.mark_completed_sell_notified(int(parts[-1]))
                await query.edit_message_text("⏭ Очистка отложена")
    
    async def add_executed_order_to_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
        await update.callback_query.edit_message_text("✅ Добавлено")
        self.db.mark_order_as_added(order_id)
    
    async def handle_sell_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return
        text = update.message.text.strip()
        if text == "❌ Нет, отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return
        if text == "✅ Да, выставить ордер на продажу":
            data = context.user_data.get('pending_sell_data')
            if not data:
                await update.message.reply_text("❌ Данные не найдены", reply_markup=self.get_main_keyboard())
                return
            self._init_bybit()
            res = await self.strategy.place_full_sell_order(update, data['symbol'], data['profit_percent'], True)
            if res['success']:
                await update.message.reply_text(f"✅ Ордер создан: {format_price(res['price'])} x {format_quantity(res['quantity'])}", reply_markup=self.get_main_keyboard())
            else:
                await update.message.reply_text(f"❌ {res.get('error')}", reply_markup=self.get_main_keyboard())
            context.user_data.pop('pending_sell_data', None)
    
    async def handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update): return
        await self._reset_bot_state(context)
        await update.message.reply_text("Используйте кнопки меню", reply_markup=self.get_main_keyboard())
    
    # ============= SETUP HANDLERS =============
    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.cmd_start_fast))
        self.application.add_handler(CallbackQueryHandler(self.handle_order_execution_callback, pattern='^(add_order_|skip_order_|confirm_clear_stats_|skip_clear_stats_)'))
        self.application.add_handler(MessageHandler(filters.Regex('^(📊 Мой Портфель)$'), self.show_portfolio))
        self.application.add_handler(MessageHandler(filters.Regex('^(🚀 Запустить Авто DCA|⏹ Остановить Авто DCA)$'), self.toggle_dca))
        self.application.add_handler(MessageHandler(filters.Regex('^(📈 Статистика DCA)$'), self.show_dca_stats_detailed))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 Статус бота)$'), self.show_status))
        self.application.add_handler(MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.return_to_main_menu))
        self.application.add_handler(MessageHandler(filters.Regex('^(✅ Да, выставить ордер на продажу|❌ Нет, отмена)$'), self.handle_sell_confirmation))
        
        # Main settings conversation
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
            fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), self.cancel_action), CommandHandler("cancel", self.cancel_action)],
            conversation_timeout=CONVERSATION_TIMEOUT, name="main_conv", persistent=False
        )
        self.application.add_handler(main_conv)
        
        # Ladder conversation
        ladder_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🪜 Настройка лестницы)$'), self.ladder_settings_menu)],
            states={
                LADDER_MENU: [
                    MessageHandler(filters.Regex('^(📉 Глубина просадки \\(%\\))$'), self.set_ladder_max_depth_start),
                    MessageHandler(filters.Regex('^(💵 Базовая сумма)$'), self.set_ladder_base_amount_start),
                    MessageHandler(filters.Regex('^(📋 Текущие настройки)$'), self.show_ladder_settings),
                    MessageHandler(filters.Regex('^(🔄 Сбросить лестницу)$'), self.reset_ladder),
                    MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.return_to_main_menu),
                ],
                SET_LADDER_DEPTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_max_depth_save)],
                SET_LADDER_BASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_base_amount_save)],
            },
            fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), self.cancel_action)],
            conversation_timeout=CONVERSATION_TIMEOUT, name="ladder_conv", persistent=False
        )
        self.application.add_handler(ladder_conv)
        
        # Tracking conversation
        track_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(⚙️ Настройки отслеживания)$'), self.tracking_settings)],
            states={
                NOTIFICATION_SETTINGS_MENU: [
                    MessageHandler(filters.Regex('^(✅ Отслеживание ордеров Вкл|❌ Отслеживание ордеров Выкл)$'), self.toggle_tracking),
                    MessageHandler(filters.Regex('^(💰 Отслеживание продаж Вкл|⏳ Отслеживание продаж Выкл)$'), self.toggle_sell_tracking_in_settings),
                    MessageHandler(filters.Regex('^(⏱ Интервал проверки Ордеров \\d+ мин)$'), self.set_tracking_interval_start),
                    MessageHandler(filters.Regex('^(🔍 Тест отслеживания)$'), self.test_tracking),
                    MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings),
                ],
                WAITING_ORDER_CHECK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_tracking_interval_done)],
            },
            fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), self.cancel_action)],
            conversation_timeout=CONVERSATION_TIMEOUT, name="track_conv", persistent=False
        )
        self.application.add_handler(track_conv)
        
        # Purchase notify conversation
        notify_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🔔 Уведомления о покупке)$'), self.purchase_notify_settings)],
            states={
                WAITING_PURCHASE_NOTIFY_TIME: [
                    MessageHandler(filters.Regex('^(🔔 Уведомления Вкл|🔕 Уведомления Выкл)$'), self.toggle_purchase_notify),
                    MessageHandler(filters.Regex('^(⏰ Время уведомления \\(\\d{2}:\\d{2}\\)|⏰ Время уведомления)$'), self.set_purchase_notify_time_start),
                    MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings_from_purchase),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_purchase_notify_time_done),
                ],
            },
            fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), self.cancel_action)],
            conversation_timeout=CONVERSATION_TIMEOUT, name="notify_conv", persistent=False
        )
        self.application.add_handler(notify_conv)
        
        # Manual add conversation
        manual_add_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(➕ Добавить покупку вручную)$'), self.manual_add_start)],
            states={
                MANUAL_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_price)],
                MANUAL_ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_amount)],
            },
            fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), self.cancel_action)],
            conversation_timeout=CONVERSATION_TIMEOUT, name="manual_add_conv", persistent=False
        )
        self.application.add_handler(manual_add_conv)
        
        # Manual buy conversation
        manual_buy_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(💰 Ручная покупка \\(лимит\\))$'), self.manual_buy_start)],
            states={
                MANUAL_BUY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_price_done)],
                MANUAL_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_amount_done)],
            },
            fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), self.cancel_action)],
            conversation_timeout=CONVERSATION_TIMEOUT, name="manual_buy_conv", persistent=False
        )
        self.application.add_handler(manual_buy_conv)
        
        # Orders conversation
        orders_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(📝 Управление ордерами)$'), self.orders_menu)],
            states={
                MANAGE_ORDERS: [
                    MessageHandler(filters.Regex('^(📋 Список открытых ордеров)$'), self.show_open_orders),
                    MessageHandler(filters.Regex('^(❌ Удалить ордер)$'), self.cancel_order_start),
                    MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.return_to_main_menu),
                ],
                WAITING_ORDER_ID_TO_CANCEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.cancel_order_execute)],
            },
            fallbacks=[MessageHandler(filters.Regex('^❌ Отмена$'), self.cancel_action)],
            conversation_timeout=CONVERSATION_TIMEOUT, name="orders_conv", persistent=False
        )
        self.application.add_handler(orders_conv)
        
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_unknown))
    
    async def post_init(self, application):
        self.scheduler_running = True
        logger.info("Bot started")
    
    def run(self):
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"{Fore.CYAN}🚀 DCA BYBIT BOT v{BOT_VERSION}")
        print(f"{Fore.CYAN}{'='*60}\n")
        if not TELEGRAM_TOKEN:
            print(f"{Fore.RED}❌ TELEGRAM_BOT_TOKEN не найден!")
            return
        self.application.post_init = self.post_init
        try:
            self.application.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            logger.error(f"Failed to start: {e}")

if __name__ == "__main__":
    try:
        import colorama
    except ImportError:
        os.system(f"{sys.executable} -m pip install colorama")
        import colorama
    bot = FastDCABot()
    bot.run()