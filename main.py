BOT_TOKEN = ""
YOUTUBE_API_KEY = ""

import json
import logging
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

import config

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------ Константы ------------------
HISTORY_FILE = "history.json"          # файл для хранения результатов
MAX_VIDEOS_PER_SEARCH = 10             # максимум видео на один запрос
MAX_KEYWORDS_PER_PAGE = 4              # кнопок на странице (2 ряда × 2)
KEYWORD_INPUT, CONFIRM_CLEAR = range(2)  # состояния для ConversationHandler

# ------------------ Работа с историей ------------------
def load_history() -> Dict[str, List[Dict[str, Any]]]:
    """Загружает историю из JSON-файла."""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_history(history: Dict[str, List[Dict[str, Any]]]) -> None:
    """Сохраняет историю в JSON-файл."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# ------------------ Парсер YouTube ------------------
def search_youtube_videos(keyword: str, max_results: int = MAX_VIDEOS_PER_SEARCH) -> List[Dict[str, Any]]:
    """
    Ищет видео на YouTube по ключевому слову.
    Возвращает список словарей с данными видео.
    """
    youtube = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)
    try:
        request = youtube.search().list(
            q=keyword,
            part="snippet",
            type="video",
            maxResults=max_results,
            order="relevance"  # можно изменить
        )
        response = request.execute()
    except HttpError as e:
        logger.error(f"YouTube API error: {e}")
        return []

    videos = []
    for item in response.get("items", []):
        video_id = item["id"]["videoId"]
        snippet = item["snippet"]
        # Получаем статистику отдельно
        try:
            stats_request = youtube.videos().list(
                part="statistics",
                id=video_id
            )
            stats_response = stats_request.execute()
            stats = stats_response["items"][0]["statistics"] if stats_response["items"] else {}
        except HttpError:
            stats = {}

        video_data = {
            "title": snippet["title"],
            "author": snippet["channelTitle"],
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": snippet["thumbnails"]["high"]["url"],  # или medium
            "likes": int(stats.get("likeCount", 0)),
            "comments": int(stats.get("commentCount", 0)),
            "transcript": None,  # заполним позже
        }
        videos.append(video_data)

    # Пытаемся получить субтитры для каждого видео (асинхронно можно, но здесь синхронно)
    for v in videos:
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(v["video_id"])
            # Берём первый доступный (обычно английский)
            transcript = transcript_list.find_manually_created_transcript()
            # или transcript_list.find_generated_transcript()
            if transcript:
                # Получаем полный текст
                full_text = " ".join([entry["text"] for entry in transcript.fetch()])
                v["transcript"] = full_text
                # Выделяем места с ключевым словом (просто помечаем)
                # Здесь можно добавить поиск совпадений и выделение, но оставим как есть
        except (TranscriptsDisabled, NoTranscriptFound, Exception) as e:
            logger.debug(f"Transcript not available for {v['video_id']}: {e}")
            v["transcript"] = None

    return videos

def format_video_message(video: Dict[str, Any], keyword: str = "") -> str:
    """
    Форматирует данные видео в сообщение для Telegram.
    """
    msg = f"<b>{video['title']}</b>\n"
    msg += f"👤 {video['author']}\n"
    msg += f"👍 {video['likes']}  💬 {video['comments']}\n"
    # Обложка будет отправлена отдельным фото, поэтому просто добавим ссылку в текст
    msg += f"🖼 <a href='{video['thumbnail']}'>&#8205;</a>\n"  # невидимый символ для принудительного отображения превью
    if video["transcript"]:
        # Ограничим длину транскрипта, чтобы не превысить лимит сообщения
        transcript = video["transcript"][:1000] + "..." if len(video["transcript"]) > 1000 else video["transcript"]
        # Выделяем ключевые слова (просто оборачиваем в жирный, если они есть)
        if keyword:
            # ищем вхождение (регистронезависимо) и заменяем на выделение
            # упрощённо – заменяем все вхождения keyword (целиком) на <b>keyword</b>
            # для более точного выделения можно использовать re, но ограничимся простым
            transcript = transcript.replace(keyword, f"<b>{keyword}</b>")
        msg += f"📝 <i>Расшифровка:</i>\n{transcript}\n"
    msg += f"🔗 <a href='{video['url']}'>Смотреть</a>"
    return msg

# ------------------ Обработчики бота ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет главное меню."""
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Привет! Я бот для парсинга YouTube.\n"
        "Выбери действие:",
        reply_markup=reply_markup,
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает нажатия кнопок главного меню.
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "parse":
        await query.edit_message_text("Введите ключевое слово или тег (можно несколько через запятую):")
        return ConversationHandler.END  # будем использовать ConversationHandler отдельно

    elif data == "stats":
        history = load_history()
        if not history:
            await query.edit_message_text("История пуста.")
            return
        # Показываем список ключевых слов с пагинацией
        keywords = list(history.keys())
        await show_keyword_page(query, context, keywords, page=0)

    elif data == "clear":
        # Подтверждение очистки
        keyboard = [
            [InlineKeyboardButton("✅ Да, очистить всё", callback_data="clear_confirm")],
            [InlineKeyboardButton("❌ Нет", callback_data="clear_cancel")],
        ]
        await query.edit_message_text(
            "Вы уверены, что хотите удалить всю историю парсинга?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

async def show_keyword_page(query, context: ContextTypes.DEFAULT_TYPE, keywords: List[str], page: int):
    """
    Отображает страницу со списком ключевых слов (пагинация).
    """
    total = len(keywords)
    start = page * MAX_KEYWORDS_PER_PAGE
    end = min(start + MAX_KEYWORDS_PER_PAGE, total)
    page_keywords = keywords[start:end]

    buttons = []
    for kw in page_keywords:
        buttons.append([InlineKeyboardButton(kw, callback_data=f"show_{kw}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"stats_page_{page-1}"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"stats_page_{page+1}"))
    if nav_buttons:
        buttons.append(nav_buttons)

    # Кнопка возврата в главное меню
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(
        f"Выберите ключевое слово (страница {page+1}):",
        reply_markup=reply_markup,
    )
    context.user_data["stats_page"] = page
    context.user_data["stats_keywords"] = keywords

async def stats_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатие кнопок пагинации в статистике."""
    query = update.callback_query
    await query.answer()
    data = query.data
    # data = "stats_page_0"
    page = int(data.split("_")[-1])
    keywords = context.user_data.get("stats_keywords", [])
    if not keywords:
        history = load_history()
        keywords = list(history.keys())
    await show_keyword_page(query, context, keywords, page)

async def show_keyword_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает все результаты для выбранного ключевого слова.
    """
    query = update.callback_query
    await query.answer()
    data = query.data  # "show_ключевое_слово"
    keyword = data[5:]  # убираем "show_"
    history = load_history()
    videos = history.get(keyword, [])
    if not videos:
        await query.edit_message_text(f"По ключевому слову '{keyword}' ничего не найдено.")
        return

    # Отправляем по одному видео в сообщении (может быть много, ограничимся первыми 10)
    for video in videos[:MAX_VIDEOS_PER_SEARCH]:
        msg = format_video_message(video, keyword=keyword)
        await query.message.reply_text(msg, parse_mode="HTML")
    # После отправки всех видео возвращаем в меню статистики
    await query.edit_message_text(f"Показаны результаты для '{keyword}'. Выберите другое действие:")
    # Показываем главное меню или список статистики?
    # Просто покажем главное меню
    await start(update, context)  # но update не подходит, лучше отправить новое сообщение
    # Вместо этого отправим новое сообщение с главным меню
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await query.message.reply_text(
        "Главное меню:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждение очистки истории."""
    query = update.callback_query
    await query.answer()
    save_history({})
    await query.edit_message_text("История полностью очищена.")
    # показать главное меню
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await query.message.reply_text(
        "Главное меню:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def clear_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмена очистки."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Очистка отменена.")
    # показать главное меню
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await query.message.reply_text(
        "Главное меню:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Возврат в главное меню."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Возврат в главное меню.")
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await query.message.reply_text(
        "Главное меню:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

# ------------------ Обработка ввода ключевого слова (конверсейшн) ------------------
async def parse_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает ключевое слово от пользователя и запускает парсинг."""
    keyword = update.message.text.strip()
    if not keyword:
        await update.message.reply_text("Пожалуйста, введите непустое ключевое слово.")
        return KEYWORD_INPUT

    # Отправляем уведомление о начале
    await update.message.reply_text(f"Ищу видео по запросу: '{keyword}'...")

    # Запускаем парсинг (синхронно, но может занять время)
    videos = search_youtube_videos(keyword)

    if not videos:
        await update.message.reply_text("Видео не найдены.")
        # Сохраняем пустой список? Можно сохранить, но чтобы статистика показывала, что ничего нет.
        history = load_history()
        if keyword not in history:
            history[keyword] = []
            save_history(history)
        return ConversationHandler.END

    # Сохраняем в историю (добавляем, а не перезаписываем)
    history = load_history()
    if keyword in history:
        # Добавляем новые видео, избегая дубликатов по video_id
        existing_ids = {v["video_id"] for v in history[keyword]}
        for v in videos:
            if v["video_id"] not in existing_ids:
                history[keyword].append(v)
    else:
        history[keyword] = videos
    save_history(history)

    # Отправляем результаты
    for video in videos:
        msg = format_video_message(video, keyword=keyword)
        try:
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            await update.message.reply_text("Не удалось отправить одно из видео (возможно, слишком длинное сообщение).")

    # Возвращаем главное меню
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await update.message.reply_text(
        "Парсинг завершён. Выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена ввода ключевого слова."""
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# ------------------ Основная функция ------------------
def main() -> None:
    """Запуск бота."""
    application = ApplicationBuilder().token(config.BOT_TOKEN).build()

    # ConversationHandler для ввода ключевого слова
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^parse$")],
        states={
            KEYWORD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, parse_keyword)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv_handler)

    # Обработчики кнопок
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(stats|clear)$"))
    application.add_handler(CallbackQueryHandler(stats_page_callback, pattern="^stats_page_"))
    application.add_handler(CallbackQueryHandler(show_keyword_results, pattern="^show_"))
    application.add_handler(CallbackQueryHandler(clear_confirm, pattern="^clear_confirm$"))
    application.add_handler(CallbackQueryHandler(clear_cancel, pattern="^clear_cancel$"))
    application.add_handler(CallbackQueryHandler(main_menu, pattern="^main_menu$"))

    # Команда /start
    application.add_handler(CommandHandler("start", start))

    # Запускаем бота
    print("Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()