import json
import logging
import re
import os
from typing import List, Dict, Any, Optional
from datetime import datetime

import httpx
import yt_dlp
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
from telegram.request import HTTPXRequest

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ТОКЕНЫ
BOT_TOKEN = "8966648824:AAFN7M-t3ALQS1KzB1jX9kGSIfjQwWUCbSE"
YOUTUBE_API_KEY = "AIzaSyDr6bMqYPUwa7BE0WgvCs_Ay6r6ImJSC-g"

# ПРОКСИ
PROXY_URL = ""

if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL

# НАСТРОЙКИ
HISTORY_FILE = "history.json"
MAX_VIDEOS_PER_SEARCH = 15
MAX_KEYWORDS_PER_PAGE = 4
KEYWORD_INPUT = 0

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# РАБОТА С ИСТОРИЕЙ
def load_history() -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_history(history: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_user_history(user_id: int) -> Dict[str, List[Dict[str, Any]]]:
    history = load_history()
    return history.get(str(user_id), {})


def save_user_history(user_id: int, user_data: Dict[str, List[Dict[str, Any]]]) -> None:
    history = load_history()
    history[str(user_id)] = user_data
    save_history(history)


def get_transcript(video_id: str) -> Optional[str]:
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'skip_download': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['ru', 'en'],
        'quiet': True,
        'no_warnings': True,
        'cookiefile': 'cookies.txt',  # <-- Путь к cookies
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            # Сначала ищем ручные субтитры
            subs = info.get('subtitles', {})
            if not subs:
                # Если ручных нет, берём автоматические
                subs = info.get('automatic_captions', {})

            if not subs:
                return None

            for lang in ['ru', 'en']:
                if lang in subs:
                    for fmt in subs[lang]:
                        if fmt.get('ext') in ('vtt', 'srt'):
                            data = fmt.get('data')
                            if data:
                                # Парсим текст из vtt/srt
                                text = parse_subtitle_data(data)
                                if text:
                                    return text
            return None
    except Exception as e:
        logger.error(f"Ошибка при получении субтитров для {video_id}: {e}")
        return None


def parse_subtitle_data(data: str) -> str:
    lines = data.splitlines()
    text_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r'^\d+$', line):
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2}', line):
            continue
        if line.startswith('WEBVTT') or line.startswith('Kind:'):
            continue
        line = re.sub(r'<[^>]+>', '', line)
        text_lines.append(line)
    return ' '.join(text_lines)


