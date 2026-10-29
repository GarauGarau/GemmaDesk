"""Build the PDF extractor, embedding worker and Linux launcher."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shlex
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUNTIME = ROOT / 'runtime/llama-b11658'
OBJECTS = HERE / 'obj'
OBJECTS.mkdir(exist_ok=True)


def flags(variable, option):
    if variable in os.environ:
        return shlex.split(os.environ[variable])
    return shlex.split(subprocess.check_output(['pkg-config', option, 'mupdf'], text=True))


cflags = flags('MUPDF_CFLAGS', '--cflags')
ldflags = flags('MUPDF_LIBS', '--libs')
core = 'common drop pdf layout publication notes model model-layout math math-layout reconstruct edits book kepub epub zip reader'.split()


def compile_one(name):
    subprocess.run(['cc', '-std=c17', '-O2', '-D_POSIX_C_SOURCE=200809L',
                    f'-I{HERE / "src"}', *cflags, '-c', str(HERE / 'src' / f'{name}.c'),
                    '-o', str(OBJECTS / f'{name}.o')], check=True)


with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
    list(pool.map(compile_one, core))
subprocess.run(['cc', '-std=c17', '-O2', f'-I{HERE / "src"}', *cflags,
                str(HERE / 'extract.c'), *[str(OBJECTS / f'{n}.o') for n in core],
                *ldflags, '-Wl,--disable-new-dtags,-rpath,$ORIGIN/lib',
                '-lz', '-lstdc++', '-lm', '-pthread', '-o', str(HERE / 'extract')], check=True)
subprocess.run(['c++', '-std=c++17', '-O2', f'-I{ROOT / "native/include"}',
                str(HERE / 'embed.cpp'),
                *[str(RUNTIME / n) for n in ('libllama.so.0.6.0', 'libmtmd.so.0.6.0',
                                             'libggml-base.so.0.26.0', 'libggml.so.0.26.0')],
                f'-Wl,-rpath-link,{RUNTIME}',
                '-Wl,--disable-new-dtags,-rpath,$ORIGIN/../../runtime/llama-b11658',
                '-o', str(HERE / 'embed')], check=True)
subprocess.run(['cc', '-std=c17', '-O2', '-Wall', '-Wextra', '-Werror',
                str(HERE / 'launcher.c'), '-o', str(ROOT / 'GemmaDesk')], check=True)
