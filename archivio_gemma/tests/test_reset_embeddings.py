"""Destructive reset checks on isolated archives, including the real GUI dialog."""
import os,sys,tempfile,threading,time,unittest,sqlite3
from pathlib import Path
from unittest.mock import Mock
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('OPENBLAS_NUM_THREADS','4');os.environ.setdefault('OMP_NUM_THREADS','4')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtTest import QTest
import app as gui
from core import Archive,Cancelled,unit

qt=QApplication([]);qt.setStyleSheet(gui.STYLE)


class ResetChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.a=Archive(self.root/'archive');self.stop=threading.Event();self.window=None
        for i in range(3):
            path=self.root/f'documento-{i}.txt';path.write_text(f'Testo conservato {i}')
            self.a.add([path]);doc=self.a.prepare(self.a.pending()[-1],self.stop)
            self.a.page_commit(doc,1,[('testo',path.read_text(),unit(np.eye(768)[i])),('figura','Immagine',unit(np.eye(768)[i]))],'contesto precedente')
            self.a.update(doc['id'],status=['ready','paused','error'][i],error='errore precedente' if i==2 else '',pages=3)
        path=self.root/'foto.png';Image.new('RGB',(8,8),'red').save(path)
        self.a.add([path]);doc=self.a.prepare(self.a.pending()[-1],self.stop)
        self.a.page_commit(doc,1,[('figura','Foto',unit(np.eye(768)[3]))]);self.a.update(doc['id'],status='ready')
        queued=self.root/'in-coda.md';queued.write_text('Testo ancora da indicizzare');self.a.add([queued])
        (self.a.data/'ultimo-rapporto.json').write_text('{"report":"precedente"}')
        (self.a.data/'rapporti').mkdir();(self.a.data/'rapporti/storico.json').write_text('rapporto storico')
        self.files={str(p):p.read_bytes() for p in self.root.glob('*.txt')}
        self.files.update({str(p):p.read_bytes() for p in (self.a.data/'documenti').iterdir()})
    def tearDown(self):
        if self.window:self.window.close();qt.processEvents()
        self.tmp.cleanup()
    def snapshot(self):
        with self.a.connect() as db:
            return ([tuple(r) for r in db.execute('SELECT * FROM documents ORDER BY id')],
                    [tuple(r) for r in db.execute('SELECT * FROM chunks ORDER BY id')])
    def settle(self):
        end=time.monotonic()+15
        for _ in range(5):qt.processEvents();time.sleep(.01)
        while self.window.task and time.monotonic()<end:qt.processEvents();time.sleep(.01)
        self.assertIsNone(self.window.task)
    def assert_reset(self):
        with self.a.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents WHERE status='queued' AND done=0 AND centroid IS NULL AND aggregate IS NULL AND tail='' AND error=''").fetchone()[0],5)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute("SELECT pages FROM documents WHERE name='foto.png'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT MAX(pages) FROM documents WHERE name LIKE 'documento-%'").fetchone()[0],0)
        for path,content in self.files.items():self.assertEqual(Path(path).read_bytes(),content)
        self.assertEqual(self.a.map(self.stop),[]);self.assertEqual(self.a.search(unit(np.eye(768)[0]),self.stop),[])
        self.assertFalse((self.a.data/'ultimo-rapporto.json').exists())
        self.assertTrue((self.a.data/'rapporti/storico.json').exists())
    def test_full_reset_preserves_documents_and_allows_new_checkpoint(self):
        before=[tuple(d[k] for k in ['id','source','name','title','stored','url']) for d in self.a.documents()]
        result=self.a.reset_embeddings(self.stop);self.assertEqual(result['embeddings'],7);self.assertEqual(result['documents'],5)
        self.assert_reset()
        self.assertEqual(before,[tuple(d[k] for k in ['id','source','name','title','stored','url']) for d in self.a.documents()])
        self.a=Archive(self.a.data);self.assertEqual(len(self.a.pending()),5)
        doc=self.a.prepare(self.a.pending()[0],self.stop)
        self.a.page_commit(doc,1,[('testo','Nuovo calcolo',unit(np.eye(768)[0]))]);self.a.update(doc['id'],pages=1,status='ready')
        self.assertEqual(len(self.a.map(self.stop)),1)
    def test_database_failure_rolls_back_deleted_vectors(self):
        before=self.snapshot()
        with self.a.connect() as db:db.execute("CREATE TRIGGER reject_reset BEFORE UPDATE ON documents BEGIN SELECT RAISE(ABORT,'simulated failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.a.reset_embeddings(self.stop)
        self.assertEqual(before,self.snapshot());self.assertTrue((self.a.data/'ultimo-rapporto.json').exists())
    def test_cancel_rolls_back_and_empty_archive_is_safe(self):
        before=self.snapshot()
        with self.assertRaises(Cancelled):self.a.reset_embeddings(self.stop,lambda _:self.stop.set())
        self.assertEqual(before,self.snapshot());self.stop.clear()
        empty=Archive(self.root/'empty');self.assertEqual(empty.reset_embeddings(self.stop)['embeddings'],0)
    def test_gui_cancel_escape_confirm_and_disabled_while_busy(self):
        self.window=w=gui.Window(self.a);w.show();self.settle()
        self.assertTrue(w.resetbutton.isEnabled());before=self.snapshot();observed=[]
        def answer(mode):
            dialog=qt.activeModalWidget()
            if not isinstance(dialog,QMessageBox):
                observed.append(False)
                if dialog:dialog.reject()
                return
            observed.append(dialog.defaultButton().text()=='Annulla' and dialog.escapeButton().text()=='Annulla')
            if mode=='escape':QTest.keyClick(dialog,Qt.Key.Key_Escape)
            else:next(b for b in dialog.buttons() if b.text()==mode).click()
        for mode in ['Annulla','escape']:
            QTimer.singleShot(20,lambda mode=mode:answer(mode));w.resetbutton.click();self.settle()
            self.assertEqual(before,self.snapshot())
        engine=Mock();w.engine=engine
        w.search_done(self.a.search(unit(np.eye(768)[0]),self.stop));w.query.setText('vecchia ricerca');w.perf['vectors'].setText('7')
        QTimer.singleShot(20,lambda:answer('Elimina tutti'));w.resetbutton.click()
        self.assertFalse(w.resetbutton.isEnabled());self.settle()
        self.assertTrue(all(observed));self.assertEqual(len(observed),3)
        self.assert_reset();engine.close.assert_called_once();self.assertIsNone(w.engine)
        self.assertFalse(w.map.items_data);self.assertFalse(w.search_rows);self.assertFalse(w.query.text())
        self.assertEqual(w.perf['vectors'].text(),'0');self.assertEqual(w.progress.value(),0)
        self.assertTrue(w.resetbutton.isEnabled());self.assertIn('Eliminati 7 embedding',w.status.text())
        self.assertTrue(all(d['status']=='queued' for d in w.docs))
        self.assertIsNone(qt.activeModalWidget())


if __name__=='__main__':unittest.main(verbosity=2)
