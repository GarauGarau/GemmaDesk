"""Playback, switching results, seeking and confirmation on actual local MP4s.

Audio output is disabled only for tests: the sandbox cannot reach PulseAudio.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import shutil
import numpy as np
from video_gemma.app import QApplication, Window, STYLE, QMessageBox, QTimer, Qt, QFileDialog, QLineEdit
from PySide6.QtTest import QTest
from PySide6.QtCore import QPoint, QPointF, QMimeData, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtMultimedia import QMediaPlayer
from video_gemma.common import run
from video_gemma.library import Library
from test_pipeline import FakeEngine


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(STYLE)
        cls.temp = tempfile.TemporaryDirectory(prefix='gemmavideo-gui-')
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / 'source'
        cls.source.mkdir()
        for name, color in [('rosso', 'red'), ('blu', 'blue')]:
            run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color={color}:s=320x180:r=10:d=12',
                 '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '12',
                 '-c:v', 'libx264', '-threads', '2', '-c:a', 'aac', '-y', cls.source / f'{name}.mp4'], threading.Event())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=self.root)
        self.library = Library(Path(self.folder.name))
        self.library.scan(self.source, threading.Event())
        self.library.index(FakeEngine(), 4, threading.Event())
        self.window = Window(self.library.data, self.source, 'cpu', audio_enabled=False)
        self.window.show()
        self.window.tabs.setCurrentIndex(1)
        query = np.zeros(768)
        query[0] = 1
        self.results = self.library.search(query)

    def tearDown(self):
        self.window.close()
        self.wait_until(lambda: self.window.worker is None)
        self.app.processEvents()
        self.folder.cleanup()

    def wait_until(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            QTest.qWait(30)
        self.assertTrue(predicate())

    def test_real_playback_waveform_seek_and_switch_results(self):
        w = self.window
        w.show_results(self.results)
        self.wait_until(lambda: w.player.duration() > 0)
        w.toggle_play()
        self.wait_until(lambda: w.player.position() > 400 and not w.video.frame.isNull())
        self.assertEqual(w.player.error(), QMediaPlayer.Error.NoError)
        self.assertGreater(len(w.waveform.peaks), 100)
        self.assertGreater(float(np.abs(w.waveform.peaks).max()), .05)
        self.assertEqual(len(w.waveform.intervals), 1)
        before = w.player.source()
        w.result_list.setCurrentRow(1)
        self.wait_until(lambda: w.player.source() != before and w.player.duration() > 0)
        self.assertEqual(w.current_result['name'], self.results[1]['name'])
        w.toggle_play()
        self.wait_until(lambda: w.player.position() > 300)
        w.player.pause()
        rect = w.waveform.plot_rect()
        QTest.mouseClick(w.waveform, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(rect.left() + .7 * rect.width()), round(rect.center().y())))
        self.wait_until(lambda: abs(w.player.position() - 8400) < 250)
        self.assertFalse(w.video.frame.isNull())

    def test_interval_navigation_and_best_match_opens_first(self):
        result = dict(self.results[0])
        result['intervals'] = [dict(start=1, end=3, score=.7, modalities={'frame'}),
                               dict(start=8, end=10, score=.9, modalities={'audio'})]
        w = self.window
        w.show_results([result])
        self.wait_until(lambda: w.player.duration() > 0 and w.pending_seek is None)
        self.assertEqual(w.intervals.currentIndex(), 1)
        self.assertAlmostEqual(w.player.position(), 8000, delta=150)
        w.change_interval(-1)
        self.wait_until(lambda: abs(w.player.position() - 1000) < 150)
        self.assertEqual(w.intervals.currentIndex(), 0)

    def test_reset_confirmation_cancel_then_confirm_keeps_originals(self):
        w = self.window
        count = sum(v['vectors'] for v in self.library.videos())
        def choose(text):
            box = QApplication.activeModalWidget()
            self.assertIsInstance(box, QMessageBox)
            self.assertEqual(box.defaultButton().text(), 'Annulla')
            next(button for button in box.buttons() if button.text() == text).click()
        QTimer.singleShot(60, lambda: choose('Annulla'))
        w.confirm_reset()
        self.assertEqual(count, sum(v['vectors'] for v in self.library.videos()))
        QTimer.singleShot(60, lambda: choose('Elimina tutti'))
        w.confirm_reset()
        self.wait_until(lambda: w.worker is None)
        self.assertEqual(sum(v['vectors'] for v in self.library.videos()), 0)
        self.assertEqual(len(list(self.source.glob('*.mp4'))), 2)

    def test_missing_source_disables_play_without_losing_results(self):
        result = dict(self.results[0], path=str(self.root / 'unplugged.mp4'))
        self.window.show_results([result])
        self.assertFalse(self.window.play_button.isEnabled())
        self.assertIn('Ricollega', self.window.playback_note.text())
        self.assertEqual(self.window.result_list.count(), 1)

    def test_search_explains_missing_files_before_loading_model(self):
        with self.library.connect() as db:
            db.execute("UPDATE videos SET path=path || '.missing'")
        self.window.query.setText('Una descrizione del video')
        self.window.search()
        self.assertIsNone(self.window.worker)
        self.assertIsNone(self.window.engine)
        self.assertIn('File video non disponibili', self.window.search_summary.text())

    def test_add_files_button_imports_and_selects_new_video(self):
        path = Path(self.folder.name) / 'nuovo.MP4'
        shutil.copy2(self.source / 'rosso.mp4', path)
        w = self.window
        with patch.object(QFileDialog, 'getOpenFileNames', return_value=([str(path)], '')):
            w.add_files_button.click()
        self.wait_until(lambda: w.worker is None)
        video = next(v for v in self.library.videos() if v['path'] == str(path))
        self.assertEqual(w.selected_ids(), [video['id']])
        self.assertEqual(video['status'], 'in coda')
        self.assertIn('1 video aggiunti', w.import_summary.text())
        self.assertTrue(w.selected_button.isEnabled())
        self.assertTrue(w.remove_button.isEnabled())
        w.close()
        self.window = Window(self.library.data, audio_enabled=False)
        self.assertEqual(self.window.source_edit.text(), str(path.parent))

    def test_real_file_picker_imports_selected_file(self):
        path = Path(self.folder.name) / 'nuovo con spazi.MP4'
        shutil.copy2(self.source / 'rosso.mp4', path)
        self.window.source_edit.setText(str(path.parent))
        def pick():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QFileDialog)
            self.assertTrue(dialog.testOption(QFileDialog.Option.DontUseNativeDialog))
            # Simulate the filename field; selectFile() only initializes a dialog
            # before show() and does not select an item in this visible picker.
            dialog.findChild(QLineEdit, 'fileNameEdit').setText(str(path))
            QTimer.singleShot(100, dialog.accept)
        guard = QTimer(self.window)
        guard.setSingleShot(True)
        guard.timeout.connect(lambda: QApplication.activeModalWidget() and QApplication.activeModalWidget().reject())
        guard.start(4000)
        QTimer.singleShot(100, pick)
        self.window.add_files_button.click()
        guard.stop()
        self.wait_until(lambda: self.window.worker is None)
        self.assertTrue(any(v['path'] == str(path) for v in self.library.videos()))

    def test_drop_local_video_adds_it_to_library(self):
        path = Path(self.folder.name) / 'trascinato.mp4'
        shutil.copy2(self.source / 'rosso.mp4', path)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        enter = QDragEnterEvent(QPoint(30, 30), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.window, enter)
        self.assertTrue(enter.isAccepted())
        drop = QDropEvent(QPointF(30, 30), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.window, drop)
        self.assertTrue(drop.isAccepted())
        self.wait_until(lambda: self.window.worker is None)
        self.assertTrue(any(v['path'] == str(path) for v in self.library.videos()))

    def test_add_folder_and_missing_filter_show_source_paths(self):
        w = self.window
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=str(self.source)):
            w.browse_button.click()
        self.wait_until(lambda: w.worker is None)
        self.assertEqual(len(w.selected_ids()), 2)
        self.assertIn('2 già presenti', w.import_summary.text())
        video = self.library.videos()[0]
        with self.library.connect() as db:
            db.execute('UPDATE videos SET path=? WHERE id=?', (str(self.root / 'missing.mp4'), video['id']))
        w.library_filter.setCurrentIndex(w.library_filter.findData('missing'))
        self.assertEqual(w.table.rowCount(), 1)
        self.assertEqual(w.table.item(0, 5).text(), 'File mancante')
        self.assertEqual(w.table.item(0, 6).text(), str(self.root / 'missing.mp4'))

    def test_remove_selected_confirmation_does_not_delete_originals(self):
        w = self.window
        first = self.library.videos()[0]
        w.table.selectRow(0)
        def choose(text):
            box = QApplication.activeModalWidget()
            self.assertEqual(box.defaultButton().text(), 'Annulla')
            self.assertIn('rimangono sul disco', box.informativeText())
            next(button for button in box.buttons() if button.text() == text).click()
        QTimer.singleShot(60, lambda: choose('Annulla'))
        w.remove_button.click()
        self.assertEqual(len(self.library.videos()), 2)
        QTimer.singleShot(60, lambda: choose('Rimuovi dall’archivio'))
        w.remove_button.click()
        self.wait_until(lambda: w.worker is None)
        self.assertEqual(len(self.library.videos()), 1)
        self.assertTrue(Path(first['path']).is_file())
        self.assertFalse(self.library.waveform_path(first['id']).exists())
        self.assertEqual(w.table.rowCount(), 1)

    def test_empty_library_does_not_automatically_readd_removed_videos(self):
        self.window.close()
        self.library.remove([v['id'] for v in self.library.videos()])
        self.window = Window(self.library.data, self.source, 'cpu', audio_enabled=False)
        self.window.show()
        QTest.qWait(200)
        self.assertIsNone(self.window.worker)
        self.assertEqual(self.window.table.rowCount(), 0)


if __name__ == '__main__':
    unittest.main()
