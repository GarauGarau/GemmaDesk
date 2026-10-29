#!/usr/bin/env python3
"""Generate a 64-second red/blue clip with a tone, using FFmpeg."""
import argparse
from pathlib import Path
import subprocess


def create(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        'ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
        'color=c=royalblue:s=672x420:r=12:d=32', '-f', 'lavfi', '-i',
        'color=c=tomato:s=672x420:r=12:d=32', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=16000:duration=64',
        '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
        '-map', '[v]', '-map', '2:a', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path),
    ], check=True)
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=Path(__file__).resolve().parent / 'generated/colors.mp4')
    print(create(parser.parse_args().output))
