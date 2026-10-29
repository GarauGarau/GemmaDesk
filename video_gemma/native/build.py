"""Build the video worker and Linux launcher against the pinned runtime."""
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNTIME = ROOT / 'runtime/llama-b11658'
subprocess.run([
    'c++', '-std=c++17', '-O2', f'-I{ROOT / "native/include"}',
    str(HERE / 'embed.cpp'),
    *[str(RUNTIME / name) for name in ('libllama.so.0.6.0', 'libmtmd.so.0.6.0',
                                       'libggml-base.so.0.26.0', 'libggml.so.0.26.0')],
    f'-Wl,-rpath-link,{RUNTIME}', '-Wl,--disable-new-dtags,-rpath,$ORIGIN/../../runtime/llama-b11658',
    '-o', str(HERE / 'embed'),
], check=True)
if (HERE / 'launcher.c').exists():
    subprocess.run(['cc', '-std=c17', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(HERE / 'launcher.c'), '-o', str(ROOT / 'GemmaVideo')], check=True)
