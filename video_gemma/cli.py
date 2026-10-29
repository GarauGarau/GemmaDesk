"""Optional command-line entry point; uses the same index and mutual-exclusion lock."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading
from .common import APP, DEFAULT_SOURCE, Cancelled
from .library import Library
from .model import Engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['scan', 'index', 'search'])
    parser.add_argument('--data', type=Path, default=APP / 'dati')
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--backend', choices=['cpu', 'vulkan'], default='cpu')
    parser.add_argument('--step', type=float, choices=[1, 2, 4, 8], default=4)
    parser.add_argument('--ids', type=int, nargs='+')
    parser.add_argument('--query')
    parser.add_argument('--mode', choices=['both', 'audio', 'frame'], default='both')
    args = parser.parse_args()
    from PySide6.QtCore import QCoreApplication, QLockFile
    app = QCoreApplication([])
    args.data.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(args.data / 'app.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        parser.error('Archivio già aperto in GemmaVideo. Chiudi la GUI prima di usare la CLI.')
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    library = Library(args.data)
    engine = Engine(args.data, args.backend)
    def notify(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        if args.action == 'scan':
            result = library.scan(args.source, stop, notify)
        elif args.action == 'index':
            result = library.index(engine, args.step, stop, notify, args.ids)
        else:
            if not args.query:
                parser.error('--query è necessario per search')
            engine.start(stop, lambda text: notify(dict(message=text)))
            result = library.search(engine.query(args.query, stop), args.mode, stop=stop)
        print(json.dumps(result, ensure_ascii=False, default=lambda value: sorted(value)), flush=True)
        return 1 if isinstance(result, dict) and result.get('errors') else 0
    except Cancelled as e:
        notify(dict(message=str(e)))
        return 130
    finally:
        engine.close()
        lock.unlock()


if __name__ == '__main__':
    raise SystemExit(main())
