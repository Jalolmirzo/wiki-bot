import logging
import asyncio
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import wikipedia
import sqlite3
import json
import os
from dotenv import load_dotenv

# .env faylini yuklash
load_dotenv()

# Loggingni sozlash
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- KONSTANTALAR ---
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN topilmadi. Iltimos, .env faylini tekshiring.")
    exit(1)

# Konstanta: Maksimal belgilar soni
MAX_CAPTION_LENGTH = 1024
MAX_MESSAGE_LENGTH = 4000

# Foydalanuvchi tarixini saqlash uchun dictionary
user_history = {}

# --- SQLITE KESH SOZLAMALARI ---
DB_NAME = "wiki_cache.db"

def init_db():
    """Ma'lumotlar bazasini yaratadi va jadvalni sozlaydi."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            query_key TEXT PRIMARY KEY,
            response_text TEXT NOT NULL,
            keyboard_json TEXT,
            photo_url TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_cache(query_key):
    """Keshni ma'lumotlar bazasidan oladi (Har safar yangi ulanish)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT response_text, keyboard_json, photo_url FROM cache WHERE query_key = ?", (query_key,))
    result = cursor.fetchone()

    conn.close() # Ulanishni yopish

    if result:
        response_text, keyboard_json, photo_url = result

        # JSON stringni Python obyektiga aylantirish
        keyboard_structure = json.loads(keyboard_json) if keyboard_json else None

        # Tugmalar strukturasini InlineKeyboardMarkup obyektiga qayta aylantirish
        restored_keyboard = None
        if keyboard_structure:
            restored_rows = []
            for row in keyboard_structure:
                restored_row = []
                for button_data in row:
                    # Lug'atni InlineKeyboardButton obyektiga aylantirish
                    # Faqat mavjud kalitlarni olamiz (url yoki callback_data)
                    valid_data = {k: v for k, v in button_data.items() if v is not None}
                    restored_row.append(InlineKeyboardButton(**valid_data))
                restored_rows.append(restored_row)
            restored_keyboard = InlineKeyboardMarkup(restored_rows)

        return response_text, restored_keyboard, photo_url
    return None

