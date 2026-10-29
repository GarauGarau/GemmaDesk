"""FFmpeg extraction with source time preserved; no transcription or tail clipping."""
from pathlib import Path
import hashlib
import json
import math
import tempfile
import wave
import numpy as np
from PIL import Image
from .common import check, run

SUPPORTED = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.mts', '.m2ts'}
SAMPLE_RATE = 16000
WAVE_RATE = 20
AUDIO_WINDOW = 20.
STRIDE = 16.


def fingerprint(path):
    """Size, nanosecond mtime and sampled content; never writes to the source."""
    stat = path.stat()
    h = hashlib.sha256()
    with path.open('rb') as f:
        h.update(f.read(65536))
        if stat.st_size > 65536:
            f.seek(max(0, stat.st_size - 65536))
            h.update(f.read(65536))
    return f'{stat.st_size}:{stat.st_mtime_ns}:{h.hexdigest()}'


def probe(path, stop):
    data = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                           '-of', 'json', path], stop, 45))
    video = next((s for s in data['streams'] if s['codec_type'] == 'video'
                  and not s.get('disposition', {}).get('attached_pic')), None)
    if not video:
        raise ValueError('Nessuna traccia video utilizzabile.')
    duration = float(data.get('format', {}).get('duration') or video.get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Durata del video non disponibile.')
    video_duration = float(video.get('duration') or duration) + float(video.get('start_time') or 0) - float(data.get('format', {}).get('start_time') or 0)
    if not math.isfinite(video_duration) or video_duration <= 0:
        video_duration = duration
    return dict(duration=duration, video_duration=min(duration, video_duration), has_audio=any(s['codec_type'] == 'audio' for s in data['streams']),
                width=video['width'], height=video['height'])


def blocks(duration, start=0.):
    while start < duration - 1e-6:
        end = min(duration, start + STRIDE)
        yield start, end, min(duration, start + AUDIO_WINDOW)
        start = end


def frame_times(start, end, step):
    for i in range(math.ceil((end - start) / step - 1e-9)):
        left = start + i * step
        right = min(end, left + step)
        yield left, right, (left + right) / 2


def extract_frames(path, start, end, step, stop):
    """Decode each block once; fps picks the middle of each sampling interval.

    The shorter final interval is extracted at its exact midpoint separately.
    No frames outside a block are attributed to that block.
    """
    if end <= start:
        return
    times = list(frame_times(start, end, step))
    full = sum(abs((right - left) - step) < 1e-6 for left, right, _ in times)
    with tempfile.TemporaryDirectory(prefix='gemmavideo-frames-') as folder:
        if full:
            run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '2', '-ss', f'{start:.6f}',
                 '-t', f'{full * step:.6f}', '-i', path, '-an',
                 '-vf', f'tpad=stop_mode=clone:stop_duration={step},fps=1/{step},scale=672:672:force_original_aspect_ratio=decrease',
                 '-frames:v', str(full), '-threads', '2', str(Path(folder) / '%05d.png')], stop)
        for i, (left, right, timestamp) in enumerate(times):
            check(stop)
            file = Path(folder) / f'{i + 1:05d}.png'
            if i >= full or not file.exists():
                run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '2', '-ss', f'{timestamp:.6f}',
                     '-i', path, '-frames:v', '1', '-an', '-vf', 'scale=672:672:force_original_aspect_ratio=decrease',
                     '-threads', '2', '-y', file], stop)
            if not file.exists():
                # A seek between the final frame's PTS and its display end can
                # legitimately decode nothing. Retry inside the same interval.
                timestamp = left
                run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '2', '-ss', f'{left:.6f}',
                     '-i', path, '-frames:v', '1', '-an', '-vf', 'scale=672:672:force_original_aspect_ratio=decrease',
                     '-threads', '2', '-y', file], stop)
            if not file.exists():
                raise RuntimeError(f'{Path(path).name}: nessun fotogramma decodificabile nell’intervallo {left:.3f}–{right:.3f} s.')
            with Image.open(file) as image:
                yield left, right, timestamp, image.convert('RGB')


def prepare_audio(path, destination, duration, stop):
    run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '2', '-i', path,
         '-map', '0:a:0', '-vn', '-af', 'aresample=async=1:first_pts=0',
         '-ac', '1', '-ar', str(SAMPLE_RATE), '-c:a', 'pcm_s16le',
         '-t', f'{duration:.6f}', '-y', destination], stop, max(600, duration))


def read_audio(file, start, end):
    with wave.open(str(file), 'rb') as wav:
        first = min(wav.getnframes(), round(start * SAMPLE_RATE))
        wav.setpos(first)
        raw = wav.readframes(max(0, round(end * SAMPLE_RATE) - first))
        return np.frombuffer(raw, dtype='<i2').astype('<f4') / 32768.


def make_waveform(wav_path, destination, duration, stop):
    """Actual min/max envelope, 20 bins/second; bounded reads, silence after audio ends."""
    samples_per_bin = SAMPLE_RATE // WAVE_RATE
    count = math.ceil(duration * WAVE_RATE)
    peaks = np.zeros((count, 2), dtype='<f4')
    with wave.open(str(wav_path), 'rb') as wav:
        for offset in range(0, count, 1200):
            check(stop)
            n = min(1200, count - offset)
            values = np.frombuffer(wav.readframes(n * samples_per_bin), dtype='<i2').astype('<f4') / 32768.
            values = np.pad(values, (0, n * samples_per_bin - len(values)))
            values = values.reshape(n, samples_per_bin)
            peaks[offset:offset + n, 0] = values.min(axis=1)
            peaks[offset:offset + n, 1] = values.max(axis=1)
    temporary = destination.with_suffix('.tmp')
    with temporary.open('wb') as out:
        np.savez_compressed(out, peaks=peaks, rate=WAVE_RATE, duration=duration)
    temporary.replace(destination)
