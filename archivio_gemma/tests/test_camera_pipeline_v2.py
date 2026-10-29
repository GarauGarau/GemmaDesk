"""Regression tests for substantive speech, stable classification and denominators."""
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from camera_preprocess import clean_html,words
from camera_dataset import bounded_parts,initialize,save_plan
from camera_quality import export
from topic_discovery import exclusion_reason,semantic_recovery,migrate,catalog,VERSION,similar_pairs
from core import Archive,Engine,Cancelled
from timeline import yearly_composition


class Cleaning(unittest.TestCase):
    def test_parties_roles_and_continuations_never_inherit_chair(self):
        text,audit=clean_html('''<div id="wrapper"><p class="titolo">Tutela della salute.</p>
        <p class="intervento">PRESIDENTE. Ha chiesto di parlare la deputata Rossi. Ne ha facoltà.</p>
        <p class="intervento">MARIA ROSSI (PD-IDP). Le cure mediche sono insufficienti.</p>
        <p class="interventoVirtuale">Gli ospedali necessitano di infermieri.</p>
        <p class="intervento">PAOLO BIANCHI , Relatore. Il servizio sanitario è essenziale.</p>
        <p class="intervento">Nome non riconosciuto: intervento da conservare.</p></div>''')
        kept=[r for r in audit if r['kept']]
        self.assertEqual([r['speaker'] for r in kept],['MARIA ROSSI','MARIA ROSSI','PAOLO BIANCHI',''])
        self.assertEqual([r['role'] for r in kept],['speech','speech','speech','unknown'])
        self.assertNotIn('PD-IDP',text);self.assertNotIn('facoltà',text)
        for r in kept:self.assertEqual(hashlib.sha256(text[r['start']:r['end']].encode()).hexdigest(),r['sha256'])

    def test_president_content_mixed_with_procedure_survives(self):
        text,audit=clean_html('''<div id="wrapper"><p class="intervento">PRESIDENTE. Dichiaro chiusa la votazione. La violenza contro le donne richiede una risposta concreta.</p>
        <p class="intervento">PRESIDENTE. Dichiaro chiusa la votazione, ma dobbiamo discutere la crisi.</p>
        <p class="intervento">MARIA ROSSI (PD). Le pensioni sono troppo basse.</p></div>''')
        self.assertIn('violenza contro le donne',text);self.assertIn('discutere la crisi',text)
        self.assertIn('pensioni',text);self.assertEqual(text.count('Dichiaro'),1)

    def test_annotations_page_numbers_and_empty_turns(self):
        text,audit=clean_html('''<div id="wrapper"><p class="intervento">PRESIDENTE. Se non vi sono osservazioni, il processo verbale si intende approvato. (È approvato).</p>
        <p class="intervento">MARIA ROSSI. La scuola <span class="numeroPagina">Pag. 12</span> richiede investimenti (Applausi dei deputati).</p></div>''')
        self.assertNotIn('Pag.',text);self.assertNotIn('Applausi',text);self.assertNotIn('verbale',text)
        self.assertIn('scuola',text)

    def test_printed_spaces_do_not_hide_voting_formulas(self):
        text,audit=clean_html('<div id="wrapper"><p>La Camera respinge .</p><p>PRESIDENTE. Passiamo dunque ai voti . Per cortesia !</p><p>ROSSI. No alla guerra.</p></div>')
        self.assertNotIn('respinge',text);self.assertNotIn('cortesia',text);self.assertIn('guerra',text)

    def test_nonoverlapping_bounded_segments_keep_every_character(self):
        for text in ['La sanità è essenziale. '*700,'🦊漢字'*900,'a'*8000]:
            parts=list(bounded_parts(text,len,'titolo: '))
            self.assertEqual(''.join(p[2] for p in parts),text)
            self.assertTrue(all(len('titolo: '+p[2])<=1024 for p in parts))
            for start,end,value in parts:self.assertEqual(text[start:end],value)


