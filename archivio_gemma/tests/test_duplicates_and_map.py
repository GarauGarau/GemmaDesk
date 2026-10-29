"""Regression tests with isolated archives; no user documents are modified."""
import os,sys,tempfile,threading,unittest,shutil
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')
os.environ.setdefault('OMP_NUM_THREADS','4')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image
from core import Archive,Engine,Cancelled,unit

class ArchiveChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.a=Archive(self.root/'archive');self.stop=threading.Event()
    def tearDown(self):self.tmp.cleanup()
    def pdf(self,name,color):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(50,50),color).save(path);return path
    def ready(self,path,vector):
        result=self.a.add([path]);id=result['added'][0]['id']
        doc=self.a.prepare(next(d for d in self.a.pending() if d['id']==id),self.stop)
        self.a.page_commit(doc,1,[('pagina','Test fixture',vector)]);self.a.update(id,status='ready')
        return id
    def snapshot(self):
        with self.a.connect() as db:
            return ([tuple(r) for r in db.execute('SELECT * FROM documents ORDER BY id')],
                    [tuple(r) for r in db.execute('SELECT * FROM chunks ORDER BY id')])
    def test_pdf_duplicates_queued_same_batch_and_ready_after_restart(self):
        original=self.pdf('originale.pdf','red');copy=self.root/'rinominato.pdf';shutil.copyfile(original,copy)
        result=self.a.add([original,original,copy])
        self.assertEqual(len(result['added']),1);self.assertEqual(len(result['duplicates']),2)
        self.assertEqual(result['duplicates'][1]['existing']['status'],'queued')
        self.assertFalse(self.a.pending()[0]['stored'])
        doc=self.a.prepare(self.a.pending()[0],self.stop)
        self.a.page_commit(doc,1,[('pagina','Fixture visiva',unit(np.ones(768)))]);self.a.update(doc['id'],status='ready')
        before=self.snapshot();self.a=Archive(self.a.data)
        result=self.a.add([copy,original]);self.assertFalse(result['added'])
        self.assertEqual([r['existing']['status'] for r in result['duplicates']],['ready','ready'])
        engine=Engine(self.a.data,'cpu')
        with patch.object(engine,'start',side_effect=AssertionError('Duplicate-only import loaded model')) as start:
            report=self.a.index(engine,self.stop,lambda p:None);start.assert_not_called()
        self.assertEqual(report['text_embeddings']+report['image_embeddings'],0)
        self.assertEqual(report['pages_processed'],0);self.assertEqual(before,self.snapshot())
    def test_equal_names_different_contents_and_changed_existing_source(self):
        first=self.pdf('one/paper.pdf','red');second=self.pdf('two/paper.pdf','blue')
        self.ready(first,unit(np.ones(768)));before=self.snapshot()
        self.assertEqual(len(self.a.add([second])['added']),1)
        first.write_bytes(b'changed source')
        result=self.a.add([first]);self.assertEqual(len(result['rejected']),1)
        self.assertIn('contenuto cambiato',result['rejected'][0]['reason'])
        after=self.snapshot();self.assertEqual(before[1],after[1]);self.assertEqual(before[0][0],after[0][0])
    def test_legacy_queue_fingerprints_and_partial_documents(self):
        original=self.pdf('old.pdf','green');copy=self.root/'copy.pdf';shutil.copyfile(original,copy)
        with self.a.connect() as db:db.execute('INSERT INTO documents(source,name) VALUES(?,?)',(str(original),original.name))
        result=self.a.add([copy]);self.assertEqual(len(result['duplicates']),1)
        doc=self.a.prepare(self.a.pending()[0],self.stop)
        self.a.page_commit(doc,1,[('pagina','Fixture',unit(np.ones(768)))]);self.a.update(doc['id'],status='paused',pages=2)
        before=self.snapshot();result=self.a.add([copy])
        self.assertEqual(result['duplicates'][0]['existing']['status'],'paused');self.assertEqual(before,self.snapshot())
    def test_cancel_import_rolls_back_whole_batch(self):
        files=[self.pdf(f'{i}.pdf',color) for i,color in enumerate(['red','blue'])]
        def notify(message):
            if '2/2' in message:self.stop.set()
        with self.assertRaises(Cancelled):self.a.add(files,self.stop,notify)
        self.assertFalse(self.a.documents())
    def fixture(self,vectors):
        ids=[]
        for i,vector in enumerate(vectors):
            path=self.root/f'{i}.txt';path.write_text(f'Unique fixture {i}');ids.append(self.ready(path,vector))
        return ids
    def test_empty_single_two_identical_and_more_clusters_than_vectors(self):
        self.assertEqual(self.a.map(self.stop),[])
        first=unit(np.eye(1,768,0)[0]);second=unit(np.eye(1,768,1)[0])
        self.fixture([first]);single=self.a.map(self.stop,12)
        self.assertEqual((single[0]['x'],single[0]['y'],single[0]['cluster']),(0.,0.,0))
        path=self.root/'copy-vector.txt';path.write_text('different contents but equal synthetic vector');self.ready(path,first)
        repeated=self.a.map(self.stop,12);self.assertTrue(all(p['x']==p['y']==0 for p in repeated))
        path=self.root/'other-vector.txt';path.write_text('Other vector');self.ready(path,second)
        mapped=self.a.map(self.stop,12)
        self.assertEqual(len(set(p['cluster'] for p in mapped)),2)
        self.assertEqual(mapped,self.a.map(self.stop,12))
        self.assertTrue(all(np.isfinite([p['x'],p['y'],p['explained']]).all() for p in mapped))
    def test_all_ready_documents_clustered_in_768d_and_search_unchanged(self):
        from sklearn.metrics import adjusted_rand_score
        import grouping
        rng=np.random.default_rng(14);centers=[unit(rng.normal(size=768)) for _ in range(3)]
        vectors=[unit(centers[i%3]+rng.normal(0,.005,768)) for i in range(30)];ids=self.fixture(vectors)
        partial=self.root/'partial.txt';partial.write_text('Still pending');self.a.add([partial])
        before=self.a.search(vectors[7],self.stop);observed=[]
        original=grouping.hierarchical_groups
        def inspect(matrix,*args,**kwargs):
            observed.append(matrix.shape);return original(matrix,*args,**kwargs)
        with patch.object(grouping,'hierarchical_groups',inspect):mapped=self.a.map(self.stop,3)
        self.assertEqual(observed,[(30,768)])
        self.assertEqual(set(ids),{p['id'] for p in mapped});self.assertEqual(len(mapped),30)
        self.assertEqual(adjusted_rand_score([i%3 for i in range(30)],[p['cluster'] for p in mapped]),1.)
        self.assertEqual(before,self.a.search(vectors[7],self.stop));self.assertEqual(before[0]['id'],ids[7])
        self.assertEqual(mapped,self.a.map(self.stop,3))
        automatic=self.a.map(self.stop)
        self.assertEqual(len({p['cluster'] for p in automatic}),3)
        self.assertEqual(adjusted_rand_score([i%3 for i in range(30)],[p['cluster'] for p in automatic]),1.)
    def test_diffuse_vectors_are_not_forced_into_artificial_groups(self):
        rng=np.random.default_rng(100)
        self.fixture([unit(rng.normal(size=768)) for _ in range(50)])
        mapped=self.a.map(self.stop)
        self.assertEqual(len({p['cluster'] for p in mapped}),1)
        self.assertLess(mapped[0]['explained'],.1)
    def test_invalid_centroid_is_explicit_and_archive_is_preserved(self):
        ids=self.fixture([unit(np.ones(768))]);self.a.update(ids[0],centroid=np.full(768,np.nan,dtype='<f4').tobytes())
        before=self.snapshot()
        with self.assertRaisesRegex(ValueError,'Vettore della mappa non valido'):self.a.map(self.stop)
        self.assertEqual(before,self.snapshot())

if __name__=='__main__':unittest.main(verbosity=2)
