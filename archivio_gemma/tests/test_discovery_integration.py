"""Real discovery, atomic publication, stale snapshots and temporal identities."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Archive,Cancelled
from camera_dataset import initialize
from topic_discovery import discover,catalog


class Integration(unittest.TestCase):
    def test_fit_reuse_and_cancelled_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive=Archive(root/'database');rng=np.random.default_rng(6)
            texts=['Osservazioni telescopi galassie stelle cosmologia universo astronomia.',
                   'Ospedali infermieri pazienti medicina salute cure sanitarie.',
                   'Viticoltura vigneti vendemmia agricoltura uva vino coltivazioni.']
            with archive.connect() as db:
                initialize(db)
                db.execute('CREATE TABLE camera_sessions(document,slug,session_date,url)')
                for d in range(3):
                    db.execute("INSERT INTO documents(id,source,name,title,done,pages,status) VALUES(?,?,?,?,60,60,'ready')",(d+1,str(root/f'{d}.txt'),f'Seduta {d}',f'Seduta {d}'))
                    db.execute('INSERT INTO camera_sessions VALUES(?,?,?,?)',(d+1,f'seduta-{d}',f'{2020+d}-01-01','https://example.org'))
                    for page in range(1,61):
                        group=(page-1)%3;vector=rng.normal(0,.003,768).astype('<f4');vector[group]=1;vector/=np.linalg.norm(vector)
                        db.execute('INSERT INTO chunks(document,page,kind,text,vector) VALUES(?,?,?,?,?)',(d+1,page,'testo',texts[group],vector.tobytes()))
                        db.execute("INSERT INTO camera_passages(document,page,start,end,words,speaker,role,heading,written) VALUES(?,?,0,100,9,'Rossi','speech','',0)",(d+1,page))
            result=discover(root,notify=lambda _:None)
            self.assertGreaterEqual(result['topics'],3)
            self.assertGreater(result['assigned_word_share'],.9)
            with archive.connect() as db:
                before=list(map(tuple,db.execute('SELECT document,page,topic,version FROM camera_passages ORDER BY document,page')))
                self.assertEqual(len({r[3] for r in before}),1)
                # The same known subjects must have the same IDs in all years.
                for page in range(1,4):
                    self.assertEqual(len({r[2] for r in before if r[1]==page}),1)
            reused=discover(root,notify=lambda _:None)
            self.assertEqual(reused['run'],result['run'])
            state=json.loads((root/'temi/scoperta-avanzamento.json').read_text())
            self.assertTrue(state['reused']);self.assertEqual(state['status'],'completed')
            # A new passage gives a different snapshot, then cancellation at
            # publication must keep the entire previous model intact.
            with archive.connect() as db:
                db.execute('INSERT INTO chunks(document,page,kind,text,vector) VALUES(1,61,\'testo\',?,?)',(texts[0],np.eye(1,768,dtype='<f4')[0].tobytes()))
                db.execute("INSERT INTO camera_passages(document,page,start,end,words,speaker,role,heading,written) VALUES(1,61,0,100,9,'Rossi','speech','',0)")
                db.execute('UPDATE documents SET done=61,pages=61 WHERE id=1')
            stop=threading.Event()
            def cancel(stage):
                if stage.startswith('Salvataggio del modello'):stop.set()
            with self.assertRaises(Cancelled):discover(root,stop=stop,notify=cancel)
            with archive.connect() as db:
                self.assertEqual(catalog(db)[1],result['run'])
                self.assertEqual(list(map(tuple,db.execute('SELECT document,page,topic,version FROM camera_passages WHERE page<=60 ORDER BY document,page'))),before)
                self.assertIsNone(db.execute('SELECT topic FROM camera_passages WHERE page=61').fetchone()[0])


if __name__=='__main__':unittest.main(verbosity=2)
