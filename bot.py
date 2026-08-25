"""
Telegram админ-бот: модерация, варны, фильтры слов, логи, настройки.
Стек: aiogram 3.x + aiosqlite. Вся логика в одном файле.

Каждый чат настраивается через /settings (варны, фильтры, приветствия,
антифлуд, язык интерфейса), каждый юзер — через /me (свой язык,
ЛС-уведомления о наказаниях).

Запуск:
    copy .env.example .env   (заполнить BOT_TOKEN)
    pip install -r requirements.txt
    python bot.py
"""

import asyncio
import html
import logging
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

import aiosqlite
from dotenv import load_dotenv

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramMigrateToChat
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ─────────────────────────── Конфигурация ───────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID") or 0)
WARN_LIMIT = int(os.getenv("WARN_LIMIT") or 3)
ADMINS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("ADMINS", "").strip()) if x.isdigit()
}
DEVELOPERS = {
    int(x)
    for x in re.split(r"[,\s]+", os.getenv("DEVELOPER_ID", "").strip())
    if x.isdigit()
}

DB_PATH = "bot.db"

GROUP_CHATS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})

DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}

# @юзернейм или ссылка t.me/юзернейм
USERNAME_RE = re.compile(r"^(?:@|(?:https?://)?t\.me/)([A-Za-z0-9_]{4,32})$")