class Discovery(unittest.TestCase):
    def test_common_parliamentary_register_is_not_a_duplicate_topic(self):
        from scipy.sparse import csr_matrix
        centers=np.array([[1.,0.],[1.,0.]])
        lexical=csr_matrix([[1.,0.],[1.,0.]])
        descriptions={0:dict(terms=['scuola','docenti']),1:dict(terms=['pazienti','ospedali'])}
        self.assertEqual(similar_pairs([0,1],centers,lexical,descriptions),[])
        descriptions[1]['terms']=['scuola','docenti']
        self.assertEqual(len(similar_pairs([0,1],centers,lexical,descriptions)),1)
    def test_structural_noise_and_substantive_chair_speech(self):
        self.assertTrue(exclusion_reason('Grazie Presidente.', 'chair'))
        self.assertTrue(exclusion_reason('Governo | 20 minuti\nGruppi | 40 minuti\nRelatore | 10 minuti'))
        self.assertTrue(exclusion_reason(('Mario Rossi Anna Bianchi '*20), 'chair'))
        self.assertFalse(exclusion_reason('Governo | 20 minuti\nGruppi | 40 minuti\nRelatore | 10 minuti\nLa violenza contro le donne richiede risposte.','chair'))
        self.assertFalse(exclusion_reason(('Mario Rossi Anna Bianchi '*20)+'sono morti sul lavoro.','chair'))
        for text in ('No alla guerra.', 'Ospedali senza medici.',
                     'Dichiaro chiusa la votazione. La violenza contro le donne richiede risposte.',
                     'Passiamo alla votazione, ma ritengo ingiusto il taglio delle pensioni.'):
            self.assertEqual(exclusion_reason(text,'chair'),'')

    def test_recovery_requires_both_evidences_and_keeps_new_topics_unassigned(self):
        from scipy.sparse import csr_matrix
        labels=np.array([0]*10+[1]*10+[-1]*3)
        matrix=np.array([[1.,0.,0.]]*10+[[0.,1.,0.]]*10+[[1.,0.,0.],[1.,0.,0.],[0.,0.,1.]],dtype=np.float32)
        bow=csr_matrix([[1.,0.]]*10+[[0.,1.]]*10+[[1.,0.],[0.,1.],[0.,0.]])
        assigned,_,_,report,_=semantic_recovery(matrix,bow,labels,csr_matrix(np.eye(2)),[0,1])
        self.assertEqual(assigned[-3:].tolist(),[0,-1,-1])
        self.assertEqual(report['cluster_agreement'],1.)

    def test_migration_erases_old_labels_but_preserves_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=Archive(Path(tmp)/'database')
            with a.connect() as db:
                initialize(db)
                db.execute("DELETE FROM meta WHERE key='camera_topic_method'")
                db.execute("INSERT INTO meta VALUES('camera_topics','old-taxonomy')")
                before=list(db.execute('SELECT * FROM chunks'))
                migrate(db)
                self.assertEqual(list(db.execute('SELECT * FROM chunks')),before)
                self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='camera_topics'").fetchone())
                self.assertEqual(catalog(db)[0],{-1:'Tema da verificare',-2:'Conduzione e tabulati'})
                self.assertIsNone(catalog(db)[1])


class Resume(unittest.TestCase):
    def test_v2_atomic_checkpoint_and_resume_uses_same_plan(self):
        class Fake(Engine):
            def start(self,stop,notify):pass
            def tokens(self,text,stop):return len(text)
            def encode_many(self,texts,stop):
                self.stats['text_embeddings']+=len(texts)
                return [np.eye(1,768,dtype='<f4')[0] for _ in texts]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'testi_tematici/session.txt';source.parent.mkdir();audit_dir=root/'temi/audit';audit_dir.mkdir(parents=True)
            text,audit=clean_html('<div id="wrapper"><p class="titolo">Salute dei pazienti.</p><p class="intervento">MARIA ROSSI (PD). '+('Ospedali e medici curano i pazienti. '*200)+'</p></div>')
            source.write_text(text+'\n')
            with gzip.open(audit_dir/'session.jsonl.gz','wt') as f:
                for row in audit:f.write(json.dumps(row)+'\n')
            archive=Archive(root/'database');archive.add([source]);stop=threading.Event();engine=Fake(archive.data,batch_size=1,text_only=True)
            with archive.connect() as db:
                db.execute("INSERT INTO meta VALUES('camera_preprocessing','camera-thematic-v2')")
                db.execute('CREATE TABLE camera_sessions(document,session_date,kept_words)');db.execute("INSERT INTO camera_sessions VALUES(1,'2020-01-01',?)",(words(text),))
            def pause(info):
                if isinstance(info,dict) and info.get('pages_processed')==1:stop.set()
            with self.assertRaises(Cancelled):archive.index(engine,stop,pause)
            stop.clear()
            with archive.connect() as db:
                saved=tuple(db.execute('SELECT * FROM chunks').fetchone())
                self.assertEqual(db.execute('SELECT count(*) FROM camera_passages WHERE topic IS NOT NULL').fetchone()[0],0)
            with patch.object(engine,'tokens',side_effect=AssertionError('Plan was re-tokenized')):
                result=archive.index(engine,stop,lambda x:None)
            self.assertEqual(result['errors'],0)
            with archive.connect() as db:
                self.assertEqual(saved,tuple(db.execute('SELECT * FROM chunks WHERE id=1').fetchone()))
                total,classified=db.execute('SELECT count(*),count(topic) FROM camera_passages').fetchone()
                self.assertGreater(total,0);self.assertEqual(classified,0)
                self.assertEqual(db.execute('SELECT sum(words) FROM camera_passages').fetchone()[0],words(text))


