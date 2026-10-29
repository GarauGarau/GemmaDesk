"""Resume the audited Graham pipeline with the same engine as the desktop."""
import argparse
import json
import signal
import threading
import time
from pathlib import Path
from PySide6.QtCore import QLockFile
from core import Archive,EnginePool,APP,Cancelled
from essay_dataset import prepare,export
from topic_discovery import discover


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',type=Path,default=APP/'dati')
    parser.add_argument('--discover-only',action='store_true');args=parser.parse_args()
    lock=QLockFile(str(args.data/'app.lock'));lock.setStaleLockTime(0)
    if not lock.tryLock(0):raise SystemExit('Archivio già aperto o in elaborazione')
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    archive=Archive(args.data);engine=None;last=0.
    def notify(value):
        nonlocal last
        now=time.monotonic()
        if not isinstance(value,dict) or now-last>=10:
            print(json.dumps(value,ensure_ascii=False) if isinstance(value,dict) else value,flush=True);last=now
    try:
        if not args.discover_only:
            prepare(archive)
            engine=EnginePool(archive.data,'vulkan',2,4,text_only=True)
            result=archive.index(engine,stop,notify);engine.close()
            if result['errors']:raise RuntimeError(f"{result['errors']} documenti in errore")
        result=discover(archive.data,stop,notify);export(archive)
        print(json.dumps(result,ensure_ascii=False),flush=True)
    except Cancelled:print('Sospeso; checkpoint conservati.',flush=True)
    finally:
        if engine:engine.close()
        lock.unlock()


if __name__=='__main__':main()
