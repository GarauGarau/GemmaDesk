"""Fast integrity and input tests, with explicitly synthetic test vectors."""
import json,sys,threading,tempfile,unittest,subprocess,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from segmentation import TEXT_LIMIT,OVERLAP_LIMIT
from core import Archive,Cancelled,Engine,APP,unit

class Integrity(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.a=Archive(Path(self.tmp.name)/'db');self.stop=threading.Event()
    def tearDown(self):self.tmp.cleanup()
    def doc(self,name):
        f=Path(self.tmp.name)/name;f.write_text('text');self.a.add([f]);return self.a.prepare(self.a.pending()[-1],self.stop)
    def test_append_restart_and_exact_image_search(self):
        rng=np.random.default_rng(15);vectors=[unit(rng.normal(size=768)) for _ in range(6)]
        for i,v in enumerate(vectors):
            # Different names and contents, so the deduplication must preserve six documents.
            f=Path(self.tmp.name)/f'{i}.txt';f.write_text(f'document {i}');self.a.add([f]);d=self.a.prepare(self.a.pending()[-1],self.stop)
            self.a.page_commit(d,1,[('figura','image test',v)]);self.a.update(d['id'],status='ready')
        a=Archive(self.a.data);found=a.search(vectors[3],self.stop);self.assertEqual(found[0]['name'],'3.txt');self.assertAlmostEqual(found[0]['score'],1,places=5)
        mapped=a.map(self.stop,2);self.assertEqual(len(mapped),6);self.assertTrue(all(np.isfinite([r['x'],r['y']]).all() for r in mapped))
        self.assertEqual(mapped,a.map(self.stop,2))
    def test_checkpoint_atomic_and_partial_visible_after_restart(self):
        d=self.doc('file.txt');v=np.ones(768)
        self.a.page_commit(d,1,[('testo','text',v)],'previous page tail')
        with self.assertRaises(RuntimeError):self.a.page_commit(d,1,[('testo','text',v)])
        with self.a.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0],1)
        found=self.a.search(unit(v),self.stop)
        self.assertEqual(len(found),1);self.assertTrue(found[0]['partial']);self.assertEqual(found[0]['done'],1)
        for scope in ('documents','passages'):
            mapped=self.a.map(self.stop,scope=scope)
            self.assertEqual(len(mapped),1);self.assertTrue(mapped[0]['partial'])
        self.a.update(d['id'],status='processing');self.assertEqual(Archive(self.a.data).documents()[0]['status'],'paused')
        self.assertEqual(Archive(self.a.data).pending()[0]['tail'],'previous page tail')
        self.assertEqual(Archive(self.a.data).search(unit(v),self.stop),found)
    def test_duplicate_bytes_other_name(self):
        first=self.doc('one.txt');other=Path(self.tmp.name)/'two.txt';other.write_text('text')
        result=self.a.add([other]);self.assertFalse(result['added']);self.assertEqual(result['duplicates'][0]['existing']['id'],first['id'])
        self.assertEqual(len(self.a.documents()),1)
    def test_cancel_does_not_import(self):
        f=Path(self.tmp.name)/'f.txt';f.write_text('hello');self.a.add([f]);self.stop.set()
        with self.assertRaises(Cancelled):self.a.prepare(self.a.pending()[0],self.stop)
        self.assertEqual(list((self.a.data/'documenti').glob('*.part')),[])
    def test_long_text_splits_without_truncation(self):
        class TokenEngine(Engine):
            def tokens(self,text,stop):return len(text)  # deliberately hostile tokenizer
            def encode(self,text,stop):return unit(np.ones(768))
        e=TokenEngine(self.a.data);text=''.join(f'漢字 🦊 word{i:06d}\n' for i in range(6000))
        chunks=list(e.text_chunks(text,'Title',self.stop));self.assertGreater(len(chunks),10)
        rebuilt=chunks[0][0]
        for passage,_ in chunks[1:]:
            overlap=max((k for k in range(OVERLAP_LIMIT+1) if passage[:k]==rebuilt[-k:] or k==0),default=0)
            rebuilt+=passage[overlap:]
        self.assertEqual(rebuilt,text)
        self.assertTrue(all(len('title: Title | text: '+s)<=TEXT_LIMIT for s,v in chunks))
    def test_invalid_embeddings_rejected(self):
        for v in [np.zeros(768),np.ones(3),np.full(768,np.nan)]:
            with self.assertRaises(ValueError):unit(v)

if __name__=='__main__':unittest.main(verbosity=2)
