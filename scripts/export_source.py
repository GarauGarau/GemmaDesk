#!/usr/bin/env python3
"""Export only application sources, examples, screenshots and benchmark results."""
import argparse
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    '.gitignore', 'README.md', 'LICENSE', 'THIRD_PARTY.txt', 'requirements.txt',
    'Avvia-*.sh', 'Ricalcola-*.sh', 'scripts/*.py', 'scripts/*.sh',
    'archivio_gemma/*.py', 'archivio_gemma/assets/*',
    'archivio_gemma/native/*.py', 'archivio_gemma/native/*.c',
    'archivio_gemma/native/*.cpp', 'archivio_gemma/native/*.h',
    'archivio_gemma/native/src/*', 'archivio_gemma/tests/test_*.py',
    'archivio_gemma/tests/*.cpp', 'video_gemma/*.py',
    'video_gemma/native/*.py', 'video_gemma/native/*.c',
    'video_gemma/native/*.cpp', 'video_gemma/native/*.h', 'video_gemma/tests/test_*.py',
    'native/include/*', 'examples/*.py', 'examples/documents/*',
    'docs/screenshots/*.png', 'bench/run.py', 'bench/results/*.json',
)


def public_files():
    return sorted({p for pattern in PATTERNS for p in ROOT.glob(pattern)
                   if p.is_file() and not p.is_symlink()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.local/GemmaDesk-source.tar.gz')
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    paths = public_files()
    if args.list:
        print('\n'.join(str(p.relative_to(ROOT)) for p in paths))
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, 'w:gz') as archive:
        for path in paths:
            archive.add(path, arcname=Path('GemmaDesk') / path.relative_to(ROOT), recursive=False)
    print(f'{args.output}: {len(paths)} files, {args.output.stat().st_size:,} bytes')


if __name__ == '__main__':
    main()
