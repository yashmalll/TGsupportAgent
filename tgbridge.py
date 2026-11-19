"""Owner-only Telegram interface to persistent tmux CLI sessions."""
import asyncio
import json
import logging
from html import escape
import os
from pathlib import Path
import re
import shlex
import shutil
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery,
                           KeyboardButton, ReplyKeyboardMarkup)
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
from console_output import ConsoleOutput

PREFIX = "tgcli-"
KEYS = {"Enter": "Enter", "↑": "Up", "↓": "Down", "Tab": "Tab", "Esc": "Escape", "Ctrl+C": "C-c"}
MENU_ACTIONS = {
    "🚀 Запустить Codex": "/startcli",
    "📂 Проекты": "/projects",
    "🖥 Сессии": "/sessions",
    "📺 Экран": "/attach",
    "🛑 Завершить сессию": "/stop",
    "❓ Помощь": "/help",
}


def main_menu():
    labels = list(MENU_ACTIONS)
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=label) for label in labels[i:i + 2]]
        for i in range(0, len(labels), 2)], resize_keyboard=True,
        is_persistent=True, input_field_placeholder="Задание для CLI или кнопка меню")


async def answer_callback(query, text=None, **kwargs):
    try:
        await query.answer(text, **kwargs)
        return True
    except TelegramBadRequest as exc:
        reason = str(exc).lower()
        if "query is too old" in reason or "query id is invalid" in reason:
            logging.info("Просроченное нажатие кнопки: %s", query.id if hasattr(query, "id") else "?")
            return False
        raise


HELP = """/menu — показать меню кнопок
/profiles — доступные CLI
/projects — проекты и кнопки запуска Codex
/startcli [имя] [проект или папка] — новая сессия
/startcli — Codex в домашней папке (~)
/sessions — список сессий
/switch имя — выбрать сессию
/attach — экран терминала
/stop имя — завершить сессию
Обычный текст отправляется в выбранный CLI с Enter.
Кнопки управляют выбранной сессией. Команды CLI можно отправить текстом /send текст.
Вывод дополняется в одном сообщении; длинные ответы продолжаются в следующих.
/attach повторно показывает текущий экран."""


