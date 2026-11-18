"""Owner-only Telegram interface to persistent tmux CLI sessions."""
import asyncio
import difflib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from dotenv import load_dotenv

PREFIX = "tgcli-"
KEYS = {"Enter": "Enter", "↑": "Up", "↓": "Down", "Tab": "Tab", "Esc": "Escape", "Ctrl+C": "C-c"}
HELP = """/profiles — доступные CLI
/startcli имя [папка] — новая сессия
/sessions — список сессий
/switch имя — выбрать сессию
/attach — экран терминала
/stop имя — завершить сессию
Обычный текст отправляется в выбранный CLI с Enter.
Кнопки управляют выбранной сессией. Команды CLI можно отправить текстом /send текст.
Вывод: доступный экран терминала, без гарантии сохранения всех промежуточных кадров."""


async def tmux(*args):
    proc = await asyncio.create_subprocess_exec("tmux", "-L", "tgbridge", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(err.decode(errors="replace").strip() or "Ошибка tmux")
    return out.decode(errors="replace")


class Bridge:
    def __init__(self, bot, owner, profiles, secrets, interval):
        self.bot, self.owner, self.profiles = bot, owner, profiles
        self.secrets, self.interval = secrets, interval
        self.active = None
        self.previous = {}
        self.lock = asyncio.Lock()

    def clean(self, text):
        for value in self.secrets:
            if value:
                text = text.replace(value, "[REDACTED]")
        return text

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

    async def handle(self, message: Message):
        if message.from_user is None or message.from_user.id != self.owner or message.chat.id != self.owner:
            return
        if not message.text:
            return
        try:
            async with self.lock:
                text = message.text
                command, _, arg = text.partition(" ")
                command = command.split("@")[0]
                if command in ("/start", "/help"):
                    await message.answer(HELP)
                elif command == "/profiles":
                    await message.answer("Профили: " + ", ".join(self.profiles))
                elif command == "/startcli":
                    parts = shlex.split(arg)
                    if not parts or len(parts) > 2 or parts[0] not in self.profiles:
                        raise ValueError('/startcli профиль ["/путь с пробелами"]')
                    profile = self.profiles[parts[0]]
                    cwd = Path(parts[1] if len(parts) == 2 else profile.get("cwd", "~")).expanduser().resolve()
                    if not cwd.is_dir():
                        raise ValueError("Рабочая папка не существует.")
                    argv = profile["command"]
                    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                        raise ValueError("command должен быть непустым массивом строк.")
                    if not shutil.which(argv[0]):
                        raise ValueError("CLI не найден: " + argv[0])
                    name = PREFIX + uuid.uuid4().hex[:10]
                    # A single shell command is required by tmux; quote each configured argument.
                    await tmux("new-session", "-d", "-s", name, "-x", "100", "-y", "35", "-c", str(cwd), shlex.join(argv))
                    self.active = name
                    await message.answer("Запущена сессия " + name, reply_markup=self.keyboard())
                elif command == "/sessions":
                    names = await self.names()
                    await message.answer("\n".join(("→ " if n == self.active else "") + n for n in names) or "Нет сессий.")
                elif command == "/switch":
                    if not arg.strip():
                        raise ValueError("Укажи имя: /switch tgcli-имя")
                    await self.target(arg.strip())
                    self.active = arg.strip()
                    await self.output(await self.screen(await self.target()), True)
                elif command == "/stop":
                    name = arg.strip() or self.active
                    await self.target(name)
                    await tmux("kill-session", "-t", name)
                    self.previous.pop(name, None)
                    if self.active == name:
                        self.active = None
                    await message.answer("Сессия завершена: " + name)
                elif command == "/attach":
                    await self.output(await self.screen(await self.target()), True)
                else:
                    payload = arg if command == "/send" else text
                    if text.startswith("/") and command != "/send":
                        raise ValueError("Неизвестная команда. Для отправки в CLI: /send текст")
                    target = await self.target()
                    await tmux("send-keys", "-t", target, "-l", "--", payload)
                    await tmux("send-keys", "-t", target, "Enter")
        except (ValueError, RuntimeError) as exc:
            await self.output(str(exc))

    async def key(self, query: CallbackQuery):
        if query.from_user.id != self.owner or not query.message or query.message.chat.id != self.owner:
            await query.answer()
            return
        try:
            async with self.lock:
                _, name, key = query.data.split(":", 2)
                if name != self.active or key not in KEYS:
                    raise ValueError("Кнопка от другой сессии. Используй /attach.")
                await tmux("send-keys", "-t", await self.target(name), KEYS[key])
            await query.answer("Отправлено")
        except (ValueError, RuntimeError) as exc:
            await query.answer(str(exc)[:180], show_alert=True)

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
                            current = await self.screen(name + ":0.0")
                            old = self.previous.get(name, "")
                            if current != old:
                                # Emit inserted/replaced lines; /attach gives the full screen.
                                before, after = old.splitlines(), current.splitlines()
                                lines = []
                                for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                                    if tag in ("insert", "replace"):
                                        lines.extend(after[j1:j2])
                                if lines:
                                    await self.output("\n".join(lines), True)
                                self.previous[name] = current
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
    async with Bot(token) as bot:
        bridge = Bridge(bot, owner, profiles, secrets, interval)
        dp = Dispatcher()
        dp.message.register(bridge.handle)
        dp.callback_query.register(bridge.key, F.data.startswith("key:"))
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