FULL_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            os.path.join(LOG_DIR, "bot.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("adminbot")

router = Router()
bot: Bot | None = None
db: aiosqlite.Connection | None = None
log_channel_ok = True  # гасим спам, если лог-канал недоступен

# ────────────────────────────── БД ──────────────────────────────────

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS warns (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    by_id      INTEGER NOT NULL DEFAULT 0,
    reason     TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_warns_chat_user ON warns(chat_id, user_id);

CREATE TABLE IF NOT EXISTS punishments (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    type    TEXT    NOT NULL,          -- 'ban' | 'mute' | 'auto_mute'
    until   REAL    NOT NULL           -- unix ts истечения
);
CREATE INDEX IF NOT EXISTS idx_punishments_until ON punishments(until);

CREATE TABLE IF NOT EXISTS filters (
    chat_id INTEGER NOT NULL,
    word    TEXT    NOT NULL,
    UNIQUE(chat_id, word)
);

-- Кэш участников: чтобы находить user_id по @юзернейму
CREATE TABLE IF NOT EXISTS users (
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    username   TEXT,
    full_name  TEXT,
    last_seen  INTEGER,
    first_seen INTEGER NOT NULL DEFAULT 0,
    messages   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_users_chat_username ON users(chat_id, username);

-- Правила чата (задаются админом: /rules set)
CREATE TABLE IF NOT EXISTS rules (
    chat_id INTEGER PRIMARY KEY,
    text    TEXT NOT NULL DEFAULT ''
);

-- Переезды чатов (группа → супергруппа меняет chat_id)
CREATE TABLE IF NOT EXISTS chat_migrations (
    old_id INTEGER PRIMARY KEY,
    new_id INTEGER NOT NULL
);

-- Кастомные роли чата
CREATE TABLE IF NOT EXISTS roles_def (
    chat_id INTEGER NOT NULL,
    name    TEXT NOT NULL,
    perms   TEXT NOT NULL DEFAULT '',   -- флаги через запятую
    rank    INTEGER NOT NULL DEFAULT 0, -- иерархия: выше = старше
    UNIQUE(chat_id, name)
);
-- Назначенные роли (одна на юзера в чате)
CREATE TABLE IF NOT EXISTS user_roles (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role    TEXT NOT NULL,
    UNIQUE(chat_id, user_id)
);
-- Приватный лог-канал чата
CREATE TABLE IF NOT EXISTS log_channels (
    chat_id    INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL
);

-- Настройки чата и юзера (key-value поверх дефолтов)
CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    UNIQUE(chat_id, key)
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    UNIQUE(user_id, key)
);
"""


async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(DB_SCHEMA)

    # Миграция старых баз: добавляем новые колонки users, если их нет.
    cur = await db.execute("PRAGMA table_info(users)")
    cols = {r["name"] for r in await cur.fetchall()}
    for col_sql in ("first_seen INTEGER NOT NULL DEFAULT 0",
                    "messages INTEGER NOT NULL DEFAULT 0"):
        col = col_sql.split()[0]
        if col not in cols:
            await db.execute(f"ALTER TABLE users ADD COLUMN {col_sql}")

    # Миграция ролей: системный админ получает новый флаг 'settings'.
    await db.execute(
        "UPDATE roles_def SET perms = perms || ',settings'"
        " WHERE name='admin' AND instr(','||perms||',', ',settings')=0"
    )

    await db.commit()


async def resolve_chat_id(chat_id: int) -> int:
    """Актуальный id чата с учётом миграций группа→супергруппа."""
    cur = await db.execute(
        "SELECT new_id FROM chat_migrations WHERE old_id=?", (chat_id,)
    )
    row = await cur.fetchone()
    return row["new_id"] if row else chat_id


async def remap_chat(old_id: int, new_id: int) -> None:
    """Чат мигрировал в супергруппу — переносим все данные на новый id."""
    tables = ("warns", "punishments", "filters", "users", "rules",
              "roles_def", "user_roles", "log_channels", "chat_settings")
    sets = ";".join(
        f"UPDATE {t} SET chat_id={int(new_id)} WHERE chat_id={int(old_id)}"
        for t in tables
    )
    await db.executescript(sets)
    await db.execute(
        "INSERT OR REPLACE INTO chat_migrations(old_id,new_id) VALUES(?,?)",
        (int(old_id), int(new_id)),
    )
    await db.commit()
    _chat_cache.pop(old_id, None)
    _chat_cache.pop(new_id, None)
    log.info("Чат %s переехал в супергруппу %s, данные перенесены", old_id, new_id)


# ─────────────────────── Настройки (chat/user) ──────────────────────

CHAT_DEFAULTS = {
    "lang": "ru",                    # ru | en
    "warn_limit": str(WARN_LIMIT),   # 1..10
    "warn_action": "mute",           # mute | ban | kick | reset
    "warn_mute_hours": "24",         # длительность мута за варны, часы
    "filter_action": "delete_warn",  # delete_warn | delete_mute | delete_only
    "welcome_on": "0",
    "welcome_text": "👋 Добро пожаловать, {name}!",
    "flood_on": "0",
    "flood_msgs": "5",               # порог сообщений
    "flood_secs": "10",              # окно, секунд
    "flood_action": "mute",          # mute | kick
    "flood_mute_min": "30",          # длительность мута за флуд, минуты
}
USER_DEFAULTS = {
    "lang": "",                      # '' = как в чате
    "notify": "on",                  # ЛС-уведомления о наказаниях
}

WARN_MUTE_HOURS = [1, 12, 24, 72, 168]
FLOOD_MUTE_MIN = [5, 15, 30, 60, 120]
WARN_ACTIONS = ["mute", "ban", "kick", "reset"]
FLOOD_ACTIONS = ["mute", "kick"]

_chat_cache: dict[int, dict[str, str]] = {}
_user_cache: dict[int, dict[str, str]] = {}

# Ожидание текста от юзера: user_id -> (expire_ts, chat_id, kind)
_text_wait: dict[int, tuple[float, int, str]] = {}


async def load_chat_settings(chat_id: int) -> dict[str, str]:
    cur = await db.execute(
        "SELECT key, value FROM chat_settings WHERE chat_id=?", (chat_id,)
    )
    data = dict(CHAT_DEFAULTS)
    data.update({r["key"]: r["value"] for r in await cur.fetchall()})
    _chat_cache[chat_id] = data
    return data


async def get_chat_setting(chat_id: int, key: str) -> str:
    if chat_id not in _chat_cache:
        await load_chat_settings(chat_id)
    return _chat_cache[chat_id].get(key, CHAT_DEFAULTS.get(key, ""))


async def set_chat_setting(chat_id: int, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO chat_settings(chat_id,key,value) VALUES(?,?,?)"
        " ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value",
        (chat_id, key, str(value)),
    )
    await db.commit()
    if chat_id in _chat_cache:
        _chat_cache[chat_id][key] = str(value)


async def chat_int_setting(chat_id: int, key: str, default: int = 0) -> int:
    try:
        return int(await get_chat_setting(chat_id, key))
    except (TypeError, ValueError):
        return default


async def get_user_setting(user_id: int, key: str) -> str:
    if user_id not in _user_cache:
        cur = await db.execute(
            "SELECT key, value FROM user_settings WHERE user_id=?", (user_id,)
        )
        data = dict(USER_DEFAULTS)
        data.update({r["key"]: r["value"] for r in await cur.fetchall()})
        _user_cache[user_id] = data
    return _user_cache[user_id].get(key, USER_DEFAULTS.get(key, ""))


async def set_user_setting(user_id: int, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?)"
        " ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
        (user_id, key, str(value)),
    )
    await db.commit()
    if user_id in _user_cache:
        _user_cache[user_id][key] = str(value)


# ────────────────────────────── i18n ────────────────────────────────


class _SafeDict(dict):
    """format_map, оставляющий неизвестные плейсхолдеры как есть."""

    def __missing__(self, key):
        return "{" + key + "}"


STR: dict[str, dict[str, str]] = {"ru": {}, "en": {}}

STR["ru"] = {
    # кнопки меню/панелей
    "b_profile": "👤 Профиль", "b_rules": "📜 Правила",
    "b_roles": "🔵 Роли чата", "b_help": "🛡 Команды",
    "b_settings": "⚙️ Настройки", "b_info": "ℹ️ Информация",
    "b_close": "✖️ Закрыть", "b_back": "◀️ Назад",
    "b_on": "Включить", "b_off": "Выключить",
    "b_edit": "✏️ Изменить текст", "b_test": "🧪 Тест",
    # меню
    "menu_title": "🤖 <b>RADMIRKA HELPER</b>", "menu_pick": "Выбери раздел 👇",
    "roles_locked": "🔒 Управление ролями доступно только админам чата.",
    # заголовки экранов
    "h_profile": "👤 ПРОФИЛЬ", "h_rules": "📜 ПРАВИЛА", "h_info": "ℹ️ ИНФО",
    "h_roles": "🔵 РОЛИ ЧАТА", "h_assign": "🔵 ВЫДАЧА РОЛИ",
    "ht1": "🛡 МОДЕРАЦИЯ · 1/4", "ht2": "🔵 РОЛИ · 2/4",
    "ht3": "💬 ЧАТ · 3/4", "ht4": "⚙️ НАСТРОЙКИ · 4/4",
    # инфо
    "info_body": (
        "🤖 <b>Radmirka Helper</b> — бот-модератор.\n"
        "Варны, баны, муты, роли, фильтры слов, логи.\n\n"
        "<b>Возможности:</b>\n"
        "• Наказания по реплаю/ID/@юзернейму\n"
        "• Кастомные роли с правами и рангами\n"
        "• Авто-мут и авто-бан за варны\n"
        "• Фильтр запрещённых слов\n"
        "• Настройки под каждый чат (/settings)\n"
        "• Личные настройки (/me)\n"
        "• Логи всех наказаний в отдельный канал\n\n"
        "<i>Примеры:</i>\n"
        "<code>/ban @flaresec 30 спам</code>\n"
        "<code>/mute 15 флуд</code>\n"
        "<code>/ban 2d</code> (реплаем)"
    ),
    # профиль
    "p_name": "👤 Имя:", "p_user": "🔗 Юзернейм:", "p_none": "—",
    "p_id": "🆔 ID:", "p_status": "📌 Статус:",
    "p_dev": "👨‍💻 Разработчик бота", "p_glob": "🌐 Глобальный админ бота",
    "st_owner": "👑 Владелец чата", "st_admin": "🛡 Администратор",
    "st_member": "👤 Участник", "st_left": "🚪 Не в чате",
    "st_banned": "⛔️ Забанен", "st_muted": "🔇 В муте до {when}",
    "p_role": "🔵 Роль:", "p_no": "нет",
    "p_warns": "⚠️ Варны:", "p_last": "последний: «{r}»",
    "p_msgs": "💬 Сообщений:", "p_since": "📅 В чате с:",
    # наказания — ответы
    "r_ban": "⛔️ Бан на {dur}: ", "r_banp": "⛔️ Бан: ",
    "r_mute": "🔇 Мут на {dur}: ", "r_mutep": "🔇 Мут (бессрочно): ",
    "r_kick": "👢 Кик: ", "r_reason": "\nПричина: ",
    "r_unban": "✅ Разбанен: ", "r_unmute": "✅ Размучен: ",
    "e_ban": "Не удалось забанить: ", "e_mute": "Не удалось замутить: ",
    "e_kick": "Не удалось кикнуть: ", "e_unban": "Не удалось разбанить: ",
    "e_unmute": "Не удалось размутить: ",
    # ошибки цели
    "t_none": ("Укажи цель: реплаем на сообщение, числовым ID либо @юзернеймом "
               "того, кто писал в этом чате."),
    "t_bot": "Ботов наказывать нельзя.", "t_self": "Нельзя наказать самого себя.",
    "t_admin": "Нельзя наказать администратора.",
    "t_dev": "👨‍💻 Разработчика наказать нельзя.",
    "t_higher": "⛔️ Цель выше тебя по ролевой иерархии.",
    "t_rank": "ранг {n}", "t_noperms": "нет прав",
    # варны
    "w_head": "⚠️ <b>ВАРН</b> · {n}/{total}\n👤 ", "w_reason": "\n📝 Причина: ",
    "lim_mute": "\n🔇 Лимит варнов — мут на {dur}.",
    "lim_ban": "\n🔨 Лимит превышен — пожизненный бан.",
    "lim_kick": "\n👢 Лимит превышен — кик.",
    "lim_reset": "\n♻️ Лимит достигнут — варны сброшены.",
    "w_fail": "\nНе удалось применить наказание: ",
    "wl_head": "Варны {link}: {n}/{total}", "wl_item": "{i}. {r} — {when}",
    "w_gone": "У {link} нет варнов.",
    "w_removed": "✅ Снят последний варн {link} ({n}/{total})",
    # фильтры
    "fa_ok": "✅ Добавлено в фильтр: «{w}»", "fa_fmt": "Формат: /filteradd слово",
    "fd_ok": "✅ Удалено из фильтра: «{w}»",
    "fd_miss": "Такого слова нет в фильтре: «{w}»",
    "fl_head": "Чёрный список:\n",
    "fl_empty": "Фильтры пусты. Добавь: /filteradd слово",
    # правила
    "ru_saved": "✅ Правила чата сохранены ({n} симв.)",
    "ru_cleared": "✅ Правила чата удалены.",
    "ru_hint": "Формат: /rules set текст правил (можно многострочно)",
    "re_body": "Правила пока не заданы 🤷\nАдмин может установить их командой:",
    "re_cmd": "/rules set текст правил",
    # роли
    "ro_now": "👤 {u}\n🆔 <code>{id}</code>\n📌 Сейчас: <b>{cur}</b>",
    "ro_hint": "Нажми на роль — применится сразу 👇",
    "ro_devskip": "👨‍💻 У разработчика особая роль — выше всех.",
    "ro_notfound": "Роль не найдена. Формат: /roleperms имя\nСписок: /rolelist",
    "rl_empty": "Ролей нет.",
    "rl_hint": "Выдать: /role @юзер · Права: /roleperms имя",
    "ra_usage": ("Формат: /roleadd &lt;имя&gt; [флаги через запятую] [ранг]\n"
                 "Пример: /roleadd helper mute,kick,warn 40\nФлаги: {flags}"),
    "ra_ok": "✅ Роль «{name}»: ранг {rank}, права: {flags}",
    "rd_ok": "✅ Роль «{name}» удалена вместе с выдачами.",
    "rd_miss": "Такой роли нет: «{name}».",
    "rp_title": "<b>⚙️ Роль «{name}»</b> · ранг {rank}",
    "rp_hint": "Кликни по праву, чтобы вкл/выкл:",
    "rr_done": "Готово: {role}", "rr_off": "Готово: роль снята",
    "no_perms_cb": "Недостаточно прав",
    "role_gone": "Эта роль была удалена", "role_del_gone": "Роль удалена",
    # лог-канал
    "lg_cur": "📌 Лог-канал этого чата: <code>{id}</code>\nОтключить: /logchannel off",
    "lg_none": ("📌 Свой лог-канал не задан — логи идут в общий канал бота.\n"
                "Привязать свой: /logchannel &lt;ID канала&gt;"),
    "lg_offok": "✅ Приватный лог-канал отключён.",
    "lg_bad": ("Не удалось написать в канал ({e}).\n"
               "Добавь бота админом в канал и проверь ID."),
    "lg_bound": "✅ Лог-канал привязан: <code>{id}</code>",
    "lg_fmt": "Формат: /logchannel <-100...ID канала> | off",
    # настройки чата
    "cf_title": "⚙️ НАСТРОЙКИ ЧАТА", "cf_pick": "Выбери раздел 👇",
    "cfg_den": "⚙️ Настраивать бота могут только админы чата.",
    "cw_t": "⚠️ ВАРНЫ",
    "cw_limit": "Лимит варнов: {n}", "cw_act": "Действие при лимите:",
    "cw_hours": "Длительность мута:",
    "u_hr": "{n} ч", "u_min": "{n} мин",
    "act_mute": "🔇 Мут", "act_ban": "🔨 Бан", "act_kick": "👢 Кик",
    "act_reset": "♻️ Только сброс",
    "cf2_t": "🚫 ФИЛЬТРЫ СЛОВ",
    "cf2_hint": "Что делать с сообщением при срабатывании:",
    "fo_dw": "🗑 Удалить + варн", "fo_dm": "🗑 Удалить + мут",
    "fo_do": "🗑 Просто удалить",
    "cg_t": "👋 ПРИВЕТСТВИЕ НОВИЧКОВ",
    "g_state": "Сейчас:", "g_text_cur": "Текст:\n",
    "cg_ph": "Плейсхолдеры: {name}, {chat}, {id}",
    "prompt_wel": ("Пришли новым сообщением текст приветствия.\n"
                   "Плейсхолдеры: {name}, {chat}, {id}.\n"
                   "/cancel — отмена."),
    "ok_wel": "✅ Текст приветствия сохранён.",
    "cncl": "Отменено.",
    "too_long": "Слишком длинный текст (макс. 1000 символов). Попробуй короче.",
    "test_sent": "✅ Тестовое приветствие отправлено в чат.",
    "cl_t": "🌊 АНТИФЛУД",
    "fd_msgs": "Порог: {n} сообщений", "fd_secs": "Окно: {n} сек",
    "fd_act": "Действие:", "fda_mute": "🔇 Мут", "fda_kick": "👢 Кик",
    "fd_dur": "Длительность мута:",
    "cla_t": "🌐 ЯЗЫК ЧАТА",
    "lang_ru": "🇷🇺 Русский", "lang_en": "🇬🇧 English",
    "fm_muted": "🌊 Флуд ({n} сообщений за {s} сек) — мут на {dur}.",
    "fm_kicked": "🌊 Флуд ({n} сообщений за {s} сек) — кик.",
    "st_on": "вкл", "st_off": "выкл",
    # личные настройки
    "me_t": "⚙️ ЛИЧНЫЕ НАСТРОЙКИ",
    "me_lang": "🌐 Язык интерфейса:", "me_auto": "Авто (как в чате)",
    "me_notif": "📬 Уведомления в ЛС о моих наказаниях:",
    "no_on": "Вкл", "no_off": "Выкл",
    "me_hint": ("Настройки действуют во всех чатах с ботом.\n"
                "Язык «авто» берётся из настроек чата (/settings)."),
    # ЛС-уведомления о наказаниях
    "dm_warn": "⚠️ Тебе выдан варн в чате «{chat}» ({n}/{total})\n📝 Причина: {reason}",
    "dm_mute_t": "🔇 Тебе выдан мут в чате «{chat}» на {dur}.",
    "dm_mute_p": "🔇 Тебе выдан мут в чате «{chat}» (бессрочно).",
    "dm_ban_t": "⛔️ Тебя забанили в чате «{chat}» на {dur}.",
    "dm_ban_p": "⛔️ Тебя забанили в чате «{chat}».",
    # хелп
    "hb1": ("<b>Наказания</b> (реплай / ID / @юзернейм):\n\n"
            "/ban &lt;цель&gt; [время] [причина] — бан\n"
            "/mute &lt;цель&gt; [время] [причина] — мут\n"
            "/kick [причина] — кикнуть\n"
            "/unban · /unmute — снять наказание\n\n"
            "<b>Варны:</b>\n"
            "/warn [причина] — выдать варн\n"
            "/warns — варны нарушителя\n"
            "/unwarn — снять последний\n\n"
            "⏱ Время: число = минуты, можно 2h / 7d / 1w\n"
            "⚖️ Этот чат: {limit} варна → {action}"),
    "hb2": ("<b>Кастомные роли</b> (управление — админы):\n\n"
            "/role &lt;цель&gt; — выдать роль кнопками\n"
            "/roleadd &lt;имя&gt; [флаги] [ранг]\n"
            "/roledel &lt;имя&gt; — удалить роль\n"
            "/roleperms &lt;имя&gt; — права кнопками\n"
            "/rolelist — список ролей\n\n"
            "<b>Флаги прав:</b>\n"
            "mute · unmute · kick · ban · warn · filter · rules · "
            "logchannel · settings\n\n"
            "🪜 Иерархия по рангам: нельзя наказать того,\n"
            "у кого роль старше твоей.\n"
            "Ранг 800+ — только системные роли."),
    "hb3": ("<b>Правила:</b>\n"
            "/rules — посмотреть\n"
            "/rules set &lt;текст&gt; — задать\n"
            "/rules clear — удалить\n\n"
            "<b>Фильтры слов:</b>\n"
            "/filteradd слово — добавить\n"
            "/filterdel слово — удалить\n"
            "/filterlist — список\n\n"
            "<b>Логи:</b>\n"
            "/logchannel &lt;ID&gt; — привязать канал\n"
            "/logchannel off · без аргумента — текущий\n\n"
            "🏠 /menu — главное меню"),
    "hb4": ("Каждый чат настраивается отдельно — команда <code>/settings</code> "
            "(админы или роль с флагом <code>settings</code>):\n\n"
            "• <b>Варны</b> — лимит, действие при лимите, срок мута\n"
            "• <b>Фильтры слов</b> — что делать при срабатывании\n"
            "• <b>Приветствие</b> новичков со своим текстом\n"
            "• <b>Антифлуд</b> — порог, окно и наказание\n"
            "• <b>Язык чата</b> — русский / English\n\n"
            "Личные настройки — <code>/me</code>:\n"
            "• свой язык интерфейса (авто/ru/en)\n"
            "• уведомления в ЛС о наказаниях"),
    "unk": "❓ Неизвестная команда. Список: /help",
}
STR["en"] = {
    "b_profile": "👤 Profile", "b_rules": "📜 Rules",
    "b_roles": "🔵 Chat roles", "b_help": "🛡 Commands",
    "b_settings": "⚙️ Settings", "b_info": "ℹ️ Info",
    "b_close": "✖️ Close", "b_back": "◀️ Back",
    "b_on": "Enable", "b_off": "Disable",
    "b_edit": "✏️ Edit text", "b_test": "🧪 Test",
    "menu_title": "🤖 <b>RADMIRKA HELPER</b>", "menu_pick": "Pick a section 👇",
    "roles_locked": "🔒 Role management is available to chat admins only.",
    "h_profile": "👤 PROFILE", "h_rules": "📜 RULES", "h_info": "ℹ️ INFO",
    "h_roles": "🔵 CHAT ROLES", "h_assign": "🔵 ASSIGN ROLE",
    "ht1": "🛡 MODERATION · 1/4", "ht2": "🔵 ROLES · 2/4",
    "ht3": "💬 CHAT · 3/4", "ht4": "⚙️ SETTINGS · 4/4",
    "info_body": (
        "🤖 <b>Radmirka Helper</b> — a moderation bot.\n"
        "Warns, bans, mutes, roles, word filters, logs.\n\n"
        "<b>Features:</b>\n"
        "• Punish by reply/ID/@username\n"
        "• Custom roles with perms and ranks\n"
        "• Auto-mute and auto-ban for warns\n"
        "• Banned-word filters\n"
        "• Per-chat configuration (/settings)\n"
        "• Personal settings (/me)\n"
        "• All punishments logged to a channel\n\n"
        "<i>Examples:</i>\n"
        "<code>/ban @flaresec 30 spam</code>\n"
        "<code>/mute 15 flood</code>\n"
        "<code>/ban 2d</code> (as a reply)"
    ),
    "p_name": "👤 Name:", "p_user": "🔗 Username:", "p_none": "—",
    "p_id": "🆔 ID:", "p_status": "📌 Status:",
    "p_dev": "👨‍💻 Bot developer", "p_glob": "🌐 Global bot admin",
    "st_owner": "👑 Chat owner", "st_admin": "🛡 Administrator",
    "st_member": "👤 Member", "st_left": "🚪 Not in chat",
    "st_banned": "⛔️ Banned", "st_muted": "🔇 Muted until {when}",
    "p_role": "🔵 Role:", "p_no": "none",
    "p_warns": "⚠️ Warns:", "p_last": "last: “{r}”",
    "p_msgs": "💬 Messages:", "p_since": "📅 In chat since:",
    "r_ban": "⛔️ Banned for {dur}: ", "r_banp": "⛔️ Banned: ",
    "r_mute": "🔇 Muted for {dur}: ", "r_mutep": "🔇 Muted (permanent): ",
    "r_kick": "👢 Kicked: ", "r_reason": "\nReason: ",
    "r_unban": "✅ Unbanned: ", "r_unmute": "✅ Unmuted: ",
    "e_ban": "Failed to ban: ", "e_mute": "Failed to mute: ",
    "e_kick": "Failed to kick: ", "e_unban": "Failed to unban: ",
    "e_unmute": "Failed to unmute: ",
    "t_none": ("Specify a target: reply to a message, numeric ID or @username "
               "of someone who has chatted here."),
    "t_bot": "Bots cannot be punished.", "t_self": "You cannot punish yourself.",
    "t_admin": "Administrators cannot be punished.",
    "t_dev": "👨‍💻 The developer cannot be punished.",
    "t_higher": "⛔️ Target outranks you.",
    "t_rank": "rank {n}", "t_noperms": "no perms",
    "w_head": "⚠️ <b>WARN</b> · {n}/{total}\n👤 ", "w_reason": "\n📝 Reason: ",
    "lim_mute": "\n🔇 Warn limit reached — muted for {dur}.",
    "lim_ban": "\n🔨 Warn limit exceeded — permanent ban.",
    "lim_kick": "\n👢 Warn limit exceeded — kicked.",
    "lim_reset": "\n♻️ Limit reached — warns were reset.",
    "w_fail": "\nPunishment failed: ",
    "wl_head": "{link}'s warns: {n}/{total}", "wl_item": "{i}. {r} — {when}",
    "w_gone": "{link} has no warns.",
    "w_removed": "✅ Removed the last warn of {link} ({n}/{total})",
    "fa_ok": "✅ Added to filter: “{w}”", "fa_fmt": "Usage: /filteradd word",
    "fd_ok": "✅ Removed from filter: “{w}”",
    "fd_miss": "Not in the filter: “{w}”",
    "fl_head": "Blacklist:\n",
    "fl_empty": "Filter is empty. Add one: /filteradd word",
    "ru_saved": "✅ Chat rules saved ({n} chars)",
    "ru_cleared": "✅ Chat rules deleted.",
    "ru_hint": "Usage: /rules set your rules text (multiline ok)",
    "re_body": "No rules yet 🤷\nAn admin can set them with:",
    "re_cmd": "/rules set rules text",
    "ro_now": "👤 {u}\n🆔 <code>{id}</code>\n📌 Current: <b>{cur}</b>",
    "ro_hint": "Tap a role to apply it instantly 👇",
    "ro_devskip": "👨‍💻 A developer holds a special role — above everyone.",
    "ro_notfound": "Role not found. Usage: /roleperms name\nList: /rolelist",
    "rl_empty": "No roles.",
    "rl_hint": "Assign: /role @user · Perms: /roleperms name",
    "ra_usage": ("Usage: /roleadd &lt;name&gt; [comma-separated flags] [rank]\n"
                 "Example: /roleadd helper mute,kick,warn 40\nFlags: {flags}"),
    "ra_ok": "✅ Role “{name}”: rank {rank}, perms: {flags}",
    "rd_ok": "✅ Role “{name}” deleted along with its assignments.",
    "rd_miss": "No such role: “{name}”.",
    "rp_title": "<b>⚙️ Role “{name}”</b> · rank {rank}",
    "rp_hint": "Tap a permission to toggle it:",
    "rr_done": "Done: {role}", "rr_off": "Done: role removed",
    "no_perms_cb": "Not enough permissions",
    "role_gone": "This role was deleted", "role_del_gone": "Role deleted",
    "lg_cur": "📌 This chat's log channel: <code>{id}</code>\nDisable: /logchannel off",
    "lg_none": ("📌 No custom log channel — logs go to the bot's global channel.\n"
                "Bind yours: /logchannel &lt;channel ID&gt;"),
    "lg_offok": "✅ Private log channel disabled.",
    "lg_bad": ("Could not post to the channel ({e}).\n"
               "Add the bot as an admin there and check the ID."),
    "lg_bound": "✅ Log channel bound: <code>{id}</code>",
    "lg_fmt": "Usage: /logchannel <-100...channel ID> | off",
    "cf_title": "⚙️ CHAT SETTINGS", "cf_pick": "Pick a section 👇",
    "cfg_den": "⚙️ Only chat admins can configure the bot.",
    "cw_t": "⚠️ WARNS",
    "cw_limit": "Warn limit: {n}", "cw_act": "Action at the limit:",
    "cw_hours": "Mute duration:",
    "u_hr": "{n} h", "u_min": "{n} min",
    "act_mute": "🔇 Mute", "act_ban": "🔨 Ban", "act_kick": "👢 Kick",
    "act_reset": "♻️ Reset only",
    "cf2_t": "🚫 WORD FILTERS",
    "cf2_hint": "What happens when a filter trips:",
    "fo_dw": "🗑 Delete + warn", "fo_dm": "🗑 Delete + mute",
    "fo_do": "🗑 Delete only",
    "cg_t": "👋 NEW MEMBER WELCOME",
    "g_state": "Currently:", "g_text_cur": "Text:\n",
    "cg_ph": "Placeholders: {name}, {chat}, {id}",
    "prompt_wel": ("Send the welcome text as a new message.\n"
                   "Placeholders: {name}, {chat}, {id}.\n"
                   "/cancel to abort."),
    "ok_wel": "✅ Welcome text saved.",
    "cncl": "Cancelled.",
    "too_long": "Text is too long (max 1000 chars). Try a shorter one.",
    "test_sent": "✅ Test welcome message posted to the chat.",
    "cl_t": "🌊 ANTIFLOOD",
    "fd_msgs": "Threshold: {n} messages", "fd_secs": "Window: {n}s",
    "fd_act": "Action:", "fda_mute": "🔇 Mute", "fda_kick": "👢 Kick",
    "fd_dur": "Mute duration:",
    "cla_t": "🌐 CHAT LANGUAGE",
    "lang_ru": "🇷🇺 Russian", "lang_en": "🇬🇧 English",
    "fm_muted": "🌊 Flooding ({n} messages in {s}s) — muted for {dur}.",
    "fm_kicked": "🌊 Flooding ({n} messages in {s}s) — kicked.",
    "st_on": "on", "st_off": "off",
    "me_t": "⚙️ PERSONAL SETTINGS",
    "me_lang": "🌐 Interface language:", "me_auto": "Auto (follow chat)",
    "me_notif": "📬 DM notifications about my punishments:",
    "no_on": "On", "no_off": "Off",
    "me_hint": ("These settings apply in every chat with the bot.\n"
                "“Auto” language follows the chat settings (/settings)."),
    "dm_warn": "⚠️ You received a warning in “{chat}” ({n}/{total})\n📝 Reason: {reason}",
    "dm_mute_t": "🔇 You were muted in “{chat}” for {dur}.",
    "dm_mute_p": "🔇 You were muted in “{chat}” (permanent).",
    "dm_ban_t": "⛔️ You were banned in “{chat}” for {dur}.",
    "dm_ban_p": "⛔️ You were banned in “{chat}”.",
    "hb1": ("<b>Punishments</b> (reply / ID / @username):\n\n"
            "/ban &lt;target&gt; [time] [reason] — ban\n"
            "/mute &lt;target&gt; [time] [reason] — mute\n"
            "/kick [reason] — kick\n"
            "/unban · /unmute — lift a punishment\n\n"
            "<b>Warns:</b>\n"
            "/warn [reason] — issue a warning\n"
            "/warns — offender's warns\n"
            "/unwarn — remove the last one\n\n"
            "⏱ Time: plain number = minutes, also 2h / 7d / 1w\n"
            "⚖️ This chat: {limit} warns → {action}"),
    "hb2": ("<b>Custom roles</b> (managed by admins):\n\n"
            "/role &lt;target&gt; — assign via buttons\n"
            "/roleadd &lt;name&gt; [flags] [rank]\n"
            "/roledel &lt;name&gt; — delete a role\n"
            "/roleperms &lt;name&gt; — toggle perms via buttons\n"
            "/rolelist — role list\n\n"
            "<b>Permission flags:</b>\n"
            "mute · unmute · kick · ban · warn · filter · rules · "
            "logchannel · settings\n\n"
            "🪜 Rank hierarchy: you cannot punish anyone whose\n"
            "role outranks yours.\n"
            "Ranks 800+ are reserved for system roles."),
    "hb3": ("<b>Rules:</b>\n"
            "/rules — show\n"
            "/rules set &lt;text&gt; — set\n"
            "/rules clear — delete\n\n"
            "<b>Word filters:</b>\n"
            "/filteradd word — add\n"
            "/filterdel word — remove\n"
            "/filterlist — list\n\n"
            "<b>Logs:</b>\n"
            "/logchannel &lt;ID&gt; — bind a channel\n"
            "/logchannel off · no args — current one\n\n"
            "🏠 /menu — main menu"),
    "hb4": ("Every chat is configured separately via <code>/settings</code> "
            "(admins or any role with the <code>settings</code> flag):\n\n"
            "• <b>Warns</b> — limit, action at the limit, mute duration\n"
            "• <b>Word filters</b> — what happens on a trip\n"
            "• <b>Welcome</b> message for newcomers\n"
            "• <b>Antiflood</b> — threshold, window and punishment\n"
            "• <b>Chat language</b> — Russian / English\n\n"
            "Personal settings — <code>/me</code>:\n"
            "• your own interface language (auto/ru/en)\n"
            "• DM notifications about punishments"),
    "unk": "❓ Unknown command. See: /help",
}


async def get_lang(chat_id: int | None, user_id: int | None) -> str:
    """Язык юзера имеет приоритет над языком чата."""
    if user_id:
        v = await get_user_setting(user_id, "lang")
        if v:
            return v
    if chat_id is not None:
        return await get_chat_setting(chat_id, "lang") or "ru"
    return "ru"


async def t(chat_id: int | None, user_id: int | None, key: str, **kw) -> str:
    lang = await get_lang(chat_id, user_id)
    s = (STR.get(lang) or STR["ru"]).get(key)
    if s is None:
        s = STR["ru"].get(key, key)
    return s.format_map(_SafeDict(**kw)) if kw else s


# ─────────────────────── Роли и права доступа ───────────────────────

PERM_FLAGS = (
    "mute",
    "unmute",
    "kick",
    "ban",
    "warn",
    "filter",
    "rules",
    "logchannel",
    "settings",
)
ROLE_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_]{2,20}$")
MAX_CUSTOM_RANK = 800  # выше рангов нативных админов (850+) кастомным нельзя

DEFAULT_ROLES = [
    ("moderator", "unmute,mute,kick,warn", 50),
    ("admin", "unmute,mute,kick,ban,warn,filter,rules,logchannel,settings", 100),
]


async def ensure_default_roles(chat_id: int) -> None:
    """Гарантирует наличие стандартных ролей в чате (идемпотентно)."""
    await db.executemany(
        "INSERT OR IGNORE INTO roles_def(chat_id,name,perms,rank) VALUES(?,?,?,?)",
        [(chat_id, name, perms, rank) for name, perms, rank in DEFAULT_ROLES],
    )
    await db.commit()


def perms_to_set(perms: str) -> set[str]:
    return {p.strip() for p in perms.split(",") if p.strip()}


async def get_user_role(chat_id: int, user_id: int) -> str | None:
    cur = await db.execute(
        "SELECT role FROM user_roles WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    row = await cur.fetchone()
    return row["role"] if row else None


async def has_perm(chat_id: int, user_id: int, perm: str) -> bool:
    """Разработчик/глобальный админ/нативный админ — всё; остальным — флаг роли."""
    if user_id in DEVELOPERS or user_id in ADMINS:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in ("creator", "administrator"):
            return True
    except (TelegramBadRequest, TelegramMigrateToChat):
        pass
    cur = await db.execute(
        "SELECT rd.perms FROM user_roles ur"
        " JOIN roles_def rd ON rd.chat_id=ur.chat_id AND rd.name=ur.role"
        " WHERE ur.chat_id=? AND ur.user_id=?",
        (chat_id, user_id),
    )
    row = await cur.fetchone()
    return bool(row) and perm in perms_to_set(row["perms"])


async def can_manage_roles(chat_id: int, user_id: int) -> bool:
    """Ролями управляют разработчик, глобальный админ или нативный админ чата."""
    if user_id in DEVELOPERS or user_id in ADMINS:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramMigrateToChat):
        return False
    return member.status in ("creator", "administrator")


async def can_configure(chat_id: int, user_id: int) -> bool:
    """Настройки бота: админы чата либо роль с флагом settings."""
    if await can_manage_roles(chat_id, user_id):
        return True
    cur = await db.execute(
        "SELECT rd.perms FROM user_roles ur"
        " JOIN roles_def rd ON rd.chat_id=ur.chat_id AND rd.name=ur.role"
        " WHERE ur.chat_id=? AND ur.user_id=?",
        (chat_id, user_id),
    )
    row = await cur.fetchone()
    return bool(row) and "settings" in perms_to_set(row["perms"])


async def get_rank(chat_id: int, user_id: int) -> float:
    """Ранг для иерархии наказаний (выше = старше)."""
    if user_id in DEVELOPERS:
        return float("inf")
    if user_id in ADMINS:
        return 950.0
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramMigrateToChat):
        member = None
    if member and member.status == "creator":
        return 900.0
    if member and member.status == "administrator":
        return 850.0
    cur = await db.execute(
        "SELECT rd.rank FROM user_roles ur"
        " JOIN roles_def rd ON rd.chat_id=ur.chat_id AND rd.name=ur.role"
        " WHERE ur.chat_id=? AND ur.user_id=?",
        (chat_id, user_id),
    )
    row = await cur.fetchone()
    return float(row["rank"]) if row else 0.0


async def get_log_channel(chat_id: int) -> int:
    """Приватный лог-канал чата или фолбэк на .env."""
    cur = await db.execute(
        "SELECT channel_id FROM log_channels WHERE chat_id=?", (chat_id,)
    )
    row = await cur.fetchone()
    return row["channel_id"] if row else LOG_CHANNEL_ID


async def add_punishment(chat_id: int, user_id: int, ptype: str, until_ts: float) -> None:
    await db.execute(
        "INSERT INTO punishments(chat_id,user_id,type,until) VALUES(?,?,?,?)",
        (chat_id, user_id, ptype, until_ts),
    )
    await db.commit()


async def clear_punishments(chat_id: int, user_id: int) -> None:
    await db.execute(
        "DELETE FROM punishments WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    )
    await db.commit()


async def get_warn_count(chat_id: int, user_id: int) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM warns WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    row = await cur.fetchone()
    return row["c"]


async def cache_user(chat_id: int, user) -> None:
    username = (user.username or "").lower() or None
    now = int(time.time())
    await db.execute(
        "INSERT INTO users(chat_id,user_id,username,full_name,last_seen,first_seen,messages)"
        " VALUES(?,?,?,?,?,?,0)"
        " ON CONFLICT(chat_id,user_id) DO UPDATE SET"
        " username=excluded.username,"
        " full_name=excluded.full_name,"
        " last_seen=excluded.last_seen",  # first_seen/messages не трогаем
        (chat_id, user.id, username, user.full_name, now, now),
    )


async def inc_messages(chat_id: int, user_id: int) -> None:
    await db.execute(
        "UPDATE users SET messages = messages + 1 WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )


async def find_user_id_by_username(chat_id: int, username: str) -> int | None:
    cur = await db.execute(
        "SELECT user_id FROM users WHERE chat_id=? AND username=? LIMIT 1",
        (chat_id, username.lower()),
    )
    row = await cur.fetchone()
    return row["user_id"] if row else None


# ─────────────────────────── Утилиты ────────────────────────────────


TIME_ARG_RE = re.compile(r"^(\d+)([mhdw]?)$", re.IGNORECASE)


def parse_time_arg(text: str) -> int | None:
    """'5' → 300 сек (число = минуты); '90m'/'2h'/'7d'/'1w' → секунды."""
    m = TIME_ARG_RE.match(text.strip())
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2).lower()
    if not unit:
        return num * 60
    return num * DURATION_UNITS[unit]


DUR_UNITS_BY_LANG = {"ru": ("д", "ч", "м"), "en": ("d", "h", "m")}


def fmt_duration(seconds: int, lang: str = "ru") -> str:
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    ud, uh, um = DUR_UNITS_BY_LANG.get(lang, DUR_UNITS_BY_LANG["ru"])
    parts = []
    if d:
        parts.append(f"{d}{ud}")
    if h:
        parts.append(f"{h}{uh}")
    if m or not parts:
        parts.append(f"{m}{um}")
    return " ".join(parts)


def hd(emoji: str, title: str | None = None) -> str:
    """Заголовок секции: жирный эмодзи+текст без псевдографики.

    Допустим один аргумент — тогда строка уже содержит эмодзи.
    """
    if title is None:
        return f"<b>{emoji}</b>"
    return f"<b>{emoji} {title}</b>"


def qt(text: str, expandable: bool = False) -> str:
    """Блок-цитата Telegram — одинаково рендерится на всех устройствах."""
    if expandable:
        return f"<blockquote expandable>{text}</blockquote>"
    return f"<blockquote>{text}</blockquote>"


def user_link(user) -> str:
    name = html.escape(user.full_name)
    return f'<a href="tg://user?id={user.id}"><b>{name}</b></a> · <code>{user.id}</code>'


async def log_action(text: str, chat_id: int | None = None) -> None:
    global log_channel_ok
    log.info(text)
    if not bot or not log_channel_ok:
        return
    try:
        channel = await get_log_channel(chat_id) if chat_id else LOG_CHANNEL_ID
    except Exception:
        channel = LOG_CHANNEL_ID
    if not channel:
        return
    try:
        cid = await resolve_chat_id(channel)
        await bot.send_message(cid, html.escape(text))
    except TelegramMigrateToChat as e:
        await remap_chat(channel, e.migrate_to_chat_id)
    except TelegramBadRequest as e:
        log_channel_ok = False
        log.warning(
            "Отключил отправку в лог-канал (однократная ошибка): %s. "
            "Добавь бота админом в лог-канал и проверь его ID.", e
        )


async def notify_user(target_id: int, key: str, **kw) -> None:
    """ЛС-уведомление юзеру о наказании; неудачи тихо игнорируем."""
    if not bot or target_id <= 0:
        return
    try:
        if await get_user_setting(target_id, "notify") != "on":
            return
        text = await t(None, target_id, key, **kw)
        await bot.send_message(target_id, text)
    except Exception:
        pass  # бот не может начать диалог первым — это нормально


async def can_moderate(message: Message) -> bool:
    """Анонимный админ, глобальный админ или админ чата."""
    if message.from_user is None:
        return False
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True
    if message.from_user.id in ADMINS:
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    except (TelegramBadRequest, TelegramMigrateToChat):
        return False
    return member.status in ("administrator", "creator")


async def target_is_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except (TelegramBadRequest, TelegramMigrateToChat):
        return False
    return member.status in ("administrator", "creator")


async def resolve_target(message: Message, token: str | None):
    """Цель наказания: реплай, либо числовой ID / @юзернейм / t.me-ссылка.

    Юзернейм резолвится через кэш участников чата (таблица users):
    Telegram API принимает только числовой user_id.
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    if not token:
        return None
    arg = token.strip()
    chat_id = message.chat.id

    # Числовой ID → сразу в API
    if arg.lstrip("-").isdigit():
        try:
            member = await bot.get_chat_member(chat_id, int(arg))
            return member.user
        except (TelegramBadRequest, TelegramMigrateToChat):
            return None

    # @юзернейм / t.me-ссылка → кэш → ID → API (только int!)
    m = USERNAME_RE.match(arg)
    if not m:
        return None
    uid = await find_user_id_by_username(chat_id, m.group(1))
    if uid is None:
        return None
    try:
        member = await bot.get_chat_member(chat_id, uid)
        return member.user
    except (TelegramBadRequest, TelegramMigrateToChat):
        return None


async def validated_target(message: Message, token: str | None):
    target = await resolve_target(message, token)
    cid = message.chat.id
    uid = message.from_user.id if message.from_user else 0
    if not target:
        await message.reply(await t(cid, uid, "t_none"))
        return None
    if target.is_bot:
        await message.reply(await t(cid, uid, "t_bot"))
        return None
    if target.id == uid:
        await message.reply(await t(cid, uid, "t_self"))
        return None
    if await target_is_admin(message.chat.id, target.id):
        await message.reply(await t(cid, uid, "t_admin"))
        return None
    if target.id in DEVELOPERS:
        await message.reply(await t(cid, uid, "t_dev"))
        return None
    actor_rank = await get_rank(message.chat.id, message.from_user.id)
    target_rank = await get_rank(message.chat.id, target.id)
    if target_rank > actor_rank:
        await message.reply(await t(cid, uid, "t_higher"))
        return None
    return target


def split_reason(args: str, parts: list[str], with_token: bool) -> str | None:
    """Причина — всё после токена цели/длительности."""
    start = 1 if (with_token and parts) else 0
    rest = " ".join(parts[start:]) if len(parts) > start else ""
    rest = rest.strip()
    return rest or args.strip() or None


async def default_permissions(chat_id: int) -> ChatPermissions:
    try:
        chat = await bot.get_chat(chat_id)
        if chat.permissions:
            return chat.permissions
    except (TelegramBadRequest, TelegramMigrateToChat):
        pass
    return FULL_PERMS


class UserCacheMiddleware(BaseMiddleware):
    """Кладёт в кэш отправителя и автора реплая из каждого группового сообщения."""

    async def __call__(self, handler, event: Message, data: dict):
        try:
            if event.chat.type != ChatType.PRIVATE:
                if event.from_user and not event.from_user.is_bot:
                    await cache_user(event.chat.id, event.from_user)
                    await inc_messages(event.chat.id, event.from_user.id)
                if event.reply_to_message and event.reply_to_message.from_user:
                    await cache_user(event.chat.id, event.reply_to_message.from_user)
                await db.commit()
        except Exception:
            log.exception("Ошибка кэша пользователей")
        return await handler(event, data)


# ─────────────────────── Варны и эскалация ──────────────────────────


async def register_warn(chat_id: int, target, by_id: int, reason: str | None) -> str:
    """Добавляет варн; при лимите применяет настроенное в чате действие."""
    limit = max(1, min(10, await chat_int_setting(chat_id, "warn_limit", 3)))
    action = await get_chat_setting(chat_id, "warn_action")
    mute_hours = await chat_int_setting(chat_id, "warn_mute_hours", 24)
    lang = await get_lang(chat_id, None)

    await db.execute(
        "INSERT INTO warns(chat_id,user_id,by_id,reason,created_at) VALUES(?,?,?,?,?)",
        (chat_id, target.id, by_id, reason or "", int(time.time())),
    )
    await db.commit()
    count = await get_warn_count(chat_id, target.id)

    text = (
        (await t(chat_id, None, "w_head", n=count, total=limit))
        + user_link(target)
    )
    if reason:
        text += await t(chat_id, None, "w_reason") + html.escape(reason)

    if count < limit:
        return text

    # Лимит достигнут — сбрасываем варны и наказываем по настройке.
    await db.execute(
        "DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, target.id)
    )
    await db.commit()

    try:
        if action == "ban":
            await clear_punishments(chat_id, target.id)
            await bot.ban_chat_member(chat_id, target.id)
            text += await t(chat_id, None, "lim_ban")
            await log_action(
                f"#ban chat={chat_id} user={target.id} авто-бан за варны", chat_id
            )
            asyncio.ensure_future(
                notify_user(target.id, "dm_ban_p",
                            chat=await _chat_title(chat_id))
            )
        elif action == "kick":
            await bot.ban_chat_member(chat_id, target.id)
            await bot.unban_chat_member(chat_id, target.id, only_if_banned=True)
            text += await t(chat_id, None, "lim_kick")
            await log_action(
                f"#kick chat={chat_id} user={target.id} авто-кик за варны", chat_id
            )
        elif action == "mute":
            until = time.time() + mute_hours * 3600
            await bot.restrict_chat_member(
                chat_id,
                target.id,
                permissions=ChatPermissions(),
                until_date=datetime.now() + timedelta(seconds=mute_hours * 3600),
            )
            await add_punishment(chat_id, target.id, "auto_mute", until)
            text += await t(
                chat_id, None, "lim_mute",
                dur=fmt_duration(mute_hours * 3600, lang),
            )
            await log_action(
                f"#mute chat={chat_id} user={target.id} авто-мут за варны",
                chat_id,
            )
            asyncio.ensure_future(
                notify_user(
                    target.id, "dm_mute_t",
                    chat=await _chat_title(chat_id),
                    dur=fmt_duration(mute_hours * 3600,
                                     await get_lang(None, target.id)),
                )
            )
        else:  # reset
            text += await t(chat_id, None, "lim_reset")
    except TelegramBadRequest as e:
        text += await t(chat_id, None, "w_fail") + html.escape(str(e))
    return text


async def _chat_title(chat_id: int) -> str:
    try:
        chat = await bot.get_chat(chat_id)
        return chat.title or str(chat_id)
    except Exception:
        return str(chat_id)


# ───────────────────────── Команды модерации ────────────────────────


def split_tail(tail: str) -> tuple[int | None, str | None]:
    """'время причина' → (секунды|None, причина). Число = минуты."""
    parts = tail.split(maxsplit=1)
    if not parts:
        return None, None
    seconds = parse_time_arg(parts[0])
    if seconds is not None:
        return seconds, (parts[1].strip() if len(parts) > 1 else None) or None
    return None, tail.strip() or None


async def punish_target_args(message: Message, command: CommandObject):
    """Общий разбор аргументов /ban и /mute. Возвращает (target|None, секунды|None, причина)."""
    args = command.args or ""
    parts = args.split(maxsplit=2)
    if message.reply_to_message:
        token = None
        tail = args
    else:
        token = parts[0] if parts else None
        tail = " ".join(parts[1:]) if len(parts) > 1 else ""

    target = await validated_target(message, token)
    if not target:
        return None, None, None
    seconds, reason = split_tail(tail)
    return target, seconds, reason


@router.message(GROUP_CHATS, Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "ban"):
        return
    target, seconds, reason = await punish_target_args(message, command)
    if not target:
        return
    lang = await get_lang(cid, uid)
    try:
        if seconds:
            await bot.ban_chat_member(
                cid,
                target.id,
                until_date=datetime.now() + timedelta(seconds=seconds),
            )
        else:
            await bot.ban_chat_member(cid, target.id)
    except TelegramBadRequest as e:
        await message.reply(await t(cid, uid, "e_ban") + html.escape(str(e)))
        return
    await clear_punishments(cid, target.id)
    if seconds:
        await add_punishment(cid, target.id, "ban", time.time() + seconds)
    key = "r_ban" if seconds else "r_banp"
    reply = await t(cid, uid, key, dur=fmt_duration(seconds, lang)) + user_link(target)
    if reason:
        reply += await t(cid, uid, "r_reason") + html.escape(reason)
    await message.answer(reply)
    dur_txt = fmt_duration(seconds, lang) if seconds else "навсегда"
    log_text = (
        f"#ban[{dur_txt}] "
        f"chat={html.escape(message.chat.title)} user={target.id}"
        f" mod={uid}"
    )
    await log_action(
        log_text + (f" причина: {reason}" if reason else ""), cid
    )
    if seconds:
        asyncio.ensure_future(notify_user(
            target.id, "dm_ban_t",
            chat=await _chat_title(cid),
            dur=fmt_duration(seconds, await get_lang(None, target.id)),
        ))
    else:
        asyncio.ensure_future(notify_user(
            target.id, "dm_ban_p", chat=await _chat_title(cid)
        ))


@router.message(GROUP_CHATS, Command("kick"))
async def cmd_kick(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "kick"):
        return
    args = command.args or ""
    parts = args.split(maxsplit=1)
    token = None if message.reply_to_message else (parts[0] if parts else None)
    target = await validated_target(message, token)
    if not target:
        return
    reason = split_reason(args, parts, with_token=not message.reply_to_message)
    try:
        await bot.ban_chat_member(cid, target.id)
        await bot.unban_chat_member(cid, target.id, only_if_banned=True)
    except TelegramBadRequest as e:
        await message.reply(await t(cid, uid, "e_kick") + html.escape(str(e)))
        return
    reply = await t(cid, uid, "r_kick") + user_link(target)
    if reason:
        reply += await t(cid, uid, "r_reason") + html.escape(reason)
    await message.answer(reply)
    await log_action(
        f"#kick chat={html.escape(message.chat.title)} user={target.id}"
        f" mod={uid}" + (f" причина: {reason}" if reason else ""),
        cid,
    )


@router.message(GROUP_CHATS, Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "mute"):
        return
    target, seconds, reason = await punish_target_args(message, command)
    if not target:
        return
    lang = await get_lang(cid, uid)
    try:
        if seconds:
            await bot.restrict_chat_member(
                cid,
                target.id,
                permissions=ChatPermissions(),
                until_date=datetime.now() + timedelta(seconds=seconds),
            )
        else:
            await bot.restrict_chat_member(
                cid, target.id, permissions=ChatPermissions()
            )
    except TelegramBadRequest as e:
        await message.reply(await t(cid, uid, "e_mute") + html.escape(str(e)))
        return
    await clear_punishments(cid, target.id)
    if seconds:
        await add_punishment(cid, target.id, "mute", time.time() + seconds)
    key = "r_mute" if seconds else "r_mutep"
    reply = await t(cid, uid, key, dur=fmt_duration(seconds, lang)) + user_link(target)
    if reason:
        reply += await t(cid, uid, "r_reason") + html.escape(reason)
    await message.answer(reply)
    dur_txt = fmt_duration(seconds, lang) if seconds else "навсегда"
    log_text = (
        f"#mute[{dur_txt}] "
        f"chat={html.escape(message.chat.title)} user={target.id}"
        f" mod={uid}"
    )
    await log_action(log_text + (f" причина: {reason}" if reason else ""), cid)
    if seconds:
        asyncio.ensure_future(notify_user(
            target.id, "dm_mute_t",
            chat=await _chat_title(cid),
            dur=fmt_duration(seconds, await get_lang(None, target.id)),
        ))
    else:
        asyncio.ensure_future(notify_user(
            target.id, "dm_mute_p", chat=await _chat_title(cid)
        ))


@router.message(GROUP_CHATS, Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "ban"):
        return
    token = (command.args or "").split(maxsplit=1)[0] if command.args else None
    target = await resolve_target(message, token)
    if not target:
        await message.reply(await t(cid, uid, "t_none"))
        return
    try:
        await bot.unban_chat_member(cid, target.id, only_if_banned=True)
    except TelegramBadRequest as e:
        await message.reply(await t(cid, uid, "e_unban") + html.escape(str(e)))
        return
    await clear_punishments(cid, target.id)
    await message.answer(await t(cid, uid, "r_unban") + user_link(target))
    await log_action(
        f"#unban chat={html.escape(message.chat.title)} user={target.id}", cid
    )


@router.message(GROUP_CHATS, Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "unmute"):
        return
    token = (command.args or "").split(maxsplit=1)[0] if command.args else None
    target = await resolve_target(message, token)
    if not target:
        await message.reply(await t(cid, uid, "t_none"))
        return
    perms = await default_permissions(cid)
    try:
        await bot.restrict_chat_member(cid, target.id, permissions=perms)
    except TelegramBadRequest as e:
        await message.reply(await t(cid, uid, "e_unmute") + html.escape(str(e)))
        return
    await clear_punishments(cid, target.id)
    await message.answer(await t(cid, uid, "r_unmute") + user_link(target))
    await log_action(
        f"#unmute chat={html.escape(message.chat.title)} user={target.id}", cid
    )


@router.message(GROUP_CHATS, Command("warn"))
async def cmd_warn(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "warn"):
        return
    args = command.args or ""
    parts = args.split(maxsplit=1)
    token = None if message.reply_to_message else (parts[0] if parts else None)
    target = await validated_target(message, token)
    if not target:
        return
    reason = split_reason(args, parts, with_token=not message.reply_to_message)
    text = await register_warn(cid, target, uid, reason)
    await message.answer(text)
    await log_action(
        f"#warn chat={html.escape(message.chat.title)} user={target.id}"
        f" mod={uid}" + (f" причина: {reason}" if reason else ""),
        cid,
    )
    limit = max(1, min(10, await chat_int_setting(cid, "warn_limit", 3)))
    count = await get_warn_count(cid, target.id)
    asyncio.ensure_future(notify_user(
        target.id, "dm_warn",
        chat=await _chat_title(cid),
        reason=reason or "", n=count, total=limit,
    ))


@router.message(GROUP_CHATS, Command("warns"))
async def cmd_warns(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "warn"):
        return
    token = (command.args or "").split(maxsplit=1)[0] if command.args else None
    target = await resolve_target(message, token)
    if not target:
        await message.reply(await t(cid, uid, "t_none"))
        return
    limit = max(1, min(10, await chat_int_setting(cid, "warn_limit", 3)))
    count = await get_warn_count(cid, target.id)
    cur = await db.execute(
        "SELECT reason, created_at FROM warns WHERE chat_id=? AND user_id=?"
        " ORDER BY created_at DESC",
        (cid, target.id),
    )
    rows = await cur.fetchall()
    lines = [
        await t(cid, uid, "wl_head",
                link=user_link(target), n=count, total=limit)
    ]
    for i, row in enumerate(rows, 1):
        when = datetime.fromtimestamp(row["created_at"]).strftime("%d.%m.%Y %H:%M")
        r = html.escape(row["reason"]) or "-"
        lines.append(
            await t(cid, uid, "wl_item", i=i, r=r, when=when)
        )
    await message.answer("\n".join(lines))


@router.message(GROUP_CHATS, Command("unwarn"))
async def cmd_unwarn(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "warn"):
        return
    token = (command.args or "").split(maxsplit=1)[0] if command.args else None
    target = await resolve_target(message, token)
    if not target:
        await message.reply(await t(cid, uid, "t_none"))
        return
    cur = await db.execute(
        "SELECT rowid FROM warns WHERE chat_id=? AND user_id=?"
        " ORDER BY created_at DESC LIMIT 1",
        (cid, target.id),
    )
    row = await cur.fetchone()
    if not row:
        await message.answer(
            await t(cid, uid, "w_gone", link=user_link(target))
        )
        return
    await db.execute("DELETE FROM warns WHERE rowid=?", (row["rowid"],))
    await db.commit()
    limit = max(1, min(10, await chat_int_setting(cid, "warn_limit", 3)))
    count = await get_warn_count(cid, target.id)
    await message.answer(await t(
        cid, uid, "w_removed",
        link=user_link(target), n=count, total=limit,
    ))


# ───────────────────────── Фильтры слов ─────────────────────────────


@router.message(GROUP_CHATS, Command("filteradd"))
async def cmd_filteradd(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "filter"):
        return
    word = " ".join((command.args or "").split()).lower()
    if not word:
        await message.reply(await t(cid, uid, "fa_fmt"))
        return
    await db.execute(
        "INSERT OR IGNORE INTO filters(chat_id,word) VALUES(?,?)", (cid, word)
    )
    await db.commit()
    await message.answer(await t(cid, uid, "fa_ok", w=html.escape(word)))
    await log_action(
        f"#filteradd chat={html.escape(message.chat.title)}"
        f" mod={uid} слово: {word}",
        cid,
    )


@router.message(GROUP_CHATS, Command("filterdel"))
async def cmd_filterdel(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "filter"):
        return
    word = " ".join((command.args or "").split()).lower()
    if not word:
        await message.reply(await t(cid, uid, "fa_fmt"))
        return
    cur = await db.execute(
        "DELETE FROM filters WHERE chat_id=? AND word=?", (cid, word)
    )
    await db.commit()
    if cur.rowcount:
        await message.answer(await t(cid, uid, "fd_ok", w=html.escape(word)))
    else:
        await message.answer(await t(cid, uid, "fd_miss", w=html.escape(word)))


@router.message(GROUP_CHATS, Command("filterlist"))
async def cmd_filterlist(message: Message):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "filter"):
        return
    cur = await db.execute("SELECT word FROM filters WHERE chat_id=?", (cid,))
    words = sorted(r["word"] for r in await cur.fetchall())
    if not words:
        await message.answer(await t(cid, uid, "fl_empty"))
    else:
        await message.answer(
            await t(cid, uid, "fl_head") + html.escape(", ".join(words))
        )


@router.message(GROUP_CHATS, F.text, ~F.text.startswith("/"))
async def filter_watcher(message: Message):
    """Проверка обычных сообщений на запрещённые слова (команды пропускает)."""
    cid = message.chat.id
    if message.from_user is None:
        return
    uid = message.from_user.id
    if await can_moderate(message):
        return
    if await get_user_role(cid, uid):
        return  # обладатели ролей не проверяются
    text = message.text.lower()
    cur = await db.execute("SELECT word FROM filters WHERE chat_id=?", (cid,))
    hit = next((r["word"] for r in await cur.fetchall() if r["word"] in text), None)
    if not hit:
        return
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    action = await get_chat_setting(cid, "filter_action")
    lang = await get_lang(cid, uid)

    if action == "delete_only":
        return

    if action == "delete_mute":
        hours = await chat_int_setting(cid, "warn_mute_hours", 24)
        until = time.time() + hours * 3600
        try:
            await bot.restrict_chat_member(
                cid,
                uid,
                permissions=ChatPermissions(),
                until_date=datetime.now() + timedelta(seconds=hours * 3600),
            )
        except TelegramBadRequest:
            pass
        await add_punishment(cid, uid, "auto_mute", until)
        notice = (
            user_link(message.from_user)
            + await t(cid, uid, "lim_mute",
                      dur=fmt_duration(hours * 3600, lang))
            + await t(cid, uid, "w_reason")
            + html.escape(f"фильтр: {hit}")
        )
        await message.answer(notice)
        asyncio.ensure_future(notify_user(
            uid, "dm_mute_t",
            chat=await _chat_title(cid),
            dur=fmt_duration(hours * 3600, await get_lang(None, uid)),
        ))
        return

    # default: delete_warn
    notice = await register_warn(cid, message.from_user, 0, f"фильтр: {hit}")
    await message.answer(notice)


# ───────────────────────── Инлайн-меню ──────────────────────────────


async def menu_view(chat_id: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        await t(chat_id, user_id, "menu_title")
        + "\n\n"
        + await t(chat_id, user_id, "menu_pick")
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=await t(chat_id, user_id, "b_profile"),
                    callback_data="menu:profile",
                ),
                InlineKeyboardButton(
                    text=await t(chat_id, user_id, "b_rules"),
                    callback_data="menu:rules",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=await t(chat_id, user_id, "b_roles"),
                    callback_data="menu:roles",
                ),
                InlineKeyboardButton(
                    text=await t(chat_id, user_id, "b_help"),
                    callback_data="menu:help",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=await t(chat_id, user_id, "b_settings"),
                    callback_data="menu:settings",
                ),
                InlineKeyboardButton(
                    text=await t(chat_id, user_id, "b_info"),
                    callback_data="menu:info",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=await t(chat_id, user_id, "b_close"),
                    callback_data="menu:close",
                )
            ],
        ]
    )
    return text, kb


