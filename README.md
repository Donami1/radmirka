<div align="center">

<img src="https://img.shields.io/badge/🤖-Radmirka%20Helper-667eea?style=for-the-badge&logo=telegram&logoColor=white" alt="Radmirka Helper Bot"/>

# Radmirka Helper Bot

**Telegram group moderation bot with advanced administration tools**

**Telegram-бот для администрирования и модерации групп**

---

<img src="https://img.shields.io/badge/Python-3.14-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python"/>
<img src="https://img.shields.io/badge/Aiogram-3.x-26A5E4?style=flat-square&logo=telegram&logoColor=white" alt="Aiogram"/>
<img src="https://img.shields.io/badge/SQLite-WAL-003B57?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite"/>
<img src="https://img.shields.io/badge/Language-RU%20%7C%20EN-blueviolet?style=flat-square" alt="Languages"/>

<br/>

<a href="https://t.me/radmirka_helper_bot">
<img src="https://img.shields.io/badge/🚀%20Try%20Bot-@radmirka__helper__bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white" alt="Open Bot"/>
</a>

</div>

---

## Features / Возможности

<div align="center">
<table>
<tr>
<td align="center" width="33%">

### 🔨 Moderation
**Модерация**

`/ban` `/mute` `/kick`
`/unban` `/unmute`

Полный набор инструментов для модерации с поддержкой таймеров и причин

*Full moderation toolkit with timer and reason support*

</td>
<td align="center" width="33%">

### ⚠️ Warn System
**Система предупреждений**

`/warn` `/warns` `/unwarn`

Автоматическая эскалация при достижении лимита

*Auto-escalation when warn limit is reached*

</td>
<td align="center" width="33%">

### 🛡️ Custom Roles
**Кастомные роли**

`/roleadd` `/roledel` `/roleperms`

Система ролей с иерархией рангов и настраиваемыми правами

*Role system with rank hierarchy and custom permissions*

</td>
</tr>
<tr>
<td align="center">

### 🔤 Word Filters
**Фильтрация слов**

`/filteradd` `/filterdel`

Авто-удаление запрещённых слов с наказанием

*Auto-delete banned words with punishment*

</td>
<td align="center">

### 🎉 Welcome Messages
**Приветственные сообщения**

Настраиваемые приветствия с плейсхолдерами `{name}`, `{chat}`, `{id}`

*Customizable greetings with placeholders*

</td>
<td align="center">

### 🌊 Anti-Flood
**Антифлуд**

Скользящее окно для обнаружения флуда с автоматическим наказанием

*Sliding-window flood detection with auto-punishment*

</td>
</tr>
<tr>
<td align="center">

### 📜 Chat Rules
**Правила чата**

`/rules` `/rules set`

Настраиваемые правила чата через inline-меню

*Configurable chat rules via inline menu*

</td>
<td align="center">

### 📊 Logging
**Логирование**

Каждое действие логируется в файл и Telegram-канал

*Every action logged to file and Telegram channel*

</td>
<td align="center">

### 👤 User Profile
**Профиль пользователя**

`/me` `/menu`

Статистика, роль, предупреждения, настройки

*Stats, role, warns, personal settings*

</td>
</tr>
<tr>
<td align="center">

### 🌐 i18n
**Интернационализация**

Полная поддержка **RU** и **EN**

Язык на уровне чата и пользователя

*Full **RU** and **EN** support*

</td>
<td align="center">

### ⚙️ Settings Panel
**Панель настроек**

`/settings`

Интерактивные inline-кнопки для всех настроек

*Interactive inline buttons for all settings*

</td>
<td align="center">

### ⏰ Auto-Unban
**Авто-разбан**

Фоновая задача автоматически снимает истёкшие наказания

*Background task automatically lifts expired punishments*

</td>
</tr>
</table>
</div>

---

## Tech Stack / Технологии

<div align="center">

| Technology | Purpose / Назначение |
|:---:|:---|
| <img src="https://img.shields.io/badge/Python-3.14-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python"/> | Core language / Основной язык |
| <img src="https://img.shields.io/badge/Aiogram-3.x-26A5E4?style=flat-square&logo=telegram&logoColor=white" alt="Aiogram"/> | Async Telegram Bot API framework / Асинхронный фреймворк для Telegram Bot API |
| <img src="https://img.shields.io/badge/Aiosqlite-0.20+-003B57?style=flat-square&logo=sqlite&logoColor=white" alt="Aiosqlite"/> | Async SQLite driver / Асинхронный драйвер SQLite |
| <img src="https://img.shields.io/badge/Dotenv-1.0+-ECD53F?style=flat-square&logo=dotenv&logoColor=black" alt="Dotenv"/> | Environment config / Конфигурация через .env |

</div>

---

## Quick Start / Быстрый старт

### 1. Clone the repository / Клонируйте репозиторий

```bash
git clone https://github.com/your-username/radmirka.git
cd radmirka
```