# ВЫДЕЛЕНИЕ ОТРЫВКА С КЛЮЧЕВЫМ СЛОВОМ ИЛИ ПЕРВЫХ 20 СЛОВ
def extract_transcript_snippet(transcript: str, keyword: str, context_chars: int = 150) -> str:
    if not transcript:
        return ""
    if not keyword:
        words = transcript.split()
        snippet = ' '.join(words[:20])
        if len(words) > 20:
            snippet += '...'
        return snippet

    # (регистронезависимо)
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    match = pattern.search(transcript)

    if match:
        start = max(0, match.start() - context_chars // 2)
        end = min(len(transcript), match.end() + context_chars // 2)
        snippet = transcript[start:end]
        snippet = pattern.sub(r'<b>\g<0></b>', snippet)
        if start > 0:
            snippet = '...' + snippet
        if end < len(transcript):
            snippet = snippet + '...'
        return snippet
    else:
        words = transcript.split()
        snippet = ' '.join(words[:20])
        if len(words) > 20:
            snippet += '...'
        return snippet


# ПАРСЕР
def search_youtube_videos(keyword: str, max_results: int = MAX_VIDEOS_PER_SEARCH) -> List[Dict[str, Any]]:
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    try:
        request = youtube.search().list(
            q=keyword,
            part="snippet",
            type="video",
            maxResults=max_results,
            order="relevance"
        )
        response = request.execute()
    except HttpError as e:
        logger.error(f"YouTube API error: {e}")
        return []

    videos = []
    for item in response.get("items", []):
        if item.get("id", {}).get("kind") != "youtube#video":
            continue
        video_id = item["id"].get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet")
        if not snippet:
            continue

        try:
            stats_request = youtube.videos().list(part="statistics", id=video_id)
            stats_response = stats_request.execute()
            stats = stats_response["items"][0]["statistics"] if stats_response["items"] else {}
        except HttpError:
            stats = {}

        video_data = {
            "title": snippet.get("title", "Без названия"),
            "author": snippet.get("channelTitle", "Неизвестный автор"),
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": snippet.get("publishedAt", ""),
            "likes": int(stats.get("likeCount", 0)),
            "comments": int(stats.get("commentCount", 0)),
            "transcript": get_transcript(video_id),  # теперь через yt-dlp
        }
        videos.append(video_data)

    return videos


def format_video_message(video: Dict[str, Any], keyword: str = "") -> str:
    pub_date = ""
    if video.get("published_at"):
        try:
            dt = datetime.fromisoformat(video["published_at"].replace("Z", "+00:00"))
            pub_date = dt.strftime("%d.%m.%Y")
        except:
            pub_date = video["published_at"][:10]

    msg = f"<b>{video['title']}</b>\n"
    msg += f"👤 {video['author']}\n"
    if pub_date:
        msg += f"📅 {pub_date}\n"
    msg += f"👍 {video['likes']}  💬 {video['comments']}\n"

    if video["transcript"]:
        snippet = extract_transcript_snippet(video["transcript"], keyword)
        if snippet:
            msg += f"📝 <i>Расшифровка (отрывок):</i>\n{snippet}\n"
    msg += f"🔗 <a href='{video['url']}'>Смотреть на YouTube</a>"
    return msg


# ОБРАБОТЧИКИ БОТА
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await update.message.reply_text(
        "Привет! Я бот для парсинга YouTube.\nВыбери действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def parse_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите ключевое слово или тег (можно несколько через запятую):")
    return KEYWORD_INPUT


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "stats":
        user_history = get_user_history(user_id)
        if not user_history:
            await query.edit_message_text("Ваша история пуста.")
            return
        keywords = list(user_history.keys())
        await show_keyword_page(query, context, keywords, page=0, user_id=user_id)
    elif data == "clear":
        keyboard = [
            [InlineKeyboardButton("✅ Да, очистить всё", callback_data="clear_confirm")],
            [InlineKeyboardButton("❌ Нет", callback_data="clear_cancel")],
        ]
        await query.edit_message_text(
            "Вы уверены, что хотите удалить всю историю парсинга?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def show_keyword_page(query, context: ContextTypes.DEFAULT_TYPE, keywords: List[str], page: int, user_id: int):
    total = len(keywords)
    start_idx = page * MAX_KEYWORDS_PER_PAGE
    end_idx = min(start_idx + MAX_KEYWORDS_PER_PAGE, total)
    page_keywords = keywords[start_idx:end_idx]

    buttons = []
    for kw in page_keywords:
        buttons.append([InlineKeyboardButton(kw, callback_data=f"show_{kw}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"stats_page_{page - 1}"))
    if end_idx < total:
        nav_buttons.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"stats_page_{page + 1}"))
    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    await query.edit_message_text(
        f"Выберите ключевое слово (страница {page + 1}):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    context.user_data["stats_page"] = page
    context.user_data["stats_keywords"] = keywords
    context.user_data["stats_user_id"] = user_id


async def stats_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    keywords = context.user_data.get("stats_keywords", [])
    user_id = context.user_data.get("stats_user_id", update.effective_user.id)
    if not keywords:
        user_history = get_user_history(user_id)
        keywords = list(user_history.keys())
    await show_keyword_page(query, context, keywords, page, user_id)


async def show_keyword_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    keyword = query.data[5:]
    user_id = update.effective_user.id
    user_history = get_user_history(user_id)
    videos = user_history.get(keyword, [])
    if not videos:
        await query.edit_message_text(f"По ключевому слову '{keyword}' ничего не найдено.")
        return

    for video in videos[:MAX_VIDEOS_PER_SEARCH]:
        msg = format_video_message(video, keyword=keyword)
        await query.message.reply_text(msg, parse_mode="HTML")

    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await query.message.reply_text("Главное меню:", reply_markup=InlineKeyboardMarkup(keyboard))
    await query.edit_message_text(f"Показаны результаты для '{keyword}'.")


async def clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    save_user_history(user_id, {})
    await query.edit_message_text("Ваша история полностью очищена.")
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await query.message.reply_text("Главное меню:", reply_markup=InlineKeyboardMarkup(keyboard))


async def clear_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Очистка отменена.")
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await query.message.reply_text("Главное меню:", reply_markup=InlineKeyboardMarkup(keyboard))


async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Возврат в главное меню.")
    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await query.message.reply_text("Главное меню:", reply_markup=InlineKeyboardMarkup(keyboard))


async def parse_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    raw_keyword = update.message.text.strip()
    if not raw_keyword:
        await update.message.reply_text("Пожалуйста, введите непустое ключевое слово.")
        return KEYWORD_INPUT

    keyword = raw_keyword.lower()
    await update.message.reply_text(f"Ищу видео по запросу: '{raw_keyword}'...")

    user_history = get_user_history(user_id)
    existing_videos = user_history.get(keyword, [])
    existing_ids = {v["video_id"] for v in existing_videos}

    all_videos = search_youtube_videos(raw_keyword)
    new_videos = [v for v in all_videos if v["video_id"] not in existing_ids]

    if not new_videos:
        await update.message.reply_text("Новых видео по этому запросу не найдено.")
        if keyword not in user_history:
            user_history[keyword] = []
            save_user_history(user_id, user_history)
        return ConversationHandler.END

    user_history[keyword] = existing_videos + new_videos
    save_user_history(user_id, user_history)

    for video in new_videos:
        msg = format_video_message(video, keyword=raw_keyword)
        try:
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            await update.message.reply_text("Не удалось отправить одно из видео (возможно, слишком длинное сообщение).")

    keyboard = [
        [InlineKeyboardButton("🔍 Начать парсить", callback_data="parse")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Стереть историю", callback_data="clear")],
    ]
    await update.message.reply_text(
        f"Найдено {len(new_videos)} новых видео. Парсинг завершён.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END


# ЗАПУСК
def main() -> None:
    request = HTTPXRequest()
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(parse_button, pattern="^parse$")],
        states={
            KEYWORD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, parse_keyword)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^(stats|clear)$"))
    application.add_handler(CallbackQueryHandler(stats_page_callback, pattern="^stats_page_"))
    application.add_handler(CallbackQueryHandler(show_keyword_results, pattern="^show_"))
    application.add_handler(CallbackQueryHandler(clear_confirm, pattern="^clear_confirm$"))
    application.add_handler(CallbackQueryHandler(clear_cancel, pattern="^clear_cancel$"))
    application.add_handler(CallbackQueryHandler(main_menu, pattern="^main_menu$"))
    application.add_handler(CommandHandler("start", start))

    print("Бот запущен...")
    application.run_polling(bootstrap_retries=5)


if __name__ == "__main__":
    main()
