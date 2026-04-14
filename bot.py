import asyncio
import aiohttp
import aiosqlite
import random
import string
import os
import re
import logging
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
    InputMediaPhoto
)
from aiogram.filters import Command
from aiogram.enums import ChatAction, ParseMode
from aiogram.client.default import DefaultBotProperties

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BANNER_PHOTO = os.getenv("BANNER_PHOTO", "https://i.imgur.com/placeholder.jpg")
CHANNEL_URL = "https://t.me/vibeclauding"
DB_PATH = "emails.db"
MAIL_API = "https://api.mail.tm"

EMAIL_LIFETIME = 10 * 60        # 10 минут в секундах
NEW_EMAIL_COOLDOWN = 30         # 30 секунд кулдаун
EXTEND_STARS = 10               # Стоимость продления
EXTEND_MINUTES = 10             # На сколько продлевать

ADMIN_IDS: set = set(
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)

OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# admin_id -> True если ждём от него сообщение для рассылки
broadcast_pending: dict = {}

# user_id -> set of mail.tm message IDs уже доставленных пользователю
seen_messages: dict = {}

# user_id -> list of messages (кеш для пагинации входящих)
inbox_cache: dict = {}


# ─────────────────────── DATABASE ───────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                email       TEXT,
                password    TEXT,
                token       TEXT,
                expires_at  REAL,
                created_at  REAL,
                last_created REAL DEFAULT 0,
                msg_id      INTEGER DEFAULT 0
            )
        """)
        await db.commit()


async def get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_user(user_id: int, **kwargs):
    user = await get_user(user_id)
    if not user:
        await _insert_user(user_id, **kwargs)
    else:
        await _update_user(user_id, **kwargs)


async def _insert_user(user_id: int, **kwargs):
    kwargs["user_id"] = user_id
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" * len(kwargs))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"INSERT OR REPLACE INTO users ({cols}) VALUES ({placeholders})", list(kwargs.values()))
        await db.commit()


async def _update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    sets = ", ".join(f"{k}=?" for k in kwargs)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {sets} WHERE user_id=?", list(kwargs.values()) + [user_id])
        await db.commit()


async def clear_email(user_id: int):
    await _update_user(user_id, email=None, password=None, token=None, expires_at=None, created_at=None)


async def get_all_user_ids() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def count_active_emails() -> int:
    now = datetime.now().timestamp()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE email IS NOT NULL AND expires_at > ?", (now,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ─────────────────────── MAIL.TM API ───────────────────────

def rand_str(n=10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


async def get_domain() -> Optional[str]:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{MAIL_API}/domains", timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                domains = data.get("hydra:member", [])
                if domains:
                    return domains[0]["domain"]
    return None


async def create_email() -> Optional[dict]:
    domain = await get_domain()
    if not domain:
        return None
    address = f"{rand_str(10)}@{domain}"
    password = rand_str(16)
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{MAIL_API}/accounts",
            json={"address": address, "password": password},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status not in (200, 201):
                return None
        async with s.post(
            f"{MAIL_API}/token",
            json={"address": address, "password": password},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                return None
            token_data = await r.json()
            return {"address": address, "password": password, "token": token_data.get("token")}


async def get_messages(token: str) -> list:
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{MAIL_API}/messages",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                data = await r.json()
                return data.get("hydra:member", [])
    return []


async def get_message_content(token: str, msg_id: str) -> Optional[dict]:
    """Получить полное содержимое письма по ID"""
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{MAIL_API}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status == 200:
                return await r.json()
    return None


def highlight_codes(text: str) -> str:
    """
    Находит коды верификации (числовые, буквенно-цифровые и прочие)
    и оборачивает их в <code> для копирования одним нажатием.
    """
    # 1. Явные числовые коды от 4 до 8 цифр (OTP, PIN)
    text = re.sub(
        r'(?<!\d)(\d{4,8})(?!\d)',
        lambda m: f"<code>{m.group(1)}</code>",
        text
    )
    # 2. Буквенно-цифровые коды: минимум 6 символов, есть и буквы и цифры
    text = re.sub(
        r'\b([A-Za-z0-9]{6,32})\b',
        lambda m: (
            f"<code>{m.group(1)}</code>"
            if re.search(r'[A-Za-z]', m.group(1)) and re.search(r'\d', m.group(1))
            else m.group(0)
        ),
        text
    )
    return text


async def delete_account(token: str, account_id: str):
    async with aiohttp.ClientSession() as s:
        await s.delete(
            f"{MAIL_API}/accounts/{account_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=5)
        )


# ─────────────────────── HELPERS ───────────────────────

def progress_bar(seconds_left: float, total: float = EMAIL_LIFETIME, length: int = 15) -> str:
    pct = max(0.0, min(1.0, seconds_left / total))
    filled = int(pct * length)
    bar = "█" * filled + "░" * (length - filled)
    mins = int(seconds_left) // 60
    secs = int(seconds_left) % 60
    return f"{bar}  {mins}:{secs:02d}"


def pe(emoji_id: str, fallback: str = "🐸") -> str:
    """Premium emoji tag"""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# Премиум эмодзи ID из скриншотов (зелёные)
PE_PERSON   = "6082512605623098578"   # человечек с плюсиком
PE_1M       = "6082441824562061129"   # кружок 1M
PE_ARROW    = "6082228441996860957"   # стрелочка вправо
PE_BULB     = "6082473581550246236"   # лампочка
PE_TRASH    = "6082184272553189481"   # мусорка
PE_INBOX    = "6082468779776809582"   # почтовый ящик
PE_FILES    = "6082403659482668235"   # два файлика (письмо в списке)
PE_CROSS    = "6080368394740178132"   # крестик (кнопка назад)


INBOX_PER_PAGE = 3


def inbox_keyboard(messages: list, page: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура списка входящих с пагинацией."""
    start = page * INBOX_PER_PAGE
    end   = start + INBOX_PER_PAGE
    page_msgs = messages[start:end]

    rows = []
    for msg in page_msgs:
        msg_id = msg.get("id", "")
        sender = msg.get("from", {}).get("address", "???")
        subj   = (msg.get("subject", "") or "(без темы)")
        # Укорачиваем для кнопки
        label_subj   = subj[:22] + "…" if len(subj) > 22 else subj
        label_sender = sender[:20] + "…" if len(sender) > 20 else sender
        rows.append([InlineKeyboardButton(
            text=f"{label_subj} | {label_sender}",
            callback_data=f"open_msg:{msg_id}",
            icon_custom_emoji_id=PE_FILES
        )])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            text="Назад",
            callback_data=f"inbox_page:{page - 1}",
            icon_custom_emoji_id="5893057118545646106"   # ◁ назад
        ))
    if end < len(messages):
        nav_row.append(InlineKeyboardButton(
            text="Вперёд",
            callback_data=f"inbox_page:{page + 1}",
            icon_custom_emoji_id="5870633910337015697"   # ✅ галочка/вперёд
        ))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(
        text="Назад",
        callback_data="back_to_main",
        icon_custom_emoji_id=PE_CROSS
    )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Новая почта",
                callback_data="new_email",
                style="success",
                icon_custom_emoji_id=PE_PERSON
            ),
            InlineKeyboardButton(
                text="Проверить письма",
                callback_data="check_email",
                style="success",
                icon_custom_emoji_id=PE_INBOX
            ),
        ],
        [
            InlineKeyboardButton(
                text="+10 минут",
                callback_data="extend_email",
                style="success",
                icon_custom_emoji_id=PE_BULB
            ),
            InlineKeyboardButton(
                text="Время осталось",
                callback_data="time_left",
                style="success",
                icon_custom_emoji_id=PE_1M
            ),
        ],
        [
            InlineKeyboardButton(
                text="Удалить почту",
                callback_data="delete_email",
                style="success",
                icon_custom_emoji_id=PE_TRASH
            ),
            InlineKeyboardButton(
                text="Наш канал ↗",
                url=CHANNEL_URL,
                style="success",
                icon_custom_emoji_id=PE_ARROW
            ),
        ],
    ])