class Export(unittest.TestCase):
    def test_full_denominator_review_written_and_missing_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=Archive(root/'database');(root/'temi').mkdir();stop=threading.Event()
            path=root/'test.txt';path.write_text('testo');ident=archive.add([path])['added'][0]['id'];doc=archive.prepare(archive.pending()[0],stop)
            with archive.connect() as db:
                initialize(db)
                db.execute("INSERT INTO camera_topic_runs VALUES('test-run','now','{}','{}','')")
                db.execute("INSERT INTO camera_discovered_topics VALUES('test-run',3,'tema appreso','[]',X'00','{}')")
                db.execute("INSERT INTO meta VALUES('camera_discovery_run','test-run')")
                db.execute('CREATE TABLE camera_sessions(slug,session_date,document,title,url,pdf,clean_source,original_source,kept_words,removed_words)')
                db.execute('INSERT INTO camera_sessions VALUES(?,?,?,?,?,?,?,?,?,?)',('one','2020-01-01',ident,'uno','https://example.org','','','',40,0))
                db.execute('INSERT INTO camera_sessions VALUES(?,?,?,?,?,?,?,?,?,?)',('two','2020-03-01',None,'due','','','','',20,0))
                for page,topic,written in [(1,3,0),(2,-1,0),(3,3,1)]:
                    db.execute('INSERT INTO camera_passages(document,page,start,end,words,speaker,role,heading,written,topic) VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,page,0,10,10,'Rossi','speech','',written,topic))
                db.execute("UPDATE camera_passages SET version='test-run'")
            for i in range(1,4):archive.page_commit(doc,i,[('testo','Dieci parole',np.eye(1,768,dtype='<f4')[0])])
            archive.update(ident,status='paused',pages=4)
            (root/'temi/manifest.json').write_text(json.dumps([dict(slug='one',kept_words=40,written_words=10),dict(slug='two',kept_words=20,written_words=0)]))
            result=export(root)
            with (root/'temi/andamento-mensile.csv').open(encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream,delimiter=';'))
            jan=[r for r in rows if r['period']=='2020-01' and r['stream']=='orale']
            self.assertAlmostEqual(sum(float(r['share_indexed_words']) for r in jan if r['share_indexed_words']),1)
            health=next(r for r in jan if r['topic']=='3');self.assertEqual(float(health['share_indexed_words']),.5)
            self.assertAlmostEqual(float(health['word_coverage']),2/3);self.assertEqual(float(health['session_coverage']),0)
            self.assertTrue(all(r['share_indexed_words']=='' for r in rows if r['period']=='2020-02'))
            self.assertEqual(result['review_passages'],1)
            sheet=root/'temi/campione-revisione.csv'
            with sheet.open(encoding='utf-8-sig',newline='') as f:
                reader=csv.DictReader(f,delimiter=';');fields=reader.fieldnames;review=list(reader)
            review[0]['notes']='Annotazione umana da conservare'
            with sheet.open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=fields,delimiter=';');writer.writeheader();writer.writerows(review)
            export(root)
            self.assertIn('Annotazione umana da conservare',sheet.read_text(encoding='utf-8-sig'))


    def test_history_uses_words_without_written_speeches(self):
        data=dict(sessions=[dict(document=1,session_date='2020-01-01',status='ready',kept_words=20)])
        points=[dict(id=1,chunk_id=i,cluster=group,topics=['tema'],stable_topics=True,topic_words=weight,written=written) for i,group,weight,written in [(1,3,10,False),(2,-1,20,False),(3,3,50,True)]]
        result=yearly_composition(data,points,'passages');self.assertTrue(result['weighted']);self.assertEqual(result['rows'][0]['total'],30)
        self.assertEqual(result['rows'][0]['groups'],{3:10,-1:20})


if __name__=='__main__':unittest.main(verbosity=2)