async def tmux(*args):
    proc = await asyncio.create_subprocess_exec("tmux", "-L", "tgbridge", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(err.decode(errors="replace").strip() or "Ошибка tmux")
    return out.decode(errors="replace")


def resolve_cli(command):
    """Resolve executables without depending on an interactive shell's PATH."""
    expanded = os.path.expanduser(command)
    found = shutil.which(expanded)
    if found or "/" in expanded:
        return found
    home = Path.home()
    directories = [home / ".local/bin", home / ".npm-global/bin",
                   home / ".npm/bin", home / ".volta/bin", home / ".cargo/bin"]
    directories += sorted((home / ".nvm/versions/node").glob("*/bin"), reverse=True)
    directories += sorted((home / ".local/share/fnm/node-versions").glob("*/installation/bin"), reverse=True)
    for directory in directories:
        found = shutil.which(command, path=str(directory))
        if found:
            return found
    return None


class Bridge:
    def __init__(self, bot, owner, profiles, secrets, interval, projects_dir=None):
        self.bot, self.owner, self.profiles = bot, owner, profiles
        self.secrets, self.interval = secrets, interval
        self.active = None
        self.projects_dir = Path(projects_dir).expanduser().resolve() if projects_dir else None
        self.project_choices = {}
        self.streams = {}
        self.outboxes = {}
        self.output_messages = {}
        self.output_dir = Path(__file__).resolve().parent / ".tgbridge-output"
        self.finished = set()
        self.lock = asyncio.Lock()

    def clean(self, text):
        for value in self.secrets:
            if value:
                text = text.replace(value, "[REDACTED]")
        return text

    async def announce_startup(self):
        await self.bot.send_message(self.owner,
            "🤖 Бот запущен и готов к работе.\n\n"
            "🚀 Запустить Codex — открыть новую сессию в домашней папке.\n"
            "📂 Проекты — выбрать папку и запустить Codex в ней.\n"
            "🖥 Сессии — вернуться к ранее открытой сессии.\n\n"
            "Выбери действие кнопками ниже.", reply_markup=main_menu())

    def projects(self):
        if self.projects_dir is None:
            return []
        if not self.projects_dir.is_dir():
            raise ValueError("PROJECTS_DIR: папка с проектами не существует.")
        return sorted((p for p in self.projects_dir.iterdir()
                       if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.casefold())

    def working_directory(self, value=None):
        if value is None:
            return Path.home()
        # A simple project name is resolved inside the configured projects folder.
        if self.projects_dir and value not in (".", "..") and "/" not in value and not value.startswith("~"):
            path = self.projects_dir / value
        else:
            path = Path(value).expanduser()
        path = path.resolve()
        if not path.is_dir():
            raise ValueError("Рабочая папка не существует. Посмотри /projects.")
        return path

    async def show_projects(self, message):
        projects = self.projects()
        if not projects:
            await message.answer("📂 Проекты не найдены. Укажи PROJECTS_DIR в .env и перезапусти бота."
                                 "\n/startcli codex — запуск в домашней папке.")
            return
        self.project_choices = {uuid.uuid4().hex[:12]: path for path in projects}
        choices = list(self.project_choices.items())
        for offset in range(0, len(choices), 40):
            rows = [[InlineKeyboardButton(text="📁 " + path.name,
                     callback_data="project:" + key)] for key, path in choices[offset:offset + 40]]
            await message.answer("📂 <b>Выбери проект</b>\nНажми на папку, чтобы запустить Codex."
                                 "\n<code>" + escape(self.clean(str(self.projects_dir))) + "</code>",
                                 parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def project(self, query: CallbackQuery):
        if query.from_user.id != self.owner or not query.message or query.message.chat.id != self.owner:
            await answer_callback(query)
            return
        path = self.project_choices.get(query.data.partition(":")[2])
        if path is None:
            await answer_callback(query, "Список устарел. Открой /projects ещё раз.", show_alert=True)
            return
        if not await answer_callback(query, "Запускаю Codex…"):
            return
        # Reuse the same authorization, validation and session lock as text commands.
        message = query.message.model_copy(update={"from_user": query.from_user,
                    "text": "/startcli codex " + shlex.quote(str(path))})
        await self.handle(message)

    async def names(self):
        try:
            result = await tmux("list-sessions", "-F", "#{session_name}")
        except RuntimeError as exc:
            if any(s in str(exc) for s in ("no server running", "No such file", "no sessions")):
                return []
            raise
        return [n for n in result.splitlines() if n.startswith(PREFIX)]

    async def target(self, name=None):
        name = name or self.active
        if not name or name not in await self.names():
            raise ValueError("Выбери существующую сессию через /sessions и /switch имя.")
        return name + ":0.0"

    async def session_directory(self, name):
        """The session's original directory survives bot and CLI restarts in tmux."""
        try:
            path = (await tmux("display-message", "-p", "-t", name + ":0.0",
                               "#{session_path}")).strip()
        except RuntimeError:
            return None
        return self.clean(path) or None

    def project_label(self, directory):
        if not directory:
            return "Папка неизвестна"
        if directory == str(Path.home()):
            return "Домашняя папка"
        return Path(directory).name or directory

    def keyboard(self):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=k, callback_data=f"key:{self.active}:{k}") for k in row]
            for row in (("↑", "↓", "Enter"), ("Tab", "Esc", "Ctrl+C"))])

    async def screen(self, target):
        return (await tmux("capture-pane", "-p", "-J", "-S", "-150", "-t", target)).rstrip()

    async def output(self, text, controls=False):
        text = self.clean(text) or "(пустой экран)"
        for i in range(0, len(text), 3800):
            await self.bot.send_message(self.owner, text[i:i+3800],
                reply_markup=self.keyboard() if controls and i + 3800 >= len(text) else None)

    async def connect_output(self, name, seed=True):
        target = await self.target(name)
        size = (await tmux("display-message", "-p", "-t", target,
                           "#{pane_width} #{pane_height}")).split()
        reader = ConsoleOutput(int(size[0]), int(size[1]))
        self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.output_dir / (name + ".bin")
        path.touch(mode=0o600, exist_ok=True)
        # Reconnect at EOF after restart; capture the existing screen once.
        reader.offset = path.stat().st_size
        await tmux("pipe-pane", "-t", target, "cat >> " + shlex.quote(str(path)))
        if seed:
            reader.feed((await self.screen(target)).replace("\n", "\r\n").encode())
        self.streams[name] = reader

    async def refresh(self, name, force=False):
        target = await self.target(name)
        if name not in self.streams:
            await self.connect_output(name)
        reader = self.streams[name]
        await self.deliver_output(name)
        if force:
            self.output_messages.pop(name, None)
        path = self.output_dir / (name + ".bin")
        with path.open("rb") as source:
            source.seek(reader.offset)
            # Drain the recorded stream, including output that scrolled off screen.
            while data := source.read(65536):
                reader.feed(data)
            reader.offset = source.tell()
        status = (await tmux("display-message", "-p", "-t", target,
                             "#{pane_dead}:#{pane_dead_status}")).strip()
        dead, _, code = status.partition(":")
        text, current = reader.pending(force=force)
        text = self.clean(text)
        chunks, chunk, units = [], "", 0
        for char in text:
            size = 2 if ord(char) > 0xffff else 1
            if units + size > 3200:
                chunks.append(chunk)
                chunk, units = "", 0
            chunk += char
            units += size
        if chunk:
            chunks.append(chunk)
        queue = self.outboxes.setdefault(name, [])
        queue.extend((chunk, dead != "1" and index == len(chunks) - 1)
                     for index, chunk in enumerate(chunks))
        reader.acknowledge(current)
        await self.deliver_output(name)
        if force and not chunks:
            await self.output("(пустой экран)", controls=dead != "1")
        if dead == "1" and name not in self.finished:
            await self.output("⚫ CLI завершился · код " + (code or "?") + " · " + name)
            self.finished.add(name)

    async def deliver_output(self, name):
        queue = self.outboxes.setdefault(name, [])
        while queue:
            chunk, controls = queue[0]
            previous = self.output_messages.get(name)
            combined = previous[1] + "\n" + chunk if previous else chunk
            merge = previous is not None and len(combined.encode('utf-16-le')) // 2 <= 3200
            body = combined if merge else chunk
            formatted = "<code>" + escape(name) + "</code>\n<pre>" + escape(body) + "</pre>"
            markup = self.keyboard() if controls else None
            if merge:
                try:
                    await self.bot.edit_message_text(formatted, chat_id=self.owner,
                        message_id=previous[0], parse_mode="HTML", reply_markup=markup)
                except TelegramBadRequest as exc:
                    reason = str(exc).lower()
                    if "message is not modified" not in reason:
                        if "message to edit not found" in reason or "message can't be edited" in reason:
                            self.output_messages.pop(name, None)
                            continue
                        raise
                message_id = previous[0]
            else:
                sent = await self.bot.send_message(self.owner, formatted,
                    parse_mode="HTML", reply_markup=markup, disable_notification=True)
                message_id = sent.message_id
            self.output_messages[name] = (message_id, body)
            queue.pop(0)

    async def handle(self, message: Message):
        if message.from_user is None or message.from_user.id != self.owner or message.chat.id != self.owner:
            return
        if not message.text:
            return
        try:
            async with self.lock:
                text = MENU_ACTIONS.get(message.text, message.text)
                # Keep replies below the user's latest message in the conversation.
                self.output_messages.clear()
                command, _, arg = text.partition(" ")
                command = command.split("@")[0]
                if command in ("/start", "/help", "/menu"):
                    await message.answer("🤖 <b>CLI · Telegram</b>\n\n" + escape(HELP), parse_mode="HTML", reply_markup=main_menu())
                    if command == "/start" and self.projects_dir:
                        await self.show_projects(message)
                elif command == "/projects":
                    await self.show_projects(message)
                elif command == "/profiles":
                    await message.answer("Профили: " + ", ".join(self.profiles))
                elif command == "/startcli":
                    parts = shlex.split(arg) or ["codex"]
                    if not parts or len(parts) > 2 or parts[0] not in self.profiles:
                        raise ValueError('/startcli профиль ["/путь с пробелами"]')
                    profile = self.profiles[parts[0]]
                    cwd = self.working_directory(parts[1] if len(parts) == 2 else None)
                    if not cwd.is_dir():
                        raise ValueError("Рабочая папка не существует.")
                    argv = profile["command"]
                    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                        raise ValueError("command должен быть непустым массивом строк.")
                    executable = resolve_cli(argv[0])
                    if not executable:
                        raise ValueError(
                            "CLI не найден: " + argv[0] +
                            "\nВ терминале, где CLI работает, выполни: command -v " + shlex.quote(argv[0]) +
                            "\nУкажи полученный абсолютный путь первым элементом command в profiles.json "
                            "и перезапусти бота. Если команда не найдена и там, CLI нужно установить.")
                    argv = [executable, *argv[1:]]
                    name = PREFIX + uuid.uuid4().hex[:10]
                    # A single shell command is required by tmux; quote each configured argument.
                    await tmux("new-session", "-d", "-s", name, "-x", "100", "-y", "35", "-c", str(cwd))
                    await tmux("set-option", "-w", "-t", name + ":0", "remain-on-exit", "on")
                    await self.connect_output(name, seed=False)
                    await tmux("respawn-pane", "-k", "-t", name + ":0.0", "-c", str(cwd), shlex.join(["env", "PATH=" + str(Path(executable).parent) + os.pathsep + os.environ.get("PATH", os.defpath), *argv]))
                    self.active = name
                    await message.answer("🚀 <b>" + escape(parts[0]) + " запущен</b>\n📂 <code>"
                                         + escape(self.clean(str(cwd))) + "</code>\n🔖 <code>" + name + "</code>",
                                         parse_mode="HTML", reply_markup=self.keyboard())
                elif command == "/sessions":
                    names = await self.names()
                    if not names:
                        await message.answer("🖥 Нет сессий. Нажми «🚀 Запустить Codex» или выбери проект.",
                                             reply_markup=main_menu())
                    for offset in range(0, len(names), 40):
                        rows = []
                        for n in names[offset:offset + 40]:
                            directory = await self.session_directory(n)
                            label = self.project_label(directory)
                            rows.append([InlineKeyboardButton(
                                text=("🟢 " if n == self.active else "📁 ") + label[:60] + " · " + n,
                                callback_data="session:" + n)])
                        await message.answer("🖥 <b>Сессии</b>\nНа кнопках указаны папки, в которых открыты сессии."
                                             "\nНажми, чтобы переключиться.", parse_mode="HTML",
                                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                elif command == "/switch":
                    if not arg.strip():
                        raise ValueError("Укажи имя: /switch tgcli-имя")
                    await self.target(arg.strip())
                    self.active = arg.strip()
                    directory = await self.session_directory(self.active)
                    await message.answer("✅ <b>Выбрана сессия · " + escape(self.project_label(directory))
                                         + "</b>\n📂 Открыта в: <code>" + escape(directory or "Папка неизвестна")
                                         + "</code>\n🔖 <code>" + escape(self.active) + "</code>", parse_mode="HTML")
                    await self.refresh(self.active, force=True)
                elif command == "/stop":
                    name = arg.strip() or self.active
                    await self.target(name)
                    await tmux("kill-session", "-t", name)
                    self.streams.pop(name, None)
                    self.outboxes.pop(name, None)
                    self.output_messages.pop(name, None)
                    (self.output_dir / (name + ".bin")).unlink(missing_ok=True)
                    self.finished.discard(name)
                    if self.active == name:
                        self.active = None
                    await message.answer("Сессия завершена: " + name)
                elif command == "/attach":
                    await self.refresh(self.active, force=True)
                else:
                    payload = arg if command == "/send" else text
                    if text.startswith("/") and command != "/send":
                        raise ValueError("Неизвестная команда. Для отправки в CLI: /send текст")
                    target = await self.target()
                    await tmux("send-keys", "-t", target, "-l", "--", payload)
                    await tmux("send-keys", "-t", target, "Enter")
        except (ValueError, RuntimeError) as exc:
            await self.output(str(exc))

    async def session(self, query: CallbackQuery):
        if query.from_user.id != self.owner or not query.message or query.message.chat.id != self.owner:
            await answer_callback(query)
            return
        if not await answer_callback(query):
            return
        name = query.data.partition(":")[2]
        message = query.message.model_copy(update={"from_user": query.from_user,
                                                  "text": "/switch " + name})
        await self.handle(message)

    async def key(self, query: CallbackQuery):
        if query.from_user.id != self.owner or not query.message or query.message.chat.id != self.owner:
            await answer_callback(query)
            return
        # Telegram's callback deadline must not include time spent waiting for output delivery.
        if not await answer_callback(query):
            return
        try:
            async with self.lock:
                _, name, key = query.data.split(":", 2)
                if name != self.active or key not in KEYS:
                    raise ValueError("Кнопка от другой сессии. Используй /attach.")
                await tmux("send-keys", "-t", await self.target(name), KEYS[key])
        except (ValueError, RuntimeError) as exc:
            await self.output(str(exc))

    async def watch(self):
        while True:
            try:
                async with self.lock:
                    if self.active:
                        name = self.active
                        if name not in await self.names():
                            self.active = None
                            await self.output("Сессия завершилась: " + name)
                        else:
                            if name not in self.finished:
                                await self.refresh(name)
            except Exception:
                logging.exception("Не удалось переслать экран")
            await asyncio.sleep(self.interval)


async def run():
    load_dotenv()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    owner = int(os.environ["TELEGRAM_OWNER_ID"])
    if owner <= 0 or not token:
        raise ValueError("Укажи токен и положительный Telegram ID владельца.")
    if not shutil.which("tmux"):
        raise RuntimeError("Установи tmux перед запуском.")
    profiles = json.loads(Path(os.getenv("CLI_PROFILES", "profiles.json")).read_text())
    secrets = [v for k, v in os.environ.items() if re.search(r"TOKEN|SECRET|PASSWORD|API_KEY", k, re.I) and len(v) >= 6]
    interval = max(1, float(os.getenv("POLL_SECONDS", "2")))
    projects_dir = os.getenv("PROJECTS_DIR", "").strip()
    async with Bot(token) as bot:
        bridge = Bridge(bot, owner, profiles, secrets, interval, projects_dir)
        dp = Dispatcher()
        dp.message.register(bridge.handle)
        dp.callback_query.register(bridge.session, F.data.startswith("session:"))
        dp.callback_query.register(bridge.project, F.data.startswith("project:"))
        dp.callback_query.register(bridge.key, F.data.startswith("key:"))
        async def on_startup():
            try:
                await bridge.announce_startup()
            except Exception:
                # An unavailable owner chat must not prevent the bot from starting.
                logging.exception("Не удалось отправить меню при запуске бота")
        dp.startup.register(on_startup)
        task = asyncio.create_task(bridge.watch())
        try:
            await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main():
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
