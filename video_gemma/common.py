"""Shared paths, validation and cancellable local processes."""
from pathlib import Path
import subprocess
import tempfile
import time
import numpy as np

APP = Path(__file__).resolve().parent
ROOT = APP.parent
DEFAULT_SOURCE = ROOT / 'Video'
MODEL = ROOT / 'modelli/embeddinggemma-2'
RUNTIME = ROOT / 'runtime/llama-b11658'


class Cancelled(Exception):
    pass


def check(stop):
    if stop.is_set():
        raise Cancelled('Elaborazione sospesa. Gli intervalli completati sono conservati.')


def unit(value):
    value = np.asarray(value, dtype='<f4')
    if value.shape != (768,) or not np.isfinite(value).all():
        raise ValueError('Embedding non valido.')
    # A float32 norm can overflow even when every component is finite.
    precise = value.astype(np.float64)
    norm = np.linalg.norm(precise)
    if norm < 1e-8:
        raise ValueError('Embedding non valido.')
    return (precise / norm).astype('<f4')


def duration(seconds):
    seconds = max(0, int(seconds))
    h, seconds = divmod(seconds, 3600)
    m, s = divmod(seconds, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


def run(command, stop, timeout=600):
    check(stop)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen([str(x) for x in command], stdout=out, stderr=err)
        start = time.monotonic()
        try:
            while p.poll() is None:
                check(stop)
                if time.monotonic() - start > timeout:
                    raise TimeoutError('Il processo multimediale ha superato il tempo massimo.')
                stop.wait(.06)
            check(stop)
            out.seek(0)
            err.seek(0)
            if p.returncode:
                raise RuntimeError(err.read().decode(errors='replace')[-2000:])
            return out.read()
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
