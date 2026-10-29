"""Annual emergence, symmetric harmonization and atomic publication."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
from scipy import sparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from annual_topics import align,CONFIG,discover_annual,fit_year,temporal_descriptions
from topic_discovery import catalog,discover
from core import Archive,Cancelled,check
from camera_dataset import initialize


class Alignment(unittest.TestCase):
    def test_family_label_does_not_backdate_a_new_word(self):
        bow=sparse.csr_matrix([[4.,0.,0.],[4.,20.,0.],[0.,0.,10.]])
        labels=np.array([0,0,1]);dates=np.array(['2016','2020','2024'])
        lex=sparse.csr_matrix([[.2,.8,0.],[0.,0.,1.]])
        descriptions={0:dict(label='pandemia',terms=['pandemia']),1:dict(label='robotica',terms=['robotica'])}
        temporal_descriptions(bow,labels,dates,[0,1],lex,descriptions,np.array(['sanità','pandemia','robotica']))
        self.assertEqual(descriptions[0]['label'],'sanità');self.assertEqual(descriptions[1]['label'],'robotica')
        self.assertEqual(descriptions[0]['label_year_support'],{'sanità':2})

    def test_alignment_is_symmetric_and_preserves_novel_subjects(self):
        centers=np.array([[1.,0,0],[.999,.001,0],[0,0,1]],dtype=np.float32)
        centers/=np.linalg.norm(centers,axis=1)[:,None]
        lex=sparse.csr_matrix([[1.,0],[1.,0],[0,1]])
        keys=[('2016',0),('2024',7),('2024',9)]
        groups,_=align(centers,lex,keys)
        self.assertEqual(groups[0],groups[1]);self.assertNotEqual(groups[0],groups[2])
        order=np.array([2,1,0]);other,_=align(centers[order],lex[order],[keys[i] for i in order])
        np.testing.assert_array_equal(groups[order],other)

    def test_no_transitive_chain_or_semantic_only_merge(self):
        angles=np.deg2rad([0,20,40]);centers=np.column_stack([np.cos(angles),np.sin(angles)])
        groups,_=align(centers,sparse.csr_matrix(np.ones((3,1))),[('2020',i) for i in range(3)])
        self.assertNotEqual(groups[0],groups[2])
        groups,_=align(np.ones((2,1)),sparse.eye(2,format='csr'),[('2020',0),('2021',0)])
        self.assertNotEqual(groups[0],groups[1])


class AnnualPipeline(unittest.TestCase):
    def test_each_year_discovers_and_emergence_survives_harmonization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=Archive(root/'database');rng=np.random.default_rng(7)
            texts=['Telescopi stelle galassie astronomia cosmologia universo.',
                'Ospedali medicina infermieri pazienti cure sanitarie.',
                'Viticoltura vigneti vendemmia agricoltura vino coltivazioni.']
            with archive.connect() as db:
                initialize(db);db.execute('CREATE TABLE camera_sessions(document,slug,session_date,url)')
                for d,(year,subjects) in enumerate([(2016,[0,1]),(2020,[0,1,2]),(2024,[0,2])],1):
                    n=len(subjects)*12
                    db.execute("INSERT INTO documents(id,source,name,title,done,pages,status) VALUES(?,?,?,?,?,?,'ready')",(d,str(root/f'{d}.txt'),str(year),str(year),n,n))
                    db.execute('INSERT INTO camera_sessions VALUES(?,?,?,?)',(d,str(d),f'{year}-01-01','https://example.org'))
                    for page in range(1,n+1):
                        g=subjects[(page-1)//12];v=rng.normal(0,.003,768).astype('<f4');v[g]=1;v/=np.linalg.norm(v)
                        db.execute("INSERT INTO chunks(document,page,kind,text,vector) VALUES(?,?,'testo',?,?)",(d,page,texts[g],v.tobytes()))
                        db.execute("INSERT INTO camera_passages(document,page,start,end,words,speaker,role,heading,written) VALUES(?,?,0,100,7,'Rossi','speech','',0)",(d,page))
            # Exercise real independent slice fits, without multiprocessing
            # startup dominating the tiny fixture.
            def prepare(root,periods,essays,check,progress):
                return dict(fit_year(root,p,essays) for p in periods)
            with patch('annual_topics.prepare_years',side_effect=prepare):
                result=discover(root,annual=True,notify=lambda _:None)
                self.assertEqual(result['local_topics'],7);self.assertEqual(result['topics'],3)
                self.assertGreater(result['assigned_word_share'],.99)
                with archive.connect() as db:
                    before=list(map(tuple,db.execute('SELECT document,page,topic,version FROM camera_passages ORDER BY document,page')))
                    for page in range(1,13):self.assertEqual(len({r[2] for r in before if r[1]==page}),1)
                    novel=db.execute('SELECT topic FROM camera_passages WHERE document=2 AND page=25').fetchone()[0]
                    self.assertEqual(db.execute('SELECT count(*) FROM camera_passages WHERE document=1 AND topic=?',(novel,)).fetchone()[0],0)
                report=json.loads((root/'temi/temi-annuali.json').read_text())
                self.assertEqual({r['year'] for r in report['topics']},{'2016','2020','2024'})
                self.assertEqual(discover(root,notify=lambda _:None)['run'],result['run'])
                stop=threading.Event()
                # Force a different harmonization setting, then cancel exactly
                # before publication; all previously visible labels must survive.
                def notify(stage):
                    if stage.startswith('Salvataggio del modello annuale'):stop.set()
                with patch.dict(CONFIG,complete_link_distance=.25):
                    with self.assertRaises(Cancelled):discover(root,stop,notify)
                with archive.connect() as db:
                    self.assertEqual(catalog(db)[1],result['run'])
                    self.assertEqual(list(map(tuple,db.execute('SELECT document,page,topic,version FROM camera_passages ORDER BY document,page'))),before)
                def change_metadata(stage):
                    if stage.startswith('Salvataggio del modello annuale'):
                        with archive.connect() as db:db.execute("UPDATE camera_sessions SET session_date='2021-01-01' WHERE document=2")
                with patch.dict(CONFIG,complete_link_distance=.24):
                    with self.assertRaisesRegex(ValueError,'Date o metadati cambiati'):
                        discover(root,notify=change_metadata)
                with archive.connect() as db:self.assertEqual(catalog(db)[1],result['run'])


if __name__=='__main__':unittest.main()
