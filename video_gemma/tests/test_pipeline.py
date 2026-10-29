"""Meaningful regression checks using real FFmpeg and a deterministic model double."""
from pathlib import Path
import hashlib
import tempfile
import shutil
import threading
import unittest
from unittest.mock import patch
import numpy as np
from video_gemma.common import Cancelled, check, run, unit
from video_gemma.media import blocks, frame_times, extract_frames, prepare_audio, read_audio, make_waveform
from video_gemma.library import Library, merge_hits


class FakeProcess:
    def poll(self):
        return None


class FakeEngine:
    process = FakeProcess()

    def __init__(self):
        self.calls = 0

    def start(self, stop, notify):
        check(stop)

    def audio(self, samples, stop):
        check(stop)
        self.calls += 1
        v = np.zeros(768, dtype='<f4')
        v[0] = 1
        return v

    def image(self, image, stop):
        check(stop)
        self.calls += 1
        v = np.zeros(768, dtype='<f4')
        v[1] = 1
        return v


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='gemmavideo-tests-')
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / 'source'
        cls.source.mkdir()
        cls.video = cls.source / 'prova.mp4'
        run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x96:rate=10',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '35.7',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2', '-c:a', 'aac', '-y', cls.video], threading.Event())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=self.root)
        self.lib = Library(Path(self.folder.name))
        self.stop = threading.Event()
        self.lib.scan(self.source, self.stop)

    def tearDown(self):
        self.folder.cleanup()

    def test_windows_cover_full_duration_including_short_tail(self):
        for duration in (.04, 4., 16., 20., 35.7, 2233.792):
            windows = list(blocks(duration))
            self.assertEqual(windows[0][0], 0)
            self.assertEqual(windows[-1][1], duration)
            self.assertEqual(windows[-1][2], duration)
            for i, (start, end, audio_end) in enumerate(windows):
                self.assertLessEqual(audio_end - start, 20)
                self.assertGreaterEqual(audio_end, end)
                if i:
                    self.assertEqual(start, windows[i - 1][1])
                frames = list(frame_times(start, end, 4))
                self.assertEqual(frames[0][0], start)
                self.assertEqual(frames[-1][1], end)

    def test_checkpoint_resume_no_duplicates_and_no_missing_tail(self):
        original_hash = hashlib.sha256(self.video.read_bytes()).hexdigest()
        def pause(progress):
            if progress.get('checkpoint', 0) >= 16:
                self.stop.set()
        with self.assertRaises(Cancelled):
            self.lib.index(FakeEngine(), 4, self.stop, pause)
        partial = self.lib.videos()[0]
        self.assertEqual(partial['checkpoint'], 16.)
        self.assertEqual(partial['vectors'], 5)
        self.assertEqual(partial['status'], 'sospeso')
        self.stop.clear()
        engine = FakeEngine()
        # Changing the preference on resume must preserve the original video's step.
        result = self.lib.index(engine, 8, self.stop)
        self.assertFalse(result['errors'])
        video = self.lib.videos()[0]
        self.assertEqual(video['status'], 'pronto')
        self.assertEqual(video['checkpoint'], video['duration'])
        self.assertEqual(video['frame_step'], 4)
        with self.lib.connect() as db:
            rows = list(db.execute('SELECT * FROM segments ORDER BY start'))
        self.assertEqual(len(rows), 12)
        self.assertEqual(engine.calls, 7)
        for modality in ('audio', 'frame'):
            segments = [r for r in rows if r['modality'] == modality]
            self.assertEqual(segments[0]['start'], 0)
            self.assertAlmostEqual(max(r['end'] for r in segments), video['duration'], places=2)
        self.assertEqual(original_hash, hashlib.sha256(self.video.read_bytes()).hexdigest())
        self.assertTrue(self.lib.waveform_path(video['id']).exists())

    def test_cancel_mid_block_commits_neither_vectors_nor_checkpoint(self):
        engine = FakeEngine()
        real_image = engine.image
        def cancel(image, stop):
            result = real_image(image, stop)
            stop.set()
            return result
        engine.image = cancel
        with self.assertRaises(Cancelled):
            self.lib.index(engine, 4, self.stop)
        video = self.lib.videos()[0]
        self.assertEqual(video['checkpoint'], 0)
        self.assertEqual(video['vectors'], 0)

    def test_reset_retains_sources_and_waveforms(self):
        self.lib.index(FakeEngine(), 8, self.stop)
        video = self.lib.videos()[0]
        count = self.lib.reset()
        self.assertGreater(count, 0)
        self.assertTrue(self.video.exists())
        self.assertTrue(self.lib.waveform_path(video['id']).exists())
        self.assertEqual(self.lib.videos()[0]['vectors'], 0)
        self.assertEqual(self.lib.videos()[0]['checkpoint'], 0)

    def test_changed_source_invalidates_only_that_video(self):
        self.lib.index(FakeEngine(), 8, self.stop)
        with patch('video_gemma.library.fingerprint', return_value='modified-file'):
            result = self.lib.scan(self.source, self.stop)
        self.assertFalse(result['errors'])
        self.assertEqual(self.lib.videos()[0]['vectors'], 0)
        self.assertFalse(self.lib.waveform_path(self.lib.videos()[0]['id']).exists())

    def test_import_files_and_folders_is_idempotent_and_remembers_folder(self):
        added = Path(self.folder.name) / 'new'
        added.mkdir()
        target = added / 'new.MP4'
        shutil.copy2(self.video, target)
        first = self.lib.add_paths([target, added, target], self.stop)
        self.assertEqual(first['added'], 1)
        self.assertEqual(len(first['video_ids']), 1)
        again = self.lib.add_paths([target], self.stop)
        self.assertEqual(again['unchanged'], 1)
        self.assertEqual(again['added'], 0)
        self.assertEqual(self.lib.last_source(), str(added))
        self.assertEqual(len(self.lib.videos()), 2)

    def test_remove_deletes_only_selected_derived_data(self):
        self.lib.index(FakeEngine(), 8, self.stop)
        old = self.lib.videos()[0]
        added = Path(self.folder.name) / 'second.mp4'
        shutil.copy2(self.video, added)
        result = self.lib.add_paths([added], self.stop)
        count = self.lib.remove([old['id'], old['id']])
        self.assertEqual(count['removed'], 1)
        self.assertEqual([v['id'] for v in self.lib.videos()], result['video_ids'])
        self.assertTrue(self.video.is_file())
        self.assertFalse(self.lib.waveform_path(old['id']).exists())
        with self.lib.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM segments WHERE video_id=?', (old['id'],)).fetchone()[0], 0)
        # A user may add a removed source again explicitly.
        self.assertEqual(self.lib.add_paths([self.video], self.stop)['added'], 1)

    def test_reconnect_moved_source_preserves_embeddings(self):
        first = Path(self.folder.name) / 'original'
        second = Path(self.folder.name) / 'moved'
        first.mkdir()
        second.mkdir()
        path = first / 'move.mp4'
        shutil.copy2(self.video, path)
        result = self.lib.add_paths([path], self.stop)
        ident = result['video_ids'][0]
        self.lib.index(FakeEngine(), 8, self.stop, video_ids=[ident])
        before = next(v for v in self.lib.videos() if v['id'] == ident)
        path.rename(second / path.name)
        result = self.lib.scan(second, self.stop)
        self.assertEqual(result['reconnected'], 1)
        self.assertEqual(result['added'], 0)
        after = next(v for v in self.lib.videos() if v['id'] == ident)
        self.assertEqual(before['vectors'], after['vectors'])
        self.assertEqual(after['status'], 'pronto')
        self.assertEqual(after['path'], str(second / path.name))

    def test_missing_files_do_not_displace_available_search_results(self):
        self.lib.index(FakeEngine(), 8, self.stop)
        query = np.zeros(768)
        query[0] = 1
        with self.lib.connect() as db:
            db.execute('UPDATE videos SET path=?', (str(self.root / 'unplugged.mp4'),))
        self.assertEqual(self.lib.search(query), [])

    def test_import_reports_invalid_and_empty_paths(self):
        result = self.lib.add_paths(['', self.root / 'not-there.mp4'], self.stop)
        self.assertEqual(len(result['errors']), 2)
        self.assertEqual(result['added'], 0)
        self.assertEqual(len(self.lib.videos()), 1)

    def test_relative_path_resolves_against_app_folder(self):
        with patch('video_gemma.library.ROOT', self.root):
            result = self.lib.scan('source', self.stop)
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(result['errors'], [])

    def test_final_frame_seek_does_not_fail_when_audio_is_longer(self):
        path = Path(self.folder.name) / 'last-frame.mp4'
        run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=30:duration=8.033333',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000:duration=8.06',
             '-c:v', 'libx264', '-threads', '2', '-c:a', 'aac', '-y', path], self.stop)
        result = self.lib.add_paths([path], self.stop)
        ident = result['video_ids'][0]
        result = self.lib.index(FakeEngine(), 4, self.stop, video_ids=[ident])
        self.assertFalse(result['errors'])
        video = next(v for v in self.lib.videos() if v['id'] == ident)
        self.assertEqual(video['status'], 'pronto')
        with self.lib.connect() as db:
            frames = db.execute("SELECT * FROM segments WHERE video_id=? AND modality='frame' ORDER BY start", (ident,)).fetchall()
        self.assertEqual(len(frames), 3)
        self.assertAlmostEqual(frames[-1]['end'], 8.033333, places=4)
        self.assertEqual(frames[-1]['timestamp'], 8.)

    def test_disjoint_hits_remain_separate_and_overlaps_merge(self):
        merged = merge_hits([
            dict(start=2., end=6., modality='frame', score=.8),
            dict(start=0., end=20., modality='audio', score=.7),
            dict(start=100., end=104., modality='frame', score=.9),
        ])
        self.assertEqual([(x['start'], x['end']) for x in merged], [(0., 20.), (100., 104.)])
        self.assertEqual(merged[0]['modalities'], {'audio', 'frame'})

    def test_search_filters_modalities_and_rejects_unrelated_query(self):
        self.lib.index(FakeEngine(), 8, self.stop)
        query = np.zeros(768)
        query[0] = 1
        hits = self.lib.search(query, mode='audio')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['intervals'][0]['modalities'], {'audio'})
        self.assertEqual(self.lib.search(query, mode='frame'), [])
        query[0] = 0
        query[2] = 1
        self.assertEqual(self.lib.search(query), [])

    def test_waveform_is_actual_audio_envelope(self):
        wav = Path(self.folder.name) / 'audio.wav'
        dest = Path(self.folder.name) / 'wave.npz'
        prepare_audio(self.video, wav, 35.7, self.stop)
        samples = read_audio(wav, 34.9, 35.7)
        self.assertEqual(len(samples), 12800)
        self.assertGreater(float(np.std(samples)), .01)
        make_waveform(wav, dest, 35.7, self.stop)
        with np.load(dest) as data:
            self.assertEqual(data['peaks'].shape, (714, 2))
            self.assertGreater(float(data['peaks'][:, 1].mean()), .05)
            self.assertLess(float(data['peaks'][:, 0].mean()), -.05)

    def test_audio_start_offset_remains_aligned_with_video(self):
        path = Path(self.folder.name) / 'delayed.mkv'
        wav = Path(self.folder.name) / 'delayed.wav'
        run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=64x64:r=10:d=6',
             '-itsoffset', '2', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000:duration=2',
             '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-threads', '2', '-c:a', 'pcm_s16le',
             '-y', path], self.stop)
        prepare_audio(path, wav, 6, self.stop)
        self.assertLess(float(np.abs(read_audio(wav, 0, 1.8)).max()), .001)
        self.assertGreater(float(np.std(read_audio(wav, 2.1, 3.8))), .05)

    def test_video_without_audio_is_indexed_visually(self):
        source = Path(self.folder.name) / 'silent-source'
        source.mkdir()
        path = source / 'silent.mp4'
        run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=64x64:r=10:d=2',
             '-c:v', 'libx264', '-threads', '2', '-y', path], self.stop)
        self.lib.scan(source, self.stop)
        video = next(v for v in self.lib.videos() if v['name'] == 'silent.mp4')
        result = self.lib.index(FakeEngine(), 4, self.stop, video_ids=[video['id']])
        self.assertFalse(result['errors'])
        video = next(v for v in self.lib.videos() if v['id'] == video['id'])
        self.assertEqual(video['status'], 'pronto')
        self.assertEqual(video['vectors'], 1)
        self.assertFalse(video['has_audio'])
        self.assertFalse(self.lib.waveform_path(video['id']).exists())

    def test_frame_sampling_preserves_timestamps_and_short_final_interval(self):
        color_video = Path(self.folder.name) / 'colors.mp4'
        run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=64x64:r=10:d=4',
             '-f', 'lavfi', '-i', 'color=blue:s=64x64:r=10:d=4',
             '-f', 'lavfi', '-i', 'color=lime:s=64x64:r=10:d=1',
             '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]', '-map', '[out]',
             '-c:v', 'libx264', '-threads', '2', '-y', color_video], self.stop)
        frames = list(extract_frames(color_video, 0., 9., 4., self.stop))
        self.assertEqual([f[2] for f in frames], [2., 6., 8.5])
        for frame, channel in zip(frames, [0, 2, 1]):
            rgb = np.asarray(frame[3]).mean(axis=(0, 1))
            self.assertEqual(int(rgb.argmax()), channel)
            self.assertGreater(rgb[channel], 200)


if __name__ == '__main__':
    unittest.main()
