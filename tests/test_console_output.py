import unittest
from console_output import ConsoleOutput


class ConsoleTests(unittest.TestCase):
    def test_redraw_and_spinner_do_not_repeat_text(self):
        reader = ConsoleOutput()
        reader.feed('⠋ Working'.encode())
        text, current = reader.pending()
        self.assertEqual(text.strip(), 'Working')
        reader.acknowledge(current)
        reader.feed('\r⠙ Working'.encode())
        self.assertEqual(reader.pending()[0], '')
        reader.feed(b'\r\x1b[2KDone\r\n')
        self.assertEqual(reader.pending()[0], 'Done')

    def test_split_utf8_and_ansi(self):
        reader = ConsoleOutput()
        data = 'Привет'.encode()
        reader.feed(b'\x1b[32m' + data[:3])
        reader.feed(data[3:] + b'\x1b[0m\r\n')
        self.assertEqual(reader.pending()[0], 'Привет')

    def test_whitespace_and_real_durations_preserved(self):
        reader = ConsoleOutput()
        reader.feed(b'header\r\n\r\n  took 5s\r\n')
        self.assertEqual(reader.pending()[0], 'header\n\n  took 5s')
