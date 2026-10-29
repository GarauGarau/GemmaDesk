"""Topic continuity, readable labels and archive preservation regressions."""
import os
import sys
from pathlib import Path
import tempfile
import threading
import unittest

os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')
os.environ.setdefault('OMP_NUM_THREADS', '4')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from sklearn.metrics import adjusted_rand_score
from core import Archive, Cancelled, unit
from grouping import hierarchical_groups
from topics import cluster_topics, terms, canonical


class HierarchyTests(unittest.TestCase):
    def matrix(self):
        rng = np.random.default_rng(721)
        centers = np.stack([unit(rng.normal(size=768)) for _ in range(4)])
        return np.stack([unit(centers[i % 4] + rng.normal(0, .007, 768)) for i in range(80)])

    def test_extra_group_only_splits_one_leaf_and_keeps_other_ids(self):
        matrix = self.matrix()
        previous = np.zeros(len(matrix), dtype=int)
        for count in range(2, 13):
            current, _, _ = hierarchical_groups(matrix, count)
            self.assertEqual(len(set(current)), count)
            changed = []
            for ident in np.unique(previous):
                descendants = set(current[previous == ident])
                if len(descendants) == 1:
                    self.assertEqual(descendants, {ident})
                else:
                    changed.append(ident)
                    self.assertEqual(descendants, {ident, count - 1})
            self.assertEqual(len(changed), 1)
            for ident in np.unique(current):
                self.assertEqual(len(set(previous[current == ident])), 1)
            previous = current

    def test_reverse_and_repeat_recover_identical_assignments(self):
        matrix = self.matrix()
        six, _, _ = hierarchical_groups(matrix, 6)
        hierarchical_groups(matrix, 3)
        repeated, _, _ = hierarchical_groups(matrix, 6)
        np.testing.assert_array_equal(six, repeated)

    def test_auto_is_a_cut_of_the_same_tree_and_recovers_clear_topics(self):
        matrix = self.matrix()
        automatic, score, _ = hierarchical_groups(matrix)
        self.assertEqual(len(set(automatic)), 4)
        manual, _, _ = hierarchical_groups(matrix, 4)
        np.testing.assert_array_equal(automatic, manual)
        self.assertEqual(adjusted_rand_score(np.arange(80) % 4, automatic), 1.)
        self.assertGreater(score, .8)

    def test_diffuse_equal_single_and_duplicate_vectors(self):
        rng = np.random.default_rng(100)
        diffuse = np.stack([unit(rng.normal(size=768)) for _ in range(50)])
        self.assertEqual(len(set(hierarchical_groups(diffuse)[0])), 1)
        for count in (1, 5):
            same = np.tile(unit(np.ones(768)), (count, 1))
            np.testing.assert_array_equal(hierarchical_groups(same, 12)[0], np.zeros(count))
        duplicate = np.eye(768, dtype='<f4')[[0, 0, 1, 1, 2, 2]]
        labels, _, _ = hierarchical_groups(duplicate, 12)
        self.assertEqual(len(set(labels)), 3)
        self.assertTrue(all(labels[i] == labels[i + 1] for i in (0, 2, 4)))

    def test_cancellation(self):
        def stop():
            raise Cancelled()
        with self.assertRaises(Cancelled):
            hierarchical_groups(self.matrix(), 6, stop)

    def test_large_library_keeps_every_point_and_the_same_hierarchy(self):
        rng = np.random.default_rng(3)
        matrix = rng.normal(0, .002, (4104, 768)).astype('<f4')
        matrix[np.arange(len(matrix)), np.arange(len(matrix)) % 3] += 1
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        before, _, sample_size = hierarchical_groups(matrix, 2)
        after, _, _ = hierarchical_groups(matrix, 3)
        self.assertEqual(sample_size, 1024)
        self.assertEqual(len(after), len(matrix))
        self.assertEqual(adjusted_rand_score(np.arange(len(matrix)) % 3, after), 1.)
        for ident in np.unique(after):
            self.assertEqual(len(set(before[after == ident])), 1)


