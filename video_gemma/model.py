"""Persistent EmbeddingGemma 2 worker: text, RGB and original PCM audio."""
import hashlib
import json
import select
import subprocess
import time
import numpy as np
from .common import APP, MODEL, RUNTIME, check, unit

HASHES = {
    'embeddinggemma-2-Q8_0.gguf': '2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135',
    'mmproj-embeddinggemma-2-Q8_0.gguf': 'c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f',
}


class Engine:
    def __init__(self, data, backend='cpu'):
        self.data = data
        self.backend = backend
        self.process = None
        self.log = None
        self.info = {}

    def start(self, stop, notify=lambda message: None):
        if self.process and self.process.poll() is None:
            return
        self.close()
        notify('Verifica e caricamento del modello locale…')
        for name, expected in HASHES.items():
            digest = hashlib.sha256()
            with (MODEL / name).open('rb') as f:
                while block := f.read(1024 * 1024):
                    check(stop)
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise RuntimeError('Pesi del modello incompatibili: ' + name)
        self.data.mkdir(parents=True, exist_ok=True)
        self.log_path = self.data / 'modello.log'
        self.log = self.log_path.open('wb')
        self.process = subprocess.Popen([
            str(APP / 'native/embed'), str(MODEL / 'embeddinggemma-2-Q8_0.gguf'),
            str(MODEL / 'mmproj-embeddinggemma-2-Q8_0.gguf'), str(RUNTIME), self.backend,
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log)
        try:
            self.info = self.receive(stop)
            if not self.info.get('ready'):
                raise RuntimeError('Modello non pronto.')
        except BaseException:
            self.close()
            raise

    def receive(self, stop):
        start = time.monotonic()
        while time.monotonic() - start < 300:
            check(stop)
            if self.process.poll() is not None:
                raise RuntimeError('Motore arrestato: ' + self.log_path.read_text(errors='replace')[-1800:])
            if select.select([self.process.stdout], [], [], .1)[0]:
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError('Il motore non ha restituito un risultato.')
                return json.loads(line)
        raise TimeoutError('Inferenza oltre 5 minuti. È possibile riprendere il lavoro.')

    def command(self, kind, payload, stop, dimensions=''):
        check(stop)
        try:
            self.process.stdin.write(f'{kind} {len(payload)}{dimensions}\n'.encode() + payload)
            self.process.stdin.flush()
            return self.receive(stop)
        except BaseException:
            self.close()
            raise

    def query(self, text, stop):
        text = text.strip()
        if not text or len(text.encode()) > 24000:
            raise ValueError('Scrivi una ricerca di lunghezza compresa tra 1 e 6.000 caratteri circa.')
        prompt = 'task: search result | query: ' + text
        if self.command('C', prompt.encode(), stop)['count'] > 8192:
            raise ValueError('La ricerca supera il contesto del modello. Usa una descrizione più breve.')
        return unit(self.command('T', prompt.encode(), stop)['vector'])

    def image(self, image, stop):
        image = image.convert('RGB')
        image.thumbnail((672, 672))
        return unit(self.command('I', image.tobytes(), stop, f' {image.width} {image.height}')['vector'])

    def audio(self, samples, stop):
        samples = np.asarray(samples, dtype='<f4')
        if samples.ndim != 1 or not 0 < len(samples) <= 30 * 16000 or not np.isfinite(samples).all():
            raise ValueError('Audio atteso: PCM mono a 16 kHz, massimo 30 secondi.')
        return unit(self.command('A', samples.tobytes(), stop)['vector'])

    def close(self):
        p, self.process = self.process, None
        if p:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            if p.stdin:
                p.stdin.close()
            if p.stdout:
                p.stdout.close()
        if self.log:
            self.log.close()
            self.log = None