async def build_profile(chat_id: int, user) -> str:
    uid = user.id
    lang = await get_lang(chat_id, uid)
    uname = f"@{html.escape(user.username)}" if user.username else await t(chat_id, uid, "p_none")
    limit = max(1, min(10, await chat_int_setting(chat_id, "warn_limit", 3)))
    q = [
        f"{await t(chat_id, uid, 'p_name')} {html.escape(user.full_name)}",
        f"{await t(chat_id, uid, 'p_user')} {uname}",
        f"{await t(chat_id, uid, 'p_id')} <code>{uid}</code>",
        "",
    ]

    if uid in DEVELOPERS:
        q.append(f"{await t(chat_id, uid, 'p_status')} {await t(chat_id, uid, 'p_dev')}")
    elif uid in ADMINS:
        q.append(f"{await t(chat_id, uid, 'p_status')} {await t(chat_id, uid, 'p_glob')}")
    else:
        try:
            member = await bot.get_chat_member(chat_id, uid)
        except TelegramBadRequest:
            member = None
        if member:
            status_map = {
                "creator": "st_owner",
                "administrator": "st_admin",
                "member": "st_member",
                "left": "st_left",
                "kicked": "st_banned",
            }
            if (
                member.status == "restricted"
                and not getattr(member, "can_send_messages", True)
            ):
                until = getattr(member, "until_date", None)
                when = until.strftime("%d.%m.%Y %H:%M") if until else "—"
                status = await t(chat_id, uid, "st_muted", when=when)
            else:
                key = status_map.get(member.status, "st_member")
                status = await t(chat_id, uid, key)
            q.append(f"{await t(chat_id, uid, 'p_status')} {status}")

    role = await get_user_role(chat_id, uid)
    role_txt = html.escape(role) if role else await t(chat_id, uid, "p_no")
    q.append(f"{await t(chat_id, uid, 'p_role')} {role_txt}")
    q.append("")

    count = await get_warn_count(chat_id, uid)
    cur = await db.execute(
        "SELECT reason FROM warns WHERE chat_id=? AND user_id=?"
        " ORDER BY created_at DESC LIMIT 1",
        (chat_id, uid),
    )
    last = await cur.fetchone()
    bar = "▰" * count + "▱" * (limit - count)
    q.append(f"{await t(chat_id, uid, 'p_warns')} {bar}  {count}/{limit}")
    if last and last["reason"]:
        q.append("<i>"
                 + await t(chat_id, uid, "p_last", r=html.escape(last["reason"]))
                 + "</i>")

    cur = await db.execute(
        "SELECT messages, first_seen FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, uid),
    )
    stats = await cur.fetchone()
    if stats:
        first_seen = stats["first_seen"] or 0
        since = (
            datetime.fromtimestamp(first_seen).strftime(
                "%d.%m.%Y" if lang != "en" else "%Y-%m-%d"
            )
            if first_seen
            else "—"
        )
        q.append(f"{await t(chat_id, uid, 'p_msgs')} {stats['messages'] or 0}")
        q.append(f"{await t(chat_id, uid, 'p_since')} {since}")

    return hd(await t(chat_id, uid, "h_profile")) + "\n\n" + qt("\n".join(q))


