"""Bound tmux raw logs to two segments; executable pipe-pane sink."""
import os
from pathlib import Path
import sys

LIMIT = 8 * 1024 * 1024


def record(path, source):
    path = Path(path)
    while data := source.read1(65536):
        if path.exists() and path.stat().st_size + len(data) > LIMIT:
            path.replace(path.with_suffix('.previous'))
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, 'ab') as destination:
            destination.write(data)


if __name__ == '__main__':
    record(sys.argv[1], sys.stdin.buffer)
