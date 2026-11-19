import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from tgbridge import Bridge


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)),
                                   edit_message_text=AsyncMock(), delete_message=AsyncMock())
        self.bridge = Bridge(self.bot, 123, {'codex': {'command': ['codex']}}, [], 2)
        self.bridge.names = AsyncMock(return_value=['tgcli-test'])

    async def test_project_discovery_and_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'My project').mkdir()
            (root / '.hidden').mkdir()
            (root / 'file').touch()
            self.bridge.projects_dir = root
            self.assertEqual([p.name for p in self.bridge.projects()], ['My project'])
            self.assertEqual(self.bridge.working_directory('My project'), root / 'My project')
            self.assertEqual(self.bridge.working_directory(), Path.home())
            message = SimpleNamespace(answer=AsyncMock())
            await self.bridge.show_projects(message)
            self.assertEqual(len(message.answer.call_args.kwargs['reply_markup'].inline_keyboard), 1)
        self.bridge.projects_dir = None
        self.assertEqual(self.bridge.projects(), [])
        self.assertEqual(self.bridge.working_directory(), Path.home())

    async def test_attach_without_session_returns_help(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123), text='/attach')
        await self.bridge.handle(message)
        self.assertIn('/sessions', self.bot.send_message.call_args.args[1])

    async def test_output_is_appended_and_long_text_is_not_lost(self):
        from console_output import ConsoleOutput
        self.bridge.active = 'tgcli-test'
        reader = ConsoleOutput(120, 35)
        self.bridge.streams['tgcli-test'] = reader
        with tempfile.TemporaryDirectory() as directory:
            self.bridge.output_dir = Path(directory)
            path = Path(directory) / 'tgcli-test.bin'
            path.write_bytes(('first <tag>\r\n' + 'line\r\n' * 1000).encode())
            with patch('tgbridge.tmux', AsyncMock(return_value='0:\n')):
                await self.bridge.refresh('tgcli-test')
                sent = ''.join(c.args[1].split('<pre>', 1)[1].split('</pre>', 1)[0] for c in self.bot.send_message.call_args_list)
                self.assertIn('first &lt;tag&gt;', sent)
                self.assertEqual(sent.count('line'), 1000)
                count = self.bot.send_message.await_count
                await self.bridge.refresh('tgcli-test')
                self.assertEqual(self.bot.send_message.await_count, count)
                with path.open('ab') as file:
                    file.write(b'next answer\r\n')
                await self.bridge.refresh('tgcli-test')
                self.assertIn('next answer', self.bot.edit_message_text.call_args.args[0])
                self.assertEqual(self.bot.send_message.await_count, count)
                self.bot.delete_message.assert_not_awaited()

    async def test_short_updates_merge_and_overflow_continues(self):
        self.bridge.active = 'tgcli-test'
        self.bridge.outboxes['tgcli-test'] = [('hello <world>', True)]
        await self.bridge.deliver_output('tgcli-test')
        self.bridge.outboxes['tgcli-test'].append(('second line', True))
        await self.bridge.deliver_output('tgcli-test')
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn('hello &lt;world&gt;\nsecond line', self.bot.edit_message_text.call_args.args[0])
        self.bridge.outboxes['tgcli-test'].append(('😀' * 1600, True))
        await self.bridge.deliver_output('tgcli-test')
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.bridge.output_messages['tgcli-test'][1], '😀' * 1600)

    async def test_deleted_output_message_is_replaced(self):
        self.bridge.active = 'tgcli-test'
        self.bridge.output_messages['tgcli-test'] = (1, 'old')
        self.bridge.outboxes['tgcli-test'] = [('new', True)]
        self.bot.edit_message_text.side_effect = TelegramBadRequest(
            method='editMessageText', message='message to edit not found')
        await self.bridge.deliver_output('tgcli-test')
        self.assertIn('new', self.bot.send_message.call_args.args[1])
        self.assertEqual(self.bridge.outboxes['tgcli-test'], [])

    async def test_failed_edit_keeps_pending_output_for_retry(self):
        self.bridge.active = 'tgcli-test'
        self.bridge.output_messages['tgcli-test'] = (1, 'old')
        self.bridge.outboxes['tgcli-test'] = [('new', True)]
        self.bot.edit_message_text.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.bridge.deliver_output('tgcli-test')
        self.assertEqual(self.bridge.output_messages['tgcli-test'], (1, 'old'))
        self.assertEqual(self.bridge.outboxes['tgcli-test'], [('new', True)])

    async def test_startcli_without_arguments_uses_codex_and_home(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123),
                                  text='/startcli', answer=AsyncMock())
        with patch('tgbridge.resolve_cli', return_value='/usr/bin/codex'), \
             patch('tgbridge.tmux', new=AsyncMock()) as tmux, \
             patch.object(self.bridge, 'connect_output', new=AsyncMock()):
            await self.bridge.handle(message)
            calls = tmux.call_args_list
            self.assertEqual(calls[0].args[-1], str(Path.home()))
            self.assertIn('/usr/bin/codex', calls[-1].args[-1])
            self.assertIn('codex запущен', message.answer.call_args.args[0])

    async def test_menu_button_opens_sessions_instead_of_sending_to_cli(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123),
                                  text='🖥 Сессии', answer=AsyncMock())
        with patch('tgbridge.tmux', new=AsyncMock(return_value='/home/user/Project/demo-project\n')) as tmux:
            await self.bridge.handle(message)
            self.assertEqual(tmux.call_args.args[0], 'display-message')
        button = message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, 'session:tgcli-test')
        self.assertIn('demo-project', button.text)

    async def test_switch_explains_original_directory_and_escapes_html(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123),
                                  text='/switch tgcli-test', answer=AsyncMock())
        with patch('tgbridge.tmux', new=AsyncMock(return_value='/projects/A <B>\n')) as tmux, \
             patch.object(self.bridge, 'refresh', new=AsyncMock()) as refresh:
            await self.bridge.handle(message)
            self.assertEqual(tmux.call_args.args[-1], '#{session_path}')
            self.assertIn('/projects/A &lt;B&gt;', message.answer.call_args.args[0])
            self.assertEqual(self.bridge.active, 'tgcli-test')
            refresh.assert_awaited_once_with('tgcli-test', force=True)

    async def test_unknown_session_directory_does_not_break_selection(self):
        with patch('tgbridge.tmux', new=AsyncMock(side_effect=RuntimeError('missing session'))):
            self.assertIsNone(await self.bridge.session_directory('tgcli-test'))
        self.assertEqual(self.bridge.project_label(None), 'Папка неизвестна')
        self.assertEqual(self.bridge.project_label(str(Path.home())), 'Домашняя папка')

    async def test_menu_is_persistent(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123),
                                  text='/menu', answer=AsyncMock())
        await self.bridge.handle(message)
        markup = message.answer.call_args.kwargs['reply_markup']
        self.assertTrue(markup.is_persistent)
        self.assertEqual(sum(len(row) for row in markup.keyboard), 6)

    async def test_startup_sends_action_hints_and_persistent_menu_to_owner(self):
        await self.bridge.announce_startup()
        call = self.bot.send_message.call_args
        self.assertEqual(call.args[0], 123)
        self.assertIn('Бот запущен', call.args[1])
        markup = call.kwargs['reply_markup']
        self.assertTrue(markup.is_persistent)
        labels = [button.text for row in markup.keyboard for button in row]
        self.assertIn('🚀 Запустить Codex', labels)
        self.assertIn('📂 Проекты', labels)
        self.assertIn('🖥 Сессии', labels)

    async def test_expired_callback_does_not_send_keys(self):
        query = SimpleNamespace(from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(chat=SimpleNamespace(id=123)), data='key:tgcli-test:Enter',
            answer=AsyncMock(side_effect=TelegramBadRequest(method='answerCallbackQuery',
                             message='query is too old and response timeout expired')))
        with patch('tgbridge.tmux', new=AsyncMock()) as tmux:
            await self.bridge.key(query)
            tmux.assert_not_awaited()
        self.assertEqual(query.answer.await_count, 1)

    async def test_callback_is_acknowledged_before_waiting_for_lock(self):
        import asyncio
        acknowledged = asyncio.Event()
        async def answer(*args, **kwargs):
            acknowledged.set()
        query = SimpleNamespace(from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(chat=SimpleNamespace(id=123)), data='key:tgcli-test:Enter',
            answer=AsyncMock(side_effect=answer))
        self.bridge.active = 'tgcli-test'
        with patch('tgbridge.tmux', new=AsyncMock()) as tmux:
            await self.bridge.lock.acquire()
            task = asyncio.create_task(self.bridge.key(query))
            try:
                await asyncio.wait_for(acknowledged.wait(), timeout=1)
                tmux.assert_not_awaited()
            finally:
                self.bridge.lock.release()
                await task
            self.assertEqual(tmux.call_args.args, ('send-keys', '-t', 'tgcli-test:0.0', 'Enter'))
        self.assertEqual(query.answer.await_count, 1)

    async def test_project_button_launches_selected_path(self):
        with tempfile.TemporaryDirectory(prefix='projects with spaces ') as directory:
            self.bridge.project_choices['chosen'] = Path(directory)
            message = Message.model_validate({'message_id': 1, 'date': 0,
                'chat': {'id': 123, 'type': 'private'},
                'from': {'id': 999, 'is_bot': True, 'first_name': 'Bot'}, 'text': 'Projects'})
            query = SimpleNamespace(from_user=SimpleNamespace(id=123), message=message,
                                    data='project:chosen', answer=AsyncMock())
            with patch.object(self.bridge, 'handle', new=AsyncMock()) as handle:
                await self.bridge.project(query)
                import shlex
                forwarded = handle.call_args.args[0]
                self.assertEqual(shlex.split(forwarded.text), ['/startcli', 'codex', directory])
                self.assertEqual(forwarded.from_user.id, 123)
            query.from_user = SimpleNamespace(id=456)
            with patch.object(self.bridge, 'handle', new=AsyncMock()) as handle:
                await self.bridge.project(query)
                handle.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