class LabelTests(unittest.TestCase):
    def test_unrelated_split_cannot_rename_an_unchanged_group(self):
        rows = [(0, 1, 'Galassie lontane e stelle osservate dal telescopio.'),
                (0, 2, 'Galassie lontane. Telescopio e osservazioni astronomiche.'),
                (1, 3, 'Farina burro biscotti. Dolci e pasticceria.'),
                (1, 4, 'Farina burro biscotti. Cottura dolci.'),
                (1, 5, 'Linguaggi di programmazione e compilatori.'),
                (1, 6, 'Compilatori e linguaggi di programmazione.')]
        titles = {1: 'Galassie lontane', 2: 'Telescopi e galassie', 3: 'Biscotti',
                  4: 'Dolci', 5: 'Linguaggi di programmazione', 6: 'Compilatori'}
        before = cluster_topics(rows, titles=titles)
        after = cluster_topics([(2 if doc >= 5 else group, doc, text) for group, doc, text in rows], titles=titles)
        self.assertEqual(before[0], after[0])

    def test_label_stability_with_documents_present_in_multiple_groups(self):
        rows = [(0, 1, 'Galassie lontane e stelle. Telescopi astronomici.'),
                (1, 1, 'Farina burro biscotti.'),
                (0, 2, 'Galassie lontane e stelle. Telescopi astronomici.'),
                (1, 2, 'Farina zucchero dolci.'),
                (1, 3, 'Programmazione linguaggi compilatori.')]
        before = cluster_topics(rows)
        after = cluster_topics([(2 if doc == 3 else group, doc, text) for group, doc, text in rows])
        self.assertEqual(before[0], after[0])

    def test_navigation_and_acknowledgments_do_not_name_a_topic(self):
        text = ('Galassie lontane e telescopi.\n\n'
                'Japanese Translation Spanish Translation Simplified Chinese Translation\n'
                'Thanks to Ada Lovelace and Alan Turing for reading drafts of this.\n'
                'https://example.org/unrelated-navigation')
        labels = cluster_topics([(0, 1, text), (0, 2, text)])
        joined = ' '.join(labels[0])
        for unwanted in ('translation', 'japanese', 'ada', 'turing', 'navigation'):
            self.assertNotIn(unwanted, joined)
        self.assertIn('galassie', joined)

    def test_translation_as_actual_subject_is_retained(self):
        text = 'Machine translation improves translation quality. Neural translation uses language models.'
        labels = cluster_topics([(0, 1, text), (0, 2, text)])
        self.assertIn('translation', ' '.join(labels[0]))

    def test_title_cannot_invent_a_theme_absent_from_a_passage(self):
        labels = cluster_topics([(0, 1, 'Farina burro biscotti. Farina zucchero.'),
                                 (0, 2, 'Farina burro biscotti. Impasto in forno.')],
                                titles={1: 'Galassie lontane', 2: 'Galassie e telescopi'})
        self.assertNotIn('galassie', ' '.join(labels[0]))
        self.assertIn('farina', ' '.join(labels[0]))

    def test_phrases_do_not_cross_punctuation_or_stopwords(self):
        extracted = set(terms('Galassie, biscotti. Stelle e farina. Linguaggi di programmazione.'))
        for false_phrase in ('galassie biscotti', 'biscotti stelle', 'stelle farina', 'linguaggi programmazione'):
            self.assertNotIn(false_phrase, extracted)
        self.assertIn('programming languages', set(terms('Lisp programming languages.')))

    def test_related_inflections_do_not_repeat_as_three_topics(self):
        labels = cluster_topics([(0, 1, 'Write writing writers. Writing essays. Essay structure.'),
                                 (0, 2, 'Writers write essays. Writing helps thinking.')])[0]
        seen = set()
        for label in labels:
            self.assertFalse(canonical(label) & seen)
            seen.update(canonical(label))


class ArchiveTopicTests(unittest.TestCase):
    def test_map_names_ids_examples_coordinates_and_search_remain_consistent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = Archive(root / 'data')
            stop = threading.Event()
            rng = np.random.default_rng(72)
            centers = [unit(rng.normal(size=768)) for _ in range(3)]
            texts = ['Galassie stelle telescopi.', 'Farina burro biscotti.', 'Programmazione compilatori linguaggi.']
            for i in range(18):
                path = root / f'documento-{i}.txt'
                path.write_text(texts[i % 3] + f' Documento {i}.')
                archive.add([path])
                doc = archive.prepare(archive.pending()[-1], stop)
                vector = unit(centers[i % 3] + rng.normal(0, .003, 768))
                archive.page_commit(doc, 1, [('testo', path.read_text(), vector)])
                archive.update(doc['id'], status='ready')
            with archive.connect() as db:
                before = [tuple(row) for row in db.execute('SELECT * FROM chunks ORDER BY id')]
            found = archive.search(centers[0], stop)
            for scope in ('documents', 'passages'):
                earlier = archive.map(stop, 3, scope=scope)
                later = archive.map(stop, 4, scope=scope)
                self.assertEqual(earlier, archive.map(stop, 3, scope=scope))
                for old in earlier:
                    new = next(p for p in later if p['point_id'] == old['point_id'])
                    self.assertEqual((old['x'], old['y']), (new['x'], new['y']))
                    old_members = {p['point_id'] for p in earlier if p['cluster'] == old['cluster']}
                    new_members = {p['point_id'] for p in later if p['cluster'] == old['cluster']}
                    if old_members == new_members:
                        self.assertEqual(old['topics'], new['topics'])
                        self.assertEqual(old['topic_examples'], new['topic_examples'])
                    self.assertTrue(new['topic_examples'])
            self.assertEqual(found, archive.search(centers[0], stop))
            with archive.connect() as db:
                self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM chunks ORDER BY id')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
