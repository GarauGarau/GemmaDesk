#!/usr/bin/env python3
"""Benchmark real embedding, indexing and retrieval with replaceable inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import statistics
import sys
import tempfile
import threading
import time

os.environ.update(OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4')
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'archivio_gemma')]
import numpy as np
from core import Archive, Engine as TextEngine
from video_gemma.model import Engine as VideoEngine
from video_gemma.library import Library
from video_gemma.media import extract_frames, prepare_audio, read_audio, probe
from examples.make_video import create


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['cpu', 'vulkan', 'both'], default='cpu')
    parser.add_argument('--text', type=Path, default=ROOT / 'examples/documents/solar-energy.txt')
    parser.add_argument('--video', type=Path)
    parser.add_argument('--repeats', type=int, default=10)
    parser.add_argument('--output', type=Path, default=ROOT / 'bench/local/results.json')
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    original = args.text.read_text(encoding='utf-8')
    if not original.strip():
        parser.error('--text must contain non-empty UTF-8 text')
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    backends = ['cpu', 'vulkan'] if args.backend == 'both' else [args.backend]
    report = dict(environment=dict(cpu=platform.processor(), platform=platform.platform(),
                                   python=platform.python_version(), numpy=np.__version__,
                                   llama_threads=8, blas_threads=4, text_workers=1, text_batch=1),
                  protocol=dict(repeats=args.repeats, warmup=True, model_load_included=False,
                                indexing_import_included=False, retrieval_includes_query_embedding=True,
                                video_step_seconds=4, input_kind='user' if args.video else 'synthetic',
                                text_sha256=hashlib.sha256(original.encode()).hexdigest()),
                  measurements=[], status='running')
    cpu = Path('/proc/cpuinfo')
    if cpu.exists():
        report['environment']['cpu'] = next((s.split(':', 1)[1].strip() for s in cpu.read_text().splitlines()
                                             if s.startswith('model name')), platform.processor())
    def persist():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix('.part')
        temporary.write_text(json.dumps(report, indent=2) + '\n')
        temporary.replace(args.output)
    def record(metric, backend, size, call=None, prepare=None, **extra):
        times = []
        for repetition in range(args.repeats):
            timed_call = prepare() if prepare else call
            started = time.perf_counter()
            value = timed_call()
            seconds = time.perf_counter() - started
            if isinstance(value, np.ndarray):
                assert value.shape == (768,) and np.isfinite(value).all()
                assert abs(float(np.linalg.norm(value)) - 1) < 1e-4
            report['measurements'].append(dict(metric=metric, backend=backend, size=size,
                                                repetition=repetition, seconds=seconds, **extra))
            times.append(seconds)
        print(f'{backend:6} {metric:18} {str(size):5} {statistics.median(times)*1000:10.2f} ms', flush=True)
        persist()
    try:
        with tempfile.TemporaryDirectory(prefix='gemma-benchmark-') as directory:
            work = Path(directory)
            clip = args.video.resolve() if args.video else create(work / 'colors.mp4')
            info = probe(clip, stop)
            if info['duration'] < 20 or not info['has_audio']:
                parser.error('--video needs audio and at least 20 seconds')
            prepare_audio(clip, work / 'audio.wav', min(64, info['duration']), stop)
            image = next(extract_frames(clip, 0, 4, 4, stop))[3]
            pcm = {size: read_audio(work / 'audio.wav', 0, size) for size in (4, 10, 20)}
            report['protocol']['video_duration_seconds'] = info['duration']
            report['protocol']['frame_pixels'] = [image.width, image.height]
            with clip.open('rb') as stream:
                report['protocol']['video_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
            for backend in backends:
                engine = TextEngine(work / f'text-{backend}', backend, batch_size=1, text_only=True)
                try:
                    engine.start(stop, lambda _: None)
                    report.setdefault('engines', []).append(dict(program='GemmaDesk', **engine.info))
                    text = (original + '\n') * (80000 // len(original) + 1)
                    prompts = {}
                    for size in (256, 1024, 4096, 8000):
                        lo, hi = 0, len(text)
                        while lo + 1 < hi:
                            mid = (lo + hi) // 2
                            if engine.tokens('title: Benchmark | text: ' + text[:mid], stop) <= size:
                                lo = mid
                            else:
                                hi = mid
                        prompt = 'title: Benchmark | text: ' + text[:lo]
                        prompts[size] = text[:lo]
                        count = engine.tokens(prompt, stop)
                        engine.encode(prompt, stop)
                        record('text_embedding', backend, size, lambda: engine.encode(prompt, stop), tokens=count)
                    source = work / 'benchmark.txt'
                    source.write_text(prompts[4096], encoding='utf-8')
                    counter = iter(range(args.repeats + 1))
                    def prepare_text():
                        archive = Archive(work / f'index-{backend}-{next(counter)}')
                        archive.add([source], stop)
                        def index_text():
                            result = archive.index(engine, stop, lambda _: None)
                            if result['errors']:
                                raise RuntimeError(result)
                            return archive
                        return index_text
                    archive = prepare_text()()
                    record('text_indexing', backend, 4096, prepare=prepare_text)
                    query = 'How can a building store electricity from solar panels?'
                    archive.search(engine.query(query, stop), stop)
                    record('retrieval', backend, 'text', lambda: archive.search(engine.query(query, stop), stop))
                    report.setdefault('corpora', {})['text'] = dict(documents=1)
                finally:
                    engine.close()
                engine = VideoEngine(work / f'video-{backend}', backend)
                try:
                    engine.start(stop, lambda _: None)
                    report['engines'].append(dict(program='GemmaVideo', **engine.info))
                    for size, samples in pcm.items():
                        engine.audio(samples, stop)
                        record('audio_embedding', backend, size, lambda: engine.audio(samples, stop))
                    engine.image(image, stop)
                    record('frame_embedding', backend, image.width, lambda: engine.image(image, stop))
                    counter = iter(range(args.repeats + 1))
                    def prepare_video():
                        library = Library(work / f'video-index-{backend}-{next(counter)}')
                        library.add_paths([clip], stop)
                        def index_video():
                            result = library.index(engine, 4, stop)
                            if result['errors']:
                                raise RuntimeError(result)
                            return library
                        return index_video
                    library = prepare_video()()
                    record('video_indexing', backend, info['duration'], prepare=prepare_video)
                    for mode in ('audio', 'frame', 'both'):
                        query = 'A red screen'
                        library.search(engine.query(query, stop), mode, stop=stop)
                        record('retrieval', backend, mode,
                               lambda: library.search(engine.query(query, stop), mode, stop=stop))
                    report['corpora']['video'] = dict(videos=1)
                finally:
                    engine.close()
            report['status'] = 'complete'
    except BaseException as exc:
        report.update(status='error', error=str(exc))
        raise
    finally:
        persist()
    print(args.output)


if __name__ == '__main__':
    main()
