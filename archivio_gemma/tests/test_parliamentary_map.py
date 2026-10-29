"""Substantive Camera themes, verified headings and preservation of saved work."""
import os
os.environ.setdefault('OMP_NUM_THREADS','4')
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')
from pathlib import Path
import gzip,hashlib,json,sys,tempfile,threading,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from sklearn.metrics import adjusted_rand_score
from core import Archive,Cancelled
from grouping import hierarchical_groups
from parliamentary_topics import (content_text,content_words,substantive_heading,
    passage_contexts,content_features,procedural_mask,topic_names)
from thematic_map import project
from timeline import yearly_composition
from map_style import group_label,group_color,RESIDUAL_COLOR

BANK='Tutela del risparmio e sistema bancario. Banca popolare, credito alle imprese, liquidazione delle banche, depositi dei risparmiatori e garanzia dei conti correnti. Il sistema bancario sostiene il credito e tutela il risparmio.'
HEALTH='Consenso informato e disposizioni anticipate di trattamento. La salute dei pazienti richiede cure mediche, consenso del paziente e tutela della salute. Le cure sanitarie rispettano la volontà del paziente e le disposizioni anticipate di trattamento.'
VOTE='PRESIDENTE. Passiamo alla votazione. Indìco la votazione nominale mediante procedimento elettronico.\n\nMARIO ROSSI. Ordine del giorno Bianchi n. 9/123/4, parere favorevole. Ordine del giorno Verdi n. 9/123/5, parere contrario.'


class ContentTests(unittest.TestCase):
    def test_voting_and_speaker_names_do_not_name_topics(self):
        names=topic_names([VOTE+'\n\n'+BANK]*4+[VOTE+'\n\n'+HEALTH]*4,[0]*4+[1]*4)
        joined=' '.join(name for values in names.values() for name in values).lower()
        for unwanted in ('passiamo','votazione','rossi','bianchi','pag','interpellanza'):self.assertNotIn(unwanted,joined)
        self.assertTrue(any('banc' in name.lower() or 'risparmio' in name.lower() for name in names[0]))
        self.assertTrue(any('consenso' in name.lower() or 'trattamento' in name.lower() for name in names[1]))
        self.assertFalse(any(name.lower().startswith('informato') for name in names[1]))
        self.assertEqual(content_words(VOTE),[])

    def test_substantive_chair_speech_and_mixed_passages_survive(self):
        self.assertIn('Consenso informato',content_text('PRESIDENTE. '+HEALTH))
        self.assertIn('Tutela del risparmio',content_text(VOTE+'\n\n'+BANK))
        self.assertIn('Tutela del risparmio',content_text('PRESIDENTE. Passiamo alla votazione.\nMARIO ROSSI. '+BANK))
        np.testing.assert_array_equal(procedural_mask([VOTE,BANK,HEALTH]),[True,False,False])

    def test_agenda_keeps_subject_instead_of_authors_of_related_bills(self):
        title='Seguito della discussione della proposta di legge: Rossi ed altri: Disposizioni in materia di tutela del risparmio (A.C. 100); e delle abbinate proposte di legge: Bianchi ed altri; Verdi ed altri (A.C. 101-102).'
        self.assertEqual(substantive_heading(title),'Disposizioni in materia di tutela del risparmio')
        for value in ('Calendario dei lavori dell’Assemblea per marzo','Si riprende la discussione del disegno di legge n. 123','Svolgimento di interpellanze urgenti'):
            self.assertFalse(substantive_heading(value))

    def test_complete_prevalent_title_and_stable_unrelated_names(self):
        heading='Norme in materia di consenso informato e di disposizioni anticipate di trattamento'
        texts=[HEALTH]*5+[BANK]*5;contexts=[heading]*5+['']*5
        before=topic_names(texts,[0]*5+[1]*5,contexts=contexts)
        after=topic_names(texts,[0]*5+[1]*3+[2]*2,contexts=contexts)
        self.assertEqual(before[0],['Consenso informato e di disposizioni anticipate di trattamento'])
        self.assertEqual(before[0],after[0])

    def test_context_requires_matching_audited_paragraph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'testi'/'seduta.txt';source.parent.mkdir();audit=root/'temi'/'audit';audit.mkdir(parents=True)
            source.write_text(BANK+'\n\n'+HEALTH+'\n')
            records=[dict(output_paragraph=i,kept=True,sha256=hashlib.sha256(text.encode()).hexdigest(),section=title)
                     for i,(text,title) in enumerate(((BANK,'Tutela del risparmio'),(HEALTH,'Consenso informato e disposizioni anticipate di trattamento')))]
            with gzip.open(audit/'seduta.jsonl.gz','wt') as stream:
                for record in records:stream.write(json.dumps(record)+'\n')
            rows=[dict(document=1,text=BANK),dict(document=1,text=HEALTH)];docs=[dict(id=1,source=str(source),stored='')]
            self.assertEqual(passage_contexts(rows,docs,root),['Tutela del risparmio','Consenso informato e disposizioni anticipate di trattamento'])
            source.write_text('TESTO MODIFICATO '+BANK+'\n\n'+HEALTH+'\n')
            self.assertEqual(passage_contexts(rows,docs,root)[0],'')

    def test_content_separates_subjects_despite_identical_old_vectors(self):
        vectors=np.tile(np.eye(1,768,dtype='<f4'),(12,1));before=vectors.copy()
        texts=[VOTE+'\n\n'+BANK]*6+[VOTE+'\n\n'+HEALTH]*6
        features=content_features(texts,vectors)
        labels,_,_=hierarchical_groups(features,2)
        self.assertEqual(adjusted_rand_score([0]*6+[1]*6,labels),1.)
        np.testing.assert_array_equal(before,vectors)
        coords,_,_=project(features);self.assertTrue(np.isfinite(coords).all())
        np.testing.assert_array_equal(coords[0],coords[1])

    def test_residual_points_remain_in_yearly_totals_and_are_gray(self):
        points=[dict(id=1,chunk_id=1,cluster=-1,topics=['Passaggi brevi o procedurali']),dict(id=1,chunk_id=2,cluster=0,topics=['Tutela del risparmio'])]
        data=yearly_composition(dict(sessions=[dict(document=1,session_date='2020-01-01',status='ready',done=2,kept_words=10)]),points,'passages')
        self.assertEqual(data['rows'][0]['total'],2);self.assertEqual(data['rows'][0]['groups'],{-1:1,0:1})
        self.assertEqual(group_color(-1),RESIDUAL_COLOR);self.assertNotIn('G0',group_label(-1))


