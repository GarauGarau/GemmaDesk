"""Lossless text boundaries, semantic labels and passage-map regressions."""
import os,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('OPENBLAS_NUM_THREADS','4');os.environ.setdefault('OMP_NUM_THREADS','4')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from segmentation import segments,tail,TEXT_LIMIT,OVERLAP_LIMIT
from topics import cluster_topics
from core import Archive,Engine,unit,Cancelled


class SegmentationChecks(unittest.TestCase):
    def verify(self,text,limit=TEXT_LIMIT,block_size=32768,prefix='title: Test | text: '):
        parts=list(segments((text[i:i+block_size] for i in range(0,len(text),block_size)),len,prefix,limit=limit))
        self.assertEqual(''.join(p.text[p.overlap:] for p in parts),text)
        previous=0
        for p in parts:
            self.assertEqual(p.start,previous);self.assertEqual(p.text[p.overlap:],text[p.start:p.end])
            self.assertLessEqual(len(prefix+p.text),limit)
            self.assertLessEqual(p.overlap,OVERLAP_LIMIT)
            self.assertEqual(p.text[:p.overlap],text[p.start-p.overlap:p.start]);previous=p.end
        return parts
    def test_short_and_exact_budget_stay_whole_across_io_buffers(self):
        for n in [1,500,TEXT_LIMIT-len('title: Test | text: ')]:
            parts=self.verify('x'*n,block_size=31)
            self.assertEqual(len(parts),1);self.assertEqual(parts[0].overlap,0)
    def test_paragraph_and_sentence_boundaries(self):
        paragraphs=['La foresta cresce. '*25+'\n\n' for _ in range(9)]
        parts=self.verify(''.join(paragraphs),limit=1200)
        self.assertTrue(all(p.text.endswith('\n\n') for p in parts[:-1]))
        parts=self.verify(('Una frase completa termina qui. '*400),limit=1200)
        self.assertTrue(all(p.text.endswith('. ') for p in parts[:-1]))
    def test_unicode_code_no_spaces_and_whitespace_are_lossless(self):
        for text in ['漢字🦊🪐'*6000,'x'*28000,'a\r\n'*10000,' '*24000,'\n\nHello.\n\n'*2000]:self.verify(text)
    def test_empty_and_cancelled(self):
        self.assertEqual(list(segments([],len)),[])
        def cancel():raise Cancelled()
        with self.assertRaises(Cancelled):list(segments(['hello'],len,check=cancel))
    def test_overlap_budget_after_nonmonotonic_word_boundary(self):
        count=lambda s:3 if s=='second third' else len(s.split())
        text='first second third'
        suffix=tail(text,count,limit=2)
        self.assertTrue(text.endswith(suffix));self.assertLessEqual(count(suffix),2)
    def test_long_character_count_but_small_token_count_stays_whole(self):
        text='abcdefghij'*20+' '
        text*=TEXT_LIMIT-1
        parts=list(segments([text[:32768],text[32768:]],lambda s:len(s.split())))
        self.assertEqual([p.text for p in parts],[text])
    def test_batch_respects_total_native_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(tmp,batch_size=4);engine.info={'max_text_batch':4}
            calls=[]
            with patch.object(engine,'tokens',side_effect=lambda text,stop:len(text)),patch.object(engine,'encode',side_effect=lambda text,stop:calls.append(text) or unit(np.ones(768))):
                result=engine.encode_many(['a'*5000,'b'*5000],threading.Event())
            self.assertEqual(calls,['a'*5000,'b'*5000]);self.assertEqual(len(result),2)
    def test_small_chunks_share_one_native_batch(self):
        from io import BytesIO
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(tmp,batch_size=4);engine.info={'max_text_batch':4}
            engine.process=SimpleNamespace(stdin=BytesIO())
            vector=unit(np.ones(768)).tolist()
            response=dict(vectors=[vector]*4,tokens=[1024]*4,seconds=.1)
            with patch.object(engine,'tokens',return_value=1024),patch.object(engine,'receive',return_value=response) as receive:
                result=engine.encode_many(['test']*4,threading.Event())
            self.assertEqual(len(result),4);receive.assert_called_once()
            self.assertTrue(engine.process.stdin.getvalue().startswith(b'B 4\n'))


class TopicChecks(unittest.TestCase):
    def test_topics_follow_groups_and_do_not_repeat(self):
        rows=[(0,1,'Galassie e stelle osservate dai telescopi. Galassie lontane.'),
              (0,2,'Galassie lontane osservate dai telescopi. Stelle luminose.'),
              (1,3,'Farina burro e zucchero. Preparare biscotti.'),
              (1,4,'Biscotti con farina burro e zucchero.')]
        labels=cluster_topics(rows)
        self.assertIn('galassie',' '.join(labels[0]));self.assertIn('farina',' '.join(labels[1]))
        self.assertEqual(labels,cluster_topics(rows));self.assertTrue(all(1<=len(v)<=3 for v in labels.values()))
        self.assertFalse(cluster_topics([(0,1,'the and di della che')]).get(0))
    def test_documents_are_balanced_and_visual_placeholders_not_topics(self):
        labels=cluster_topics([(0,1,'startup startups founders '*1000),(0,2,'startup founders investors')])
        self.assertLessEqual(sum('startup' in t for t in labels[0]),1)
        self.assertEqual(cluster_topics([]),{})
    def test_passages_keep_unique_identity_and_document_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a=Archive(root/'data');stop=threading.Event()
            path=root/'mixed.txt';path.write_text('Galassie lontane. Farina burro.')
            a.add([path]);doc=a.prepare(a.pending()[0],stop)
            a.page_commit(doc,1,[('testo','Galassie lontane stelle telescopi.',unit(np.eye(768)[0])),('testo','Farina burro biscotti.',unit(np.eye(768)[1])),('pagina','Vista completa della pagina 1',unit(np.eye(768)[2]))]);a.update(doc['id'],status='ready')
            mapped=a.map(stop,2,scope='passages')
            self.assertEqual(len(mapped),2);self.assertEqual(len({p['point_id'] for p in mapped}),2)
            self.assertEqual({p['id'] for p in mapped},{doc['id']})
            self.assertTrue(all(p['topics'] and p['text'] for p in mapped))
            self.assertEqual(mapped,a.map(stop,2,scope='passages'))
            self.assertEqual(len(a.map(stop)),1)
            before=a.search(unit(np.eye(768)[0]),stop)[0]
            self.assertEqual(before['chunk_id'],mapped[0]['chunk_id'])
            stop.set()
            with self.assertRaises(Cancelled):a.map(stop,scope='passages')


if __name__=='__main__':unittest.main(verbosity=2)
