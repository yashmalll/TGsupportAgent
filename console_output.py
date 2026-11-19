"""Read tmux's output stream while interpreting terminal redraws locally."""
import codecs
from difflib import SequenceMatcher
import re

import pyte


def visible_line(line):
    # Remove animation glyphs; keep their accompanying meaningful text.
    line = re.sub(r'[\u2800-\u28ff◐◓◑◒◴◷◶◵◜◝◞◟]', '', line).rstrip()
    if re.fullmatch(r'\s*[-\\|/⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏.·•]*\s*', line):
        return ''
    return line


class OutputScreen(pyte.Screen):
    def __init__(self, columns, lines):
        self.scrolled = []
        super().__init__(columns, lines)

    def index(self):
        bottom = self.margins.bottom if self.margins else self.lines - 1
        top = self.margins.top if self.margins else 0
        if self.cursor.y == bottom:
            self.scrolled.append(self.display[top])
        super().index()


class ConsoleOutput:
    def __init__(self, columns=100, lines=35):
        self.screen = OutputScreen(columns, lines)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.previous = []
        self.offset = 0

    def feed(self, data):
        self.stream.feed(self.decoder.decode(data))

    def pending(self, force=False, raw=False):
        normalize = (lambda row: row.rstrip()) if raw else visible_line
        rows = [normalize(row) for row in self.screen.scrolled + self.screen.display]
        current = [normalize(row) for row in self.screen.display]
        while rows and not rows[-1]:
            rows.pop()
        while current and not current[-1]:
            current.pop()
        if force:
            added = rows
        else:
            added = []
            for tag, _, _, start, end in SequenceMatcher(None, self.previous, rows, autojunk=False).get_opcodes():
                if tag in ('insert', 'replace'):
                    added.extend(rows[start:end])
        return '\n'.join(added).strip('\n'), current

    def acknowledge(self, current):
        self.previous = current
        self.screen.scrolled.clear()
