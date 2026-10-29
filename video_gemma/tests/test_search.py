"""Exact retrieval against a full-matrix oracle, using only temporary data."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
from video_gemma.common import Cancelled, unit
from video_gemma.library import Library, merge_hits


def reference_search(library, query, mode='both', limit=30, minimum=.65):
    """Original full-matrix ranking, deliberately independent of candidate retention."""
    with library.connect() as db:
        rows = db.execute('''SELECT s.*,v.path,v.name,v.duration,v.checkpoint,v.fingerprint,v.has_audio
            FROM segments s JOIN videos v ON v.id=s.video_id
            WHERE (?='both' OR s.modality=?) ORDER BY s.id''', (mode, mode)).fetchall()
    rows = [row for row in rows if Path(row['path']).is_file()]
    if not rows:
        return []
    scores = np.frombuffer(b''.join(row['vector'] for row in rows), dtype='<f4').reshape(-1, 768) @ unit(query)
    order = np.argsort(-scores, kind='stable')
    cutoff = max(minimum, float(scores[order[0]]) - .065)
    counts, groups = {}, {}
    total = 0
    for index in order:
        row, score = rows[index], float(scores[index])
        if score < cutoff:
            break
        ident = row['video_id']
        if counts.get(ident, 0) == 80:
            continue
        counts[ident] = counts.get(ident, 0) + 1
        groups.setdefault(ident, []).append(dict(row, score=score))
        total += 1
        if total == 300:
            break
    occurrences = []
    for ident, hits in groups.items():
        occurrences.extend((interval['score'], ident, interval) for interval in merge_hits(hits))
    chosen = {ident: [] for ident in groups}
    for _, ident, interval in sorted(occurrences, key=lambda value: -value[0])[:limit]:
        chosen[ident].append(interval)
    results = [dict(video_id=ident, score=max(i['score'] for i in intervals),
                    intervals=sorted(intervals, key=lambda interval: interval['start']))
               for ident, intervals in chosen.items() if intervals]
    return sorted(results, key=lambda group: -group['score'])


def summary(results):
    return [(group['video_id'], group['score'], group['intervals']) for group in results]


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gemmavideo-search-')
        self.root = Path(self.temp.name)
        self.library = Library(self.root / 'index')
        self.stop = threading.Event()
        self.query = np.eye(1, 768, dtype='<f4')[0]

    def tearDown(self):
        self.temp.cleanup()

    def populate(self, tied=False):
        rng = np.random.default_rng(45)
        with self.library.connect() as db:
            for ident in range(1, 10):
                path = self.root / f'{ident}.mp4'
                if ident != 9:  # Highest scores from an unavailable source must not set the cutoff.
                    path.touch()
                db.execute('''INSERT INTO videos(id,path,name,fingerprint,duration,has_audio,width,height)
                    VALUES (?,?,?,?,60000,1,64,64)''', (ident, str(path), path.name, str(ident)))
            rows = []
            for index in range(6000):
                ident = index % 9 + 1
                score = .8 if tied else rng.uniform(.72, .84)
                if ident == 9:
                    score = 1.
                # Raising the best score in the final batch must discard early weak hits.
                if not tied and index == 5999:
                    score = .88
                vector = np.zeros(768, dtype='<f4')
                vector[0], vector[1] = score, np.sqrt(1 - score * score)
                start = float(index // 9 * 30)
                rows.append((ident, 'audio' if index % 2 else 'frame', start,
                             start + (20 if index % 2 else 4), start + 2, vector.tobytes()))
            db.executemany('''INSERT INTO segments(video_id,modality,start,end,timestamp,vector)
                VALUES (?,?,?,?,?,?)''', rows)

    def test_matches_full_matrix_with_quotas_filters_and_late_winner(self):
        self.populate()
        for mode, limit, minimum in [('both', 30, .65), ('audio', 100, .65),
                                     ('frame', 3, .8), ('both', 300, .65), ('both', 30, .95)]:
            with self.subTest(mode=mode, limit=limit, minimum=minimum):
                expected = reference_search(self.library, self.query, mode, limit, minimum)
                actual = self.library.search(self.query, mode, limit, minimum, self.stop)
                self.assertEqual(summary(actual), summary(expected))

    def test_equal_scores_preserve_original_order_across_batches(self):
        self.populate(tied=True)
        self.assertEqual(summary(self.library.search(self.query, limit=300)),
                         summary(reference_search(self.library, self.query, limit=300)))

    def test_reads_bounded_batches_and_can_cancel_between_them(self):
        self.populate()
        original = self.library.connect
        sizes = []
        stop = self.stop

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(self):
                raise AssertionError('Search loaded all vectors at once')

            def fetchmany(self, size):
                sizes.append(size)
                if len(sizes) == 2:
                    stop.set()
                return self.cursor.fetchmany(size)

        class Connection:
            def __init__(self, db):
                self.db = db

            def execute(self, *args):
                return Cursor(self.db.execute(*args))

        @contextmanager
        def guarded():
            with original() as db:
                yield Connection(db)

        with patch.object(self.library, 'connect', guarded):
            with self.assertRaises(Cancelled):
                self.library.search(self.query, stop=self.stop)
        self.assertEqual(len(sizes), 2)
        self.assertLessEqual(max(sizes), 1024)

    def test_empty_cancelled_and_invalid_requests(self):
        self.assertEqual(self.library.search(self.query), [])
        self.stop.set()
        with self.assertRaises(Cancelled):
            self.library.search(self.query, stop=self.stop)
        for kwargs in [dict(mode='typo'), dict(minimum=float('nan'))]:
            with self.assertRaises(ValueError):
                self.library.search(self.query, **kwargs)
        for limit in (0, -1):
            self.assertEqual(self.library.search(self.query, limit=limit), [])

    def test_invalid_stored_vectors_are_reported_without_modifying_them(self):
        self.populate()
        for bad in (b'bad', np.full(768, np.nan, dtype='<f4').tobytes()):
            with self.library.connect() as db:
                db.execute('UPDATE segments SET vector=? WHERE id=1', (bad,))
            with self.assertRaisesRegex(ValueError, 'Embedding archiviato non valido'):
                self.library.search(self.query)
            with self.library.connect() as db:
                self.assertEqual(db.execute('SELECT vector FROM segments WHERE id=1').fetchone()[0], bad)


if __name__ == '__main__':
    unittest.main(verbosity=2)
