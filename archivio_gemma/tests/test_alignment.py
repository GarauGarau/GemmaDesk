"""Cross-corpus regressions: audited weights, tiny shares, picking and cache."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from core import Archive,Engine
from essay_dataset import clean,prepare
from camera_preprocess import words
from chart_scale import axis_maximum,tick_label
from timeline import snapshot,aggregate,available


class EssayPipeline(unittest.TestCase):
    def test_consensus_finds_new_groups_but_rejects_projection_only_density(self):
        from topic_residuals import discover_consensus
        rng=np.random.default_rng(7);matrix=rng.normal(0,.005,(210,8)).astype(np.float32)
        matrix[:90,0]+=1;matrix[90:180,1]+=1
        matrix[180:,:2]=0;matrix[180:,2:]=rng.normal(size=(30,6))
        matrix/=np.linalg.norm(matrix,axis=1)[:,None]
        def reduce(values,neighbors,seed):
            return values[:,:2]+np.random.default_rng(seed).normal(0,.001,(len(values),2)),None
        result,reports,_=discover_consensus(matrix,np.full(210,-1),np.arange(210)%70,reduce)
        self.assertTrue(np.all(result[:180]>=0));self.assertTrue(np.all(result[180:]==-1))
        self.assertEqual(reports[0]['topics'],2)

    def test_geometry_cache_depends_on_vectors_and_parameters(self):
        from topic_discovery import cached_reduction
        from scipy import sparse
        matrix=np.eye(20,8,dtype=np.float32)
        with tempfile.TemporaryDirectory() as tmp:
            with patch('topic_discovery.reduce_embeddings',return_value=(matrix[:,:2],sparse.eye(20))) as fit:
                cached_reduction(matrix,10,42,Path(tmp));cached_reduction(matrix.copy(),10,42,Path(tmp))
                self.assertEqual(fit.call_count,1)
                cached_reduction(matrix+.01,10,42,Path(tmp));cached_reduction(matrix,12,42,Path(tmp))
                self.assertEqual(fit.call_count,3)

    def test_parliamentary_fragments_never_remove_a_substantive_clause(self):
        from camera_procedure_fragments import reason
        for text,role in [('Chiedo di parlare.','speech'),('Il Governo?','chair'),
            ('Onorevole Rossi, deve concludere.','chair'),('Passiamo all’emendamento 6.101 Guerra.','chair')]:
            self.assertTrue(reason(text,role),text)
        for text in ['Ospedali senza medici.','Grazie, ministro, la sanità ha bisogno di medici.',
            'Ha chiesto di parlare, ma ritengo sbagliata la scelta.',
            'Il Governo? La guerra deve finire.', 'Prego, ministro. Le vittime meritano giustizia.']:
            self.assertFalse(reason(text,'chair'),text)

    def test_local_recovery_retains_novel_or_lexically_conflicting_passages(self):
        from scipy import sparse
        from topic_recovery import recover
        labels=np.array([0]*90+[1]*90+[-1]*4)
        matrix=np.array([[1.,0.,0.]]*90+[[0.,1.,0.]]*90+[[1.,0.,0.],[0.,1.,0.],[0.,0.,1.],[1.,0.,0.]],dtype=np.float32)
        bow=sparse.csr_matrix([[1.,0.]]*90+[[0.,1.]]*90+[[1.,0.],[0.,1.],[1.,0.],[0.,1.]])
        graph=np.zeros((184,184),dtype=np.float32)
        graph[:90,:90]=1;graph[90:180,90:180]=1
        graph[180,:90]=1;graph[181,90:180]=1;graph[182:,:90]=1;np.fill_diagonal(graph,0)
        result,report=recover(matrix,bow,labels,labels.copy(),sparse.csr_matrix(graph),np.arange(184),sparse.eye(2,format='csr'),[0,1])
        self.assertEqual(result[-4:].tolist(),[0,1,-1,-1]);self.assertEqual(report['recovered'],2)
        self.assertGreaterEqual(report['document_holdout_members'],30)

    def test_cleaning_is_conservative_and_audited(self):
        text='A Title\n\nFebruary 2022\n\nWriting helps you think.\n\nNotes\n\n[1] Code: (map f xs).\n\nThanks to Jane for reading drafts of this.\n\nFrench Translation\n\nThanks to science we understand nature.'
        result,audit=clean(text,'A Title','February 2022')
        self.assertNotIn('French Translation',result);self.assertNotIn('reading drafts',result)
        self.assertIn('Code: (map f xs)',result);self.assertIn('Thanks to science',result)
        self.assertEqual(words(result),sum(r['words'] for r in audit if r['kept']))
        for r in audit:self.assertEqual(words(text[r['start']:r['end']]),r['words'])

    def test_english_profile_indexes_without_overlap_and_query_weights_words(self):
        class Fake(Engine):
            def start(self,stop,notify):pass
            def tokens(self,text,stop):return len(text)
            def encode_many(self,texts,stop):
                self.stats['text_embeddings']+=len(texts)
                return [np.eye(1,768,dtype='<f4')[0] for _ in texts]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'testi/essay.txt';source.parent.mkdir();(root/'metadati').mkdir()
            original='Essay\n\nFebruary 2022\n\n'+('Writing explores ideas. '*220)+'\n\nNotes\n\n[1] Keep this note.'
            source.write_text(original)
            (root/'metadati/essay.json').write_text(json.dumps(dict(date='February 2022',sha256=hashlib.sha256(original.encode()).hexdigest())))
            archive=Archive(root/'archive');archive.add([source]);doc=archive.pending()[0];archive.update(doc['id'],title='Essay')
            prepare(archive);self.assertFalse(available(archive))
            stop=threading.Event();result=archive.index(Fake(archive.data,text_only=True,batch_size=4),stop,lambda _:None)
            self.assertEqual(result['errors'],0);self.assertEqual(result['overlap_max_tokens'],0)
            with archive.connect() as db:
                parts=db.execute('SELECT text FROM chunks ORDER BY page').fetchall()
                weights=db.execute('SELECT sum(words) FROM camera_passages').fetchone()[0]
            cleaned=(archive.data/'testi_tematici'/f'{doc["id"]}.txt').read_text()
            self.assertEqual(''.join(r[0] for r in parts),cleaned)
            self.assertEqual(weights,words(cleaned));self.assertEqual(source.read_text(),original)
            data=snapshot(archive,np.eye(1,768)[0],stop,'ideas')
            self.assertEqual(data['kind'],'essays');self.assertTrue(data['weighted'])
            rows=aggregate(data,.7,'year');self.assertEqual(rows[0]['period'],'2022')
            self.assertEqual(rows[0]['indexed_words'],weights);self.assertEqual(rows[0]['word_share'],1.)
            self.assertEqual(aggregate(data,1.,'year')[0]['coverage'],1.)

    def test_query_word_weight_differs_from_chunk_count(self):
        data=dict(sessions=[dict(session_date='2020-01',kept_words=1010,status='ready',passages=[dict(score=.9,words=10),dict(score=.1,words=1000)])])
        row=aggregate(data,.7,'year')[0]
        self.assertEqual(row['passage_share'],.5);self.assertAlmostEqual(row['word_share'],10/1010)


class Views(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])

    def test_tiny_percentages_and_full_overview(self):
        from history_gui import StackedYearChart
        from timeline_gui import TimelineChart
        from map_style import COLORS
        chart=StackedYearChart(COLORS)
        data=dict(rows=[dict(total=100000,groups={3:30})],weighted=True,groups={3:'Small'})
        chart.set_data(data);self.assertEqual(chart.axis_maximum(),100)
        chart.set_data(data,3);self.assertLess(chart.axis_maximum(),.1)
        query=TimelineChart();query.set_rows([dict(indexed_passages=10000,passage_share=.0002)],'passage_share')
        self.assertLess(query.axis_maximum(),.1)
        labels=[tick_label(query.axis_maximum()*i/4,query.axis_maximum()) for i in range(5)]
        self.assertEqual(len(set(labels)),5)
        self.assertEqual(axis_maximum([None,0],percent=True),1.)

    def test_large_cloud_exact_selection_overlap_zoom_and_resize(self):
        from app import Map
        points=[dict(id=i,point_id=i,chunk_id=i,page=1,name=str(i),path='/tmp/x',cluster=i%3,x=(i%40)/40-.5,y=(i//40)/40-.5) for i in range(1600)]
        # Identical embeddings must remain selectable individually.
        points[1].update(x=points[0]['x'],y=points[0]['y'])
        view=Map();view.resize(900,600);view.show();view.set_data(points);self.qt.processEvents()
        self.assertEqual(len(view.scene().items()),1);self.assertIsNotNone(view.cloud)
        position=view.mapFromScene(points[0]['x']*550,-points[0]['y']*550)
        self.assertTrue({0,1}<={p['id'] for p in view.documents_at(position)})
        picked=[];view.chosen.connect(picked.append)
        QTest.mouseClick(view.viewport(),Qt.MouseButton.LeftButton,pos=position);self.qt.processEvents()
        self.assertTrue(picked);self.assertIn(picked[0]['id'],{0,1})
        view.highlight_group(2);self.assertTrue(all(points[i]['cluster']==2 for i in view.cloud.foreground))
        view.highlight([6],{6});self.assertEqual(view.cloud.foreground.tolist(),[6])
        view.scale(2,2);view._navigated=True;scale=view.transform().m11();view.resize(1000,650);self.qt.processEvents()
        self.assertEqual(view.transform().m11(),scale)
        view.grab();view.close()

    def test_projection_cache_and_invalidation(self):
        from map_projection import project
        from umap import UMAP
        rng=np.random.default_rng(5);matrix=rng.normal(size=(80,12)).astype(np.float32)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);ids=np.arange(80)
            first=project(matrix,ids,root,'passages',None,lambda:None,lambda _:None)
            self.assertEqual(first.shape,(80,2));self.assertTrue(np.isfinite(first).all())
            with patch.object(UMAP,'fit_transform',side_effect=AssertionError('Cache not reused')):
                second=project(matrix,ids,root,'passages',None,lambda:None,lambda _:None)
                np.testing.assert_array_equal(first,second)
                with self.assertRaises(AssertionError):project(matrix+.01,ids,root,'passages',None,lambda:None,lambda _:None)

    def test_legend_collapse_and_resize_preserves_selected_topic(self):
        from app import Window
        with tempfile.TemporaryDirectory() as tmp:
            archive=Archive(Path(tmp));window=Window(archive);window.show();window.tabs.setCurrentIndex(1)
            import time
            deadline=time.monotonic()+10
            for _ in range(10):self.qt.processEvents();time.sleep(.01)
            while window.task:
                self.qt.processEvents();time.sleep(.01);self.assertLess(time.monotonic(),deadline)
            points=[dict(id=i,point_id=i,name=str(i),path='/tmp/x',page=1,cluster=i%3,x=i%40/40,y=i//40/40,topics=['test'],topic_run='first') for i in range(1600)]
            window.map_done(points);self.qt.processEvents();window.select_group(1)
            height=window.map.viewport().height();scale=window.map.transform().m11()
            window.legend_toggle.setChecked(False);self.qt.processEvents()
            self.assertFalse(window.legend.isVisible());self.assertGreater(window.map.viewport().height(),height)
            self.assertEqual(window.map.active_group,1);self.assertEqual(window.map.transform().m11(),scale)
            window.legend_toggle.setChecked(True);self.qt.processEvents();self.assertTrue(window.legend.isVisible())
            window.map_done([dict(p,topic_run='new',topics=['different subject']) for p in points]);self.qt.processEvents()
            self.assertIsNone(window.map.active_group)
            window.close()


if __name__=='__main__':unittest.main()