async def build_rules(chat_id: int, user_id: int = 0) -> str:
    cur = await db.execute("SELECT text FROM rules WHERE chat_id=?", (chat_id,))
    row = await cur.fetchone()
    if row and row["text"].strip():
        return (
            hd(await t(chat_id, user_id, "h_rules"))
            + "\n\n"
            + qt(html.escape(row["text"]), expandable=True)
        )
    body = (
        await t(chat_id, user_id, "re_body")
        + "\n<code>"
        + await t(chat_id, user_id, "re_cmd")
        + "</code>"
    )
    return hd(await t(chat_id, user_id, "h_rules")) + "\n\n" + qt(body)


async def info_view(chat_id: int, user_id: int = 0) -> str:
    body = "\n".join(
        [
            "🤖 <b>Radmirka Helper</b>",
            await t(chat_id, user_id, "info_body"),
        ]
    )
    return hd(await t(chat_id, user_id, "h_info")) + "\n\n" + qt(body)


@router.message(GROUP_CHATS, Command("start"))
@router.message(GROUP_CHATS, Command("menu"))
async def cmd_menu(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    cid = message.chat.id
    uid = message.from_user.id
    text, kb = await menu_view(cid, uid)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("menu:"))
async def on_menu_callback(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    if action == "close":
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        await callback.answer()
        return
    chat_id = callback.message.chat.id
    uid = callback.from_user.id
    if action == "profile":
        text = await build_profile(chat_id, callback.from_user)
    elif action == "rules":
        text = await build_rules(chat_id, uid)
    elif action == "roles":
        if await can_manage_roles(chat_id, uid):
            text = await build_rolelist(chat_id, uid)
        else:
            text = (
                hd(await t(chat_id, uid, "h_roles"))
                + "\n\n"
                + qt(await t(chat_id, uid, "roles_locked"))
            )
    elif action == "settings":
        if not await can_configure(chat_id, uid):
            await callback.answer(
                await t(chat_id, uid, "cfg_den"), show_alert=True
            )
            return
        text, kb = await settings_view(chat_id, uid, "w")
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            log.warning("Не удалось открыть настройки: %s", e)
        await callback.answer()
        return
    elif action == "help":
        pages = await build_help_pages(chat_id, uid)
        title, body = pages[0]
        try:
            await callback.message.edit_text(
                f"{title}\n\n{body}", reply_markup=help_kb(0)
            )
        except TelegramBadRequest as e:
            log.warning("Не удалось открыть хелп: %s", e)
        await callback.answer()
        return
    else:  # info / back
        if action == "back":
            text, kb = await menu_view(chat_id, uid)
            try:
                await callback.message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest as e:
                log.warning("Не удалось обновить меню: %s", e)
            await callback.answer()
            return
        text = await info_view(chat_id, uid)
    try:
        _, kb2 = await menu_view(chat_id, uid)
        await callback.message.edit_text(text, reply_markup=kb2)
    except TelegramBadRequest as e:
        log.warning("Не удалось обновить меню: %s", e)
    await callback.answer()


@router.callback_query(F.data.startswith("help:"))
async def on_help_callback(callback: CallbackQuery):
    page_s = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id if callback.message else None
    uid = callback.from_user.id
    pages = await build_help_pages(chat_id, uid)
    if not page_s.isdigit() or int(page_s) >= len(pages):
        await callback.answer()
        return
    page = int(page_s)
    title, body = pages[page]
    try:
        await callback.message.edit_text(
            f"{title}\n\n{body}", reply_markup=help_kb(page)
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


# ────────────────────────── Правила чата ────────────────────────────


@router.message(GROUP_CHATS, Command("rules"))
async def cmd_rules(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    sub, _, rest = args.partition(" ")
    sub_l = sub.lower()

    if sub_l in ("set", "clear"):
        if not await has_perm(message.chat.id, message.from_user.id, "rules"):
            return
        if sub_l == "clear":
            await db.execute("DELETE FROM rules WHERE chat_id=?", (message.chat.id,))
            await db.commit()
            await message.reply("✅ Правила чата удалены.")
            return
        rules_text = rest.strip()
        if not rules_text:
            await message.reply(
                "Формат: /rules set текст правил (можно многострочно)"
            )
            return
        await db.execute(
            "INSERT INTO rules(chat_id,text) VALUES(?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET text=excluded.text",
            (message.chat.id, rules_text),
        )
        await db.commit()
        await message.reply(f"✅ Правила чата сохранены ({len(rules_text)} симв.)")
        await log_action(
            f"#rules chat={html.escape(message.chat.title)}"
            f" mod={message.from_user.id} обновил правила",
            message.chat.id,
        )
        return

    await message.answer(await build_rules(message.chat.id))


# ────────────────────── Роли: управление ────────────────────────────


def role_pick_kb(target_uid: int, rows) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🔵 {r['name']} · ранг {r['rank']}",
                callback_data=f"role:{target_uid}:{r['rid']}",
            )
        ]
        for r in rows
    ]
    buttons.append(
        [
            InlineKeyboardButton(
                text="⚪️ Снять роль", callback_data=f"role:{target_uid}:off"
            ),
            InlineKeyboardButton(text="✖️", callback_data="roleclose"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def role_perms_kb(rid: int, active: set[str]) -> InlineKeyboardMarkup:
    buttons, row = [], []
    for flag in PERM_FLAGS:
        row.append(
            InlineKeyboardButton(
                text=("✅ " if flag in active else "❌ ") + flag,
                callback_data=f"rp:{rid}:{flag}",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="roleclose")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(GROUP_CHATS, Command("role"))
async def cmd_role(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await can_manage_roles(cid, uid):
        return
    args = command.args or ""
    token = None if message.reply_to_message else (args.split(maxsplit=1)[0] if args else None)
    target = await resolve_target(message, token)
    if not target:
        await message.reply(await t(cid, uid, "t_none"))
        return
    if target.id in DEVELOPERS:
        await message.reply(await t(cid, uid, "ro_devskip"))
        return
    await ensure_default_roles(cid)
    cur = await db.execute(
        "SELECT rowid AS rid, name, rank FROM roles_def WHERE chat_id=?"
        " ORDER BY rank DESC, name",
        (cid,),
    )
    rows = await cur.fetchall()
    current = await get_user_role(cid, target.id)
    uname = f"@{html.escape(target.username)}" if target.username else html.escape(target.full_name)
    text = (
        hd(await t(cid, uid, "h_assign"))
        + "\n\n"
        + qt(await t(
            cid, uid, "ro_now",
            u=uname, id=target.id,
            cur=html.escape(current) if current else await t(cid, uid, "p_no"),
        ))
        + "\n\n"
        + await t(cid, uid, "ro_hint")
    )
    await message.answer(text, reply_markup=role_pick_kb(target.id, rows))


@router.message(GROUP_CHATS, Command("roleperms"))
async def cmd_roleperms(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await can_manage_roles(cid, uid):
        return
    name = (command.args or "").strip()
    cur = await db.execute(
        "SELECT rowid AS rid, name, perms, rank FROM roles_def"
        " WHERE chat_id=? AND name=?",
        (cid, name),
    )
    row = await cur.fetchone()
    if not row:
        await message.reply(await t(cid, uid, "ro_notfound"))
        return
    text = (
        await t(cid, uid, "rp_title", name=html.escape(row["name"]), rank=row["rank"])
        + "\n\n"
        + await t(cid, uid, "rp_hint")
    )
    await message.answer(
        text, reply_markup=await role_perms_kb(row["rid"], perms_to_set(row["perms"]))
    )


@router.message(GROUP_CHATS, Command("roleadd"))
async def cmd_roleadd(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await can_manage_roles(cid, uid):
        return
    parts = (command.args or "").split()
    if not parts or not ROLE_NAME_RE.match(parts[0]):
        await message.reply(await t(cid, uid, "ra_usage", flags=", ".join(PERM_FLAGS)))
        return
    name = parts[0]
    flags = (
        {f for f in parts[1].split(",") if f in PERM_FLAGS} if len(parts) > 1 else set()
    )
    try:
        rank = min(max(int(parts[2]), 0), MAX_CUSTOM_RANK) if len(parts) > 2 else 25
    except ValueError:
        rank = 25
    await db.execute(
        "INSERT INTO roles_def(chat_id,name,perms,rank) VALUES(?,?,?,?)"
        " ON CONFLICT(chat_id,name) DO UPDATE SET perms=excluded.perms,"
        " rank=excluded.rank",
        (cid, name, ",".join(f for f in PERM_FLAGS if f in flags), rank),
    )
    await db.commit()
    await message.answer(
        await t(
            cid, uid, "ra_ok",
            name=html.escape(name), rank=rank,
            flags=", ".join(sorted(flags)) or await t(cid, uid, "p_no"),
        )
    )
    await log_action(
        f"#roleadd chat={html.escape(message.chat.title)}"
        f" mod={uid} роль {name} (ранг {rank})",
        cid,
    )


@router.message(GROUP_CHATS, Command("roledel"))
async def cmd_roledel(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await can_manage_roles(cid, uid):
        return
    name = (command.args or "").strip()
    cur = await db.execute(
        "DELETE FROM roles_def WHERE chat_id=? AND name=?", (cid, name)
    )
    await db.execute(
        "DELETE FROM user_roles WHERE chat_id=? AND role=?", (cid, name)
    )
    await db.commit()
    if cur.rowcount:
        await message.answer(await t(cid, uid, "rd_ok", name=html.escape(name)))
    else:
        await message.answer(await t(cid, uid, "rd_miss", name=html.escape(name)))


async def build_rolelist(chat_id: int, user_id: int = 0) -> str:
    lang = await get_lang(chat_id, user_id)
    _ = lang
    await ensure_default_roles(chat_id)
    cur = await db.execute(
        "SELECT r.name, r.perms, r.rank,"
        " (SELECT COUNT(*) FROM user_roles u"
        "  WHERE u.chat_id=r.chat_id AND u.role=r.name) AS holders"
        " FROM roles_def r WHERE r.chat_id=? ORDER BY r.rank DESC, r.name",
        (chat_id,),
    )
    rows = await cur.fetchall()
    head = hd(await t(chat_id, user_id, "h_roles"))
    if not rows:
        return head + "\n\n" + qt(await t(chat_id, user_id, "rl_empty"))
    cards = []
    for i, r in enumerate(rows):
        p = ", ".join(perms_to_set(r["perms"]))
        icon = "🔹" if i % 2 == 0 else "🔸"
        holders = f" · 👤×{r['holders']}" if r["holders"] else ""
        cards.append(
            qt(
                f"{icon} <b>{html.escape(r['name'])}</b>{holders}\n"
                f"{await t(chat_id, user_id, 't_rank', n=r['rank'])} · "
                f"{p or await t(chat_id, user_id, 't_noperms')}"
            )
        )
    return (
        head + "\n\n" + "\n".join(cards)
        + "\n\n<i>" + html.escape(await t(chat_id, user_id, "rl_hint")) + "</i>"
    )


@router.message(GROUP_CHATS, Command("rolelist"))
async def cmd_rolelist(message: Message):
    if not await can_manage_roles(message.chat.id, message.from_user.id):
        return
    await message.answer(await build_rolelist(message.chat.id))


@router.callback_query(F.data == "roleclose")
async def on_role_close(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("role:"))
async def on_role_assign(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    uid = callback.from_user.id
    if not await can_manage_roles(chat_id, uid):
        await callback.answer(await t(chat_id, uid, "no_perms_cb"), show_alert=True)
        return
    _, uid_s, act = callback.data.split(":")
    uid = int(uid_s)
    if act == "off":
        await db.execute(
            "DELETE FROM user_roles WHERE chat_id=? AND user_id=?",
            (chat_id, uid),
        )
        new_role = None
    else:
        cur = await db.execute(
            "SELECT name FROM roles_def WHERE rowid=? AND chat_id=?",
            (int(act), chat_id),
        )
        row = await cur.fetchone()
        if not row:
            await callback.answer(
                await t(chat_id, uid, "role_gone"), show_alert=True
            )
            return
        await db.execute(
            "INSERT INTO user_roles(chat_id,user_id,role) VALUES(?,?,?)"
            " ON CONFLICT(chat_id,user_id) DO UPDATE SET role=excluded.role",
            (chat_id, uid, row["name"]),
        )
        new_role = row["name"]
    await db.commit()
    cur = await db.execute(
        "SELECT rowid AS rid, name, rank FROM roles_def WHERE chat_id=?"
        " ORDER BY rank DESC, name",
        (chat_id,),
    )
    rows = await cur.fetchall()
    ucur = await db.execute(
        "SELECT full_name, username FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, uid),
    )
    urow = await ucur.fetchone()
    if urow and urow["username"]:
        uname = "@" + html.escape(urow["username"])
    elif urow and urow["full_name"]:
        uname = html.escape(urow["full_name"])
    else:
        uname = str(uid)
    text = (
        hd(await t(chat_id, uid, "h_assign"))
        + "\n\n"
        + qt(await t(
            chat_id, uid, "ro_now",
            u=uname, id=uid,
            cur=html.escape(new_role) if new_role else await t(chat_id, uid, "p_no"),
        ))
        + "\n\n"
        + await t(chat_id, uid, "ro_hint")
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=role_pick_kb(uid, rows)
        )
    except TelegramBadRequest:
        pass
    await callback.answer(
        await t(chat_id, uid, "rr_done", role=new_role or await t(chat_id, uid, "p_no"))
    )


@router.callback_query(F.data.startswith("rp:"))
async def on_rp_toggle(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    uid = callback.from_user.id
    if not await can_manage_roles(chat_id, uid):
        await callback.answer(await t(chat_id, uid, "no_perms_cb"), show_alert=True)
        return
    _, rid_s, flag = callback.data.split(":")
    rid = int(rid_s)
    cur = await db.execute(
        "SELECT perms FROM roles_def WHERE rowid=? AND chat_id=?",
        (rid, chat_id),
    )
    row = await cur.fetchone()
    if not row:
        await callback.answer(
            await t(chat_id, uid, "role_del_gone"), show_alert=True
        )
        return
    active = perms_to_set(row["perms"])
    if flag in active:
        active.discard(flag)
        verdict = await t(chat_id, uid, "st_off")
    else:
        active.add(flag)
        verdict = await t(chat_id, uid, "st_on")
    ordered = ",".join(f for f in PERM_FLAGS if f in active)
    await db.execute(
        "UPDATE roles_def SET perms=? WHERE rowid=?", (ordered, rid)
    )
    await db.commit()
    await callback.answer(f"{flag}: {verdict}")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=await role_perms_kb(rid, active)
        )
    except TelegramBadRequest:
        pass


# ─────────────────────── Лог-канал чата ─────────────────────────────


@router.message(GROUP_CHATS, Command("logchannel"))
async def cmd_logchannel(message: Message, command: CommandObject):
    cid = message.chat.id
    uid = message.from_user.id
    if not await has_perm(cid, uid, "logchannel"):
        return
    args = (command.args or "").strip()

    if not args:
        cur = await db.execute(
            "SELECT channel_id FROM log_channels WHERE chat_id=?", (cid,)
        )
        row = await cur.fetchone()
        if row:
            await message.answer(await t(cid, uid, "lg_cur", id=row["channel_id"]))
        else:
            await message.answer(await t(cid, uid, "lg_none"))
        return

    if args.lower() == "off":
        await db.execute("DELETE FROM log_channels WHERE chat_id=?", (cid,))
        await db.commit()
        await message.answer(await t(cid, uid, "lg_offok"))
        return

    if not args.lstrip("-").isdigit():
        await message.reply(await t(cid, uid, "lg_fmt"))
        return
    channel_id = int(args)
    target_cid = channel_id
    try:
        try:
            target_cid = await resolve_chat_id(channel_id)
            await bot.send_message(target_cid, "Тест лог-канала ✅")
        except TelegramMigrateToChat as e:
            await remap_chat(channel_id, e.migrate_to_chat_id)
            target_cid = e.migrate_to_chat_id
            await bot.send_message(target_cid, "Тест лог-канала ✅")
    except TelegramBadRequest as e:
        await message.reply(await t(cid, uid, "lg_bad", e=html.escape(str(e))))
        return
    await db.execute(
        "INSERT INTO log_channels(chat_id,channel_id) VALUES(?,?)"
        " ON CONFLICT(chat_id) DO UPDATE SET channel_id=excluded.channel_id",
        (cid, channel_id),
    )
    await db.commit()
    await message.answer(await t(cid, uid, "lg_bound", id=target_cid))
    await log_action(
        f"#logchannel chat={html.escape(message.chat.title)}"
        f" mod={uid} → {target_cid}",
        cid,
    )


# ───────────────────────────── Хелп ─────────────────────────────────


async def build_help_pages(chat_id: int, user_id: int = 0):
    """4 страницы хелпа с учётом настроек текущего чата."""
    lang = await get_lang(chat_id, user_id)
    limit = max(1, min(10, await chat_int_setting(chat_id, "warn_limit", 3)))
    action_key = {
        "mute": "act_mute", "ban": "act_ban",
        "kick": "act_kick", "reset": "lim_reset",
    }.get(await get_chat_setting(chat_id, "warn_action"), "act_mute")
    pages = []
    for i in range(1, 5):
        title = "<b>" + await t(chat_id, user_id, f"ht{i}") + "</b>"
        body = await t(
            chat_id, user_id, f"hb{i}",
            limit=limit, action=await t(chat_id, user_id, action_key),
            dur=fmt_duration(
                await chat_int_setting(chat_id, "warn_mute_hours", 24) * 3600,
                lang,
            ),
        )
        pages.append((title, qt(body)))
    return pages


def help_kb(page: int, total: int = 4) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"help:{page - 1}"))
    nav.append(
        InlineKeyboardButton(
            text=f"· {page + 1}/{total} ·", callback_data="help:noop"
        )
    )
    if page < total - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"help:{page + 1}"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            nav,
            [
                InlineKeyboardButton(text="🏠", callback_data="menu:back"),
                InlineKeyboardButton(text="✖️", callback_data="menu:close"),
            ],
        ]
    )


@router.message(GROUP_CHATS, Command("help"))
async def cmd_help(message: Message):
    cid = message.chat.id
    uid = message.from_user.id
    pages = await build_help_pages(cid, uid)
    title, body = pages[0]
    await message.answer(f"{title}\n\n{body}", reply_markup=help_kb(0, len(pages)))


# ───────────────────────── Неизвестные команды ──────────────────────
# (регистрируется последним — ловит все оставшиеся команды)


@router.message(GROUP_CHATS, F.text, F.text.startswith("/"))
async def unknown_command(message: Message):
    await message.reply(await t(message.chat.id, message.from_user.id, "unk"))


# ───────────────────────── Настройки чата (/settings) ───────────────


WARN_ACT_CYCLE = ["mute", "ban", "kick", "reset"]
FLOOD_ACT_CYCLE = ["mute", "kick"]


def _mark(label: str, active: bool) -> str:
    return f"✅ {label}" if active else label


async def settings_view(chat_id: int, user_id: int, page: str = "m"):
    """Панель настроек чата: возвращает (text, keyboard)."""
    lang = await get_lang(chat_id, user_id)

    async def dur_h(hours: int) -> str:
        return await t(chat_id, user_id, "u_hr", n=hours)

    async def dur_m(minutes: int) -> str:
        return await t(chat_id, user_id, "u_min", n=minutes)

    cats = [
        [InlineKeyboardButton(text=await t(chat_id, user_id, "cw_t"), callback_data="cfg:w"),
         InlineKeyboardButton(text=await t(chat_id, user_id, "cf2_t"), callback_data="cfg:f")],
        [InlineKeyboardButton(text=await t(chat_id, user_id, "cg_t"), callback_data="cfg:g"),
         InlineKeyboardButton(text=await t(chat_id, user_id, "cl_t"), callback_data="cfg:l")],
        [InlineKeyboardButton(text=await t(chat_id, user_id, "cla_t"), callback_data="cfg:a")],
        [InlineKeyboardButton(text="🏠", callback_data="menu:back"),
         InlineKeyboardButton(text="✖️", callback_data="menu:close")],
    ]

    if page == "m":
        text = (
            hd(await t(chat_id, user_id, "cf_title"))
            + "\n\n"
            + await t(chat_id, user_id, "cf_pick")
        )
        return text, InlineKeyboardMarkup(inline_keyboard=cats)

    nav = [
        [
            InlineKeyboardButton(text="⬅️", callback_data="cfg:m"),
            InlineKeyboardButton(text="✖️", callback_data="menu:close"),
        ]
    ]

    if page == "w":
        limit = await chat_int_setting(chat_id, "warn_limit", 3)
        action = await get_chat_setting(chat_id, "warn_action")
        act_label = await t(
            chat_id, user_id,
            {"mute": "act_mute", "ban": "act_ban",
             "kick": "act_kick", "reset": "act_reset"}.get(action, "act_mute"),
        )
        hours = await chat_int_setting(chat_id, "warn_mute_hours", 24)
        body = "\n".join([
            await t(chat_id, user_id, "cw_limit", n=limit),
            await t(chat_id, user_id, "cw_act") + f" <b>{act_label}</b>",
        ])
        if action == "mute":
            body += "\n" + await t(chat_id, user_id, "cw_hours") + \
                f" <b>{await dur_h(hours)}</b>"
        rows = [[
            InlineKeyboardButton(text="➖", callback_data="cfgadj:w:limit:-1"),
            InlineKeyboardButton(text=f"⚠️ {limit}", callback_data="cfg:w"),
            InlineKeyboardButton(text="➕", callback_data="cfgadj:w:limit:1"),
        ], [
            InlineKeyboardButton(text=act_label,
                                 callback_data="cfgcyc:w:warn_action"),
        ]]
        if action == "mute":
            rows.append([
                InlineKeyboardButton(
                    text=_mark(await dur_h(h), h == hours),
                    callback_data=f"cfgk:w:hours:{h}",
                )
                for h in WARN_MUTE_HOURS
            ])
        rows.append(nav[0])
        kb = rows
        title = await t(chat_id, user_id, "cw_t")

    elif page == "f":
        action = await get_chat_setting(chat_id, "filter_action")
        opts = {
            "delete_warn": "fo_dw",
            "delete_mute": "fo_dm",
            "delete_only": "fo_do",
        }
        cur_label = await t(chat_id, user_id, opts.get(action, "fo_dw"))
        body = await t(chat_id, user_id, "cf2_hint") + f"\n<b>{cur_label}</b>\n\n" + \
            "/filteradd · /filterdel · /filterlist"
        kb = [[
            InlineKeyboardButton(
                text=_mark(await t(chat_id, user_id, k), action == v),
                callback_data=f"cfgk:f:filter_action:{v}",
            )
        ] for v, k in opts.items()]
        kb.append(nav[0])
        title = await t(chat_id, user_id, "cf2_t")

    elif page == "g":
        on = bool(await chat_int_setting(chat_id, "welcome_on", 0))
        tpl = await get_chat_setting(chat_id, "welcome_text")
        shown = html.escape(tpl[:400] + ("…" if len(tpl) > 400 else ""))
        body = "\n".join([
            await t(chat_id, user_id, "g_state") +
            f" <b>{await t(chat_id, user_id, 'st_on' if on else 'st_off')}</b>",
            "",
            await t(chat_id, user_id, "g_text_cur") + shown,
            "",
            "<i>" + html.escape(await t(chat_id, user_id, "cg_ph")) + "</i>",
        ])
        kb = [
            [InlineKeyboardButton(
                text=_mark(await t(chat_id, user_id, "st_on"), on) + " / " +
                _mark(await t(chat_id, user_id, "st_off"), not on),
                callback_data=f"cfgk:g:welcome_on:{0 if on else 1}",
            )],
            [
                InlineKeyboardButton(text="✏️", callback_data="cfgwelk"),
                InlineKeyboardButton(text="🧪", callback_data="cfgtest"),
            ],
            nav[0],
        ]
        title = await t(chat_id, user_id, "cg_t")

    elif page == "l":
        on = bool(await chat_int_setting(chat_id, "flood_on", 0))
        msgs = await chat_int_setting(chat_id, "flood_msgs", 5)
        secs = await chat_int_setting(chat_id, "flood_secs", 10)
        action = await get_chat_setting(chat_id, "flood_action")
        act_label = await t(
            chat_id, user_id,
            "fda_mute" if action != "kick" else "fda_kick",
        )
        mins = await chat_int_setting(chat_id, "flood_mute_min", 30)
        body = "\n".join([
            await t(chat_id, user_id, "g_state") +
            f" <b>{await t(chat_id, user_id, 'st_on' if on else 'st_off')}</b>",
            await t(chat_id, user_id, "fd_msgs", n=msgs),
            await t(chat_id, user_id, "fd_secs", n=secs),
            await t(chat_id, user_id, "fd_act") + f" <b>{act_label}</b>",
        ])
        if action != "kick":
            body += "\n" + await t(chat_id, user_id, "fd_dur") + \
                f" <b>{await dur_m(mins)}</b>"
        kb = [
            [InlineKeyboardButton(
                text=_mark(await t(chat_id, user_id, "st_on"), on) + " / " +
                _mark(await t(chat_id, user_id, "st_off"), not on),
                callback_data=f"cfgk:l:flood_on:{0 if on else 1}",
            )],
            [
                InlineKeyboardButton(text="➖", callback_data="cfgadj:l:flood_msgs:-1"),
                InlineKeyboardButton(text=f"💬 {msgs}", callback_data="cfg:l"),
                InlineKeyboardButton(text="➕", callback_data="cfgadj:l:flood_msgs:1"),
            ],
            [
                InlineKeyboardButton(text="➖", callback_data="cfgadj:l:flood_secs:-5"),
                InlineKeyboardButton(text=f"⏱ {secs}s", callback_data="cfg:l"),
                InlineKeyboardButton(text="➕", callback_data="cfgadj:l:flood_secs:5"),
            ],
            [InlineKeyboardButton(text=act_label,
                                  callback_data="cfgcyc:l:flood_action")],
        ]
        if action != "kick":
            kb.insert(4, [
                InlineKeyboardButton(
                    text=_mark(await dur_m(m), m == mins),
                    callback_data=f"cfgk:l:flood_mute_min:{m}",
                )
                for m in FLOOD_MUTE_MIN
            ])
        kb.append(nav[0])
        title = await t(chat_id, user_id, "cl_t")

    elif page == "a":
        cur = await get_chat_setting(chat_id, "lang")
        body = await t(chat_id, user_id, "cf_pick")
        kb = [
            [InlineKeyboardButton(
                text=_mark(await t(chat_id, user_id, "lang_ru"), cur != "en"),
                callback_data="cfgk:a:lang:ru",
            )],
            [InlineKeyboardButton(
                text=_mark(await t(chat_id, user_id, "lang_en"), cur == "en"),
                callback_data="cfgk:a:lang:en",
            )],
            nav[0],
        ]
        title = await t(chat_id, user_id, "cla_t")

    else:
        page = "m"
        text = (
            hd(await t(chat_id, user_id, "cf_title"))
            + "\n\n"
            + await t(chat_id, user_id, "cf_pick")
        )
        return text, InlineKeyboardMarkup(inline_keyboard=cats)

    text = hd(title) + "\n\n" + qt(body)
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


@router.message(GROUP_CHATS, Command("settings"))
async def cmd_settings(message: Message):
    cid = message.chat.id
    uid = message.from_user.id
    if not await can_configure(cid, uid):
        await message.reply(await t(cid, uid, "cfg_den"))
        return
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    text, kb = await settings_view(cid, uid, "m")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cfg"))
async def on_cfg_callback(callback: CallbackQuery):
    data = callback.data or ""
    chat_id = callback.message.chat.id if callback.message else None
    uid = callback.from_user.id
    if chat_id is None:
        await callback.answer()
        return
    if not await can_configure(chat_id, uid):
        await callback.answer(
            await t(chat_id, uid, "cfg_den"), show_alert=True
        )
        return

    parts = data.split(":")

    if parts[0] == "cfgwelk":
        _text_wait[uid] = (time.time() + 600, chat_id, "welcome")
        await callback.message.answer(
            await t(chat_id, uid, "prompt_wel")
        )
        await callback.answer()
        return

    if parts[0] == "cfgtest":
        tpl = await get_chat_setting(chat_id, "welcome_text")
        u = callback.from_user
        name = html.escape(u.full_name)
        mention = f'<a href="tg://user?id={u.id}">{name}</a>'
        ctitle = html.escape(callback.message.chat.title or str(chat_id))
        try:
            text = tpl.format_map(_SafeDict(name=mention, chat=ctitle, id=u.id))
        except Exception:
            text = tpl
        await callback.message.answer(text)
        await callback.answer(await t(chat_id, uid, "test_sent"))
        return

    if parts[0] == "cfgk" and len(parts) == 4:
        _, page_s, key, value = parts
        if key in ("warn_limit", "warn_mute_hours", "flood_on",
                   "flood_msgs", "flood_secs", "flood_mute_min",
                   "welcome_on"):
            if value.lstrip("-").isdigit():
                await set_chat_setting(chat_id, key, int(value))
        elif key in ("warn_action", "filter_action",
                     "flood_action", "welcome_text", "lang"):
            await set_chat_setting(chat_id, key, value)
        text, kb = await settings_view(chat_id, uid, page_s)
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    if parts[0] == "cfgadj" and len(parts) == 4:
        _, page_s, key, delta_s = parts
        if delta_s.lstrip("-").isdigit():
            delta = int(delta_s)
            if key == "limit":
                cur = await chat_int_setting(chat_id, "warn_limit", 3)
                await set_chat_setting(
                    chat_id, "warn_limit", min(10, max(1, cur + delta))
                )
            elif key == "flood_msgs":
                cur = await chat_int_setting(chat_id, "flood_msgs", 5)
                await set_chat_setting(
                    chat_id, "flood_msgs", min(20, max(3, cur + delta))
                )
            elif key == "flood_secs":
                cur = await chat_int_setting(chat_id, "flood_secs", 10)
                val = min(120, max(5, cur + delta))
                val = max(5, round(val / 5) * 5)
                await set_chat_setting(chat_id, "flood_secs", val)
        text, kb = await settings_view(chat_id, uid, page_s)
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    if parts[0] == "cfgcyc" and len(parts) == 3:
        _, page_s, key = parts
        if key == "warn_action":
            cycle = WARN_ACT_CYCLE
        else:
            cycle = FLOOD_ACT_CYCLE
        cur = await get_chat_setting(chat_id, key)
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else cycle[0]
        await set_chat_setting(chat_id, key, nxt)
        text, kb = await settings_view(chat_id, uid, page_s)
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    if parts[0] == "cfg" and len(parts) == 2:
        text, kb = await settings_view(chat_id, uid, parts[1])
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    await callback.answer()


# ─────────────────── Ввод текста приветствия ────────────────────────


class TextCaptureMiddleware(BaseMiddleware):
    """Ловит сообщение админа с текстом приветствия (после кнопки ✏️)."""

    async def __call__(self, handler, event: Message, data: dict):
        if isinstance(event, Message) and event.from_user:
            pending = _text_wait.get(event.from_user.id)
            if pending:
                expire, cid, kind = pending
                if event.chat.id == cid:
                    if time.time() > expire:
                        _text_wait.pop(event.from_user.id, None)
                    else:
                        text = event.text or ""
                        if text.startswith("/cancel"):
                            _text_wait.pop(event.from_user.id, None)
                            await event.answer(await t(cid, event.from_user.id, "cncl"))
                            return
                        if len(text) > 1000:
                            await event.answer(
                                await t(cid, event.from_user.id, "too_long")
                            )
                            return
                        _text_wait.pop(event.from_user.id, None)
                        await set_chat_setting(cid, "welcome_text", text)
                        await event.answer(await t(cid, event.from_user.id, "ok_wel"))
                        return
        return await handler(event, data)


# ───────────────────────────── Антифлуд ─────────────────────────────


_flood: dict[tuple[int, int], deque] = {}


class FloodMiddleware(BaseMiddleware):
    """Слайдинг-окно на пару (чат, юзер); мут/кик при превышении порога."""

    async def __call__(self, handler, event: Message, data: dict):
        if not isinstance(event, Message) or not event.text:
            return await handler(event, data)
        if event.chat.type not in ("group", "supergroup"):
            return await handler(event, data)
        u = event.from_user
        if u is None or u.is_bot:
            return await handler(event, data)
        cid, uid = event.chat.id, u.id
        if uid in DEVELOPERS or uid in ADMINS:
            return await handler(event, data)
        if await target_is_admin(cid, uid):
            return await handler(event, data)
        if await has_perm(cid, uid, "mute"):
            return await handler(event, data)
        if await get_user_role(cid, uid):
            return await handler(event, data)
        if not await chat_int_setting(cid, "flood_on", 0):
            return await handler(event, data)

        msgs_n = max(3, min(20, await chat_int_setting(cid, "flood_msgs", 5)))
        secs = max(5, min(120, await chat_int_setting(cid, "flood_secs", 10)))
        key = (cid, uid)
        dq = _flood.setdefault(key, deque())
        now = time.time()
        dq.append(now)
        while dq and dq[0] < now - secs:
            dq.popleft()
        if len(dq) < msgs_n:
            return await handler(event, data)

        dq.clear()
        action = await get_chat_setting(cid, "flood_action")
        lang = await get_lang(cid, uid)
        kw = dict(n=msgs_n, s=secs)
        try:
            if action == "kick":
                try:
                    await bot.ban_chat_member(cid, uid)
                    await bot.unban_chat_member(cid, uid, only_if_banned=True)
                except TelegramBadRequest:
                    pass
                await event.answer(await t(cid, uid, "fm_kicked", **kw))
                await log_action(f"#flood_kick chat={cid} user={uid}", cid)
            else:
                mins = await chat_int_setting(cid, "flood_mute_min", 30)
                until = now + mins * 60
                try:
                    await bot.restrict_chat_member(
                        cid,
                        uid,
                        permissions=ChatPermissions(),
                        until_date=datetime.now()
                        + timedelta(seconds=mins * 60),
                    )
                except TelegramBadRequest:
                    pass
                await add_punishment(cid, uid, "auto_mute", until)
                try:
                    await event.answer(await t(
                        cid, uid, "fm_muted",
                        dur=fmt_duration(mins * 60, lang), **kw,
                    ))
                except Exception:
                    pass
                asyncio.ensure_future(notify_user(
                    uid, "dm_mute_t",
                    chat=await _chat_title(cid),
                    dur=fmt_duration(mins * 60, await get_lang(None, uid)),
                ))
                await log_action(f"#flood_mute chat={cid} user={uid}", cid)
        except Exception as e:
            log.warning("Антифлуд: %s", e)
        try:
            await event.delete()
        except Exception:
            pass
        return


# ───────────────────── Личные настройки (/me) ───────────────────────


async def me_view(chat_id: int | None, user_id: int):
    cur_lang = await get_user_setting(user_id, "lang")
    notify = await get_user_setting(user_id, "notify")

    async def lang_btn(code: str, label_key: str) -> InlineKeyboardButton:
        active = cur_lang == code
        return InlineKeyboardButton(
            text=_mark(await t(chat_id, user_id, label_key), active),
            callback_data=f"mek:lang:{code}",
        )

    kb = [
        [
            await lang_btn("", "me_auto"),
            await lang_btn("ru", "lang_ru"),
            await lang_btn("en", "lang_en"),
        ],
        [InlineKeyboardButton(
            text=(
                await t(chat_id, user_id, "no_on")
                + ("✅" if notify == "on" else "")
                + " / "
                + await t(chat_id, user_id, "no_off")
                + ("✅" if notify != "on" else "")
            ),
            callback_data="mek:notif:toggle",
        )],
        [InlineKeyboardButton(text="🏠", callback_data="menu:back"),
         InlineKeyboardButton(text="✖️", callback_data="menu:close")],
    ]
    body = "\n".join([
        await t(chat_id, user_id, "me_lang") + f" <b>"
        + (await t(chat_id, user_id, "me_auto") if cur_lang == "" else
           "🇷🇺" if cur_lang == "ru" else "🇬🇧")
        + "</b>",
        "",
        await t(chat_id, user_id, "me_notif") +
        f" <b>{await t(chat_id, user_id, 'no_on' if notify == 'on' else 'no_off')}</b>",
        "",
        "<i>" + html.escape(await t(chat_id, user_id, "me_hint")) + "</i>",
    ])
    return hd(await t(chat_id, user_id, "me_t")) + "\n\n" + qt(body), \
        InlineKeyboardMarkup(inline_keyboard=kb)


@router.message(Command("me"))
async def cmd_me(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    cid = message.chat.id if message.chat.type != "private" else None
    text, kb = await me_view(cid, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("mek:"))
async def on_me_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    parts = (callback.data or "").split(":")
    if len(parts) == 3 and parts[1] == "lang":
        val = parts[2]
        if val in ("", "ru", "en"):
            await set_user_setting(uid, "lang", val)
    elif len(parts) == 3 and parts[1] == "notif":
        cur = await get_user_setting(uid, "notify")
        await set_user_setting(uid, "notify", "off" if cur == "on" else "on")
    cid = callback.message.chat.id if callback.message else None
    if cid is not None and callback.message.chat.type == "private":
        cid = None
    text, kb = await me_view(cid, uid)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()


# ───────────────────── Приветствие новичков ─────────────────────────


@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated):
    old, new = event.old_chat_member, event.new_chat_member
    if old.status not in ("left", "kicked"):
        return
    if new.status not in ("member", "administrator"):
        return
    u = new.user
    if u.is_bot:
        return
    cid = event.chat.id
    if not await chat_int_setting(cid, "welcome_on", 0):
        return
    tpl = await get_chat_setting(cid, "welcome_text")
    if not tpl:
        return
    name = html.escape(u.full_name)
    mention = f'<a href="tg://user?id={u.id}">{name}</a>'
    ctitle = html.escape(event.chat.title or str(cid))
    try:
        text = tpl.format_map(_SafeDict(name=mention, chat=ctitle, id=u.id))
    except Exception:
        text = tpl
    try:
        await bot.send_message(cid, text)
    except TelegramBadRequest as e:
        log.warning("Приветствие не отправлено (%s): %s", cid, e)


# ─────────────────── Авто-снятие истёкших наказаний ─────────────────


async def expiry_worker():
    while True:
        try:
            now = time.time()
            cur = await db.execute(
                "SELECT rowid AS rid, chat_id, user_id, type FROM punishments"
                " WHERE until<=?",
                (now,),
            )
            rows = await cur.fetchall()
            for row in rows:
                try:
                    cid = await resolve_chat_id(row["chat_id"])
                    if row["type"] == "ban":
                        await bot.unban_chat_member(
                            cid, row["user_id"], only_if_banned=True
                        )
                        await log_action(
                            f"#auto_unban chat={cid} user={row['user_id']}", cid
                        )
                    else:
                        perms = await default_permissions(cid)
                        await bot.restrict_chat_member(
                            cid, row["user_id"], permissions=perms
                        )
                        await log_action(
                            f"#auto_unmute chat={cid} user={row['user_id']}", cid
                        )
                except TelegramMigrateToChat as e:
                    try:
                        old = await resolve_chat_id(row["chat_id"])
                        await remap_chat(old, e.migrate_to_chat_id)
                        if row["type"] == "ban":
                            await bot.unban_chat_member(
                                e.migrate_to_chat_id,
                                row["user_id"],
                                only_if_banned=True,
                            )
                        else:
                            perms = await default_permissions(e.migrate_to_chat_id)
                            await bot.restrict_chat_member(
                                e.migrate_to_chat_id,
                                row["user_id"],
                                permissions=perms,
                            )
                        log.info("Отложенное снятие после ремапа: user=%s", row["user_id"])
                    except Exception as ex:
                        log.warning(
                            "Снятие после ремапа не удалось (%s): %s",
                            row["user_id"], ex,
                        )
                except Exception as e:
                    log.warning("Авто-снятие не удалось (%s): %s", row["user_id"], e)
            if rows:
                await db.execute("DELETE FROM punishments WHERE until<=?", (now,))
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка expiry_worker")
        await asyncio.sleep(60)


# ───────────────────────────── Запуск ───────────────────────────────


async def main() -> None:
    global bot
    if not BOT_TOKEN:
        sys.exit("Ошибка: заполни BOT_TOKEN в .env (пример в .env.example)")

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview_is_disabled=True
        ),
    )
    dp = Dispatcher()
    dp.include_router(router)
    # Порядок важен: сначала перехват ввода текста, затем кэш юзера, затем флуд.
    dp.message.middleware(TextCaptureMiddleware())
    dp.message.middleware(UserCacheMiddleware())
    dp.message.middleware(FloodMiddleware())

    await init_db()

    # Однократная проверка лог-канала при старте.
    global log_channel_ok
    if LOG_CHANNEL_ID:
        try:
            cid = await resolve_chat_id(LOG_CHANNEL_ID)
            await bot.send_message(cid, "✅ Бот запущен")
        except TelegramMigrateToChat as e:
            await remap_chat(LOG_CHANNEL_ID, e.migrate_to_chat_id)
            log.warning(
                "Лог-канал переехал в супергруппу %s, данные обновлены. "
                "Рекомендую вписать новый ID в LOG_CHANNEL_ID (.env).",
                e.migrate_to_chat_id,
            )
        except TelegramBadRequest as e:
            log_channel_ok = False
            log.warning(
                "Лог-канал недоступен (%s). Добавь бота админом в канал "
                "и укажи его ID (-100...) в LOG_CHANNEL_ID (.env).", e
            )

    worker = asyncio.create_task(expiry_worker())

    log.info("Бот запущен. Чатов в базе ждёт своих модераторов.")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "chat_member"],
        )
    finally:
        worker.cancel()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Остановлено.")
