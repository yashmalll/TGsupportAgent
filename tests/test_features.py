import asyncio
from io import BytesIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramRetryAfter
from tgbridge import Bridge
from console_output import ConsoleOutput
import stream_recorder


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                                   edit_message_text=AsyncMock(), send_document=AsyncMock(), download=AsyncMock())
        self.bridge = Bridge(self.bot, 123, {'codex': {'command': ['codex']}}, ['secret123'], 1)
        self.bridge.output_dir = Path(self.temp.name) / 'output'
        self.bridge.names = AsyncMock(return_value=['tgcli-test'])
        self.bridge.active = 'tgcli-test'

    def message(self, text=None, **kwargs):
        return SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=123),
                               text=text, answer=AsyncMock(), **kwargs)

    async def test_draft_collects_parts_and_survives_restart(self):
        self.bridge.durable = True
        await self.bridge.handle(self.message('/draft'))
        await self.bridge.handle(self.message('first'))
        await self.bridge.handle(self.message('second'))
        restored = Bridge(self.bot, 123, {}, [], 1)
        restored.output_dir = self.bridge.output_dir
        restored.load_state()
        self.assertEqual(restored.active, 'tgcli-test')
        self.assertEqual(restored.draft['parts'], ['first', 'second'])
        with patch.object(self.bridge, 'send_payload', new=AsyncMock()) as send:
            await self.bridge.handle(self.message('/submit'))
            send.assert_awaited_once_with('tgcli-test', 'first\n\nsecond')
        self.assertIsNone(self.bridge.draft)

    async def test_draft_cannot_be_sent_to_another_session(self):
        self.bridge.draft = dict(session='tgcli-other', parts=['private'])
        with patch.object(self.bridge, 'send_payload', new=AsyncMock()) as send:
            await self.bridge.handle(self.message('/submit'))
            send.assert_not_awaited()
        self.assertIsNotNone(self.bridge.draft)

    async def test_retry_after_preserves_queue_and_does_not_lock_controls(self):
        self.bridge.enqueue('tgcli-test', 'text')
        self.bot.send_message.side_effect = TelegramRetryAfter(method='sendMessage', message='flood', retry_after=30)
        await self.bridge.deliver_output('tgcli-test')
        self.assertEqual(len(self.bridge.outboxes['tgcli-test']), 1)
        self.assertFalse(self.bridge.lock.locked())
        await self.bridge.deliver_output('tgcli-test')
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_pending_output_survives_restart_and_success_is_committed(self):
        self.bridge.durable = True
        self.bridge.enqueue('tgcli-test', 'pending')
        self.bridge.save_state()
        restored = Bridge(self.bot, 123, {}, [], 1)
        restored.output_dir = self.bridge.output_dir
        restored.load_state()
        await restored.deliver_output('tgcli-test')
        self.assertEqual(restored.outboxes['tgcli-test'], [])
        restored.load_state()
        self.assertEqual(restored.outboxes['tgcli-test'], [])

    async def test_attachment_stays_inside_project_and_path_is_sent(self):
        async def download(file_id, destination):
            destination.write(b'example')
        self.bot.download.side_effect = download
        item = SimpleNamespace(file_id='file', file_size=7, file_name='../../test.txt')
        msg = self.message(document=item, photo=None, caption='Inspect this')
        with patch('tgbridge.tmux', new=AsyncMock(return_value=self.temp.name)), \
             patch.object(self.bridge, 'send_payload', new=AsyncMock()) as send:
            await self.bridge.handle(msg)
        files = list((Path(self.temp.name) / '.tgbridge-uploads').iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b'example')
        self.assertIn(str(files[0]), send.call_args.args[1])

    async def test_unauthorized_attachment_is_ignored(self):
        msg = self.message(document=SimpleNamespace(file_id='x'), photo=None)
        msg.from_user.id = 999
        await self.bridge.handle(msg)
        self.bot.download.assert_not_awaited()

    async def test_notify_mode_suppresses_stream_but_sends_prompt_and_exit(self):
        self.bridge.durable = True
        self.bridge.mode = 'notify'
        self.bridge.output_dir.mkdir()
        path = self.bridge.output_dir / 'tgcli-test.bin'
        path.write_bytes(b'Working\r\nApprove? [y/n]')
        self.bridge.streams['tgcli-test'] = ConsoleOutput()
        with patch('tgbridge.tmux', new=AsyncMock(return_value='0:')):
            await self.bridge.refresh('tgcli-test')
            queued = self.bridge.outboxes['tgcli-test']
            self.assertEqual(len(queued), 1)
            self.assertIn('CLI', queued[0][0])
            await self.bridge.refresh('tgcli-test')
            self.assertEqual(len(queued), 1)
        with patch('tgbridge.tmux', new=AsyncMock(return_value='1:0')):
            await self.bridge.refresh('tgcli-test')
            await self.bridge.refresh('tgcli-test')
        self.assertIn('код 0', self.bridge.outboxes['tgcli-test'][-1][0])

    async def test_busy_delivery_does_not_hold_control_lock(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(message_id=1)
        self.bot.send_message.side_effect = send
        self.bridge.enqueue('tgcli-test', 'text')
        task = asyncio.create_task(self.bridge.deliver_output('tgcli-test'))
        try:
            await asyncio.wait_for(started.wait(), 1)
            async with self.bridge.lock:
                pass
        finally:
            release.set()
            await task

    async def test_reconnect_restores_partial_utf8_and_terminal_state(self):
        self.bridge.output_dir.mkdir()
        path = self.bridge.output_dir / 'tgcli-test.bin'
        full = 'Привет'.encode()
        path.write_bytes(b'\x1b[32m' + full[:5])
        self.bridge.offsets['tgcli-test'] = {str(path.stat().st_ino): path.stat().st_size}
        with path.open('ab') as file:
            file.write(full[5:] + b'\x1b[0m\r\n')
        with patch('tgbridge.tmux', new=AsyncMock(return_value='100 35')):
            await self.bridge.connect_output('tgcli-test')
        with patch('tgbridge.tmux', new=AsyncMock(return_value='0:')):
            await self.bridge.refresh('tgcli-test')
        text = '\n'.join(item[0] for item in self.bridge.outboxes['tgcli-test'])
        self.assertIn('Привет', text)
        self.assertNotIn('�', text)

    async def test_rotated_segments_are_read_in_order_without_repeating(self):
        self.bridge.output_dir.mkdir()
        path = self.bridge.output_dir / 'tgcli-test.bin'
        path.write_bytes(b'first\r\n')
        self.bridge.streams['tgcli-test'] = ConsoleOutput()
        with patch('tgbridge.tmux', new=AsyncMock(return_value='0:')):
            await self.bridge.refresh('tgcli-test')
            self.bridge.outboxes['tgcli-test'].clear()
            path.replace(path.with_suffix('.previous'))
            path.write_bytes(b'second\r\n')
            await self.bridge.refresh('tgcli-test')
            text = '\n'.join(entry[0] for entry in self.bridge.outboxes['tgcli-test'])
            self.assertIn('second', text)
            self.assertNotIn('first', text)

    async def test_log_redacts_secrets(self):
        self.bridge.output_dir.mkdir()
        (self.bridge.output_dir / 'tgcli-test.bin').write_bytes(b'password secret123\r\n')
        await self.bridge.send_log(self.message('/log'))
        document = self.bot.send_document.call_args.args[1]
        self.assertNotIn(b'secret123', document.data)
        self.assertIn(b'[REDACTED]', document.data)


class RecorderTests(unittest.TestCase):
    def test_rotation_retains_two_bounded_segments(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(stream_recorder, 'LIMIT', 8):
            path = Path(folder) / 'log.bin'
            class Source:
                chunks = iter([b'12345678', b'abcdefgh', b'ABCDEFGH', b''])
                def read1(self, size):
                    return next(self.chunks)
            stream_recorder.record(path, Source())
            self.assertEqual(path.read_bytes(), b'ABCDEFGH')
            self.assertEqual(path.with_suffix('.previous').read_bytes(), b'abcdefgh')

    def test_full_mode_keeps_braille(self):
        reader = ConsoleOutput()
        reader.feed('⠿ chart'.encode())
        self.assertIn('⠿', reader.pending(raw=True)[0])