def welcome_caption(bot_username: str) -> str:
    return (
        f"{pe(PE_1M)} <b>{bot_username}</b>\n\n"
        f"{pe(PE_PERSON)} Временные почты за 5 секунд\n"
        f"{pe(PE_1M)} Живёт 10 минут, затем удаляется\n"
        f"{pe(PE_INBOX)} Письма приходят автоматически\n"
        f"{pe(PE_ARROW)} Полностью бесплатно\n\n"
        f"Нажми <b>Новая почта</b> чтобы начать:"
    )


# ─────────────────────── HANDLERS ───────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    if not user:
        await upsert_user(user_id)

    me = await bot.get_me()
    caption = welcome_caption(me.username or "TenMinEmailBot")
    kb = main_keyboard()

    try:
        sent = await message.answer_photo(
            photo=BANNER_PHOTO,
            caption=caption,
            reply_markup=kb
        )
    except Exception:
        sent = await message.answer(
            text=caption,
            reply_markup=kb
        )

    await _update_user(user_id, msg_id=sent.message_id)


async def _edit_main(user_id: int, chat_id: int, caption: str, kb: InlineKeyboardMarkup):
    user = await get_user(user_id)
    msg_id = user.get("msg_id") if user else 0
    if msg_id:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=msg_id,
                caption=caption,
                reply_markup=kb,
                parse_mode=ParseMode.HTML
            )
            return
        except Exception:
            pass
    # Если не вышло — шлём новое
    try:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=BANNER_PHOTO,
            caption=caption,
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
        await _update_user(user_id, msg_id=sent.message_id)
    except Exception:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
        await _update_user(user_id, msg_id=sent.message_id)