### 2. Set up environment / Настройте окружение

```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Fill in `BOT_TOKEN` in `.env` — get it from [@BotFather](https://t.me/BotFather)

Заполните `BOT_TOKEN` в `.env` — получите его у [@BotFather](https://t.me/BotFather)

### 3. Install dependencies / Установите зависимости

```bash
pip install -r requirements.txt
```

### 4. Run the bot / Запустите бота

```bash
python bot.py
```

---

## Configuration / Конфигурация

The bot is configured via `.env` file:

Бот настраивается через файл `.env`:

| Variable / Переменная | Required / Обязательно | Description / Описание |
|:---|:---:|:---|
| `BOT_TOKEN` | ✅ | Telegram Bot API token from @BotFather |
| `DEVELOPER_ID` | ❌ | Telegram user ID of the bot developer (infinite rank, full access) / ID разработчика (бесконечный ранг) |
| `GLOBAL_ADMIN_IDS` | ❌ | Comma-separated Telegram IDs with rank 950 / ID глобальных администраторов через запятую |
| `LOG_CHANNEL_ID` | ❌ | Telegram chat ID for logging actions / ID канала для логирования |
| `LOG_FILE` | ❌ | Path to log file / Путь к файлу логов |

---

## Commands / Команды

### Moderation / Модерация

| Command | Description | Описание |
|:---|:---|:---|
| `/ban [target] [duration] [reason]` | Ban a user | Забанить пользователя |
| `/unban [target]` | Unban a user | Разбанить пользователя |
| `/mute [target] [duration] [reason]` | Mute a user | Замутить пользователя |
| `/unmute [target]` | Unmute a user | Размутить пользователя |
| `/kick [reason]` | Kick a user | Выгнать пользователя |
| `/warn [reason]` | Issue a warning | Выдать предупреждение |
| `/warns` | View warnings | Посмотреть предупреждения |
| `/unwarn` | Remove last warning | Убрать последнее предупреждение |

### Management / Управление

| Command | Description | Описание |
|:---|:---|:---|
| `/settings` | Open settings panel | Открыть панель настроек |
| `/roleadd` | Create a custom role | Создать кастомную роль |
| `/roledel` | Delete a role | Удалить роль |
| `/roleperms` | Configure role permissions | Настроить права роли |
| `/rolelist` | List all roles | Список всех ролей |
| `/filteradd <word>` | Add a word filter | Добавить фильтр слов |
| `/filterdel <word>` | Remove a word filter | Удалить фильтр слов |
| `/filterlist` | List all filters | Список фильтров |
| `/rules` | View chat rules | Просмотр правил чата |
| `/rules set <text>` | Set chat rules | Установить правила чата |
| `/rules clear` | Clear chat rules | Очистить правила чата |
| `/logchannel` | Set log channel | Установить канал логов |

### Utility / Утилиты

| Command | Description | Описание |
|:---|:---|:---|
| `/menu` or `/start` | Open main menu | Открыть главное меню |
| `/me` | View your profile | Посмотреть свой профиль |
| `/help` | Show help (4 pages) | Справка (4 страницы) |
| `/info` | Bot information | Информация о боте |

---

## Architecture / Архитектура

```
bot.py (single file / один файл)
├── Database Layer       — SQLite with WAL mode (10 tables)
├── i18n System          — ~200 keys × 2 languages (RU, EN)
├── Middleware Chain     — TextCapture → UserCache → Flood
├── Handlers             — Commands, callbacks, messages
├── Role System          — Hierarchical permissions (9 flags, rank-based)
├── Warn System          — Auto-escalation (mute/ban/kick/reset)
├── Anti-Flood           — Sliding-window detection
├── Punishment Worker    — Background task, checks every 60s
└── Migration Handler    — Group → Supergroup data remapping
```

<div align="center">

### Permission Hierarchy / Иерархия прав

```
Developer (∞) → Global Admin (950) → Creator (900) → Admin (850) → Custom Roles (0-800) → Member (0)
```

</div>

---

## Database Schema / Схема базы данных

| Table | Purpose / Назначение |
|:---|:---|
| `warns` | User warnings per chat |
| `punishments` | Active bans and mutes with expiry |
| `filters` | Banned words per chat |
| `users` | User data and message counts |
| `rules` | Chat rules text |
| `chat_migrations` | Group → supergroup migration tracking |
| `roles_def` | Custom role definitions |
| `user_roles` | User-role assignments |
| `log_channels` | Per-chat log channel settings |
| `chat_settings` | Per-chat configuration |
| `user_settings` | Per-user preferences (language, DM notifications) |

---

<div align="center">

### License / Лицензия

This project is provided as-is for educational purposes.

Проект предоставлен как есть в образовательных целях.

---

<img src="https://img.shields.io/badge/Made%20with%20%E2%9D%A4%20for%20Radmirka-764ba2?style=for-the-badge" alt="Made with love"/>

</div>
