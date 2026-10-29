#!/usr/bin/env python3
"""Import, index and search user documents with the GemmaDesk pipeline."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'archivio_gemma'))
from core import Archive, EnginePool, SUPPORTED, Cancelled
from PySide6.QtCore import QCoreApplication, QLockFile


def collect(sources):
    paths = set()
    for source in sources:
        source = source.expanduser().resolve()
        if not source.exists():
            raise ValueError(f'Input does not exist: {source}')
        candidates = source.rglob('*') if source.is_dir() else [source]
        paths.update(p for p in candidates if p.is_file() and p.suffix.lower() in SUPPORTED)
    if not paths:
        raise ValueError('No supported documents found.')
    return sorted(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['index', 'search', 'map'])
    parser.add_argument('--source', type=Path, nargs='+')
    parser.add_argument('--data', type=Path, default=ROOT / 'data/my-archive')
    parser.add_argument('--backend', choices=['cpu', 'vulkan'], default='cpu')
    parser.add_argument('--query')
    parser.add_argument('--scope', choices=['documents', 'passages'], default='passages')
    parser.add_argument('--clusters', type=int, default=0)
    args = parser.parse_args()
    if args.action == 'index' and not args.source:
        parser.error('index requires --source FILE_OR_FOLDER [FILE_OR_FOLDER ...]')
    if args.action == 'search' and not args.query:
        parser.error('search requires --query TEXT')
    try:
        paths = collect(args.source) if args.action == 'index' else []
    except ValueError as exc:
        parser.error(str(exc))
    qt = QCoreApplication([])
    args.data.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(args.data / 'app.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        parser.error('This archive is open in another process. Close it first.')
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    engine = None
    def notify(value):
        print(json.dumps(value, ensure_ascii=False, default=str), file=sys.stderr, flush=True)
    try:
        archive = Archive(args.data)
        if args.action == 'map':
            result = archive.map(stop, clusters=args.clusters, notify=notify, scope=args.scope)
        else:
            if paths:
                imported = archive.add(paths, stop, notify)
                notify(imported)
                if imported['rejected']:
                    return 1
            text_only = all(Path(d['source']).suffix.lower() in {'.txt', '.md'}
                            for d in archive.pending())
            engine = EnginePool(args.data, args.backend, workers=1, batch_size=4,
                                text_only=text_only)
            if args.action == 'index':
                result = archive.index(engine, stop, notify)
            else:
                engine.start(stop, notify)
                result = archive.search(engine.query(args.query, stop), stop)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 1 if isinstance(result, dict) and result.get('errors') else 0
    except Cancelled as exc:
        notify(str(exc))
        return 130
    finally:
        if engine:
            engine.close()
        lock.unlock()


if __name__ == '__main__':
    raise SystemExit(main())