# ── Новая почта ──

@dp.callback_query(F.data == "new_email")
async def cb_new_email(call: CallbackQuery):
    user_id = call.from_user.id

    user = await get_user(user_id)
    now = datetime.now().timestamp()

    # Проверяем кулдаун
    last = (user.get("last_created", 0) or 0) if user else 0
    if now - last < NEW_EMAIL_COOLDOWN:
        wait = int(NEW_EMAIL_COOLDOWN - (now - last))
        # Сначала показываем алерт с таймером
        await call.answer(f"⏳ Подожди ещё {wait} сек.", show_alert=True)
        # Затем всегда переходим на экран текущей почты (из любого меню)
        if user and user.get("email"):
            expires_at = user.get("expires_at", 0) or 0
            expire_str = datetime.fromtimestamp(expires_at).strftime("%H:%M:%S") if expires_at else "—"
            left = max(0.0, expires_at - now)
            bar = progress_bar(left, EMAIL_LIFETIME)
            caption = (
                f"{pe(PE_ARROW)} <b>Временная почта</b>\n\n"
                f"{pe(PE_PERSON)} <b>Адрес:</b>\n"
                f"<code>{user['email']}</code>\n\n"
                f"{pe(PE_1M)} {bar}\n"
                f"Действует до: <b>{expire_str}</b>\n\n"
                f"{pe(PE_INBOX)} <i>Письма придут автоматически</i>\n\n"
                f"<i>Нажми на адрес чтобы скопировать</i>"
            )
            await _edit_main(user_id, call.message.chat.id, caption, main_keyboard())
        return

    await call.answer()
    await bot.send_chat_action(call.message.chat.id, ChatAction.TYPING)

    result = await create_email()
    if not result:
        await call.answer("❌ Ошибка создания почты. Попробуйте позже.", show_alert=True)
        return

    expires_at = now + EMAIL_LIFETIME
    created_at = now

    await upsert_user(
        user_id,
        email=result["address"],
        password=result["password"],
        token=result["token"],
        expires_at=expires_at,
        created_at=created_at,
        last_created=now
    )

    expire_str = datetime.fromtimestamp(expires_at).strftime("%H:%M:%S")
    bar = progress_bar(EMAIL_LIFETIME, EMAIL_LIFETIME)

    caption = (
        f"{pe(PE_ARROW)} <b>Временная почта создана!</b>\n\n"
        f"{pe(PE_PERSON)} <b>Адрес:</b>\n"
        f"<code>{result['address']}</code>\n\n"
        f"{pe(PE_1M)} {bar}\n"
        f"Действует до: <b>{expire_str}</b>\n\n"
        f"{pe(PE_INBOX)} <i>Письма придут автоматически</i>\n\n"
        f"<i>Нажми на адрес чтобы скопировать</i>"
    )

    await _edit_main(user_id, call.message.chat.id, caption, main_keyboard())


# ── Проверить письма ──

