"""User text folders and sidecars work without any bundled corpus."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'archivio_gemma'))
from core import Archive
from timeline import catalog

spec = importlib.util.spec_from_file_location('documents_cli', ROOT / 'scripts/documents.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CustomInputs(unittest.TestCase):
    def test_recursive_import_preserves_originals_and_uses_sidecar_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'texts' / 'nested' / 'notes.md'
            source.parent.mkdir(parents=True)
            content = 'Energy storage keeps solar electricity available after sunset.\n'
            source.write_text(content)
            sidecar = source.with_suffix('.md.json')
            sidecar.write_text(json.dumps({'title': 'Energy notes', 'date': '2024-06-12'}))
            (source.parent / 'unrelated.csv').write_text('ignore,this\n')
            self.assertEqual(cli.collect([root / 'texts']), [source])
            archive = Archive(root / 'archive')
            result = archive.add(cli.collect([root / 'texts']))
            self.assertEqual(len(result['added']), 1)
            doc = archive.documents()[0]
            self.assertEqual(doc['name'], 'Energy notes')
            dates = catalog(archive, threading.Event())
            self.assertEqual(dates['sessions'][0]['session_date'], '2024-06-12')
            self.assertEqual(source.read_text(), content)
            self.assertEqual(len(archive.add([source])['duplicates']), 1)

    def test_missing_and_empty_folders_fail_before_opening_an_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                cli.collect([Path(directory)])
            with self.assertRaises(ValueError):
                cli.collect([Path(directory) / 'missing'])

    def test_invalid_optional_metadata_does_not_block_text_import(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'notes.txt'
            source.write_text('Original text')
            source.with_suffix('.txt.json').write_text('{bad json')
            archive = Archive(Path(directory) / 'archive')
            self.assertEqual(len(archive.add([source])['added']), 1)
            self.assertEqual(archive.documents()[0]['name'], 'notes.txt')


if __name__ == '__main__':
    unittest.main()
