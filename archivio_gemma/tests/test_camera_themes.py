"""Cleaning preserves political content; timeline measures all passages and coverage."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from camera_preprocess import clean_html
from timeline import aggregate, matching_passages, snapshot
from core import Archive,Cancelled


class CleaningTests(unittest.TestCase):
    def test_procedures_removed_and_political_speech_preserved(self):
        source = '''<div id="wrapper"><p class="centerBold">Seduta 1</p>
        <p class="intervento">PRESIDENTE. La seduta è aperta.</p>
        <p class="titolo">Missioni.</p><p>ELENCO DEPUTATI ASSENTI</p>
        <p class="titolo">Sull'ordine dei lavori.</p>
        <p class="intervento">ROSSI. Discutiamo il salario minimo e le missioni internazionali.</p>
        <p class="interventoVirtuale">Le tutele retributive devono aumentare.</p>
        <p class="sottotitolo">Dichiarazioni di voto.</p>
        <p>BIANCHI. Voto contro la proposta sul salario minimo.</p>
        <p>PRESIDENTE. Dichiaro chiusa la votazione.</p>
        <p class="titolo">Ordine del giorno della prossima seduta.</p><p>FUTURO ARGOMENTO</p>
        <p class="titolo_allegato">Testi degli interventi autorizzati in calce.</p><p>TESTO SCRITTO DA CONSERVARE</p>
        <p class="titolo_votazioni center">VOTAZIONI QUALIFICATE EFFETTUATE</p>
        <table><tr><td>12345</td></tr></table></div>'''
        text, audit = clean_html(source)
        for absent in ['ASSENTI', 'FUTURO ARGOMENTO', '12345', 'Dichiaro chiusa', 'Seduta 1']:
            self.assertNotIn(absent, text)
        for present in ['salario minimo', 'missioni internazionali', 'retributive', 'Voto contro', 'TESTO SCRITTO']:
            self.assertIn(present, text)
        self.assertTrue(any(r.get('text') == 'FUTURO ARGOMENTO' for r in audit))

    def test_substantive_tables_and_nonexact_formulas_survive(self):
        text, _ = clean_html('''<div id="wrapper"><p class="titolo">Retribuzioni.</p>
            <p>PRESIDENTE. Dichiaro chiusa la votazione, ma dobbiamo discutere la crisi.</p>
            <p class="center">Salario minimo</p><table><tr><td>Salari</td><td>1000</td></tr></table></div>''')
        self.assertIn('discutere la crisi', text)
        self.assertIn('Salario minimo', text)
        self.assertIn('Salari | 1000', text)

    def test_opening_speech_without_heading_is_preserved(self):
        text,_=clean_html('<div id="wrapper"><p class="center">XIX LEGISLATURA</p><p>PRESIDENTE. La seduta è aperta.</p><p>PRESIDENTE. La precarietà richiede un salario minimo.</p><p>ROSSI, Segretario, legge il processo verbale della seduta di ieri.</p></div>')
        self.assertIn('precarietà',text)
        self.assertNotIn('processo verbale',text)
        self.assertNotIn('La seduta è aperta',text)


def session(date, scores, status='ready', kept=10):
    return dict(session_date=date, status=status, kept_words=kept, title=date, url='https://example.org',
                clean_source='/tmp/text.txt', passages=[dict(score=s, id=i, part=i+1, text='test') for i,s in enumerate(scores)])


class TimelineTests(unittest.TestCase):
    def test_volume_normalization_coverage_and_gaps(self):
        data = dict(sessions=[session('2020-01-01', [.8,.9,.4]), session('2020-01-02', [], 'queued'),
                              session('2020-03-01', [.5]), session('2020-04-01', [], 'queued')])
        rows = aggregate(data, .7)
        self.assertEqual([r['period'] for r in rows], ['2020-01','2020-02','2020-03','2020-04'])
        self.assertEqual(rows[0]['matched_passages'], 2)
        self.assertAlmostEqual(rows[0]['passage_share'], 2/3)
        self.assertEqual(rows[0]['coverage'], .5)
        self.assertIsNone(rows[1]['passage_share'])
        self.assertEqual(rows[2]['passage_share'], 0)
        self.assertIsNone(rows[3]['passage_share'])
        annual = aggregate(data, .7, 'year')[0]
        self.assertEqual(annual['passage_share'], .5)  # weighted by passages, not month averages
        self.assertEqual(len(matching_passages(data, '2020-01', .7)), 2)
        self.assertEqual(aggregate(data, .95)[0]['matched_passages'], 0)

    def test_search_ranking_unchanged_and_timeline_exceeds_top20(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Archive(Path(tmp)/'db');stop = threading.Event();v = np.eye(1,768,dtype='<f4')[0]
            with archive.connect() as db:
                db.execute('CREATE TABLE camera_sessions(slug,session_date,document,title,url,pdf,clean_source,original_source,kept_words,removed_words)')
            for i in range(25):
                path = Path(tmp)/f'{i}.txt';path.write_text(f'Paragrafo diverso {i}')
                id = archive.add([path])['added'][0]['id']
                doc = archive.prepare(next(r for r in archive.pending() if r['id']==id), stop)
                archive.page_commit(doc,1,[('testo',path.read_text(),v)])
                archive.update(id,status='ready')
                with archive.connect() as db:
                    db.execute('INSERT INTO camera_sessions VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (str(i),'2020-01-01',id,str(i),'https://example.org','',str(path),str(path),5,0))
            before = archive.search(v,stop)
            data = snapshot(archive,v,stop,'tema')
            self.assertEqual(before,archive.search(v,stop))
            self.assertEqual(len(before),20)
            self.assertEqual(aggregate(data)[0]['matched_passages'],25)
            self.assertEqual(aggregate(data)[0]['matched_sessions'],25)
            stop.set()
            with self.assertRaises(Cancelled):snapshot(archive,v,stop)

    def test_readding_camera_document_restores_date_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive=Archive(Path(tmp)/'db');path=Path(tmp)/'camera.txt';path.write_text('Contenuto')
            with archive.connect() as db:
                db.execute('CREATE TABLE camera_sessions(slug PRIMARY KEY,document,title,url,clean_source)')
                db.execute('INSERT INTO camera_sessions VALUES(?,?,?,?,?)',('test',None,'Seduta 1','https://www.camera.it',str(path)))
            result=archive.add([path]);id=result['added'][0]['id']
            with archive.connect() as db:
                self.assertEqual(db.execute('SELECT document FROM camera_sessions').fetchone()[0],id)
            doc=archive.documents()[0]
            self.assertEqual(doc['name'],'Seduta 1');self.assertEqual(doc['url'],'https://www.camera.it')


if __name__ == '__main__':
    unittest.main(verbosity=2)