async def _show_inbox(user_id: int, chat_id: int, page: int = 0):
    """Общая логика отображения входящих (используется и при check, и при пагинации)."""
    user = await get_user(user_id)
    if not user or not user.get("email"):
        return False  # нет почты

    now = datetime.now().timestamp()
    if user.get("expires_at") and now > user["expires_at"]:
        await clear_email(user_id)
        return False

    # Берём из кеша или запрашиваем заново
    messages = inbox_cache.get(user_id, [])

    if not messages:
        caption = (
            f"{pe(PE_INBOX)} <b>Входящих нет.</b>\n"
            f"Письма придут автоматически."
        )
        await _edit_main(user_id, chat_id, caption, InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Назад",
                callback_data="back_to_main",
                icon_custom_emoji_id=PE_CROSS
            )
        ]]))
        return True

    total = len(messages)
    page = max(0, min(page, (total - 1) // INBOX_PER_PAGE))
    caption = (
        f"{pe(PE_INBOX)} <b>Входящие — {total} шт.</b>\n"
        f"Выбери письмо:"
    )
    await _edit_main(user_id, chat_id, caption, inbox_keyboard(messages, page))
    return True


@dp.callback_query(F.data == "check_email")
async def cb_check_email(call: CallbackQuery):
    user_id = call.from_user.id
    user = await get_user(user_id)

    if not user or not user.get("email"):
        await call.answer("📭 Сначала создайте почту!", show_alert=True)
        return

    now = datetime.now().timestamp()
    if user.get("expires_at") and now > user["expires_at"]:
        await clear_email(user_id)
        await call.answer("⌛ Почта истекла. Создайте новую.", show_alert=True)
        return

    await call.answer()
    await bot.send_chat_action(call.message.chat.id, ChatAction.TYPING)

    # Обновляем кеш при каждом нажатии кнопки
    try:
        inbox_cache[user_id] = await get_messages(user["token"])
    except Exception:
        inbox_cache[user_id] = []

    await _show_inbox(user_id, call.message.chat.id, page=0)


@dp.callback_query(F.data.startswith("inbox_page:"))
async def cb_inbox_page(call: CallbackQuery):
    await call.answer()
    user_id = call.from_user.id
    try:
        page = int(call.data.split(":")[1])
    except (IndexError, ValueError):
        page = 0
    await _show_inbox(user_id, call.message.chat.id, page=page)


@dp.callback_query(F.data.startswith("open_msg:"))
async def cb_open_msg(call: CallbackQuery):
    await call.answer()
    user_id = call.from_user.id
    msg_id = call.data.split(":", 1)[1]

    user = await get_user(user_id)
    if not user or not user.get("token"):
        await call.answer("❌ Нет активной почты", show_alert=True)
        return

    await bot.send_chat_action(call.message.chat.id, ChatAction.TYPING)

    # Ищем базовые данные в кеше
    cached = inbox_cache.get(user_id, [])
    base_msg = next((m for m in cached if m.get("id") == msg_id), None)

    # Запрашиваем полное содержимое
    try:
        full = await get_message_content(user["token"], msg_id)
    except Exception:
        full = None

    if not full and not base_msg:
        await call.answer("❌ Не удалось загрузить письмо", show_alert=True)
        return

    src = full or base_msg
    sender  = (src.get("from") or {}).get("address", "???")
    subject = src.get("subject", "(без темы)")
    user_email = user.get("email", "???")

    raw_date = src.get("createdAt", "")
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        date_str = dt.strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        date_str = raw_date or "—"

    body = ""
    if full:
        body = full.get("text", "") or ""
        if not body:
            html_body = full.get("html", [""])[0] if isinstance(full.get("html"), list) else full.get("html", "")
            body = re.sub(r"<[^>]+>", " ", html_body or "")
            body = re.sub(r"\s+", " ", body).strip()
    body = body.strip()[:3000] or "(тело письма пусто)"

    def escape_html(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body_escaped = escape_html(body)
    body_with_codes = highlight_codes(body_escaped)

    text = (
        f"{pe(PE_INBOX)} <b>Новое письмо!</b>\n\n"
        f"{pe(PE_PERSON)} <b>На почту:</b> <code>{user_email}</code>\n"
        f"{pe(PE_ARROW)} <b>От:</b> <code>{sender}</code>\n"
        f"{pe('5890937706803894250')} <b>Дата:</b> {date_str}\n"
        f"{pe('5870676941614354370')} <b>Тема:</b> {escape_html(subject)}\n\n"
        f"<blockquote expandable>{body_with_codes}</blockquote>"
    )

    # Кнопка "Назад в список"
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Назад",
            callback_data="check_email",
            icon_custom_emoji_id=PE_CROSS
        )
    ]])

    try:
        await bot.send_photo(
            chat_id=call.message.chat.id,
            photo=BANNER_PHOTO,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb
        )
    except Exception:
        await bot.send_message(
            chat_id=call.message.chat.id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb
        )


