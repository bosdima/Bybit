import os
import logging
import asyncio
from typing import Optional, Dict, Any
from urllib.parse import urlencode
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram import F

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Получение переменных окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')
YANDEX_FOLDER_PATH = os.getenv('YANDEX_FOLDER_PATH')

# Проверка наличия всех необходимых переменных
required_vars = [BOT_TOKEN, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, YANDEX_FOLDER_PATH]
if not all(required_vars):
    logger.error("Не все переменные окружения заданы!")
    exit(1)

# URL для Яндекс.Диск API
YANDEX_API_BASE = "https://cloud-api.yandex.net/v1/disk"
YANDEX_OAUTH_URL = "https://oauth.yandex.ru/authorize"

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Хранилище токенов пользователей
user_tokens: Dict[int, str] = {}
# Хранилище состояний авторизации
auth_states: Dict[int, bool] = {}

class YandexDiskAPI:
    """Класс для работы с Яндекс.Диск API"""
    
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"OAuth {token}",
            "Content-Type": "application/json"
        }
    
    async def get_folder_contents(self, path: str) -> Optional[Dict[str, Any]]:
        """Получение содержимого папки"""
        url = f"{YANDEX_API_BASE}/resources"
        params = {
            "path": path,
            "fields": "_embedded.items.name,_embedded.items.type,_embedded.items.path,_embedded.items.size,_embedded.items.modified"
        }
        
        logger.info(f"Запрос к API: path={path}")
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=self.headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        logger.info(f"Получено {len(data.get('_embedded', {}).get('items', []))} элементов")
                        return data
                    elif response.status == 401:
                        logger.error("Токен истек или недействителен")
                        return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Ошибка: {response.status} - {error_text}")
                        return None
            except Exception as e:
                logger.error(f"Исключение при запросе: {e}", exc_info=True)
                return None
    
    async def get_download_link(self, path: str) -> Optional[str]:
        """Получение ссылки для скачивания файла"""
        url = f"{YANDEX_API_BASE}/resources/download"
        params = {"path": path}
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=self.headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("href")
                    else:
                        logger.error(f"Ошибка получения ссылки: {response.status}")
                        return None
            except Exception as e:
                logger.error(f"Исключение при получении ссылки: {e}")
                return None
    
    async def download_file(self, download_url: str) -> Optional[bytes]:
        """Скачивание файла по ссылке"""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(download_url) as response:
                    if response.status == 200:
                        return await response.read()
                    else:
                        logger.error(f"Ошибка скачивания: {response.status}")
                        return None
            except Exception as e:
                logger.error(f"Исключение при скачивании: {e}")
                return None

def get_auth_url() -> str:
    """Получение URL для авторизации в Яндекс"""
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI
    }
    return f"{YANDEX_OAUTH_URL}?{urlencode(params)}"

async def get_access_token(auth_code: str) -> Optional[str]:
    """Получение access token по коду авторизации"""
    url = "https://oauth.yandex.ru/token"
    data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, data=data) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("access_token")
                else:
                    error_text = await response.text()
                    logger.error(f"Ошибка получения токена: {response.status} - {error_text}")
                    return None
        except Exception as e:
            logger.error(f"Исключение при получении токена: {e}")
            return None

def format_size(size: int) -> str:
    """Форматирование размера файла"""
    if not size:
        return "0 Б"
    for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} ТБ"

