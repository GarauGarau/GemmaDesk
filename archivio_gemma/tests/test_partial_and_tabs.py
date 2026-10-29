"""Saved partial passages and map tabs, with isolated synthetic vectors only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')
os.environ.setdefault('OMP_NUM_THREADS','4')
from pathlib import Path
import csv
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PySide6.QtCore import Qt,QUrl
from PySide6.QtWidgets import QApplication
import app as gui
from core import Archive,Engine,Cancelled,check
from timeline import snapshot,aggregate,matching_passages
from timeline_gui import export_csv


class SyntheticEngine(Engine):
    """Deterministic test inference; real indexing, persistence and cancellation."""
    def start(self,stop,notify):check(stop)
    def tokens(self,text,stop):check(stop);return len(text)
    def encode_many(self,texts,stop):
        check(stop);self.stats['text_embeddings']+=len(texts)
        return [np.eye(1,768,dtype='<f4')[0] for text in texts]


class PartialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name);self.archive=Archive(self.root/'archive');self.stop=threading.Event()
        self.vector=np.eye(1,768,dtype='<f4')[0]

    def tearDown(self):self.temp.cleanup()

    def document(self,name,text='Salario minimo e lavoro. '):
        path=self.root/name;path.write_text(text)
        ident=self.archive.add([path])['added'][0]['id']
        return self.archive.prepare(next(r for r in self.archive.pending() if r['id']==ident),self.stop)

    def test_partial_pause_restart_and_resume_preserve_saved_vectors(self):
        doc=self.document('lungo.txt','Salario minimo e lavoro. '*150)
        engine=SyntheticEngine(self.archive.data,'cpu',batch_size=1,text_only=True)
        def pause(value):
            if isinstance(value,dict) and value.get('pages_processed',0)==1:self.stop.set()
        with self.assertRaises(Cancelled):self.archive.index(engine,self.stop,pause)
        self.stop.clear()
        with self.archive.connect() as db:
            saved=[tuple(r) for r in db.execute('SELECT id,page,text,vector FROM chunks')]
        self.assertEqual(len(saved),1)
        self.archive=Archive(self.archive.data)
        for state in ('paused','error','processing'):
            self.archive.update(doc['id'],status=state)
            found=self.archive.search(self.vector,self.stop)
            self.assertEqual(len(found),1);self.assertTrue(found[0]['partial'])
            for scope in ('documents','passages'):
                points=self.archive.map(self.stop,scope=scope)
                self.assertEqual(len(points),1);self.assertTrue(points[0]['partial'])
        self.document('vuoto.txt','Ancora da elaborare.')
        self.assertEqual(len(self.archive.search(self.vector,self.stop)),1)
        report=self.archive.index(engine,self.stop,lambda value:None)
        self.assertEqual(report['errors'],0)
        self.assertTrue(all(not r['partial'] for r in self.archive.search(self.vector,self.stop)))
        with self.archive.connect() as db:
            after=[tuple(r) for r in db.execute('SELECT id,page,text,vector FROM chunks WHERE id=?',(saved[0][0],))]
        self.assertEqual(saved,after)

    def test_partial_timeline_counts_saved_passages_without_full_coverage(self):
        with self.archive.connect() as db:
            db.execute('CREATE TABLE camera_sessions(slug,session_date,document,title,url,clean_source,kept_words)')
        for i,state in enumerate(('ready','paused','error','queued')):
            doc=self.document(f'{i}.txt',f'Salario minimo {i}')
            if state!='queued':self.archive.page_commit(doc,1,[('testo','Salario minimo',self.vector)])
            self.archive.update(doc['id'],status=state,pages=1 if state=='ready' else 3)
            with self.archive.connect() as db:
                db.execute('INSERT INTO camera_sessions VALUES(?,?,?,?,?,?,?)',
                    (str(i),'2020-01-01',doc['id'],str(i),'https://www.camera.it',doc['source'],10))
        data=snapshot(self.archive,self.vector,self.stop,'salario minimo')
        self.assertFalse(next(s for s in data['sessions'] if s['status']=='queued')['partial'])
        row=aggregate(data)[0]
        self.assertEqual(row['indexed_passages'],3)
        self.assertEqual(row['indexed_sessions'],3)
        self.assertEqual(row['complete_sessions'],1)
        self.assertEqual(row['partial_sessions'],2)
        self.assertEqual(row['coverage'],.25)
        self.assertEqual(row['passage_share'],1)
        matches=matching_passages(data,'2020-01')
        self.assertEqual(sum(r['partial'] for r in matches),2)
        path=self.root/'export.csv';export_csv(path,data,.7,'month')
        with path.open(encoding='utf-8-sig') as stream:exported=list(csv.DictReader(stream,delimiter=';'))[0]
        self.assertEqual(exported['sedute_parziali'],'2')
        self.assertEqual(exported['copertura_sedute_percento'],'25.0')


class TabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt=QApplication.instance() or QApplication([]);cls.qt.setStyleSheet(gui.STYLE)

    def settle(self,window):
        deadline=time.monotonic()+20
        for _ in range(5):self.qt.processEvents();time.sleep(.01)
        while window.task and time.monotonic()<deadline:self.qt.processEvents();time.sleep(.01)
        self.assertIsNone(window.task)

    def test_both_collections_have_large_map_tab_and_partial_inspector(self):
        for collection in ('Camera dei deputati','Paul Graham'):
            with self.subTest(collection=collection),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);archive=Archive(root/'archive');stop=threading.Event()
                path=root/'parziale.txt';path.write_text('Galassie stelle telescopi.')
                archive.add([path]);doc=archive.prepare(archive.pending()[0],stop)
                vector=np.eye(1,768,dtype='<f4')[0]
                archive.page_commit(doc,1,[('testo',path.read_text(),vector)])
                archive.update(doc['id'],status='paused',pages=4)
                window=gui.Window(archive,collection=collection);window.show();self.settle(window)
                try:
                    self.assertEqual(window.tabs.count(),2)
                    self.assertTrue(window.table.isVisible());self.assertFalse(window.map.isVisible())
                    self.assertIn('1 parziali',window.librarylabel.text())
                    self.assertIn('Parziale',window.table.item(0,1).text())
                    window.stats_toggle.setChecked(True)
                    window.tabs.setCurrentIndex(1);self.qt.processEvents()
                    self.assertTrue(window.map.isVisible());self.assertFalse(window.table.isVisible())
                    self.assertFalse(window.stats_panel.isVisible())
                    width=window.map.width();window.expand_map.click();self.settle(window)
                    self.assertGreater(window.map.width(),width)
                    self.assertFalse(window.map_inspector.isVisible())
                    item=window.map.items_data[0]
                    self.assertEqual(window.map.dots[item['point_id']].pen().style(),Qt.PenStyle.DashLine)
                    window.select_point(item);self.qt.processEvents()
                    self.assertTrue(window.map_inspector.isVisible())
                    self.assertIn('Documento parziale',window.map_results.toPlainText())
                    self.assertIn('1 / 4',window.map_results.toPlainText())
                    rows=archive.search(vector,stop);window.search_done(rows)
                    self.assertIn('Documento parziale',window.results.toPlainText())
                    window.select_group(item['cluster'])
                    self.assertIn('Documento parziale',window.map_results.toPlainText())
                    self.assertEqual(window.search_rows,rows)
                    window.show_all_groups()
                    with patch.object(gui,'open_document') as opened:
                        window.open_result(QUrl('result:0'))
                        opened.assert_called_once_with(rows[0]['path'],rows[0]['page'])
                    window.tabs.setCurrentIndex(0);self.qt.processEvents()
                    self.assertTrue(window.stats_panel.isVisible())
                    self.assertIn('Documento parziale',window.results.toPlainText())
                finally:window.close();self.qt.processEvents()

    def test_suspending_index_refreshes_partial_map_and_unlocks_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=Archive(root/'archive');path=root/'lungo.txt'
            path.write_text('Stelle e galassie. '*200);archive.add([path])
            window=gui.Window(archive);window.show();self.settle(window)
            saved=threading.Event()
            class PausingEngine(SyntheticEngine):
                calls=0
                def encode_many(self,texts,stop):
                    self.calls+=1
                    if self.calls==2:
                        saved.set()
                        while not stop.wait(.01):pass
                        check(stop)
                    return super().encode_many(texts,stop)
            engine=PausingEngine(archive.data,'cpu',batch_size=1,text_only=True)
            window.get_engine=lambda:engine
            try:
                window.index()
                deadline=time.monotonic()+10
                while not saved.is_set() and time.monotonic()<deadline:self.qt.processEvents();time.sleep(.01)
                self.assertTrue(saved.is_set())
                window.stopbutton.click();self.settle(window)
                self.assertTrue(window.searchbutton.isEnabled())
                self.assertTrue(window.startbutton.isEnabled())
                self.assertEqual(len(window.map.items_data),1)
                self.assertTrue(window.map.items_data[0]['partial'])
                self.assertNotIn('Dettagli dell’interruzione',window.results.toPlainText())
                self.assertEqual(archive.documents()[0]['status'],'paused')
            finally:
                if window.task:window.cancel();self.settle(window)
                window.close();self.qt.processEvents()


if __name__=='__main__':unittest.main(verbosity=2)
