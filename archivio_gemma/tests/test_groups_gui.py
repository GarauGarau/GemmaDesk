"""Real mouse interaction with the bottom legend, on isolated document archives."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '4')
os.environ.setdefault('OMP_NUM_THREADS', '4')
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PySide6.QtCore import Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsTextItem, QGraphicsLineItem
import app as gui
from core import Archive


class GroupGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setStyleSheet(gui.STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gemmadesk-groups-')
        self.root = Path(self.temp.name)
        self.archive = Archive(self.root / 'data')
        self.stop = threading.Event()
        self.vectors = np.eye(2, 768, dtype='<f4')
        self.mixed = self.add('Misto', [0, 0, 1])
        self.special = self.add('Documento <due> & tre', [0])
        for i in range(24):
            self.add(f'Galassie {i:02d}', [0])
        for i in range(3):
            self.add(f'Biscotti {i:02d}', [1])
        self.window = gui.Window(self.archive)
        self.window.show()
        self.window.tabs.setCurrentIndex(1)
        self.settle()
        self.window.clusters.setValue(2)
        self.window.refresh_map()
        self.settle()

    def tearDown(self):
        self.settle()
        self.window.close()
        self.qt.processEvents()
        self.temp.cleanup()

    def add(self, title, kinds):
        path = self.root / f'{title}.txt'
        path.write_text(title)
        self.archive.add([path])
        document = self.archive.prepare(self.archive.pending()[-1], self.stop)
        for page, kind in enumerate(kinds, 1):
            text = 'Galassie stelle telescopi.' if kind == 0 else 'Biscotti farina burro.'
            self.archive.page_commit(document, page, [('testo', text, self.vectors[kind])])
        self.archive.update(document['id'], status='ready', pages=len(kinds))
        return document['id']

    def settle(self):
        deadline = time.monotonic() + 20
        for _ in range(8):
            self.qt.processEvents()
            time.sleep(.005)
        while self.window.task and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(.005)
        self.assertIsNone(self.window.task)

    def group_for(self, ident, page=None):
        return next(p['cluster'] for p in self.window.map.items_data
                    if p['id'] == ident and (page is None or p['page'] == page))

    def test_history_toggle_keeps_selection_and_zoom(self):
        w=self.window;group=self.group_for(self.special);self.click_group(group)
        scale=w.map.transform().m11();height=w.map.viewport().height()
        w.history_toggle.click();self.qt.processEvents()
        self.assertFalse(w.history.isVisible());self.assertGreater(w.map.viewport().height(),height)
        self.assertEqual(w.map.active_group,group);self.assertEqual(w.map.transform().m11(),scale)
        w.history_toggle.click();self.qt.processEvents()
        self.assertTrue(w.history.isVisible());self.assertEqual(w.history.stacked.group,group)

    def test_fullscreen_only_map_search_results_then_escape_restores_panels(self):
        w=self.window
        w.history_toggle.setChecked(False);w.legend_toggle.setChecked(False);w.expand_map.setChecked(True)
        self.qt.processEvents();points=w.map.items_data;scale=w.map.transform().m11()
        w.map_query.setText('galassie');w.fullscreen_button.click();self.qt.processEvents()
        self.assertTrue(w.isFullScreen());self.assertIs(w.map.items_data,points)
        for widget in (w.header,w.separator,w.index_controls,w.status,w.progress,w.tabs.tabBar(),
                w.map_controls,w.legend_panel,w.history,w.inspector_tabs.tabBar()):
            self.assertFalse(widget.isVisible(),type(widget).__name__)
        for widget in (w.map,w.map_query,w.map_searchbutton,w.map_clearbutton,w.map_search_results):
            self.assertTrue(widget.isVisible(),type(widget).__name__)
        w.search_done(self.archive.search(self.vectors[0],self.stop));self.qt.processEvents()
        self.assertTrue(w.map_search_results.toPlainText());self.assertFalse(w.history.isVisible())
        self.assertFalse(w.legend_panel.isVisible());self.assertFalse(w.inspector_tabs.tabBar().isVisible())
        w.map.scale(1.2,1.2);scale=w.map.transform().m11()
        w.activateWindow();w.map_query.setFocus();self.qt.processEvents()
        QTest.keyClick(w.map_query,Qt.Key.Key_Escape);self.qt.processEvents()
        self.assertFalse(w.isFullScreen());self.assertIsNone(w._focus_state)
        self.assertTrue(w.header.isVisible());self.assertTrue(w.map_controls.isVisible())
        self.assertFalse(w.history.isVisible());self.assertFalse(w.legend.isVisible())
        self.assertFalse(w.map_inspector.isVisible());self.assertTrue(w.expand_map.isChecked())
        self.assertEqual(w.map_query.text(),'galassie');self.assertEqual(w.map.transform().m11(),scale)

    def click_group(self, ident):
        legend = self.window.legend
        entry = next(legend.item(i) for i in range(legend.count())
                     if legend.item(i).data(Qt.ItemDataRole.UserRole) == ident)
        legend.scrollToItem(entry)
        self.qt.processEvents()
        QTest.mouseClick(legend.viewport(), Qt.MouseButton.LeftButton,
                         pos=legend.visualItemRect(entry).center())
        self.qt.processEvents()
        self.assertEqual(self.window.map.active_group, ident)

    def assert_highlight(self, group):
        for dot in self.window.map.dots.values():
            if dot.item['cluster'] == group:
                self.assertEqual(dot.opacity(), 1.)
                self.assertGreater(dot.pen().widthF(), 1.)
            else:
                self.assertLess(dot.opacity(), .5)

    def test_click_lists_every_document_and_opens_the_correct_file(self):
        w = self.window
        group = self.group_for(self.special)
        self.click_group(group)
        expected = {p['id'] for p in w.map.items_data if p['cluster'] == group}
        self.assertGreater(len(expected), 20)
        self.assertEqual({r['id'] for r in w.group_rows}, expected)
        self.assertEqual(len(w.group_rows), len(expected))
        self.assert_highlight(group)
        for row in w.group_rows:
            self.assertIn(row['name'], w.map_results.toPlainText())
        self.assertIn('Documento <due> & tre.txt', w.map_results.toPlainText())
        row = w.group_rows[-1]
        with patch.object(gui, 'open_document') as opened:
            w.open_result(QUrl(f'groupdoc:{len(w.group_rows)-1}'))
            opened.assert_called_once_with(row['path'], row['page'])
            for url in ('groupdoc:-1', 'groupdoc:999999', 'groupdoc:invalid'):
                w.open_result(QUrl(url))
            self.assertEqual(opened.call_count, 1)
        self.assertEqual(w.map.active_group, group)

    def test_passages_highlight_only_the_group_and_list_documents_once(self):
        w = self.window
        w.map_scope.setCurrentIndex(1)
        self.settle()
        first = self.group_for(self.mixed, 1)
        self.click_group(first)
        self.assert_highlight(first)
        mixed = [r for r in w.group_rows if r['id'] == self.mixed]
        self.assertEqual(len(mixed), 1)
        self.assertEqual(mixed[0]['pages'], [1, 2])
        second = self.group_for(self.mixed, 3)
        self.assertNotEqual(first, second)
        self.click_group(second)
        self.assert_highlight(second)
        index = next(i for i, row in enumerate(w.group_rows) if row['id'] == self.mixed)
        with patch.object(gui, 'open_document') as opened:
            w.open_result(QUrl(f'groupdoc:{index}'))
            opened.assert_called_once_with(w.group_rows[index]['path'], 3)

    def test_search_previous_selection_and_show_all_do_not_pollute_group_filter(self):
        w = self.window
        w.map.select_document(self.mixed)
        found = self.archive.search(self.vectors[1], self.stop)
        w.search_done(found)
        group = self.group_for(self.special)
        self.click_group(group)
        self.assertIsNone(w.map.selected_id)
        self.assert_highlight(group)
        w.search_done(found)
        self.assertIsNone(w.map.active_group)
        self.assertFalse(w.group_rows)
        self.assertFalse(w.legend.selectedItems())
        self.click_group(group)
        w.showallbutton.click()
        self.assertIsNone(w.map.active_group)
        self.assertFalse(w.group_rows)
        self.assertFalse(w.legend.selectedItems())
        self.assertTrue(all(dot.opacity() == 1 for dot in w.map.dots.values()))

    def test_redraw_preserves_group_and_changing_view_clears_stale_members(self):
        w = self.window
        group = self.group_for(self.special)
        self.click_group(group)
        members = {row['id'] for row in w.group_rows}
        w.refresh_map()
        self.settle()
        self.assertEqual(w.map.active_group, group)
        self.assertEqual({r['id'] for r in w.group_rows}, members)
        self.assert_highlight(group)
        w.map_scope.setCurrentIndex(1)
        self.settle()
        self.assertIsNone(w.map.active_group)
        self.assertFalse(w.group_rows)
        self.assertFalse(w.legend.selectedItems())

    def test_empty_map_clears_group_and_disables_its_controls(self):
        w = self.window
        self.click_group(self.group_for(self.special))
        w.map.highlight([])  # also exercise the reset-embeddings callback order
        w.map_done([])
        self.assertIsNone(w.map.active_group)
        self.assertFalse(w.group_rows)
        self.assertFalse(w.showallbutton.isEnabled())
        self.assertIsNone(w.legend.item(0).data(Qt.ItemDataRole.UserRole))

    def test_larger_dots_and_labels_only_in_the_bottom_legend(self):
        w = self.window
        self.assertTrue(all(dot.rect().width() == 16 for dot in w.map.dots.values()))
        self.assertFalse(any(isinstance(item, (QGraphicsTextItem, QGraphicsLineItem)) for item in w.map.scene().items()))
        self.assertNotIn('drawForeground', gui.Map.__dict__)
        self.assertFalse(hasattr(w, 'topic_toggle'))
        self.assertEqual(w.legend.count(), 2)
        for dot in w.map.dots.values():
            self.assertNotIn('Documenti rappresentativi:', dot.toolTip())

    def test_map_stays_in_view_when_window_and_legend_resize(self):
        w = self.window
        for size in ((1080, 750), (1460, 960)):
            w.resize(*size)
            w.legend.setFixedHeight(164)
            self.settle()
            viewport = w.map.viewport().rect()
            for dot in w.map.dots.values():
                self.assertTrue(viewport.contains(w.map.mapFromScene(dot.pos())))


if __name__ == '__main__':
    unittest.main(verbosity=2)
