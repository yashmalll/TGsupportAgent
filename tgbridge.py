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
import time
import sys
from datetime import datetime
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery,
                           KeyboardButton, ReplyKeyboardMarkup, BufferedInputFile)
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
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
    "✍️ Составить задание": "/draft",
    "📨 Отправить задание": "/submit",
    "⚙️ Вид вывода": "/settings",
    "📄 Лог": "/log",
    "↩️ Продолжить": "/resume",
    "🚫 Отменить задание": "/cancel",
    "🤖 Выбрать CLI": "/cli",
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
/cli — выбрать CLI, включая Kimi и agy
/draft, /submit, /cancel — составить, отправить или отменить задание
/settings — режим отображения
/log — скачать сохранённый вывод
/resume — последняя сессия
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
                   home / ".npm/bin", home / ".volta/bin", home / ".cargo/bin", home / ".kimi-code/bin"]
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
        self.agent_choices = {}
        self.streams = {}
        self.outboxes = {}
        self.output_messages = {}
        self.output_dir = Path(__file__).resolve().parent / ".tgbridge-output"
        self.finished = set()
        self.lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.mode = "compact"
        self.metadata = {}
        self.draft = None
        self.waiting = {}
        self.dead_observed = {}
        self.offsets = {}
        self.retry_at = 0
        self.durable = False

    def save_state(self):
        if not self.durable:
            return
        self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = dict(active=self.active, mode=self.mode, metadata=self.metadata,
                    draft=self.draft, waiting=self.waiting, offsets=self.offsets,
                    outboxes=self.outboxes, finished=list(self.finished))
        temp = self.output_dir / "state.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False))
        temp.chmod(0o600)
        temp.replace(self.output_dir / "state.json")

    def load_state(self):
        self.durable = True
        try:
            data = json.loads((self.output_dir / "state.json").read_text())
            if not isinstance(data, dict) or data.get('mode', 'compact') not in ('compact', 'full', 'notify'):
                raise ValueError('Некорректный файл состояния')
            for key in ('metadata', 'waiting', 'offsets', 'outboxes'):
                if key in data and not isinstance(data[key], dict):
                    raise ValueError('Некорректное поле состояния: ' + key)
            self.finished = set(data.get('finished', []))
            for key in ('active', 'mode', 'metadata', 'draft', 'waiting', 'offsets', 'outboxes'):
                if key in data:
                    setattr(self, key, data[key])
        except FileNotFoundError:
            pass
        except (ValueError, TypeError):
            logging.exception("Не удалось восстановить состояние")

    def enqueue(self, name, text, controls=False, notify=False):
        self.outboxes.setdefault(name, []).append((text, controls, notify))

    async def session_label(self, name):
        directory = await self.session_directory(name)
        meta = self.metadata.get(name, {})
        status = (await tmux('display-message', '-p', '-t', name + ':0.0',
                             '#{pane_dead}|#{pane_current_command}|#{session_created}')).strip().split('|')
        cli = meta.get('cli') or (status[1] if len(status) > 1 else 'CLI')
        created = meta.get('created') or (status[2] if len(status) > 2 else '')
        try:
            clock = datetime.fromtimestamp(float(created)).strftime('%d.%m %H:%M')
        except (ValueError, TypeError):
            clock = ''
        state = 'завершена' if status[0] == '1' else ('ждёт действия' if self.waiting.get(name) else 'работает')
        return f'{self.project_label(directory)[:40]} · {cli} · {clock} · {state}'


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
        if self.active and self.active in await self.names():
            await self.bot.send_message(self.owner, '↩️ Последняя сессия: ' + await self.session_label(self.active),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text='Продолжить', callback_data='session:' + self.active)]]))
        else:
            self.active = None
        self.save_state()

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
        self.agent_choices.clear()
        self.project_choices = {uuid.uuid4().hex[:12]: path for path in projects}
        choices = list(self.project_choices.items())
        for offset in range(0, len(choices), 40):
            rows = [[InlineKeyboardButton(text="📁 " + path.name,
                     callback_data="project:" + key)] for key, path in choices[offset:offset + 40]]
            await message.answer("📂 <b>Выбери проект</b>\nНажми на папку, затем выбери агента."
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
        if not await answer_callback(query):
            return
        rows = []
        for profile in self.profiles:
            token = uuid.uuid4().hex[:12]
            self.agent_choices[token] = (path, profile)
            rows.append([InlineKeyboardButton(text=profile, callback_data='agent:' + token)])
        await query.message.answer('🤖 Выбери агента для проекта <b>' + escape(self.clean(path.name))
                                   + '</b>\n📂 <code>' + escape(self.clean(str(path))) + '</code>',
                                   parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def agent(self, query):
        if query.from_user.id != self.owner or not query.message or query.message.chat.id != self.owner:
            await answer_callback(query)
            return
        choice = self.agent_choices.get(query.data.partition(':')[2])
        if choice is None:
            await answer_callback(query, 'Список устарел. Открой /projects.', show_alert=True)
            return
        if not await answer_callback(query, 'Запускаю агента…'):
            return
        path, profile = choice
        message = query.message.model_copy(update={'from_user': query.from_user,
            'text': '/startcli ' + shlex.quote(profile) + ' ' + shlex.quote(str(path))})
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

    def keyboard(self, name=None):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=k, callback_data=f"key:{name or self.active}:{k}") for k in row]
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
        if name not in self.offsets:
            self.offsets[name] = {str(path.stat().st_ino): reader.offset}
        else:
            seed = False
        await tmux("pipe-pane", "-t", target, shlex.join([sys.executable, str(Path(__file__).with_name("stream_recorder.py")), str(path)]))
        if seed:
            reader.feed((await self.screen(target)).replace("\n", "\r\n").encode())
        else:
            def restore_terminal():
                for segment in (path.with_suffix('.previous'), path):
                    try:
                        with segment.open('rb') as source:
                            committed = self.offsets[name].get(str(os.fstat(source.fileno()).st_ino), 0)
                            while committed > 0 and (data := source.read(min(65536, committed))):
                                reader.feed(data)
                                committed -= len(data)
                    except FileNotFoundError:
                        continue
                reader.acknowledge(reader.pending()[1])
            await asyncio.to_thread(restore_terminal)
        self.streams[name] = reader

    async def refresh(self, name, force=False):
        target = await self.target(name)
        if name not in self.streams:
            await self.connect_output(name)
        reader = self.streams[name]
        if force:
            self.output_messages.pop(name, None)
        path = self.output_dir / (name + ".bin")
        # Stop consuming when delivery falls behind; raw logs have bounded retention.
        if sum(len(item[0]) for item in self.outboxes.get(name, [])) > 512_000 and not force:
            return
        positions = dict(self.offsets.get(name, {}))
        def read_recorded():
            drained = True
            for segment in (path.with_suffix('.previous'), path):
                try:
                    with segment.open('rb') as source:
                        inode = str(os.fstat(source.fileno()).st_ino)
                        source.seek(positions.get(inode, 0))
                        budget = 256 * 1024
                        while budget > 0 and (data := source.read(min(65536, budget))):
                            reader.feed(data)
                            budget -= len(data)
                        positions[inode] = source.tell()
                        if source.tell() < os.fstat(source.fileno()).st_size:
                            drained = False
                            break
                except FileNotFoundError:
                    continue
            alive = {str(p.stat().st_ino) for p in (path.with_suffix('.previous'), path) if p.exists()}
            for inode in list(positions):
                if inode not in alive:
                    del positions[inode]
            return drained
        drained = await asyncio.to_thread(read_recorded)
        status = (await tmux("display-message", "-p", "-t", target,
                             "#{pane_dead}:#{pane_dead_status}")).strip()
        dead, _, code = status.partition(":")
        text, current = await asyncio.to_thread(reader.pending, force=force, raw=self.mode == "full")
        text = self.clean(text)
        prompt = next((line.strip() for line in current[-12:] if re.search(
            r'(?i)(\[y/n\]|\[yes/no\]|would you like|do you want|press enter|allow once|approve|подтверд|разрешить|нажмите enter)', line)), '')
        if prompt and prompt != self.waiting.get(name):
            self.enqueue(name, '⚠️ Возможно, CLI ждёт действия:\n' + self.clean(prompt)[:500], True, True)
        self.waiting[name] = prompt
        if self.mode == 'notify' and not force:
            text = ''
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
        self.offsets[name] = positions
        self.save_state()
        if force and not chunks:
            await self.output("(пустой экран)", controls=dead != "1")
        signature = tuple(sorted(positions.items()))
        stable_exit = self.dead_observed.get(name) == signature
        if dead == '1':
            self.dead_observed[name] = signature
        else:
            self.dead_observed.pop(name, None)
        # Give pipe-pane one more poll to flush bytes after the process has exited.
        if dead == "1" and drained and stable_exit and name not in self.finished:
            self.enqueue(name, "⚫ CLI завершился · код " + (code or "?") + " · " + name, False, True)
            self.finished.add(name)
            self.save_state()

    async def deliver_output(self, name):
        if time.monotonic() < self.retry_at:
            return
        async with self.delivery_lock:
            try:
                await self._deliver_output(name)
            except TelegramRetryAfter as exc:
                self.retry_at = time.monotonic() + exc.retry_after

    async def _deliver_output(self, name):
        queue = self.outboxes.setdefault(name, [])
        delivered = 0
        while queue and delivered < 3:
            entry = queue[0]
            chunk, controls = entry[:2]
            notify = len(entry) > 2 and entry[2]
            previous = self.output_messages.get(name)
            combined = previous[1] + "\n" + chunk if previous else chunk
            merge = self.mode == 'compact' and not notify and previous is not None and len(combined.encode('utf-16-le')) // 2 <= 3200
            body = combined if merge else chunk
            formatted = "<code>" + escape(name) + "</code>\n<pre>" + escape(body) + "</pre>"
            markup = self.keyboard(name) if controls else None
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
                    parse_mode="HTML", reply_markup=markup, disable_notification=not notify)
                message_id = sent.message_id
            if notify:
                self.output_messages.pop(name, None)
            else:
                self.output_messages[name] = (message_id, body)
            queue.pop(0)
            delivered += 1
            self.save_state()

    async def send_payload(self, name, payload):
        target = await self.target(name)
        if len(payload.encode()) > 100_000:
            raise ValueError("Задание слишком длинное: максимум 100 КБ.")
        buffer = 'tgbridge-' + uuid.uuid4().hex
        await tmux('set-buffer', '-b', buffer, '--', payload)
        await tmux('paste-buffer', '-p', '-d', '-b', buffer, '-t', target)
        await tmux('send-keys', '-t', target, 'Enter')
        self.waiting.pop(name, None)
        self.save_state()

    async def attachment(self, message):
        await self.target()
        name = self.active
        if self.draft is not None and self.draft['session'] != name:
            raise ValueError('Черновик относится к другой сессии. Вернись к ней или /cancel.')
        document = getattr(message, 'document', None)
        photos = getattr(message, 'photo', None)
        item = document or (photos[-1] if photos else None)
        if item is None:
            return
        if (item.file_size or 0) > 20 * 1024 * 1024:
            raise ValueError('Максимальный размер вложения — 20 МБ.')
        raw_directory = (await tmux('display-message', '-p', '-t', name + ':0.0', '#{session_path}')).strip()
        if not raw_directory or not Path(raw_directory).is_dir():
            raise ValueError('Папка сессии недоступна.')
        folder = Path(raw_directory) / '.tgbridge-uploads'
        if folder.is_symlink():
            raise ValueError('Папка вложений не должна быть символической ссылкой.')
        folder.mkdir(mode=0o700, exist_ok=True)
        # Download into memory first: failed downloads leave no partial project files.
        stream = BytesIO()
        await self.bot.download(item.file_id, destination=stream)
        data = stream.getvalue()
        if len(data) > 20 * 1024 * 1024:
            raise ValueError('Максимальный размер вложения — 20 МБ.')
        original = getattr(item, 'file_name', None) or 'screenshot.jpg'
        filename = re.sub(r'[^\w. -]', '_', Path(original).name)[:100] or 'attachment'
        destination = folder / (uuid.uuid4().hex[:12] + '-' + filename)
        with destination.open('xb') as file:
            destination.chmod(0o600)
            file.write(data)
        payload = ((getattr(message, 'caption', None) or 'Посмотри вложенный файл')
                   + '\nФайл: ' + shlex.quote(str(destination)))
        if self.draft is not None:
            if self.draft['session'] != name:
                raise ValueError('Черновик относится к другой сессии. Вернись к ней или /cancel.')
            if len(('\n\n'.join(self.draft['parts']) + payload).encode()) > 100_000:
                raise ValueError('Черновик превышает 100 КБ. Файл сохранён, но не добавлен.')
            self.draft['parts'].append(payload)
            self.save_state()
            await message.answer('📎 Вложение добавлено в задание. /submit — отправить.')
        else:
            await self.send_payload(name, payload)
            await message.answer('📎 Файл сохранён и путь передан CLI: ' + self.clean(str(destination)))

    async def send_log(self, message):
        await self.target()
        name = self.active
        path = self.output_dir / (name + '.bin')
        # Reconstruct a readable terminal transcript from the retained raw segments.
        reader = ConsoleOutput(100, 35)
        def render():
            for segment in (path.with_suffix('.previous'), path):
                if segment.exists():
                    with segment.open('rb') as source:
                        while data := source.read(65536):
                            reader.feed(data)
            rows = reader.screen.scrolled + reader.screen.display
            return self.clean('\n'.join(row.rstrip() for row in rows).rstrip()).encode()
        data = await asyncio.to_thread(render)
        await self.bot.send_document(self.owner, BufferedInputFile(data or b'(empty)', filename=name + '.txt'),
                                     caption='Сохранённый вывод (в пределах лимита хранения).')

    async def action(self, query):
        if query.from_user.id != self.owner or not query.message or query.message.chat.id != self.owner:
            await answer_callback(query)
            return
        if not await answer_callback(query):
            return
        kind, value = query.data.split(':', 1)
        if kind == 'cli' and value in self.profiles:
            command = '/startcli ' + shlex.quote(value)
        elif kind == 'mode' and value in ('compact', 'full', 'notify'):
            command = '/settings ' + value
        else:
            return
        await self.handle(query.message.model_copy(update={'from_user': query.from_user, 'text': command}))

    async def handle(self, message: Message):
        if message.from_user is None or message.from_user.id != self.owner or message.chat.id != self.owner:
            return
        if not message.text and not getattr(message, 'document', None) and not getattr(message, 'photo', None):
            return
        try:
            async with self.lock:
                if not message.text:
                    await self.attachment(message)
                    return
                text = MENU_ACTIONS.get(message.text, message.text)
                # Keep replies below the user's latest message in the conversation.
                self.output_messages.clear()
                command, _, arg = text.partition(" ")
                command = command.split("@")[0]
                if command in ("/start", "/help", "/menu"):
                    await message.answer("🤖 <b>CLI · Telegram</b>\n\n" + escape(HELP), parse_mode="HTML", reply_markup=main_menu())
                    if command == "/start" and self.projects_dir:
                        await self.show_projects(message)
                elif command == '/draft':
                    await self.target()
                    if self.draft is not None:
                        raise ValueError('Задание уже составляется. /submit — отправить, /cancel — отменить.')
                    self.draft = dict(session=self.active, parts=[])
                    self.save_state()
                    await message.answer('✍️ Присылай текст и вложения несколькими сообщениями. Затем нажми «📨 Отправить задание».')
                elif command == '/cancel':
                    self.draft = None
                    self.save_state()
                    await message.answer('Черновик отменён.')
                elif command == '/submit':
                    if not self.draft or not self.draft['parts']:
                        raise ValueError('Пустое задание. Начни с /draft.')
                    if self.draft['session'] != self.active:
                        raise ValueError('Выбрана другая сессия. Вернись к сессии черновика или /cancel.')
                    await self.send_payload(self.active, '\n\n'.join(self.draft['parts']))
                    self.draft = None
                    self.save_state()
                    await message.answer('📨 Задание отправлено.')
                elif command == '/resume':
                    await self.target()
                    await message.answer('↩️ ' + await self.session_label(self.active))
                    await self.refresh(self.active, force=True)
                elif command == '/cli':
                    rows = [[InlineKeyboardButton(text=profile, callback_data='cli:' + profile)]
                            for profile in self.profiles if len(profile.encode()) <= 59]
                    await message.answer('Выбери CLI для запуска в домашней папке. Для другой папки: /startcli имя проект',
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                elif command == '/settings':
                    modes = {'compact': 'Компактный', 'full': 'Полный поток', 'notify': 'Только уведомления'}
                    if arg:
                        if arg not in modes:
                            raise ValueError('/settings compact|full|notify')
                        self.mode = arg
                        self.save_state()
                    await message.answer('Вывод: ' + modes[self.mode], reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data='mode:' + mode)]
                                         for mode, label in modes.items()]))
                elif command == '/log':
                    await self.send_log(message)
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
                    self.metadata[name] = dict(cli=parts[0], created=time.time(), directory=str(cwd))
                    self.save_state()
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
                            label = await self.session_label(n)
                            rows.append([InlineKeyboardButton(
                                text=("🟢 " if n == self.active else "📁 ") + label[:120],
                                callback_data="session:" + n)])
                        await message.answer("🖥 <b>Сессии</b>\nНа кнопках указаны папки, в которых открыты сессии."
                                             "\nНажми, чтобы переключиться.", parse_mode="HTML",
                                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
                elif command == "/switch":
                    if not arg.strip():
                        raise ValueError("Укажи имя: /switch tgcli-имя")
                    await self.target(arg.strip())
                    self.active = arg.strip()
                    self.save_state()
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
                    self.offsets.pop(name, None)
                    self.metadata.pop(name, None)
                    self.waiting.pop(name, None)
                    (self.output_dir / (name + ".previous")).unlink(missing_ok=True)
                    if self.active == name:
                        self.active = None
                    self.save_state()
                    await message.answer("Сессия завершена: " + name)
                elif command == "/attach":
                    await self.refresh(self.active, force=True)
                else:
                    payload = arg if command == "/send" else text
                    if text.startswith("/") and command != "/send":
                        raise ValueError("Неизвестная команда. Для отправки в CLI: /send текст")
                    await self.target()
                    if self.draft is not None:
                        if self.draft['session'] != self.active:
                            raise ValueError('Черновик относится к другой сессии. Вернись к ней или /cancel.')
                        if len(('\n\n'.join(self.draft['parts']) + payload).encode()) > 100_000:
                            raise ValueError('Черновик превышает 100 КБ.')
                        self.draft['parts'].append(payload)
                        self.save_state()
                        await message.answer(f"✍️ Добавлено частей: {len(self.draft['parts'])}. /submit — отправить.")
                    else:
                        await self.send_payload(self.active, payload)
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

    async def delivery_worker(self):
        while True:
            for name in list(self.outboxes):
                try:
                    await self.deliver_output(name)
                except Exception:
                    logging.exception("Не удалось доставить вывод")
            await asyncio.sleep(1)

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
        bridge.load_state()
        dp = Dispatcher()
        dp.message.register(bridge.handle)
        dp.callback_query.register(bridge.action, F.data.startswith('cli:') | F.data.startswith('mode:'))
        dp.callback_query.register(bridge.session, F.data.startswith("session:"))
        dp.callback_query.register(bridge.agent, F.data.startswith('agent:'))
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
        delivery_task = asyncio.create_task(bridge.delivery_worker())
        try:
            await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
        finally:
            task.cancel()
            delivery_task.cancel()
            await asyncio.gather(task, delivery_task, return_exceptions=True)
            bridge.save_state()


def main():
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