def save_cache(query_key, response_text, keyboard, photo_url):
    """Natijani ma'lumotlar bazasiga saqlaydi (Har safar yangi ulanish)."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # InlineKeyboardMarkup obyektini JSON stringga aylantirish
    keyboard_structure = None
    if keyboard:
        keyboard_structure = []
        for row in keyboard.inline_keyboard:
            new_row = []
            for button in row:
                # Tugmaning faqat muhim ma'lumotlarini (text, url, callback_data) olamiz
                button_data = {
                    'text': button.text,
                    'url': button.url,
                    'callback_data': button.callback_data
                }
                new_row.append(button_data)
            keyboard_structure.append(new_row)

    keyboard_json = json.dumps(keyboard_structure, ensure_ascii=False)

    cursor.execute("""
        INSERT OR REPLACE INTO cache (query_key, response_text, keyboard_json, photo_url)
        VALUES (?, ?, ?, ?)
    """, (query_key, response_text, keyboard_json, photo_url))

    conn.commit()
    conn.close()
# --- SQLITE KESH SOZLAMALARI TUGADI ---


# Animatsiya funksiyasi
async def animate_searching(message_to_edit: Update.message, stop_event: asyncio.Event) -> None:
    base_text = "⏳ Qidirilmoqda"
    dots = ["", ".", "..", "..."]
    i = 0
    while not stop_event.is_set():
        current_text = f"{base_text}{dots[i % len(dots)]}"
        try:
            await message_to_edit.edit_text(current_text)
        except Exception as e:
            logger.warning(f"Animatsiya xabarini tahrirlashda xatolik: {e}. Animatsiya to'xtatildi.")
            break
        await asyncio.sleep(0.8)
        i += 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"Salom, {user.first_name}! 👋\n\n"
        "*Men eng ko'p ma'lumot beradigan Wikipedia botiman*.\n"
        "Sizga kerakli bilimlarni topishda yordam beraman.\n"
        "Men Latifjonov Jalolmirzo (@JalolmirzoC63) tomonidan yaratilganman.\n\n"
        "Nimani bilmoqchisiz? Yozing, men sizga ma'lumot topib beraman.\n\n"
        "Misol:\n• Python\n• O'zbekiston\n• Elon Musk\n",
        parse_mode="Markdown"
    )


# Wikipedia qidiruv funksiyasi
def _perform_wikipedia_search_sync(query: str):

    # So'rovni kesh kalitiga aylantirish
    cache_key = query.lower()

    # --- 1. KESHNI TEKSHIRISH (SQLite) ---
    cached_data = get_cache(cache_key)
    if cached_data:
        response_text, restored_keyboard, photo_url = cached_data
        logger.info(f"'{query}' uchun ma'lumot SQLite keshdan olindi.")
        return response_text, restored_keyboard, photo_url

    # --- 2. INTERNETDAN QIDIRISH (Agar keshda bo'lmasa) ---

    response_text = ""
    keyboard = None
    photo_url = None
    lang_found = None

    EXCLUDE_SECTIONS = [
        "see also", "references", "external links", "footnotes", "bibliography",
        "further reading", "disclaimer", "notes", "citations", "g'alereya",
        "adabiyotlar", "havolalar", "ko'proq ma'lumot", "tashqi havolalar",
        "manbalar", "galereya", "qo'shimcha o'qish", "linklar", "shuningdek qarang",
        "bibliografiya", "eslatmalar", "ijtimoiy tarmoqlar"
    ]

    def get_summary_and_sections(q, lang):
        wikipedia.set_lang(lang)
        page_obj = wikipedia.page(q)
        initial_summary = wikipedia.summary(q)[:MAX_MESSAGE_LENGTH]
        full_text = initial_summary

        # Yaxshiroq rasm tanlash logikasi
        found_photo_url = None
        for img_url in page_obj.images:
            if img_url.lower().endswith((".jpg", ".png", ".jpeg")):
                found_photo_url = img_url
                break

        # Sektsiyalarni tahlil qilish
        for section_title in page_obj.sections:
            if section_title.lower() in EXCLUDE_SECTIONS:
                continue
            section_content = page_obj.section(section_title)
            if section_content:
                section_snippet = f"\n\n*{section_title}*\n{section_content}"
                if len(full_text) + len(section_snippet) + len("\n\n...") > MAX_MESSAGE_LENGTH:
                    remaining_chars = MAX_MESSAGE_LENGTH - len(full_text) - len("\n\n...")
                    if remaining_chars > 0:
                        full_text += section_snippet[:remaining_chars] + "\n\n..."
                    else:
                        full_text += "\n\n..."
                    break
                else:
                    full_text += section_snippet
        return page_obj, full_text, found_photo_url

    try:
        page, summary, photo_url_from_func = get_summary_and_sections(query, "uz")
        lang_found = "O'zbekcha Vikipediya"
        url = page.url
        photo_url = photo_url_from_func

        response_text = f"📌 *{query}* ({lang_found})\n\n{summary}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔗 To‘liq maqolani o‘qish (UZ)", url=url)]])

    except wikipedia.exceptions.PageError:
        logger.info(f"'{query}' uchun O'zbekcha sahifa topilmadi. Inglizcha qidirilmoqda...")
        try:
            page, summary, photo_url_from_func = get_summary_and_sections(query, "en")
            lang_found = "English Wikipedia"
            url = page.url
            photo_url = photo_url_from_func

            response_text = f"📌 *{query}* ({lang_found})\n\n{summary}"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔗 To‘liq maqolani o‘qish (EN)", url=url)]])
        except wikipedia.exceptions.PageError:
            logger.info(f"'{query}' uchun Inglizcha sahifa ham topilmadi.")
            response_text = f"❌ Kechirasiz, '{query}' bo‘yicha hech qanday ma'lumot topilmadi.\nIltimos so‘rovni boshqacha yozib ko'ring."
        except wikipedia.exceptions.DisambiguationError as e:
            variants = "\n".join(e.options[:7])
            lang_found = "English Wikipedia"
            response_text = f"'{query}' bir nechta ma'noga ega. ({lang_found})\nQuyidagi variantlardan birini aniq yozing:\n\n{variants}"
        except Exception as e:
            logger.error(f"Inglizcha qidirishda kutilmagan xatolik yuz berdi: {e}")
            response_text = "❌ Qidirishda kutilmagan xatolik yuz berdi. Iltimos, keyinroq urinib ko'ring."

    except wikipedia.exceptions.DisambiguationError as e:
        variants = "\n".join(e.options[:7])
        lang_found = "O'zbekcha Vikipediya"
        response_text = f"'{query}' bir nechta ma'noga ega. ({lang_found})\nQuyidagi variantlardan birini aniq yozing:\n\n{variants}"
    except Exception as e:
        logger.error(f"O'zbekcha qidirishda kutilmagan xatolik yuz berdi: {e}")
        response_text = "❌ Qidirishda kutilmagan xatolik yuz berdi. Iltimos, keyinroq urinib ko'ring."

    # --- 3. KESHGA SAQLASH (Agar muvaffaqiyatli bo'lsa) ---
    if response_text and not response_text.startswith("❌") and not response_text.startswith("'"):
        save_cache(cache_key, response_text, keyboard, photo_url)
        logger.info(f"'{query}' uchun ma'lumot SQLite keshga saqlandi.")

    return response_text, keyboard, photo_url


# Wikipedia qidirish va javobni yuborish
async def search_wikipedia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.message.text
    logger.info(f"Foydalanuvchi '{query}' ni qidirdi.")

    # Foydalanuvchi tarixini yangilash
    user_id = update.message.from_user.id
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append(query)

    sent_message = await update.message.reply_text("⏳ Qidirilmoqda...")
    stop_animation_event = asyncio.Event()

    # Animatsiya vazifasini yaratish
    animation_task = asyncio.create_task(animate_searching(sent_message, stop_animation_event))

    # Qidiruvni bajarish (alohida threadda)
    loop = asyncio.get_running_loop()
    response_text, keyboard, photo_url = await loop.run_in_executor(None, _perform_wikipedia_search_sync, query)

    # Animatsiyani to'xtatish
    stop_animation_event.set()
    await animation_task

    try:
        await sent_message.delete()
    except Exception as e:
        logger.warning(f"'Qidirilmoqda...' xabarini o'chirishda xatolik: {e}")

    if response_text:
        if photo_url:
            caption_to_send = response_text
            if len(caption_to_send) > MAX_CAPTION_LENGTH:
                ellipsis_text = "\n\n...To'liq maqolani o'qish uchun tugmani bosing."
                if len(caption_to_send) > MAX_CAPTION_LENGTH - len(ellipsis_text):
                    caption_to_send = caption_to_send[:MAX_CAPTION_LENGTH - len(ellipsis_text)] + ellipsis_text
                else:
                    caption_to_send = caption_to_send[:MAX_CAPTION_LENGTH]

            try:
                await update.message.reply_photo(
                    photo=photo_url,
                    caption=caption_to_send,
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
            except telegram.error.BadRequest as e:
                logger.warning(f"Rasm yuborishda xatolik yuz berdi (BadRequest): {e}. URL: {photo_url}. Matnli xabar yuborilmoqda.")
                await update.message.reply_text(response_text, reply_markup=keyboard, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Kutilmagan xato rasm yuborishda: {e}. URL: {photo_url}. Matnli xabar yuborilmoqda.")
                await update.message.reply_text(response_text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await update.message.reply_text(response_text, reply_markup=keyboard, parse_mode="Markdown")


# Foydalanuvchi tarixini ko'rsatish
async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    if user_id in user_history:
        history = "\n".join(user_history[user_id][-5:])  # Faqat oxirgi 5 ta qidiruvni ko'rsatish
        await update.message.reply_text(f"Sizning tarixingiz:\n{history}")
    else:
        await update.message.reply_text("Siz hali hech narsa qidirmadingiz.")


def main() -> None:
    # Ma'lumotlar bazasini bir marta sozlash (jadvalni yaratish)
    init_db()

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("history", show_history))  # Foydalanuvchi tarixini ko'rsatish
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_wikipedia))

    # Botni ishga tushirish
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()