async def send_folder_list(message: Message, folder_path: str, is_root: bool = False):
    """Отправка списка содержимого папки"""
    user_id = message.from_user.id
    token = user_tokens.get(user_id)
    
    if not token:
        await message.answer("⚠️ Вы не авторизованы! Используйте /start")
        return
    
    # Отправляем сообщение о загрузке
    loading_msg = await message.answer("📂 Загружаю содержимое папки...")
    
    try:
        disk_api = YandexDiskAPI(token)
        contents = await disk_api.get_folder_contents(folder_path)
        
        if not contents:
            await loading_msg.edit_text(
                f"❌ Не удалось получить содержимое папки.\n\n"
                f"Путь: `{folder_path}`",
                parse_mode="Markdown"
            )
            return
        
        items = contents.get("_embedded", {}).get("items", [])
        
        if not items:
            await loading_msg.edit_text("📂 Папка пуста")
            return
        
        # Удаляем сообщение о загрузке
        await loading_msg.delete()
        
        # Создаем клавиатуру
        keyboard = []
        
        # Добавляем папки
        for item in items:
            if item.get('type') == 'dir':
                folder_name = item.get('name', 'Без названия')
                folder_path_item = item.get('path', '')
                keyboard.append([InlineKeyboardButton(
                    text=f"📁 {folder_name}",
                    callback_data=f"folder:{folder_path_item}"
                )])
        
        # Добавляем файлы
        for item in items:
            if item.get('type') == 'file':
                file_name = item.get('name', 'Без названия')
                file_size = format_size(item.get('size', 0))
                file_path = item.get('path', '')
                keyboard.append([InlineKeyboardButton(
                    text=f"📄 {file_name} ({file_size})",
                    callback_data=f"file:{file_path}"
                )])
        
        # Добавляем кнопку обновления
        keyboard.append([InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data=f"refresh:{folder_path}"
        )])
        
        inline_kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
        
        folders_count = len([i for i in items if i.get('type') == 'dir'])
        files_count = len([i for i in items if i.get('type') == 'file'])
        folder_display = folder_path if folder_path != "/" else "Корень"
        
        await message.answer(
            f"📁 *Папка:* `{folder_display}`\n\n"
            f"📊 *Элементов:* {len(items)}\n"
            f"📁 *Папок:* {folders_count}\n"
            f"📄 *Файлов:* {files_count}",
            reply_markup=inline_kb,
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Ошибка в send_folder_list: {e}", exc_info=True)
        await loading_msg.edit_text(f"❌ Ошибка: {str(e)}")

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    
    if user_id in user_tokens:
        await message.answer(
            f"🤖 Привет! Вы уже авторизованы.\n\n"
            f"📁 Текущая папка: `{YANDEX_FOLDER_PATH}`\n\n"
            f"Используйте /list для просмотра содержимого.",
            parse_mode="Markdown"
        )
    else:
        auth_states[user_id] = True
        
        welcome_text = (
            "🤖 Привет! Я бот для работы с Яндекс.Диском\n\n"
            "📁 Целевая папка:\n"
            f"`{YANDEX_FOLDER_PATH}`\n\n"
            "🔐 Для работы необходимо авторизоваться:\n\n"
            "1️⃣ Нажмите кнопку ниже\n"
            "2️⃣ Войдите в аккаунт Яндекс\n"
            "3️⃣ Скопируйте код из адресной строки\n"
            "4️⃣ Отправьте код мне"
        )
        
        auth_button = InlineKeyboardButton(
            text="🔑 Авторизоваться",
            url=get_auth_url()
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[auth_button]])
        
        await message.answer(welcome_text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(Command("list"))
async def cmd_list(message: Message):
    """Обработчик команды /list"""
    user_id = message.from_user.id
    
    if user_id not in user_tokens:
        await message.answer("⚠️ Сначала авторизуйтесь: /start")
        return
    
    await send_folder_list(message, YANDEX_FOLDER_PATH, True)

@dp.message(Command("checkpath"))
async def cmd_check_path(message: Message):
    """Проверка существования пути"""
    user_id = message.from_user.id
    
    if user_id not in user_tokens:
        await message.answer("⚠️ Сначала авторизуйтесь: /start")
        return
    
    status_msg = await message.answer(f"🔍 Проверяю: `{YANDEX_FOLDER_PATH}`", parse_mode="Markdown")
    
    disk_api = YandexDiskAPI(user_tokens[user_id])
    contents = await disk_api.get_folder_contents(YANDEX_FOLDER_PATH)
    
    if contents:
        items = contents.get("_embedded", {}).get("items", [])
        folders = len([i for i in items if i.get('type') == 'dir'])
        files = len([i for i in items if i.get('type') == 'file'])
        
        await status_msg.edit_text(
            f"✅ Папка найдена!\n\n"
            f"📁 `{YANDEX_FOLDER_PATH}`\n"
            f"📊 Элементов: {len(items)}\n"
            f"📁 Папок: {folders}\n"
            f"📄 Файлов: {files}",
            parse_mode="Markdown"
        )
    else:
        await status_msg.edit_text(
            f"❌ Папка не найдена!\n\n"
            f"Путь: `{YANDEX_FOLDER_PATH}`\n\n"
            f"Используйте /listroot для просмотра корня",
            parse_mode="Markdown"
        )

@dp.message(Command("listroot"))
async def cmd_list_root(message: Message):
    """Показать корневую папку"""
    user_id = message.from_user.id
    
    if user_id not in user_tokens:
        await message.answer("⚠️ Сначала авторизуйтесь: /start")
        return
    
    await send_folder_list(message, "/", True)

@dp.message(Command("logout"))
async def cmd_logout(message: Message):
    """Выход из аккаунта"""
    user_id = message.from_user.id
    
    if user_id in user_tokens:
        del user_tokens[user_id]
        if user_id in auth_states:
            del auth_states[user_id]
        await message.answer("✅ Вы вышли из аккаунта. Используйте /start для повторной авторизации.")
    else:
        await message.answer("Вы не были авторизованы.")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Справка"""
    help_text = (
        "📚 *Команды:*\n\n"
        "/start - Авторизация\n"
        "/list - Показать папку\n"
        "/checkpath - Проверить путь\n"
        "/listroot - Корневая папка\n"
        "/logout - Выйти\n"
        "/help - Справка\n\n"
        f"📁 *Папка:* `{YANDEX_FOLDER_PATH}`"
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("folder:"))
async def handle_folder_callback(callback: CallbackQuery):
    """Обработка нажатия на папку"""
    folder_path = callback.data[7:]
    user_id = callback.from_user.id
    
    if user_id not in user_tokens:
        await callback.answer("⚠️ Сессия истекла", show_alert=True)
        return
    
    await callback.answer()
    
    # Создаем новое сообщение вместо редактирования
    await send_folder_list(callback.message, folder_path)
    
    # Удаляем старое сообщение
    try:
        await callback.message.delete()
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")

@dp.callback_query(F.data.startswith("file:"))
async def handle_file_callback(callback: CallbackQuery):
    """Обработка нажатия на файл"""
    file_path = callback.data[5:]
    user_id = callback.from_user.id
    
    if user_id not in user_tokens:
        await callback.answer("⚠️ Сессия истекла", show_alert=True)
        return
    
    await callback.answer("⏬ Скачиваю...")
    
    status_msg = await callback.message.answer("📥 Загрузка файла...")
    
    try:
        disk_api = YandexDiskAPI(user_tokens[user_id])
        
        download_url = await disk_api.get_download_link(file_path)
        
        if not download_url:
            await status_msg.edit_text("❌ Не удалось получить ссылку")
            return
        
        file_data = await disk_api.download_file(download_url)
        
        if not file_data:
            await status_msg.edit_text("❌ Ошибка при скачивании")
            return
        
        file_name = file_path.split("/")[-1]
        
        await callback.message.answer_document(
            types.BufferedInputFile(file_data, filename=file_name),
            caption=f"✅ {file_name}"
        )
        
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"Ошибка скачивания: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка: {str(e)}")

@dp.callback_query(F.data.startswith("refresh:"))
async def handle_refresh_callback(callback: CallbackQuery):
    """Обработка обновления"""
    folder_path = callback.data[8:]
    user_id = callback.from_user.id
    
    if user_id not in user_tokens:
        await callback.answer("⚠️ Сессия истекла", show_alert=True)
        return
    
    await callback.answer("🔄 Обновляю...")
    
    # Создаем новое сообщение с обновленным содержимым
    await send_folder_list(callback.message, folder_path)
    
    # Удаляем старое сообщение
    try:
        await callback.message.delete()
    except Exception as e:
        logger.error(f"Ошибка удаления: {e}")

@dp.callback_query(F.data == "empty")
async def handle_empty_callback(callback: CallbackQuery):
    """Пустая папка"""
    await callback.answer("Папка пуста", show_alert=False)

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_auth_code(message: Message):
    """Обработка кода авторизации"""
    text = message.text.strip()
    user_id = message.from_user.id
    
    if user_id in auth_states and auth_states[user_id]:
        status_msg = await message.answer("🔄 Авторизация...")
        
        token = await get_access_token(text)
        
        if token:
            user_tokens[user_id] = token
            auth_states[user_id] = False
            
            await status_msg.edit_text(
                "✅ Авторизация успешна!\n\n"
                "Используйте /list для просмотра папки."
            )
            
            # Показываем папку
            await send_folder_list(message, YANDEX_FOLDER_PATH, True)
        else:
            await status_msg.edit_text(
                "❌ Ошибка авторизации.\n\n"
                "Проверьте код и попробуйте снова: /start"
            )
    else:
        if user_id not in user_tokens:
            await message.answer(
                "❓ Неизвестная команда.\n\n"
                "Используйте /start для авторизации."
            )

async def main():
    """Запуск бота"""
    logger.info("Запуск бота...")
    logger.info(f"Папка: {YANDEX_FOLDER_PATH}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
