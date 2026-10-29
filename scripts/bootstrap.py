#!/usr/bin/env python3
"""Fetch the pinned Linux x86_64 runtime and Q8_0 models, then build workers."""
import argparse
import hashlib
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REVISION = 'bfcd298762cc34d0357ece5ebdd31791a3a374d8'
MODEL_URL = f'https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve/{REVISION}'
MODELS = {
    'embeddinggemma-2-Q8_0.gguf': '2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135',
    'mmproj-embeddinggemma-2-Q8_0.gguf': 'c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f',
}
RUNTIME = 'llama-b11658-bin-ubuntu-vulkan-x64.tar.gz'
RUNTIME_SHA = '69b648eb609d841f9b91bdc618d81f694ca08bcb5905ef77ff60813fcec256e8'


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(url, path, expected, offline=False):
    if path.is_file() and checksum(path) == expected:
        print(f'Verified {path.name}', flush=True)
        return
    if offline:
        raise RuntimeError(f'Missing or invalid offline asset: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.part')
    print(f'Downloading {path.name}', flush=True)
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'GemmaDesk-setup'})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        if checksum(temporary) != expected:
            raise RuntimeError(f'Checksum mismatch: {path.name}')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'AMD64'):
        parser.error('This setup targets Linux x86_64.')
    for name, digest in MODELS.items():
        fetch(f'{MODEL_URL}/{name}', ROOT / 'modelli/embeddinggemma-2' / name, digest, args.offline)
    archive = ROOT / 'runtime' / RUNTIME
    fetch(f'https://github.com/ggml-org/llama.cpp/releases/download/b11658/{RUNTIME}',
          archive, RUNTIME_SHA, args.offline)
    with tarfile.open(archive) as bundle:
        bundle.extractall(ROOT / 'runtime', filter='data')
    for project in ('archivio_gemma', 'video_gemma'):
        subprocess.run([sys.executable, str(ROOT / project / 'native/build.py')], check=True)
    print('Ready. Run ./Avvia-GemmaDesk.sh or ./Avvia-GemmaVideo.sh')


if __name__ == '__main__':
    main()