@dp.callback_query(F.data == "back_to_main")
async def cb_back_to_main(call: CallbackQuery):
    await call.answer()
    user_id = call.from_user.id
    user = await get_user(user_id)

    if not user or not user.get("email"):
        me = await bot.get_me()
        caption = welcome_caption(me.username or "TenMinEmailBot")
        await _edit_main(user_id, call.message.chat.id, caption, main_keyboard())
        return

    now = datetime.now().timestamp()
    expires_at = user.get("expires_at", 0) or 0
    expire_str = datetime.fromtimestamp(expires_at).strftime("%H:%M:%S") if expires_at else "—"
    left = max(0.0, expires_at - now)
    bar = progress_bar(left, EMAIL_LIFETIME)

    caption = (
        f"{pe(PE_ARROW)} <b>Временная почта</b>\n\n"
        f"{pe(PE_PERSON)} <b>Адрес:</b>\n"
        f"<code>{user['email']}</code>\n\n"
        f"{pe(PE_1M)} {bar}\n"
        f"Действует до: <b>{expire_str}</b>\n\n"
        f"{pe(PE_INBOX)} <i>Письма придут автоматически</i>\n\n"
        f"<i>Нажми на адрес чтобы скопировать</i>"
    )
    await _edit_main(user_id, call.message.chat.id, caption, main_keyboard())


# ── Время осталось ──

@dp.callback_query(F.data == "time_left")
async def cb_time_left(call: CallbackQuery):
    user_id = call.from_user.id
    await call.answer()

    user = await get_user(user_id)
    if not user or not user.get("email"):
        await call.answer("📭 Сначала создайте почту!", show_alert=True)
        return

    now = datetime.now().timestamp()
    expires_at = user.get("expires_at", 0) or 0
    left = expires_at - now

    if left <= 0:
        await clear_email(user_id)
        await call.answer("⌛ Почта уже истекла.", show_alert=True)
        return

    bar = progress_bar(left, EMAIL_LIFETIME)
    expire_str = datetime.fromtimestamp(expires_at).strftime("%H:%M:%S")
    mins = int(left) // 60
    secs = int(left) % 60

    caption = (
        f"{pe(PE_1M)} Осталось: {mins}:{secs:02d}\n\n"
        f"{bar}\n\n"
        f"{pe(PE_PERSON)} <code>{user['email']}</code>\n"
        f"Удалится в {expire_str}"
    )

    await _edit_main(user_id, call.message.chat.id, caption, main_keyboard())


# ── Удалить почту ──

@dp.callback_query(F.data == "delete_email")
async def cb_delete_email(call: CallbackQuery):
    user_id = call.from_user.id
    await call.answer()

    user = await get_user(user_id)
    if not user or not user.get("email"):
        await call.answer("📭 Нет активной почты.", show_alert=True)
        return

    await clear_email(user_id)

    caption = f"{pe(PE_TRASH)} Почта удалена"
    await _edit_main(user_id, call.message.chat.id, caption, main_keyboard())


# ── +10 минут (Stars) ──

