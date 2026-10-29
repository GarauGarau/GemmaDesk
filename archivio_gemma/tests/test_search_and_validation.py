"""Regression checks for both apps; all archives and processes are isolated."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import core as desk
from video_gemma import common as video


class ValidationTests(unittest.TestCase):
    def test_large_finite_vectors_remain_normalized(self):
        for module in (desk, video):
            with self.subTest(app=module.__name__):
                with np.errstate(over='raise', invalid='raise'):
                    value = module.unit(np.full(768, 1e38, dtype='<f4'))
                self.assertTrue(np.isfinite(value).all())
                self.assertEqual(value.dtype, np.dtype('<f4'))
                self.assertAlmostEqual(float(np.linalg.norm(value)), 1., places=6)

    def test_invalid_inputs_are_rejected_by_both_apps(self):
        for module in (desk, video):
            for value in (np.zeros(768), np.ones(3), np.full(768, np.nan), np.full(768, np.inf)):
                with self.subTest(app=module.__name__, shape=value.shape):
                    with self.assertRaises(ValueError):
                        module.unit(value)

    def test_cancelled_extraction_does_not_launch_a_process(self):
        stop = threading.Event()
        stop.set()
        for module in (desk, video):
            with self.subTest(app=module.__name__):
                with patch.object(module.subprocess, 'Popen') as popen:
                    with self.assertRaises(module.Cancelled):
                        module.run([sys.executable, '-c', 'pass'], stop)
                    popen.assert_not_called()

    def test_cancellation_as_process_exits_is_not_reported_as_success(self):
        for module in (desk, video):
            stop = threading.Event()

            class Completed:
                returncode = 0

                def poll(self):
                    stop.set()
                    return 0

            with self.subTest(app=module.__name__):
                with patch.object(module.subprocess, 'Popen', return_value=Completed()):
                    with self.assertRaises(module.Cancelled):
                        module.run(['completed-process'], stop)


class DeskSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gemmadesk-search-')
        self.root = Path(self.temp.name)
        self.archive = desk.Archive(self.root / 'archive')
        self.stop = threading.Event()
        self.query = np.eye(1, 768, dtype='<f4')[0]

    def tearDown(self):
        self.temp.cleanup()

    def document(self):
        path = self.root / 'fixture.txt'
        path.write_text('Contenuto del documento di prova.')
        self.archive.add([path])
        document = self.archive.prepare(self.archive.pending()[0], self.stop)
        self.archive.page_commit(document, 1, [('testo', path.read_text(), self.query)])
        self.archive.update(document['id'], status='ready')

    def test_search_uses_cosine_even_for_unnormalized_queries(self):
        self.document()
        result = self.archive.search(self.query * 7, self.stop)
        self.assertAlmostEqual(result[0]['score'], 1., places=6)
        self.assertEqual(result[0]['name'], 'fixture.txt')

    def test_cancelled_or_invalid_query_is_checked_on_empty_archives(self):
        self.stop.set()
        with self.assertRaises(desk.Cancelled):
            self.archive.search(self.query, self.stop)
        self.stop.clear()
        with self.assertRaises(ValueError):
            self.archive.search(np.zeros(768), self.stop)
        self.document()
        for limit in (0, -1):
            self.assertEqual(self.archive.search(self.query, self.stop, limit), [])

    def test_corrupt_stored_vectors_are_reported_and_preserved(self):
        self.document()
        for bad in (b'bad', np.full(768, np.nan, dtype='<f4').tobytes()):
            with self.archive.connect() as db:
                db.execute('UPDATE chunks SET vector=?', (bad,))
            with self.assertRaisesRegex(ValueError, 'Embedding archiviato non valido'):
                self.archive.search(self.query, self.stop)
            with self.archive.connect() as db:
                self.assertEqual(db.execute('SELECT vector FROM chunks').fetchone()[0], bad)


if __name__ == '__main__':
    unittest.main(verbosity=2)
