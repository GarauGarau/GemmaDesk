"""Dates, annual stacked totals and integrated map search; isolated fixtures."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('OMP_NUM_THREADS','4')
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')
from pathlib import Path
import csv,json,sys,tempfile,threading,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from core import Archive
from timeline import publication_date,catalog,snapshot,aggregate,yearly_composition
from timeline_gui import export_csv
import app as gui


class Fixture:
    def __init__(self,root,camera=False):
        self.root=Path(root);self.archive=Archive(self.root/'archive');self.stop=threading.Event()
        self.corpus=self.root/'corpus'/'testi';self.corpus.mkdir(parents=True)
        (self.corpus.parent/'metadati').mkdir()
        self.vectors=np.eye(2,768,dtype='<f4');self.camera=camera
        if camera:
            with self.archive.connect() as db:
                db.execute('CREATE TABLE camera_sessions(slug,session_date,document,title,url,clean_source,kept_words)')

    def add(self,name,date,groups=(0,),status='ready'):
        path=self.corpus/(name+'.txt');path.write_text(f'{name} · Galassie e telescopi. Biscotti farina e burro.')
        ident=self.archive.add([path])['added'][0]['id']
        doc=self.archive.prepare(next(d for d in self.archive.pending() if d['id']==ident),self.stop)
        for page,group in enumerate(groups,1):
            self.archive.page_commit(doc,page,[('testo','Galassie e telescopi' if group==0 else 'Biscotti farina burro',self.vectors[group])])
        self.archive.update(ident,status=status,pages=len(groups)+(2 if status!='ready' else 0))
        if self.camera:
            with self.archive.connect() as db:
                db.execute('INSERT INTO camera_sessions VALUES(?,?,?,?,?,?,?)',(name,publication_date(date),ident,name,'https://www.camera.it',str(path),10))
        else:
            meta=dict(date=date,title=name,url=f'https://paulgraham.com/{name}.html',sha256=doc['sha'],words=10)
            (self.corpus.parent/'metadati'/(name+'.json')).write_text(json.dumps(meta))
        return ident


class DataTests(unittest.TestCase):
    def test_dates_retain_precision_and_validate_calendar(self):
        self.assertEqual(publication_date('February 2009'),'2009-02')
        self.assertEqual(publication_date('May 2001'),'2001-05')
        self.assertEqual(publication_date('2020-02-29'),'2020-02-29')
        self.assertEqual(publication_date('Spring 2005'),'2005')
        self.assertEqual(publication_date('2004'),'2004')
        for value in ('','unknown','2021-02-29','2020-13','2024-01-99'):
            self.assertEqual(publication_date(value),'')

    def test_essays_use_publication_dates_keep_undated_searchable_and_handle_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Fixture(tmp);f.add('primo','January 2020',(0,0));f.add('parziale','March 2022',(0,),'paused')
            f.add('solo-anno','2023',(0,));f.add('senza-data','',(0,));f.add('coda','May 2024',(),'queued')
            data=snapshot(f.archive,f.vectors[0],f.stop,'galassie')
            self.assertEqual(data['kind'],'essays');self.assertEqual(data['undated_documents'],1)
            self.assertEqual(data['annual_only_documents'],1)
            self.assertEqual(len(f.archive.search(f.vectors[0],f.stop)),4)
            annual=aggregate(data,.7,'year')
            self.assertEqual([r['period'] for r in annual],['2020','2021','2022','2023','2024'])
            self.assertEqual([r['matched_passages'] for r in annual],[2,0,1,1,0])
            self.assertEqual(annual[2]['partial_sessions'],1);self.assertEqual(annual[2]['coverage'],0)
            self.assertEqual(sum(r['indexed_passages'] for r in aggregate(data,.7,'month')),3)
            path=Path(tmp)/'export.csv';export_csv(path,data,.7,'year')
            with path.open(encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream,delimiter=';'))
            self.assertIn('documenti_archivio',rows[0]);self.assertEqual(rows[0]['documenti_senza_data'],'1')
            self.assertNotIn('sedute_archivio',rows[0])

    def test_metadata_for_another_version_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Fixture(tmp);f.add('saggio','May 2004')
            path=f.corpus.parent/'metadati'/'saggio.json';meta=json.loads(path.read_text());meta['sha256']='different';path.write_text(json.dumps(meta))
            self.assertEqual(catalog(f.archive)['sessions'],[])

    def test_stacked_totals_count_documents_or_chunks_once_with_empty_years(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Fixture(tmp);one=f.add('uno','January 2020',(0,1));two=f.add('due','June 2022',(1,));missing=f.add('nessuna','',(0,))
            data=catalog(f.archive)
            points=[dict(id=one,cluster=0,topics=['galassie']),dict(id=two,cluster=1,topics=['biscotti']),dict(id=missing,cluster=0)]
            composition=yearly_composition(data,points+[points[0]])
            self.assertEqual([r['total'] for r in composition['rows']],[1,0,1]);self.assertEqual(composition['undated_points'],1)
            points=[dict(id=one,chunk_id=1,cluster=0),dict(id=one,chunk_id=2,cluster=1),dict(id=two,chunk_id=3,cluster=1)]
            composition=yearly_composition(data,points,'passages')
            self.assertEqual([r['total'] for r in composition['rows']],[2,0,1])
            self.assertEqual(composition['rows'][0]['groups'],{0:1,1:1})
            for row in composition['rows']:self.assertEqual(row['total'],sum(row['groups'].values()))


class HistoryGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt=QApplication.instance() or QApplication([]);cls.qt.setStyleSheet(gui.STYLE)

    def settle(self,w):
        end=time.monotonic()+30
        for _ in range(5):self.qt.processEvents();time.sleep(.01)
        while w.task and time.monotonic()<end:self.qt.processEvents();time.sleep(.01)
        self.assertIsNone(w.task)

    def test_map_search_query_history_and_stacked_overview_in_both_collections(self):
        for camera in (True,False):
            with self.subTest(camera=camera),tempfile.TemporaryDirectory() as tmp:
                f=Fixture(tmp,camera);f.add('galassie','January 2020',(0,0));f.add('biscotti','March 2022',(1,));f.add('parziale','May 2022',(0,),'paused')
                f.add('futuro','June 2023',(),'queued')
                w=gui.Window(f.archive,corpus=f.corpus,collection='Camera dei deputati' if camera else 'Paul Graham')
                w.show();w.tabs.setCurrentIndex(1);self.settle(w)
                self.assertEqual(w.map_scope.currentData(),'passages' if camera else 'documents')
                w.map_scope.setCurrentIndex(0);self.settle(w)
                class Engine:
                    calls=0
                    def start(self,*args):pass
                    def query(self,*args):self.calls+=1;return f.vectors[0]
                engine=Engine();w.get_engine=lambda:engine
                try:
                    self.assertTrue(w.map_query.isVisible());self.assertTrue(w.map_searchbutton.isVisible())
                    self.assertEqual(w.history.mode,'overview');self.assertEqual(sum(w.history.stacked.totals()),3)
                    w.map_scope.setCurrentIndex(1);self.settle(w)
                    self.assertEqual(sum(w.history.stacked.totals()),4)
                    self.assertEqual(sum(w.history.stacked.totals()),len(w.map.items_data))
                    # The stack uses exactly the same cluster identities as the map.
                    for row in w.history.stacked.data['rows']:
                        for group,count in row['groups'].items():
                            dates={s['document']:s['session_date'][:4] for s in w.history_catalog['sessions']}
                            self.assertEqual(count,sum(p['cluster']==group and dates[p['id']]==row['period'] for p in w.map.items_data))
                    self.qt.processEvents();chart=w.history.stacked
                    rect,row,group,count=chart.regions[0]
                    QTest.mouseClick(chart,Qt.MouseButton.LeftButton,pos=rect.center().toPoint());self.qt.processEvents()
                    self.assertEqual(w.map.active_group,group);self.assertEqual(w.history.mode,'group')
                    w.history.reset.click();self.qt.processEvents();self.assertEqual(w.history.mode,'overview')
                    w.expand_map.click();self.qt.processEvents();self.assertTrue(w.map_query.isVisible())
                    w.map_query.setText('galassie');self.assertEqual(w.query.text(),'galassie')
                    QTest.keyClick(w.map_query,Qt.Key.Key_Return);self.settle(w)
                    self.assertEqual(engine.calls,1);self.assertEqual(w.history.mode,'query')
                    self.assertTrue(w.history.query_chart.isVisible());self.assertTrue(w.map_search_results.isVisible())
                    self.assertEqual(w.results.toPlainText(),w.map_search_results.toPlainText())
                    self.assertIn('Documento parziale',w.map_search_results.toPlainText())
                    self.assertEqual(sum(r['matched_passages'] for r in w.history.rows),3)
                    self.assertEqual(w.map.matches,{r['id'] for r in w.search_rows})
                    w.resize(1080,750);self.qt.processEvents()
                    self.assertEqual(w.size().width(),1080);self.assertEqual(w.size().height(),750)
                    out=Path(__file__).resolve().parents[2]/'verifiche/ricerca-e-andamento-2026-10-28'
                    w.grab().save(str(out/('camera-ricerca-test.png' if camera else 'paul-graham-ricerca-test.png')))
                    w.show_timeline('2022');self.qt.processEvents()
                    self.assertEqual(w.timeline_window.period,'2022')
                    w.timeline_window.close()
                    w.map_clearbutton.click();self.qt.processEvents()
                    self.assertFalse(w.query.text());self.assertFalse(w.map_query.text());self.assertEqual(w.history.mode,'overview')
                    w.resize(1080,750);self.qt.processEvents()
                    self.assertEqual(w.size().width(),1080);self.assertEqual(w.size().height(),750)
                    out=Path(__file__).resolve().parents[2]/'verifiche/ricerca-e-andamento-2026-10-28'
                    w.grab().save(str(out/('camera-test.png' if camera else 'paul-graham-test.png')))
                finally:
                    if w.task:w.cancel();self.settle(w)
                    w.close();self.qt.processEvents()


if __name__=='__main__':unittest.main(verbosity=2)