@dp.callback_query(F.data == "extend_email")
async def cb_extend_email(call: CallbackQuery):
    user_id = call.from_user.id
    await call.answer()

    user = await get_user(user_id)
    if not user or not user.get("email"):
        await call.answer("📭 Сначала создайте почту!", show_alert=True)
        return

    now = datetime.now().timestamp()
    if user.get("expires_at") and now > user["expires_at"]:
        await clear_email(user_id)
        await call.answer("⌛ Почта уже истекла.", show_alert=True)
        return

    try:
        await bot.send_invoice(
            chat_id=call.message.chat.id,
            title=f"+{EXTEND_MINUTES} минут к почте",
            description=f"Продлить время жизни текущей почты на {EXTEND_MINUTES} минут.",
            payload="extend_10min",
            currency="XTR",
            prices=[LabeledPrice(label=f"+{EXTEND_MINUTES} минут", amount=EXTEND_STARS)],
        )
    except Exception as e:
        log.error(f"Invoice error: {e}")
        await call.answer("❌ Ошибка создания платежа", show_alert=True)


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    stars = message.successful_payment.total_amount

    # Уведомляем владельца о поступивших звёздах
    if OWNER_ID:
        try:
            await bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"{pe('5904462880941545555')} <b>Получены звёзды!</b>\n\n"
                    f"{pe(PE_PERSON)} От: <code>{user_id}</code>\n"
                    f"⭐ Звёзд: <b>{stars}</b>\n"
                    f"{pe('5870676941614354370')} Payload: <code>{payload}</code>"
                )
            )
        except Exception as notify_err:
            log.warning(f"Could not notify owner: {notify_err}")

    if payload == "extend_10min":
        user = await get_user(user_id)
        if user and user.get("email"):
            now = datetime.now().timestamp()
            expires = user.get("expires_at") or now
            new_expires = max(expires, now) + EXTEND_MINUTES * 60
            await _update_user(user_id, expires_at=new_expires)

            new_expire_str = datetime.fromtimestamp(new_expires).strftime("%H:%M:%S")
            left = new_expires - now
            bar = progress_bar(left, EMAIL_LIFETIME)

            caption = (
                f"{pe(PE_BULB)} <b>+{EXTEND_MINUTES} минут добавлено!</b>\n\n"
                f"{pe(PE_1M)} {bar}\n"
                f"Новое время: <b>{new_expire_str}</b>\n\n"
                f"{pe(PE_PERSON)} <code>{user['email']}</code>"
            )
            await _edit_main(user_id, message.chat.id, caption, main_keyboard())
        else:
            await message.answer("❌ Нет активной почты для продления.")


# ─────────────────────── AUTO-EXPIRY TASK ───────────────────────

async def expiry_task():
    while True:
        await asyncio.sleep(30)
        try:
            now = datetime.now().timestamp()
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT user_id FROM users WHERE email IS NOT NULL AND expires_at IS NOT NULL AND expires_at < ?",
                    (now,)
                ) as cur:
                    rows = await cur.fetchall()
            for row in rows:
                uid = row["user_id"]
                await clear_email(uid)
                log.info(f"Auto-expired email for user {uid}")
                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=(
                            "<blockquote>"
                            f"{pe('5983150113483134607')} <b>Время вышло! Почта удалена.</b>\n\n"
                            "Создай новую — /start"
                            "</blockquote>"
                        ),
                    )
                except Exception as notify_err:
                    log.warning(f"Could not notify user {uid}: {notify_err}")
        except Exception as e:
            log.error(f"Expiry task error: {e}")


