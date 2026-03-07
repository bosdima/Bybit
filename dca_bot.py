import os
import json
import time
import threading
from datetime import datetime
from dotenv import load_dotenv
import telebot
from telebot import types
from pybit.unified_trading import HTTP

# Загружаем переменные окружения
load_dotenv()

# Конфигурация
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
ALLOWED_USER = '@bosdima'  # Только этот пользователь может использовать бота
DCA_FILE = 'dca_data.json'  # Файл для хранения данных DCA
SYMBOL = 'TONUSDT'  # Торговая пара

# Инициализация бота
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Инициализация клиента Bybit
session = HTTP(
    testnet=False,  # Используем реальную торговлю
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# Глобальные переменные для хранения состояния DCA
dca_data = {
    'start_time': None,
    'total_quantity': 0.0,
    'total_cost': 0.0,
    'avg_price': 0.0,
    'trades': []
}

# Функция для проверки API ключей
def check_api_keys():
    """Проверка работоспособности API ключей Bybit"""
    try:
        # Пробуем получить информацию о кошельке
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response['retCode'] == 0:
            return True, "✅ API ключи Bybit работают корректно"
        else:
            return False, f"❌ Ошибка API Bybit: {response['retMsg']}"
    except Exception as e:
        return False, f"❌ Ошибка подключения к Bybit: {str(e)}"

# Функция для проверки Telegram бота
def check_telegram_bot():
    """Проверка работоспособности Telegram бота"""
    try:
        bot_info = bot.get_me()
        return True, f"✅ Telegram бот @{bot_info.username} работает корректно"
    except Exception as e:
        return False, f"❌ Ошибка Telegram бота: {str(e)}"

# Функция для загрузки данных DCA из файла
def load_dca_data():
    global dca_data
    try:
        if os.path.exists(DCA_FILE):
            with open(DCA_FILE, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
                # Обновляем данные, сохраняя структуру
                dca_data.update(loaded_data)
    except Exception as e:
        print(f"Ошибка загрузки данных DCA: {e}")

# Функция для сохранения данных DCA в файл
def save_dca_data():
    try:
        with open(DCA_FILE, 'w', encoding='utf-8') as f:
            json.dump(dca_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка сохранения данных DCA: {e}")

# Функция для получения текущей цены
def get_current_price(symbol=SYMBOL):
    try:
        response = session.get_tickers(category="spot", symbol=symbol)
        if response['retCode'] == 0 and response['result']['list']:
            price_str = response['result']['list'][0].get('lastPrice', '0')
            return float(price_str) if price_str else None
        return None
    except Exception as e:
        print(f"Ошибка получения цены: {e}")
        return None

# Функция для получения средней цены входа для конкретной монеты
def get_coin_avg_price(coin):
    """Получаем среднюю цену входа для монеты из истории сделок"""
    try:
        symbol = f"{coin}USDT"
        # Получаем историю сделок
        response = session.get_executions(
            category="spot",
            symbol=symbol,
            limit=100
        )
        
        if response['retCode'] == 0 and response['result']['list']:
            buys = []
            for trade in response['result']['list']:
                # Только покупки
                if trade.get('side') == 'Buy':
                    try:
                        price = float(trade.get('execPrice', '0'))
                        qty = float(trade.get('execQty', '0'))
                        if price > 0 and qty > 0:
                            buys.append({'price': price, 'qty': qty})
                    except ValueError:
                        continue
            
            if buys:
                total_cost = sum(b['price'] * b['qty'] for b in buys)
                total_qty = sum(b['qty'] for b in buys)
                if total_qty > 0:
                    return total_cost / total_qty, total_qty, total_cost
        return None, 0, 0
    except Exception as e:
        print(f"Ошибка получения средней цены для {coin}: {e}")
        return None, 0, 0

# Функция для получения баланса спотового счета с детальной информацией
def get_detailed_portfolio():
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        
        if response['retCode'] == 0:
            portfolio = []
            total_usdt_value = 0.0
            total_pnl = 0.0
            total_pnl_percentage = 0.0
            total_invested = 0.0
            
            # Проверяем наличие данных
            if not response['result']['list']:
                return [], 0.0, 0.0, 0.0, 0.0
            
            # Получаем курсы всех монет
            all_prices = {}
            
            # Обрабатываем баланс
            for coin_data in response['result']['list'][0].get('coin', []):
                coin = coin_data.get('coin', '')
                wallet_balance_str = coin_data.get('walletBalance', '0')
                
                try:
                    wallet_balance = float(wallet_balance_str) if wallet_balance_str else 0.0
                except ValueError:
                    wallet_balance = 0.0
                
                if wallet_balance > 0 and coin != 'USDT':
                    # Получаем текущую цену для монеты
                    symbol = f"{coin}USDT"
                    if symbol not in all_prices:
                        current_price = get_current_price(symbol)
                        all_prices[symbol] = current_price
                    else:
                        current_price = all_prices[symbol]
                    
                    if current_price and current_price > 0:
                        # Получаем среднюю цену входа
                        avg_price, total_qty, total_cost = get_coin_avg_price(coin)
                        
                        current_value = wallet_balance * current_price
                        total_usdt_value += current_value
                        
                        if avg_price and total_qty > 0:
                            invested = total_cost
                            pnl = current_value - invested
                            pnl_percentage = (pnl / invested) * 100 if invested > 0 else 0
                            
                            total_pnl += pnl
                            total_invested += invested
                        else:
                            avg_price = current_price
                            pnl = 0
                            pnl_percentage = 0
                            invested = current_value
                            total_invested += invested
                        
                        portfolio.append({
                            'coin': coin,
                            'balance': wallet_balance,
                            'current_price': current_price,
                            'avg_price': avg_price,
                            'current_value': current_value,
                            'invested': invested,
                            'pnl': pnl,
                            'pnl_percentage': pnl_percentage
                        })
                    else:
                        # Если не удалось получить цену, добавляем без PnL
                        portfolio.append({
                            'coin': coin,
                            'balance': wallet_balance,
                            'current_price': 0,
                            'avg_price': None,
                            'current_value': 0,
                            'invested': 0,
                            'pnl': 0,
                            'pnl_percentage': 0,
                            'note': 'Цена не доступна'
                        })
                elif coin == 'USDT' and wallet_balance > 0:
                    total_usdt_value += wallet_balance
                    portfolio.append({
                        'coin': 'USDT',
                        'balance': wallet_balance,
                        'current_price': 1.0,
                        'avg_price': 1.0,
                        'current_value': wallet_balance,
                        'invested': wallet_balance,
                        'pnl': 0,
                        'pnl_percentage': 0
                    })
            
            # Сортируем портфель по стоимости (самые дорогие первые)
            portfolio.sort(key=lambda x: x['current_value'], reverse=True)
            
            # Рассчитываем общий PnL процент
            if total_invested > 0:
                total_pnl_percentage = (total_pnl / total_invested) * 100
            
            return portfolio, total_usdt_value, total_pnl, total_pnl_percentage, total_invested
        return [], 0.0, 0.0, 0.0, 0.0
    except Exception as e:
        print(f"Ошибка получения детального портфеля: {e}")
        return [], 0.0, 0.0, 0.0, 0.0

# Функция для покупки монет (РЕАЛЬНЫЙ ОРДЕР)
def buy_ton(usdt_amount):
    try:
        # Получаем текущую цену
        current_price = get_current_price()
        if not current_price or current_price <= 0:
            return None, "Не удалось получить текущую цену"
        
        # Рассчитываем количество TON для покупки
        quantity = usdt_amount / current_price
        
        # Получаем информацию о точности для символа
        try:
            symbol_info = session.get_instruments_info(category="spot", symbol=SYMBOL)
            if symbol_info['retCode'] == 0 and symbol_info['result']['list']:
                lot_size_filter = symbol_info['result']['list'][0].get('lotSizeFilter', {})
                qty_step = float(lot_size_filter.get('qtyStep', '0.001'))
                # Округляем до шага количества
                quantity = round(quantity / qty_step) * qty_step
        except:
            # Если не удалось получить информацию, округляем до 3 знаков
            quantity = round(quantity, 3)
        
        # Проверяем минимальное количество
        min_qty = 0.1  # Минимальное количество для TON
        if quantity < min_qty:
            return None, f"Минимальное количество для покупки: {min_qty} TON. Вы пытаетесь купить {quantity:.4f} TON"
        
        # Проверяем баланс USDT
        portfolio, total_usdt, _, _, _ = get_detailed_portfolio()
        usdt_balance = 0
        for item in portfolio:
            if item['coin'] == 'USDT':
                usdt_balance = item['balance']
                break
        
        if usdt_balance < usdt_amount:
            return None, f"Недостаточно USDT. Баланс: {usdt_balance:.2f} USDT, необходимо: {usdt_amount:.2f} USDT"
        
        # РЕАЛЬНЫЙ ОРДЕР НА ПОКУПКУ
        print(f"Размещаем ордер на покупку {quantity} TON по рыночной цене")
        
        order = session.place_order(
            category="spot",
            symbol=SYMBOL,
            side="Buy",
            orderType="Market",
            qty=str(quantity),
            timeInForce="GTC"
        )
        
        if order['retCode'] != 0:
            return None, f"Ошибка биржи: {order['retMsg']}"
        
        # Получаем информацию о выполненном ордере
        order_id = order['result']['orderId']
        time.sleep(2)  # Небольшая задержка для обновления данных
        
        # Ищем исполненную сделку
        executions = session.get_executions(
            category="spot",
            symbol=SYMBOL,
            limit=10
        )
        
        executed_qty = 0
        executed_price = 0
        executed_cost = 0
        
        if executions['retCode'] == 0:
            for exec in executions['result']['list']:
                if exec.get('orderId') == order_id:
                    try:
                        exec_price = float(exec.get('execPrice', '0'))
                        exec_qty = float(exec.get('execQty', '0'))
                        if exec_price > 0 and exec_qty > 0:
                            executed_qty += exec_qty
                            executed_cost += exec_price * exec_qty
                    except ValueError:
                        continue
        
        if executed_qty > 0:
            executed_price = executed_cost / executed_qty
            return {
                'quantity': executed_qty,
                'price': executed_price,
                'cost': executed_cost,
                'order_id': order_id
            }, None
        else:
            # Если не нашли исполнение, используем расчетные данные
            return {
                'quantity': quantity,
                'price': current_price,
                'cost': usdt_amount,
                'order_id': order_id
            }, None
        
    except Exception as e:
        return None, f"Ошибка при покупке: {str(e)}"

# Функция для проверки цены и отправки уведомлений
def check_price_and_notify():
    while True:
        try:
            if dca_data['avg_price'] > 0:
                current_price = get_current_price()
                if current_price and current_price > 0:
                    price_change = ((current_price - dca_data['avg_price']) / dca_data['avg_price']) * 100
                    
                    if price_change >= 10:
                        # Отправляем уведомление @bosdima
                        message = (
                            f"🚨 ВНИМАНИЕ! Цена выросла на {price_change:.2f}%\n\n"
                            f"Средняя цена покупки DCA: {dca_data['avg_price']:.4f} USDT\n"
                            f"Текущая цена: {current_price:.4f} USDT\n"
                            f"Количество монет: {dca_data['total_quantity']:.4f} TON\n\n"
                            f"💡 Рекомендация: Продать все монеты по текущей цене!"
                        )
                        bot.send_message(ALLOWED_USER, message)
        except Exception as e:
            print(f"Ошибка в проверке цены: {e}")
        
        # Проверяем каждые 30 минут
        time.sleep(1800)

# Запуск потока для проверки цены
def start_price_checker():
    thread = threading.Thread(target=check_price_and_notify, daemon=True)
    thread.start()

# Обработчик команды /start
@bot.message_handler(commands=['start'])
def send_welcome(message):
    if message.from_user.username != ALLOWED_USER.replace('@', ''):
        bot.reply_to(message, "❌ У вас нет доступа к этому боту.")
        return
    
    # Проверяем API ключи при запуске
    bybit_status, bybit_message = check_api_keys()
    telegram_status, telegram_message = check_telegram_bot()
    
    # Создаем клавиатуру
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn1 = types.KeyboardButton("💰 Мой Портфель")
    btn2 = types.KeyboardButton("📊 Куплено по DCA")
    btn3 = types.KeyboardButton("🛒 Купить по DCA")
    markup.add(btn1, btn2, btn3)
    
    welcome_text = (
        "👋 Добро пожаловать в DCA Trading Bot!\n\n"
        f"{bybit_message}\n"
        f"{telegram_message}\n\n"
        "Выберите действие:"
    )
    
    bot.send_message(message.chat.id, welcome_text, reply_markup=markup)

# Обработчик кнопки "Мой Портфель" (УЛУЧШЕННАЯ ВЕРСИЯ)
@bot.message_handler(func=lambda message: message.text == "💰 Мой Портфель")
def show_detailed_portfolio(message):
    if message.from_user.username != ALLOWED_USER.replace('@', ''):
        return
    
    # Отправляем сообщение о загрузке
    status_msg = bot.reply_to(message, "🔄 Загружаю данные портфеля...")
    
    portfolio, total_value, total_pnl, total_pnl_percentage, total_invested = get_detailed_portfolio()
    
    if not portfolio:
        bot.edit_message_text(
            "📭 Портфель пуст или не удалось загрузить данные",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )
        return
    
    # Формируем заголовок
    response = "💰 **ДЕТАЛЬНЫЙ ПОРТФЕЛЬ**\n\n"
    response += f"📊 **Общая стоимость:** {total_value:.2f} USDT\n"
    response += f"💵 **Всего инвестировано:** {total_invested:.2f} USDT\n"
    
    # Добавляем общий PnL
    if total_pnl != 0:
        pnl_emoji = "📈" if total_pnl > 0 else "📉"
        response += f"{pnl_emoji} **Общий PnL:** {total_pnl:+.2f} USDT ({total_pnl_percentage:+.2f}%)\n"
    
    response += "\n" + "═" * 30 + "\n\n"
    
    # Добавляем информацию по каждой монете
    for item in portfolio:
        coin = item['coin']
        balance = item['balance']
        current_price = item['current_price']
        avg_price = item['avg_price']
        current_value = item['current_value']
        invested = item['invested']
        pnl = item['pnl']
        pnl_percentage = item['pnl_percentage']
        
        if coin == 'USDT':
            response += f"💵 **USDT**\n"
            response += f"   Баланс: {balance:.2f}\n"
            response += f"   Стоимость: {current_value:.2f} USDT\n\n"
        else:
            # Выбираем эмодзи для монеты
            coin_emoji = "💎"  # По умолчанию
            
            response += f"{coin_emoji} **{coin}**\n"
            response += f"   Количество: {balance:.4f}\n"
            
            if avg_price and avg_price > 0:
                response += f"   Средняя цена входа: {avg_price:.4f} USDT\n"
            else:
                response += f"   Средняя цена входа: N/A\n"
            
            if current_price > 0:
                response += f"   Текущая цена: {current_price:.4f} USDT\n"
                response += f"   Стоимость: {current_value:.2f} USDT\n"
                
                # Добавляем PnL
                if pnl != 0:
                    pnl_emoji = "✅" if pnl > 0 else "❌"
                    response += f"   {pnl_emoji} PnL: {pnl:+.2f} USDT ({pnl_percentage:+.2f}%)\n"
            else:
                response += f"   Цена не доступна\n"
            
            # Добавляем процент от портфеля
            if total_value > 0:
                percentage = (current_value / total_value) * 100
                response += f"   Доля в портфеле: {percentage:.1f}%\n"
            
            response += "\n"
    
    # Добавляем распределение портфеля
    response += "═" * 30 + "\n"
    response += "📊 **Распределение портфеля:**\n"
    
    for item in portfolio:
        if item['coin'] != 'USDT' and total_value > 0 and item['current_value'] > 0:
            percentage = (item['current_value'] / total_value) * 100
            bar_length = int(percentage / 2)  # Максимум 50 символов
            bar = "█" * bar_length + "░" * (25 - bar_length)
            response += f"{item['coin']}: {bar} {percentage:.1f}%\n"
    
    # Добавляем информацию о DCA стратегии
    if dca_data['total_quantity'] > 0:
        response += "\n" + "═" * 30 + "\n"
        response += "📊 **DCA Статистика:**\n"
        response += f"   TON в DCA: {dca_data['total_quantity']:.4f}\n"
        response += f"   Средняя цена DCA: {dca_data['avg_price']:.4f} USDT\n"
        
        # Находим текущую цену TON
        ton_item = next((item for item in portfolio if item['coin'] == 'TON'), None)
        if ton_item and ton_item['current_price'] > 0:
            dca_value = dca_data['total_quantity'] * ton_item['current_price']
            dca_pnl = dca_value - dca_data['total_cost']
            dca_pnl_percentage = (dca_pnl / dca_data['total_cost']) * 100 if dca_data['total_cost'] > 0 else 0
            dca_emoji = "✅" if dca_pnl > 0 else "⏳"
            response += f"   {dca_emoji} DCA PnL: {dca_pnl:+.2f} USDT ({dca_pnl_percentage:+.2f}%)\n"
    
    bot.edit_message_text(
        response,
        chat_id=message.chat.id,
        message_id=status_msg.message_id,
        parse_mode='Markdown'
    )

# Обработчик кнопки "Куплено по DCA"
@bot.message_handler(func=lambda message: message.text == "📊 Куплено по DCA")
def show_dca_stats(message):
    if message.from_user.username != ALLOWED_USER.replace('@', ''):
        return
    
    if not dca_data['start_time'] or dca_data['total_quantity'] == 0:
        bot.reply_to(message, "📊 Стратегия DCA еще не начата. Нажмите 'Купить по DCA' для первой покупки.")
        return
    
    current_price = get_current_price()
    
    if current_price is None:
        bot.reply_to(message, "❌ Не удалось получить текущую цену")
        return
    
    # Рассчитываем PnL
    current_value = dca_data['total_quantity'] * current_price
    pnl = current_value - dca_data['total_cost']
    pnl_percentage = (pnl / dca_data['total_cost']) * 100 if dca_data['total_cost'] > 0 else 0
    
    start_date = datetime.fromtimestamp(dca_data['start_time']).strftime('%Y-%m-%d %H:%M:%S')
    
    # Выбираем эмодзи для PnL
    pnl_emoji = "📈" if pnl > 0 else "📉" if pnl < 0 else "📊"
    
    response = (
        f"📊 **СТАТИСТИКА DCA**\n\n"
        f"📅 Начало стратегии: {start_date}\n"
        f"💰 Всего куплено: {dca_data['total_quantity']:.4f} TON\n"
        f"💵 Средняя цена: {dca_data['avg_price']:.4f} USDT\n"
        f"📈 Текущая цена: {current_price:.4f} USDT\n"
        f"💵 Всего инвестировано: {dca_data['total_cost']:.2f} USDT\n"
        f"💰 Текущая стоимость: {current_value:.2f} USDT\n"
        f"{pnl_emoji} **Текущий PnL: {pnl:+.2f} USDT ({pnl_percentage:+.2f}%)**\n\n"
        f"📊 Всего сделок DCA: {len(dca_data['trades'])}"
    )
    
    # Добавляем рекомендацию если есть прибыль
    if pnl > 0:
        response += f"\n\n💡 **Рекомендация:** Текущая прибыль {pnl_percentage:+.2f}%. Рассмотрите продажу для фиксации прибыли."
    
    bot.send_message(message.chat.id, response, parse_mode='Markdown')

# Обработчик кнопки "Купить по DCA"
@bot.message_handler(func=lambda message: message.text == "🛒 Купить по DCA")
def buy_dca(message):
    if message.from_user.username != ALLOWED_USER.replace('@', ''):
        return
    
    current_price = get_current_price()
    
    if current_price is None:
        bot.reply_to(message, "❌ Не удалось получить текущую цену")
        return
    
    msg = bot.send_message(
        message.chat.id,
        f"🛒 **Покупка по DCA**\n\n"
        f"Текущая цена TON: {current_price:.4f} USDT\n\n"
        f"Введите сумму в USDT для покупки (минимум 1 USDT):",
        parse_mode='Markdown'
    )
    
    bot.register_next_step_handler(msg, process_dca_purchase)

def process_dca_purchase(message):
    try:
        amount = float(message.text.strip())
        
        if amount < 1:
            bot.reply_to(message, "❌ Минимальная сумма покупки: 1 USDT")
            return
        
        # Отправляем сообщение о начале покупки
        status_msg = bot.reply_to(message, "🔄 Размещаю ордер на покупку...")
        
        # Покупаем монеты
        result, error = buy_ton(amount)
        
        if error:
            bot.edit_message_text(
                f"❌ {error}",
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
            return
        
        # Обновляем данные DCA
        if not dca_data['start_time']:
            dca_data['start_time'] = time.time()
        
        # Добавляем сделку
        trade = {
            'timestamp': time.time(),
            'quantity': result['quantity'],
            'price': result['price'],
            'cost': result['cost'],
            'order_id': result.get('order_id', '')
        }
        
        dca_data['trades'].append(trade)
        dca_data['total_quantity'] += result['quantity']
        dca_data['total_cost'] += result['cost']
        dca_data['avg_price'] = dca_data['total_cost'] / dca_data['total_quantity']
        
        # Сохраняем данные
        save_dca_data()
        
        current_price = get_current_price()
        
        response = (
            f"✅ **Покупка выполнена успешно!**\n\n"
            f"Куплено: {result['quantity']:.4f} TON\n"
            f"Цена покупки: {result['price']:.4f} USDT\n"
            f"Потрачено: {result['cost']:.2f} USDT\n"
            f"ID ордера: {result.get('order_id', 'N/A')}\n\n"
            f"📊 **Обновленная статистика DCA:**\n"
            f"Всего монет DCA: {dca_data['total_quantity']:.4f} TON\n"
            f"Средняя цена DCA: {dca_data['avg_price']:.4f} USDT\n"
            f"Всего инвестировано DCA: {dca_data['total_cost']:.2f} USDT\n"
        )
        
        bot.edit_message_text(
            response,
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            parse_mode='Markdown'
        )
        
        # Проверяем прибыль и рекомендуем продажу
        if current_price and current_price > dca_data['avg_price']:
            profit_percentage = ((current_price - dca_data['avg_price']) / dca_data['avg_price']) * 100
            
            if profit_percentage > 0:
                # Получаем общий баланс TON из портфеля
                portfolio, _, _, _, _ = get_detailed_portfolio()
                ton_balance = 0
                for item in portfolio:
                    if item['coin'] == 'TON':
                        ton_balance = item['balance']
                        break
                
                sell_message = (
                    f"💡 **РЕКОМЕНДАЦИЯ ПО ПРОДАЖЕ**\n\n"
                    f"Текущая цена ({current_price:.4f} USDT) выше средней цены покупки DCA ({dca_data['avg_price']:.4f} USDT)\n"
                    f"Прибыль по DCA: {profit_percentage:.2f}%\n\n"
                    f"Рекомендуется продать:\n"
                    f"• Все монеты DCA: {dca_data['total_quantity']:.4f} TON\n"
                )
                
                if ton_balance > 0:
                    sell_message += f"• +1 TON от общего портфеля (всего TON: {ton_balance:.4f})\n"
                
                sell_message += f"\nДля продажи используйте интерфейс биржи или создайте отдельную функцию продажи."
                
                bot.send_message(message.chat.id, sell_message, parse_mode='Markdown')
        
    except ValueError:
        bot.reply_to(message, "❌ Пожалуйста, введите число")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {str(e)}")

# Загружаем данные DCA при запуске
load_dca_data()

# Запускаем проверку цены
start_price_checker()

# Запускаем бота
if __name__ == '__main__':
    print("=" * 50)
    print("Бот запущен...")
    print("Используется РЕАЛЬНАЯ торговля на Bybit")
    print("=" * 50)
    
    # Дополнительная проверка при запуске
    bybit_status, bybit_message = check_api_keys()
    print(bybit_message)
    
    if bybit_status:
        # Проверяем портфель для информации
        portfolio, total_value, total_pnl, total_pnl_percentage, total_invested = get_detailed_portfolio()
        print(f"Общая стоимость портфеля: {total_value:.2f} USDT")
        print(f"Общий PnL: {total_pnl:+.2f} USDT ({total_pnl_percentage:+.2f}%)")
        print(f"Количество монет в портфеле: {len([p for p in portfolio if p['coin'] != 'USDT'])}")
    
    print("=" * 50)
    print("Ожидание команд...")
    print("=" * 50)
    
    bot.infinity_polling()