class PreservationTests(unittest.TestCase):
    def test_map_reuses_saved_work_and_cached_positions_including_partial_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=Archive(root/'data');stop=threading.Event();vector=np.eye(1,768,dtype='<f4')[0]
            for index,body in enumerate((BANK,HEALTH)):
                path=root/f'{index}.txt';path.write_text(body+'\n\n'+VOTE)
                ident=archive.add([path])['added'][0]['id'];doc=archive.prepare(next(d for d in archive.pending() if d['id']==ident),stop)
                archive.page_commit(doc,1,[('testo',body,vector)]);archive.page_commit(doc,2,[('testo',VOTE,vector)])
                archive.update(ident,status='ready' if index==0 else 'paused',pages=2 if index==0 else 3)
            with archive.connect() as db:
                db.execute('CREATE TABLE camera_sessions(document INTEGER)')
                before=[tuple(r) for r in db.execute('SELECT * FROM chunks ORDER BY id')]
                states=[tuple(r) for r in db.execute('SELECT * FROM documents ORDER BY id')]
            results=archive.search(vector,stop)
            mapped=archive.map(stop,2,scope='passages')
            self.assertEqual(len(mapped),4);self.assertEqual(sum(item['cluster']==-1 for item in mapped),2)
            self.assertEqual(sum(item['partial'] for item in mapped),2)
            with patch('thematic_map.project',side_effect=AssertionError('cache not used')):
                again=archive.map(stop,2,scope='passages');self.assertEqual(mapped,again)
                another=archive.map(stop,1,scope='passages')
                self.assertEqual([(p['x'],p['y']) for p in mapped],[(p['x'],p['y']) for p in another])
            self.assertEqual(archive.search(vector,stop),results)
            with archive.connect() as db:
                self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM chunks ORDER BY id')])
                self.assertEqual(states,[tuple(r) for r in db.execute('SELECT * FROM documents ORDER BY id')])
            stop.set()
            with self.assertRaises(Cancelled):archive.map(stop,scope='passages')


if __name__=='__main__':unittest.main(verbosity=2)