# ─────────────────────── ADMIN PANEL ───────────────────────

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Рассылка",
                callback_data="admin_broadcast",
                style="primary",
                icon_custom_emoji_id="6039422865189638057"  # рупор
            ),
            InlineKeyboardButton(
                text="Статистика",
                callback_data="admin_stats",
                style="primary",
                icon_custom_emoji_id="5870921681735781843"  # график
            ),
        ],
    ])


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    total = await count_users()
    active = await count_active_emails()
    await message.answer(
        f"{pe('5870982283724328568')} <b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"{pe('6082441824562061129')} Активных почт: <b>{active}</b>",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.answer()
    total = await count_users()
    active = await count_active_emails()
    await call.message.edit_text(
        f"{pe('5870982283724328568')} <b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"{pe('6082441824562061129')} Активных почт: <b>{active}</b>",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.answer()
    broadcast_pending[call.from_user.id] = True
    await call.message.answer(
        f"{pe('6039422865189638057')} <b>Рассылка</b>\n\n"
        "Отправь сообщение для рассылки.\n"
        "Поддерживается: текст (любое форматирование), фото, видео, стикер, "
        "аудио, документ, голосовое, анимация, премиум эмодзи — всё что угодно.\n\n"
        "<i>Для отмены напиши /cancel</i>"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if message.from_user.id in broadcast_pending:
        broadcast_pending.pop(message.from_user.id)
        await message.answer(f"{pe('5870657884844462243')} Рассылка отменена.")


@dp.message(F.from_user.func(lambda u: u.id in broadcast_pending))
async def handle_broadcast_message(message: Message):
    admin_id = message.from_user.id
    if admin_id not in broadcast_pending:
        return
    broadcast_pending.pop(admin_id)

    user_ids = await get_all_user_ids()
    ok = 0
    fail = 0

    status_msg = await message.answer(
        f"{pe('5345906554510012647')} Запускаю рассылку на <b>{len(user_ids)}</b> пользователей..."
    )

    for uid in user_ids:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # защита от флуд-лимита

    await status_msg.edit_text(
        f"{pe('5870633910337015697')} <b>Рассылка завершена!</b>\n\n"
        f"✅ Доставлено: <b>{ok}</b>\n"
        f"❌ Ошибок: <b>{fail}</b>"
    )


# ─────────────────────── MAIL POLL TASK (авто-получение писем) ───────────────────────

async def mail_poll_task():
    """Каждые 15 секунд проверяет новые письма для всех активных почт и пушит их пользователям."""
    while True:
        await asyncio.sleep(15)
        try:
            now = datetime.now().timestamp()
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT user_id, email, token, expires_at FROM users "
                    "WHERE email IS NOT NULL AND token IS NOT NULL AND expires_at > ?",
                    (now,)
                ) as cur:
                    rows = await cur.fetchall()

            for row in rows:
                uid        = row["user_id"]
                user_email = row["email"]
                token      = row["token"]

                try:
                    messages = await get_messages(token)
                except Exception:
                    continue

                if uid not in seen_messages:
                    seen_messages[uid] = set()

                for msg in messages:
                    msg_id = msg.get("id")
                    if not msg_id or msg_id in seen_messages[uid]:
                        continue

                    seen_messages[uid].add(msg_id)

                    # Получаем полное содержимое письма
                    try:
                        full = await get_message_content(token, msg_id)
                    except Exception:
                        full = None

                    sender  = msg.get("from", {}).get("address", "???")
                    subject = msg.get("subject", "(без темы)")

                    # Дата письма
                    raw_date = msg.get("createdAt", "")
                    try:
                        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                        date_str = dt.strftime("%d.%m.%Y %H:%M:%S")
                    except Exception:
                        date_str = raw_date or "—"

                    # Тело письма — берём text/plain
                    body = ""
                    if full:
                        body = full.get("text", "") or ""
                        # Если text пустой — пробуем html без тегов
                        if not body:
                            html_body = full.get("html", [""])[0] if isinstance(full.get("html"), list) else full.get("html", "")
                            body = re.sub(r"<[^>]+>", " ", html_body or "")
                            body = re.sub(r"\s+", " ", body).strip()

                    body = body.strip()[:3000] or "(тело письма пусто)"

                    # Экранируем HTML-спецсимволы в теле (кроме наших <code>)
                    def escape_html(s: str) -> str:
                        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

                    body_escaped = escape_html(body)
                    body_with_codes = highlight_codes(body_escaped)

                    text = (
                        f"{pe(PE_INBOX)} <b>Новое письмо!</b>\n\n"
                        f"{pe(PE_PERSON)} <b>На почту:</b> <code>{user_email}</code>\n"
                        f"{pe(PE_ARROW)} <b>От:</b> <code>{sender}</code>\n"
                        f"{pe('5890937706803894250')} <b>Дата:</b> {date_str}\n"
                        f"{pe('5870676941614354370')} <b>Тема:</b> {escape_html(subject)}\n\n"
                        f"<blockquote expandable>{body_with_codes}</blockquote>"
                    )

                    try:
                        await bot.send_photo(
                            chat_id=uid,
                            photo=BANNER_PHOTO,
                            caption=text,
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        # Если фото не работает — шлём без него
                        try:
                            await bot.send_message(
                                chat_id=uid,
                                text=text,
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            log.warning(f"Could not deliver mail to user {uid}: {e}")

        except Exception as e:
            log.error(f"Mail poll task error: {e}")


# ─────────────────────── STARTUP ───────────────────────

async def main():
    await init_db()
    asyncio.create_task(expiry_task())
    asyncio.create_task(mail_poll_task())
    log.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
