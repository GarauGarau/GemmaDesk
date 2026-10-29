"""Persistent local multimodal archive. AGPL-3.0-or-later."""
from __future__ import annotations
import hashlib, json, os, sqlite3, subprocess, tempfile, time
from contextlib import contextmanager
from pathlib import Path
import numpy as np
from PIL import Image
from segmentation import segments, tail, TEXT_LIMIT, OVERLAP_LIMIT

APP = Path(__file__).resolve().parent
ROOT = APP.parent
DEFAULT_CORPUS = ROOT/'data/documents'
SUPPORTED = {'.pdf', '.txt', '.md', '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.bmp', '.docx'}
# Bump when preprocessing changes: never mix incompatible representations silently.
PIPELINE = 'eg2-q8-768-smart1024-overlap96-v5'
CONTEXT_TOKENS = 8192  # Native worker budget, shared by the inputs in a batch.
EXPECTED_HASHES = {'embeddinggemma-2-Q8_0.gguf':'2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135', 'mmproj-embeddinggemma-2-Q8_0.gguf':'c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f'}

class Cancelled(Exception): pass

def check(stop):
    if stop.is_set(): raise Cancelled('Elaborazione sospesa; le pagine completate sono conservate.')

def unit(v):
    v=np.asarray(v,dtype='<f4')
    if v.shape != (768,) or not np.isfinite(v).all():
        raise ValueError('Embedding non valido: servono 768 valori finiti e norma positiva.')
    # Accumulate in float64: finite float32 components can overflow a float32 norm.
    precise=v.astype(np.float64);norm=np.linalg.norm(precise)
    if norm<1e-8:
        raise ValueError('Embedding non valido: servono 768 valori finiti e norma positiva.')
    return (precise/norm).astype('<f4')

def digest_file(path, stop):
    check(stop)
    h=hashlib.sha256()
    with open(path,'rb') as f:
        while b:=f.read(1024*1024): check(stop);h.update(b)
    return h.hexdigest()

def run(command, stop, timeout=180):
    # File-backed output avoids deadlocks and holds no unbounded pipe buffer.
    check(stop)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p=subprocess.Popen([str(x) for x in command],stdout=out,stderr=err)
        start=time.monotonic()
        try:
            while p.poll() is None:
                check(stop)
                if time.monotonic()-start>timeout: raise TimeoutError(f'Estrazione troppo lenta: superati {timeout:g} secondi per gruppo di pagine.')
                stop.wait(.08)
            check(stop)
            out.seek(0);err.seek(0)
            if p.returncode: raise RuntimeError(err.read().decode(errors='replace')[-3000:])
            return out.read()
        finally:
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.wait()

class Engine:
    """Persistent native subprocess. Pipes only: no HTTP service or network socket."""
    def __init__(self, data, backend='vulkan', port=None, batch_size=4, text_only=False, slot=0):
        self.data=Path(data);self.backend=backend;self.process=None;self.log=None
        self.batch_size=batch_size;self.text_only=text_only
        self.log_path=self.data/('modello.log' if slot==0 else f'modello-{slot+1}.log')
        self.info={};self._token_cache={};self.reset_stats()
    def reset_stats(self):
        self.stats=dict(load_seconds=0.,embedding_seconds=0.,tokenizer_seconds=0.,
                        text_embeddings=0,image_embeddings=0,input_tokens=0,batch_calls=0)
    def resources(self):
        result={};p=self.process
        if not p or p.poll() is not None:return result
        try:
            fields=dict(line.split(':',1) for line in Path(f'/proc/{p.pid}/status').read_text().splitlines() if ':' in line)
            result['ram_mib']=int(fields['VmRSS'].split()[0])/1024
            result['ram_peak_mib']=int(fields['VmHWM'].split()[0])/1024
        except (OSError,KeyError,ValueError):pass
        clients={}
        try:
            for path in Path(f'/proc/{p.pid}/fdinfo').iterdir():
                fields=dict(line.split(':',1) for line in path.read_text().splitlines() if ':' in line)
                if fields.get('drm-driver','').strip()!='amdgpu':continue
                clients[fields.get('drm-client-id',path.name)]=fields
            if clients:
                for key,out in [('drm-memory-vram','gpu_vram_mib'),('drm-memory-gtt','gpu_gtt_mib')]:
                    result[out]=sum(int(f.get(key,'0').split()[0]) for f in clients.values())/1024
                result['gpu_compute_ns']=sum(int(value.split()[0]) for f in clients.values() for key,value in f.items() if key.startswith('drm-engine-'))
        except (OSError,ValueError):pass
        if self.backend=='vulkan':
            for path in Path('/sys/class/drm').glob('card*/device/gpu_busy_percent'):
                try:result['gpu_device_busy_percent']=int(path.read_text());break
                except (OSError,ValueError):pass
        return result
    def receive(self,stop):
        import select
        start=time.monotonic()
        while time.monotonic()-start<180:
            try:check(stop)
            except Cancelled:self.close();raise
            if self.process.poll() is not None:
                tail=self.log_path.read_text(errors='replace')[-1000:]
                raise RuntimeError('Motore arrestato: '+tail)
            if select.select([self.process.stdout],[],[],.1)[0]:
                line=self.process.stdout.readline()
                if not line:raise RuntimeError('Motore chiuso senza risultato; consulta modello.log.')
                return json.loads(line)
        self.close();raise TimeoutError('Inferenza oltre 180 secondi: pagina conservata per un nuovo tentativo.')
    def start(self,stop,notify):
        if self.process and self.process.poll() is None:return
        self.close();self.info={};load_start=time.monotonic();notify('Verifica dei pesi locali…')
        for name,expected in EXPECTED_HASHES.items():
            if self.text_only and name.startswith('mmproj'):continue
            if digest_file(ROOT/'modelli/embeddinggemma-2'/name,stop)!=expected:
                raise RuntimeError('Il modello locale non corrisponde alla versione dell’archivio: '+name)
        self.data.mkdir(parents=True,exist_ok=True);self.log=open(self.log_path,'wb')
        self.process=subprocess.Popen([str(APP/'native/embed'),str(ROOT/'modelli/embeddinggemma-2/embeddinggemma-2-Q8_0.gguf'),str(ROOT/'modelli/embeddinggemma-2/mmproj-embeddinggemma-2-Q8_0.gguf'),str(ROOT/'runtime/llama-b11658'),self.backend],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log)
        notify('Caricamento EmbeddingGemma 2 ('+self.backend+')…')
        try:
            self.info=self.receive(stop)
            if not self.info.get('ready'):raise RuntimeError('Il modello non è pronto.')
            self.info.update(text_only=self.text_only,batch_size=self.batch_size,workers=1)
            self.info['gpu_verified']=False
            if self.backend=='vulkan':
                notify('Verifica GPU: inferenza reale '+('di testo…' if self.text_only else 'di testo e immagine…'))
                saved_stats=dict(self.stats)
                before=self.resources().get('gpu_compute_ns')
                self.encode('title: Test GPU | text: Test di esecuzione locale.',stop)
                if not self.text_only:
                    pixels=Image.new('RGB',(336,336),(180,60,30))
                    result=self.command('I',pixels.tobytes(),stop,' 336 336');unit(result['vector'])
                after=self.resources().get('gpu_compute_ns')
                log=self.log_path.read_text(errors='replace')
                import re
                offload=re.findall(r'offloaded (\d+)/(\d+) layers to GPU',log)
                self.info['layers']=offload[-1] if offload else None
                self.info['vision_gpu']='CLIP using Vulkan0 backend' in log
                if not offload or offload[-1][0]!=offload[-1][1] or (not self.text_only and not self.info['vision_gpu']):
                    raise RuntimeError('Il log non conferma il caricamento completo di testo e visione su Vulkan0.')
                self.info['gpu_compute_delta_ns']=None if before is None or after is None else after-before
                if before is not None and after is not None and after<=before:
                    raise RuntimeError('GPU richiesta, ma il contatore di calcolo non è aumentato durante il test.')
                self.info['gpu_verified']=True
                # Do not count the startup probes as corpus embeddings.
                self.stats=saved_stats
            self.stats['load_seconds']+=time.monotonic()-load_start
        except BaseException:
            self.info['ready']=False
            self.close();raise
    def command(self,kind,data,stop,dimensions=''):
        check(stop)
        if not self.process or self.process.poll() is not None:raise RuntimeError('Motore non attivo. Avvia nuovamente il calcolo.')
        self.process.stdin.write(f'{kind} {len(data)}{dimensions}\n'.encode()+data);self.process.stdin.flush()
        return self.receive(stop)
    def tokens(self,text,stop):
        check(stop)
        if text in self._token_cache:return self._token_cache[text]
        t=time.monotonic();result=self.command('C',text.encode(),stop)
        self.stats['tokenizer_seconds']+=time.monotonic()-t
        if len(self._token_cache)>=512:self._token_cache.clear()
        self._token_cache[text]=result['count']
        return result['count']
    def record_embedding(self,result,kind):
        vector=unit(result['vector']);self.stats[kind+'_embeddings']+=1
        self.stats['input_tokens']+=result.get('tokens',0)
        self.stats['embedding_seconds']+=result.get('seconds',0.)
        return vector
    def encode(self,text,stop):return self.record_embedding(self.command('T',text.encode(),stop),'text')
    def encode_many(self,texts,stop):
        if len(texts)==1 or self.batch_size==1 or self.info.get('max_text_batch',0)<2:
            return [self.encode(text,stop) for text in texts]
        if len(texts)>min(8,self.batch_size):raise ValueError('Batch testuale troppo grande.')
        sizes=[self.tokens(text,stop) for text in texts]
        if any(size>CONTEXT_TOKENS for size in sizes):raise ValueError('Input oltre 8.192 token.')
        if sum(sizes)>CONTEXT_TOKENS:
            result=[];batch=[];used=0
            for text,size in zip(texts,sizes):
                if batch and used+size>CONTEXT_TOKENS:
                    result.extend(self.encode_many(batch,stop));batch=[];used=0
                batch.append(text);used+=size
            if batch:result.extend(self.encode_many(batch,stop))
            return result
        check(stop);payload=b'B '+str(len(texts)).encode()+b'\n'
        for text in texts:
            data=text.encode();payload+=str(len(data)).encode()+b'\n'+data
        self.process.stdin.write(payload);self.process.stdin.flush();result=self.receive(stop)
        vectors=[unit(v) for v in result['vectors']]
        if len(vectors)!=len(texts):raise RuntimeError('Il batch ha restituito un numero errato di vettori.')
        self.stats['text_embeddings']+=len(vectors);self.stats['input_tokens']+=sum(result['tokens'])
        self.stats['embedding_seconds']+=result['seconds'];self.stats['batch_calls']+=1
        return vectors
    def overlap_tail(self,text,stop):
        return tail(text,lambda value:self.tokens(value,stop))

    def text_inputs(self,text,title,stop,context=''):
        prefix=f'title: {title[:200]} | text: '
        blocks=(text[i:i+32768] for i in range(0,len(text),32768))
        for part in segments(blocks,lambda value:self.tokens(value,stop),prefix,context,check=lambda:check(stop)):
            yield part.text

    def file_inputs(self,path,title,stop):
        prefix=f'title: {title[:200]} | text: '
        with open(path,encoding='utf-8-sig',errors='strict',newline='') as f:
            for part in segments(iter(lambda:f.read(32768),''),lambda value:self.tokens(value,stop),prefix,check=lambda:check(stop)):
                yield part.text

    def text_chunks(self,text,title,stop,context=''):
        passages=[];prefix=f'title: {title[:200]} | text: '
        for passage in self.text_inputs(text,title,stop,context):
            passages.append(passage)
            if len(passages)>=self.batch_size:
                vectors=self.encode_many([prefix+p for p in passages],stop)
                yield from zip(passages,vectors);passages=[]
        if passages:yield from zip(passages,self.encode_many([prefix+p for p in passages],stop))
    def image(self,path,stop):
        with Image.open(path) as im:
            im=im.convert('RGB');im.thumbnail((1344,1344))
            return self.record_embedding(self.command('I',im.tobytes(),stop,f' {im.width} {im.height}'),'image')
    def query(self,text,stop):
        text='task: search result | query: '+text
        if len(text)>20000 or self.tokens(text,stop)>8000:raise ValueError('Ricerca troppo lunga: usa meno di 8.000 token.')
        return self.encode(text,stop)
    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
            if self.process.stdin:self.process.stdin.close()
            if self.process.stdout:self.process.stdout.close()
        self.process=None
        self.info['ready']=False
        if self.log:self.log.close();self.log=None

class EnginePool:
    """At most two independent model contexts; each receives disjoint batches.

    Tokenization runs between batches on the primary worker. No pipe or context
    is ever shared by concurrent calls. Archive writes stay on the caller thread.
    """
    def __init__(self,data,backend='vulkan',workers=1,batch_size=4,text_only=False):
        from concurrent.futures import ThreadPoolExecutor
        if workers not in (1,2) or batch_size not in (1,2,4,8):raise ValueError('Configurazione worker non valida.')
        self.data=Path(data);self.backend=backend;self.workers=workers;self.per_worker_batch=batch_size
        self.batch_size=workers*batch_size;self.text_only=text_only;self.dispatch_seconds=0.
        self.engines=[Engine(data,backend,batch_size=batch_size,text_only=text_only,slot=i) for i in range(workers)]
        self.executor=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='embedding')
    @property
    def info(self):
        info=dict(self.engines[0].info)
        info.update(workers=self.workers,batch_size=self.per_worker_batch,text_only=self.text_only)
        info['gpu_verified']=all(e.info.get('gpu_verified',False) for e in self.engines)
        info['ready']=all(e.info.get('ready',False) for e in self.engines)
        return info
    @property
    def stats(self):
        values={key:sum(e.stats.get(key,0) for e in self.engines) for key in self.engines[0].stats}
        values['batch_wall_seconds']=self.dispatch_seconds
        return values
    def reset_stats(self):
        for engine in self.engines:engine.reset_stats()
        self.dispatch_seconds=0.
    def start(self,stop,notify):
        if self.executor is None:
            from concurrent.futures import ThreadPoolExecutor
            self.executor=ThreadPoolExecutor(max_workers=self.workers,thread_name_prefix='embedding')
        try:
            for i,engine in enumerate(self.engines):
                engine.start(stop,lambda message:notify(f'Worker {i+1}/{self.workers} · {message}'))
        except BaseException:
            self.close();raise
    def encode_many(self,texts,stop):
        if len(texts)>self.batch_size:raise ValueError('Troppi input nel batch.')
        started=time.monotonic();futures=[]
        size=max(1,(len(texts)+self.workers-1)//self.workers)
        for worker,i in enumerate(range(0,len(texts),size)):
            engine=self.engines[worker]
            futures.append(self.executor.submit(engine.encode_many,texts[i:i+size],stop))
        try:return [v for future in futures for v in future.result()]
        finally:
            # Join every submitted request even on error before reusing a pipe.
            for future in futures:
                try:future.result()
                except Exception:pass
            self.dispatch_seconds+=time.monotonic()-started
    def tokens(self,*args):return self.engines[0].tokens(*args)
    def overlap_tail(self,*args):return self.engines[0].overlap_tail(*args)
    def encode(self,*args):return self.engines[0].encode(*args)
    def image(self,*args):return self.engines[0].image(*args)
    def query(self,*args):return self.engines[0].query(*args)
    text_inputs=Engine.text_inputs
    text_chunks=Engine.text_chunks
    file_inputs=Engine.file_inputs
    def resources(self):
        parts=[e.resources() for e in self.engines];result={}
        for key in ['ram_mib','ram_peak_mib','gpu_vram_mib','gpu_gtt_mib','gpu_compute_ns']:
            if any(key in p for p in parts):result[key]=sum(p.get(key,0) for p in parts)
        if parts and 'gpu_device_busy_percent' in parts[0]:result['gpu_device_busy_percent']=parts[0]['gpu_device_busy_percent']
        return result
    def close(self):
        if self.executor:self.executor.shutdown(wait=True,cancel_futures=True);self.executor=None
        for engine in self.engines:engine.close()

class Archive:
    def __init__(self,data=APP/'dati'):
        self.data=Path(data);self.data.mkdir(parents=True,exist_ok=True)
        self._thematic_cache={}
        (self.data/'documenti').mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(
              id INTEGER PRIMARY KEY, source TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
              sha TEXT, stored TEXT, title TEXT, pages INTEGER DEFAULT 0,
              done INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
              error TEXT DEFAULT '', centroid BLOB, aggregate BLOB, tail TEXT DEFAULT '',
              created TEXT DEFAULT CURRENT_TIMESTAMP,url TEXT DEFAULT '');
            CREATE UNIQUE INDEX IF NOT EXISTS document_sha ON documents(sha) WHERE sha IS NOT NULL;
            CREATE TABLE IF NOT EXISTS chunks(
              id INTEGER PRIMARY KEY, document INTEGER NOT NULL REFERENCES documents(id),
              page INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, vector BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document,page);
            ''')
            version=db.execute("SELECT value FROM meta WHERE key='pipeline'").fetchone()
            if version and version[0]!=PIPELINE:raise RuntimeError('Archivio creato con una pipeline diversa; usa una nuova cartella dati.')
            db.execute("INSERT OR IGNORE INTO meta VALUES('pipeline',?)",(PIPELINE,))
            db.execute("UPDATE documents SET status='paused' WHERE status='processing'")
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.data/'archivio.sqlite3',timeout=30)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:yield db
        finally:db.close()
    def reset_embeddings(self,stop,notify=lambda message:None):
        """Atomically clear every indexed representation, retaining the library."""
        check(stop);notify('Eliminazione di tutti gli embedding della biblioteca…')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            embeddings=db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
            documents=db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
            db.execute('DELETE FROM chunks')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='camera_passages'").fetchone():
                db.execute('UPDATE camera_passages SET topic=NULL,suggested=NULL,secondary=NULL,similarity=NULL,margin=NULL,evidence=NULL,method=NULL,version=NULL')
                db.execute("DELETE FROM meta WHERE key='camera_discovery_run'")
            db.execute("""UPDATE documents SET status='queued',done=0,error='',
                          centroid=NULL,aggregate=NULL,tail='',
                          pages=CASE WHEN lower(source) LIKE '%.txt' OR lower(source) LIKE '%.md'
                                     THEN 0 ELSE pages END""")
            check(stop)  # cancellation before commit rolls back the whole reset
        self._thematic_cache.clear()
        self.last_report=None;self.live_stats={};self.run_documents=[]
        warning=''
        try:(self.data/'ultimo-rapporto.json').unlink(missing_ok=True)
        except OSError:
            warning='Gli embedding sono eliminati, ma non è stato possibile rimuovere il vecchio rapporto delle prestazioni.'
        return dict(embeddings=embeddings,documents=documents,warning=warning)
    def add(self,paths,stop=None,notify=lambda message:None):
        """Reject byte-identical files before loading any model, including queued files."""
        import threading
        stop=stop if stop is not None else threading.Event()
        paths=list(paths);result=dict(added=[],duplicates=[],rejected=[])
        def fingerprint(path):
            before=path.stat();sha=digest_file(path,stop);after=path.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
                raise ValueError('File modificato durante la lettura: riprova quando il salvataggio è terminato.')
            return sha
        # One transaction: cancellation rolls back the entire selection. WAL keeps
        # readers responsive; hashing runs in the UI's worker, never its main thread.
        with self.connect() as db:
            camera=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='camera_sessions'").fetchone())
            # Older queues did not store fingerprints until indexing. Backfill them
            # without extracting pages, starting the model or touching original files.
            legacy=db.execute('SELECT * FROM documents WHERE sha IS NULL ORDER BY id').fetchall()
            for number,doc in enumerate(legacy,1):
                check(stop);notify(f'Controllo coda esistente {number}/{len(legacy)}: {doc["name"]}')
                p=self.data/doc['stored'] if doc['stored'] else Path(doc['source'])
                try:sha=fingerprint(p)
                except (OSError,ValueError):continue
                if not db.execute('SELECT id FROM documents WHERE sha=?',(sha,)).fetchone():
                    db.execute('UPDATE documents SET sha=? WHERE id=?',(sha,doc['id']))
            for number,path in enumerate(paths,1):
                check(stop)
                p=Path(path).resolve()
                notify(f'Verifica duplicati {number}/{len(paths)}: {p.name}')
                try:
                    if not p.is_file() or p.suffix.lower() not in SUPPORTED:
                        raise ValueError('File mancante o formato non supportato.')
                    sha=fingerprint(p)
                    other=db.execute('SELECT id,name,status,source FROM documents WHERE sha=?',(sha,)).fetchone()
                    if other:
                        result['duplicates'].append(dict(path=str(p),name=p.name,existing=dict(other)))
                        continue
                    if db.execute('SELECT id FROM documents WHERE source=?',(str(p),)).fetchone():
                        raise ValueError('Percorso già presente ma contenuto cambiato: per aggiungere questa versione usa un nome diverso. La versione in biblioteca è conservata.')
                    title=None;url='';name=p.name
                    from metadata import read_metadata
                    meta=read_metadata(p,sha)
                    if meta:
                        title=meta.get('title') or p.stem;url=meta.get('url','');name=title
                    camera_row=db.execute('SELECT slug,title,url FROM camera_sessions WHERE clean_source=?',(str(p),)).fetchone() if camera else None
                    if camera and not camera_row and db.execute("SELECT 1 FROM meta WHERE key='camera_preprocessing' AND value='camera-thematic-v2'").fetchone():
                        raise ValueError('La raccolta Camera richiede resoconti con metadati ufficiali. Aggiorna il catalogo e prepara il corpus prima di aggiungerli.')
                    if camera_row:title=camera_row['title'];name=title;url=camera_row['url']
                    cursor=db.execute('INSERT INTO documents(source,name,sha,title,url) VALUES(?,?,?,?,?)',(str(p),name,sha,title,url))
                    if camera_row:db.execute('UPDATE camera_sessions SET document=? WHERE slug=?',(cursor.lastrowid,camera_row['slug']))
                    result['added'].append(dict(id=cursor.lastrowid,path=str(p),name=p.name))
                except (OSError,ValueError) as e:
                    result['rejected'].append(dict(path=str(p),name=p.name,reason=str(e)))
            check(stop)
        return result
    def documents(self):
        with self.connect() as db:return [dict(r) for r in db.execute('SELECT id,name,title,source,stored,status,pages,done,error,url FROM documents ORDER BY id DESC')]
    def pending(self):
        with self.connect() as db:return [dict(r) for r in db.execute("SELECT * FROM documents WHERE status!='ready' ORDER BY id")]
    def update(self,id,**fields):
        with self.connect() as db:db.execute('UPDATE documents SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),id))
    def prepare(self,doc,stop):
        if doc['stored']:
            p=self.data/doc['stored']
            if not p.is_file():raise FileNotFoundError('Copia archiviata mancante: '+str(p))
            return doc
        source=Path(doc['source']);temp=None
        try:
            h=hashlib.sha256()
            with tempfile.NamedTemporaryFile(dir=self.data/'documenti',suffix='.part',delete=False) as out:
                temp=Path(out.name)
                with source.open('rb') as inp:
                    while b:=inp.read(1024*1024):check(stop);h.update(b);out.write(b)
                out.flush();os.fsync(out.fileno())
            sha=h.hexdigest()
            with self.connect() as db:
                other=db.execute('SELECT id FROM documents WHERE sha=? AND id!=?',(sha,doc['id'])).fetchone()
                if other:
                    db.execute('DELETE FROM documents WHERE id=?',(doc['id'],));return None
            stored=Path('documenti')/(sha+source.suffix.lower())
            os.replace(temp,self.data/stored);temp=None
            suffix=source.suffix.lower()
            if suffix=='.pdf':
                info=json.loads(run([APP/'native/extract',self.data/stored,'info'],stop))
                pages=info['pages'];title=info['title'] or source.stem
            elif suffix in {'.tif','.tiff'}:
                with Image.open(self.data/stored) as im:pages=im.n_frames
                title=source.stem
            elif suffix in {'.txt','.md'}:
                pages=doc.get('pages') or 1  # exact token plan is prepared by index()
                title=doc.get('title') or source.stem
            else:pages=1;title=doc.get('title') or source.stem
            if pages<1:raise ValueError('Documento senza pagine.')
            self.update(doc['id'],sha=sha,stored=str(stored),pages=pages,title=title)
            doc.update(sha=sha,stored=str(stored),pages=pages,title=title)
            return doc
        finally:
            if temp:temp.unlink(missing_ok=True)
    def page_commit(self,doc,page,rows,tail=''):
        save_start=time.monotonic()
        if not rows:raise ValueError('Pagina senza contenuto indicizzabile.')
        vectors=np.stack([r[2] for r in rows]);page_vector=unit(vectors.mean(axis=0))
        with self.connect() as db:
            current=db.execute('SELECT done,aggregate FROM documents WHERE id=?',(doc['id'],)).fetchone()
            if current['done']!=page-1:raise RuntimeError('Checkpoint non sequenziale; archivio non modificato.')
            aggregate=np.zeros(768,dtype='<f4') if current['aggregate'] is None else np.frombuffer(current['aggregate'],dtype='<f4').copy()
            aggregate+=page_vector
            db.executemany('INSERT INTO chunks(document,page,kind,text,vector) VALUES(?,?,?,?,?)',[(doc['id'],page,kind,text,unit(vec).tobytes()) for kind,text,vec in rows])
            if getattr(self,'camera_v2',False):
                from camera_dataset import validate_commit
                if len(rows)!=1 or rows[0][0]!='testo':raise ValueError('Passaggio Camera non testuale')
                validate_commit(db,doc,page)
            db.execute('UPDATE documents SET done=?,aggregate=?,centroid=?,tail=? WHERE id=?',(page,aggregate.tobytes(),unit(aggregate).tobytes(),tail,doc['id']))
        if hasattr(self,'timings'):self.timings['storage_seconds']+=time.monotonic()-save_start
    def text_plan(self,doc,engine,stop):
        from camera_dataset import PLAN_VERSION
        camera=getattr(self,'camera_v2',False)
        version=('essays-no-overlap-v1' if getattr(self,'essay_profile',False) else PLAN_VERSION) if camera else PIPELINE
        key=hashlib.sha256(json.dumps([version,doc['sha'],doc['title']],ensure_ascii=False).encode()).hexdigest()
        folder=self.data/'plans';folder.mkdir(exist_ok=True);path=folder/(key+'.json')
        if path.exists():
            plan=json.loads(path.read_text())
        else:
            if camera:
                from camera_dataset import make_plan
                if getattr(self,'essay_profile',False):
                    from essay_dataset import make_plan as essay_plan
                    plan=essay_plan(doc,engine,stop,self.data)
                else:plan=make_plan(doc,engine,stop)
            else:
                plan=[dict(text=text,prefix=f'title: {doc["title"][:200]} | text: ') for text in engine.file_inputs(self.data/doc['stored'],doc['title'],stop)]
            check(stop);temp=path.with_suffix('.part');temp.write_text(json.dumps(plan,ensure_ascii=False));temp.replace(path)
        if camera:
            from camera_dataset import save_plan
            with self.connect() as db:save_plan(db,doc,plan)
        return plan

    def index(self,engine,stop,notify):
        self.timings=dict(preparation_seconds=0.,extraction_seconds=0.,storage_seconds=0.)
        self.live_stats={};self.run_documents=[];engine.reset_stats();started=time.monotonic()
        result={};state='completed'
        try:
            result=self._index(engine,stop,notify)
            return result
        except Cancelled:
            state='paused';raise
        except Exception:
            state='error';raise
        finally:
            elapsed=time.monotonic()-started
            pages=self.live_stats.get('pages_processed',0)
            stats=dict(engine.stats);processing=max(0.,elapsed-stats['load_seconds'])
            vectors=stats['text_embeddings']+stats['image_embeddings']
            report=dict(status=state,elapsed_seconds=elapsed,processing_seconds=processing,
                        pages_processed=pages,pages_per_second=pages/processing if processing else 0.,
                        embeddings_per_second=vectors/processing if processing else 0.,
                        inference_embeddings_per_second=vectors/(stats.get('batch_wall_seconds') or stats['embedding_seconds']) if (stats.get('batch_wall_seconds') or stats['embedding_seconds']) else 0.,
                        tokens_per_second=stats['input_tokens']/processing if processing else 0.,
                        documents_per_minute=result.get('completed',0)*60/processing if processing else 0.,
                        progress_unit=f'passaggio entro {TEXT_LIMIT} token o pagina PDF/immagine',
                        engine=dict(engine.info),backend_requested=engine.backend,
                        documents=self.run_documents,progress=dict(self.live_stats),
                        model=PIPELINE,runtime='llama.cpp b11658',quantization='Q8_0',threads=8,embedding_dimensions=768,context_tokens=CONTEXT_TOKENS,text_max_tokens=TEXT_LIMIT,overlap_max_tokens=0 if getattr(self,'camera_v2',False) else OVERLAP_LIMIT,**stats,**self.timings,resources=engine.resources())
            result.update(report)
            folder=self.data/'rapporti';folder.mkdir(exist_ok=True)
            report_path=folder/(time.strftime('%Y%m%d-%H%M%S')+f'-{time.time_ns()%1000000:06d}.json')
            report['report_file']=str(report_path);result['report_file']=str(report_path)
            serialized=json.dumps(report,ensure_ascii=False,indent=2)
            report_path.write_text(serialized)
            temporary=self.data/'ultimo-rapporto.json.part';temporary.write_text(serialized)
            os.replace(temporary,self.data/'ultimo-rapporto.json')
            self.last_report=report
    def _index(self,engine,stop,notify):
        jobs=self.pending();total=len(jobs);finished=0;ok=0;failed=0;duplicates=0
        page_count=0;page_seconds=0.;words_processed=0;remaining_words=0;remaining=0;start=time.monotonic()
        def estimate_eta():
            if getattr(self,'camera_v2',False):
                return max(0,remaining_words-words_processed)*(time.monotonic()-start)/words_processed if words_processed else None
            return remaining*page_seconds/page_count if page_count else None
        def emit(**extra):
            self.live_stats=dict(total=total,finished=finished,ok=ok,failed=failed,duplicates=duplicates,
                        elapsed=time.monotonic()-start,pages_processed=page_count,words_processed=words_processed,**extra)
            notify(self.live_stats)
        if not jobs:
            emit(stage='Nessun nuovo documento da elaborare',eta=0)
            return dict(completed=0,errors=0,duplicates=0,seconds=time.monotonic()-start)
        emit(stage='Preparazione del modello',eta=None)
        engine.start(stop,lambda s:emit(stage=s,eta=None))
        with self.connect() as db:
            meta=dict(db.execute('SELECT key,value FROM meta'))
        self.essay_profile=meta.get('thematic_preprocessing')=='essays-no-overlap-v1'
        self.camera_v2=meta.get('camera_preprocessing')=='camera-thematic-v2' or self.essay_profile
        if self.camera_v2:
            # Interleave years so partial runs do not cover only the oldest years.
            from collections import defaultdict,deque
            with self.connect() as db:dates={r['document']:r['session_date'] for r in db.execute('SELECT document,session_date FROM camera_sessions')}
            years=defaultdict(deque)
            for doc in jobs:years[dates.get(doc['id'],'')[:4]].append(doc)
            jobs=[]
            while any(years.values()):
                for year in sorted(years):
                    if years[year]:jobs.append(years[year].popleft())
        if self.camera_v2:
            from camera_dataset import initialize
            with self.connect() as db:
                initialize(db)
                total_words=db.execute('SELECT COALESCE(sum(kept_words),0) FROM camera_sessions').fetchone()[0]
                saved_words=db.execute('SELECT COALESCE(sum(p.words),0) FROM camera_passages p JOIN documents d ON d.id=p.document WHERE p.page<=d.done').fetchone()[0]
                remaining_words=total_words-saved_words
        # No full-corpus tokenization before the first checkpoint. Counts for
        # untouched files are estimates; one exact plan is cached per document.
        for doc in jobs:
            if not doc['pages']:
                try:doc['pages']=max(1,Path(doc['source']).stat().st_size//3200)
                except OSError:doc['pages']=1
        remaining=sum(max(0,d['pages']-d['done']) for d in jobs)
        for doc in jobs:
            check(stop);doc_start=time.monotonic();old_done=doc['done'];id=doc['id'];name=doc['name'];old_remaining=max(0,doc['pages']-doc['done'])
            try:
                emit(stage='Copia e verifica: '+name,eta=estimate_eta())
                preparation=time.monotonic();doc=self.prepare(doc,stop)
                self.timings['preparation_seconds']+=time.monotonic()-preparation
                if doc is None:
                    duplicates+=1;finished+=1;remaining-=old_remaining;continue
                planned=None
                if Path(doc['source']).suffix.lower() in {'.txt','.md'}:
                    preparation=time.monotonic()
                    planned=self.text_plan(doc,engine,stop)
                    doc['pages']=len(planned);self.update(id,pages=len(planned))
                    self.timings['preparation_seconds']+=time.monotonic()-preparation
                remaining+=max(0,doc['pages']-doc['done'])-old_remaining
                engine.start(stop,lambda message:emit(stage=message,eta=None))
                self.update(id,status='processing',error='')
                source=self.data/doc['stored'];suffix=source.suffix.lower()
                if suffix=='.pdf':
                    for first in range(doc['done']+1,doc['pages']+1,8):
                        check(stop)
                        last=min(first+7,doc['pages'])
                        emit(stage=f'{name} · analisi pagine {first}–{last}',eta=estimate_eta())
                        t=time.monotonic()
                        with tempfile.TemporaryDirectory(prefix='pagine-',dir=self.data) as temp:
                            result=json.loads(run([APP/'native/extract',source,first,last,temp,'json'],stop))
                            extract_elapsed=time.monotonic()-t
                            self.timings['extraction_seconds']+=extract_elapsed
                            extraction=extract_elapsed/(last-first+1)
                            for item in result['items']:
                                check(stop);page=item['page'];t=time.monotonic();rows=[]
                                emit(stage=f'{name} · embedding pagina {page}/{doc["pages"]}',eta=estimate_eta(),current_page=page-1,current_pages=doc['pages'])
                                # Keep all block text, including margins/notes. Layout ordering is Paperdesk's.
                                text='\n\n'.join(b['text'] for b in item['blocks'] if b['text'].strip())
                                for passage,vec in engine.text_chunks(text,doc['title'],stop,doc.get('tail','')):rows.append(('testo',passage,vec))
                                rows.append(('pagina',f'Vista completa della pagina {page}',engine.image(Path(temp)/f'page-{page}.png',stop)))
                                for b in item['blocks']:
                                    if b['image']:
                                        rows.append(('figura',b['text'] or b['reason'],engine.image(Path(temp)/b['image'],stop)))
                                check(stop);tail=engine.overlap_tail(text,stop)+'\n' if text else ''
                                self.page_commit(doc,page,rows,tail);doc['tail']=tail
                                page_count+=1;remaining-=1;page_seconds+=time.monotonic()-t+extraction
                                emit(stage=f'{name} · pagina {page}/{doc["pages"]} salvata',eta=estimate_eta(),current_page=page,current_pages=doc['pages'])
                elif suffix in {'.txt','.md','.docx'}:
                    if suffix=='.docx' and doc['done']==0:
                        t=time.monotonic()
                        if suffix=='.docx':
                            import zipfile,xml.etree.ElementTree as ET
                            with zipfile.ZipFile(source) as z:
                                root=ET.fromstring(z.read('word/document.xml'))
                                ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                                # Preserve runs within a paragraph; paragraph breaks
                                # are meaningful boundaries for the smart splitter.
                                text='\n\n'.join(''.join(n.text or '' if n.tag==ns+'t' else '\t' if n.tag==ns+'tab' else '\n' if n.tag in {ns+'br',ns+'cr'} else '' for n in p.iter()) for p in root.iter(ns+'p'))
                                rows=[('testo',s,v) for s,v in engine.text_chunks(text,doc['title'],stop)]
                                for entry in z.namelist():
                                    if entry.startswith('word/media/') and Path(entry).suffix.lower() in SUPPORTED- {'.pdf','.txt','.md','.docx'}:
                                        with tempfile.TemporaryDirectory(dir=self.data) as tmp:
                                            image=Path(tmp)/Path(entry).name;image.write_bytes(z.read(entry))
                                            rows.append(('figura',Path(entry).name,engine.image(image,stop)))
                        self.page_commit(doc,1,rows);page_count+=1;remaining-=1;page_seconds+=time.monotonic()-t
                    elif suffix in {'.txt','.md'}:
                        # Token-aware boundaries are deterministic across pause/resume.
                        # Each checkpoint now stores one complete semantic passage.
                        from itertools import islice
                        passages=iter(planned[doc['done']:])
                        page=doc['done']
                        while batch:=list(islice(passages,engine.batch_size)):
                            check(stop);t=time.monotonic()
                            emit(stage=f'{name} · embedding passaggi {page+1}–{page+len(batch)}/{doc["pages"]}',eta=estimate_eta(),current_page=page,current_pages=doc['pages'])
                            vectors=engine.encode_many([part['prefix']+part['text'] for part in batch],stop)
                            elapsed=(time.monotonic()-t)/len(batch)
                            for passage,vector in zip(batch,vectors):
                                check(stop);page+=1
                                self.page_commit(doc,page,[('testo',passage['text'],vector)])
                                words_processed+=passage.get('words',0)
                                page_count+=1;remaining-=1;page_seconds+=elapsed
                                emit(stage=f'{name} · passaggio {page}/{doc["pages"]} salvato',eta=estimate_eta(),current_page=page,current_pages=doc['pages'])
                        if page!=doc['pages'] or not page:raise ValueError('Testo vuoto o piano dei passaggi incompleto.')

                else:
                    with Image.open(source) as image:
                        for frame in range(doc['done'],doc['pages']):
                            check(stop);t=time.monotonic();image.seek(frame)
                            with tempfile.TemporaryDirectory(dir=self.data) as tmp:
                                target=Path(tmp)/'image.png';image.convert('RGB').save(target)
                                rows=[('figura',doc['title'],engine.image(target,stop))]
                                self.page_commit(doc,frame+1,rows);page_count+=1;remaining-=1;page_seconds+=time.monotonic()-t
                self.update(id,status='ready',error='');ok+=1
            except Cancelled:
                self.update(id,status='paused');raise
            except Exception as e:
                with self.connect() as db:row=db.execute('SELECT pages,done FROM documents WHERE id=?',(id,)).fetchone()
                if row:remaining-=max(0,(row['pages'] or doc['pages'])-row['done'])
                self.update(id,status='error',error=str(e));failed+=1
                emit(stage=name+' · errore: '+str(e),eta=None)
            finally:
                with self.connect() as db:summary=db.execute('SELECT status,done,pages,error FROM documents WHERE id=?',(id,)).fetchone()
                doc_seconds=time.monotonic()-doc_start
                new_pages=max(0,summary['done']-old_done) if summary else 0
                self.run_documents.append(dict(name=name,seconds=doc_seconds,new_pages=new_pages,pages_per_second=new_pages/doc_seconds if doc_seconds else 0.,status=summary['status'] if summary else 'duplicate',error=summary['error'] if summary else ''))
            finished+=1
            emit(stage='Avanzamento archivio',eta=estimate_eta())
        emit(stage='Elaborazione terminata',eta=0)
        return {'completed':ok,'errors':failed,'duplicates':duplicates,'seconds':time.monotonic()-start}
    def search(self,vector,stop,limit=20):
        # Exact streamed cosine retrieval over all passages/images, grouped by document.
        check(stop);vector=unit(vector)
        if limit<=0:return []
        best={}
        with self.connect() as db:
            cursor=db.execute("SELECT c.id AS chunk_id,c.document,c.page,c.kind,c.text,c.vector,d.name,d.stored,d.url,d.status,d.done,d.pages FROM chunks c JOIN documents d ON d.id=c.document WHERE d.done>0 AND c.page<=d.done")
            while rows:=cursor.fetchmany(2048):
                check(stop)
                if any(len(r['vector'])!=768*4 for r in rows):
                    raise ValueError('Embedding archiviato non valido. Archivio conservato.')
                matrix=np.stack([np.frombuffer(r['vector'],dtype='<f4') for r in rows])
                if not np.isfinite(matrix).all():
                    raise ValueError('Embedding archiviato non valido. Archivio conservato.')
                scores=matrix@vector
                for r,score in zip(rows,scores):
                    id=r['document']
                    if id not in best or score>best[id]['score']:
                        best[id]={'id':id,'chunk_id':r['chunk_id'],'name':r['name'],'page':r['page'],'kind':r['kind'],'text':r['text'][:900],'path':str(self.data/r['stored']),'url':r['url'],'score':float(score),
                                  'partial':r['status']!='ready','done':r['done'],'total_parts':r['pages']}
        check(stop)
        return sorted(best.values(),key=lambda x:x['score'],reverse=True)[:limit]
    def map(self,stop,clusters=0,notify=lambda message:None,scope='documents'):
        if scope not in {'documents','passages'}:raise ValueError('Vista mappa non valida.')
        with self.connect() as db:
            camera=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='camera_sessions'").fetchone())
            stable_camera=camera and bool(db.execute("SELECT 1 FROM meta WHERE (key='camera_preprocessing' AND value='camera-thematic-v2') OR (key='thematic_preprocessing' AND value='essays-no-overlap-v1')").fetchone())
            if scope=='documents':
                rows=db.execute("SELECT id,name,stored,centroid,status,done,pages AS total_parts,1 AS page,NULL AS chunk_id,'' AS text FROM documents WHERE done>0 ORDER BY id").fetchall()
            else:
                rows=db.execute("SELECT d.id,d.name,d.stored,d.status,d.done,d.pages AS total_parts,c.vector AS centroid,c.page,c.id AS chunk_id,c.text FROM chunks c JOIN documents d ON d.id=c.document WHERE d.done>0 AND c.page<=d.done AND c.kind='testo' ORDER BY c.id").fetchall()
            if scope=='passages' and stable_camera:
                excluded={r[0] for r in db.execute("SELECT c.id FROM chunks c JOIN camera_passages p ON p.document=c.document AND p.page=c.page WHERE p.topic=-2 AND p.version=(SELECT value FROM meta WHERE key='camera_discovery_run')")}
                rows=[r for r in rows if r['chunk_id'] not in excluded]
            if camera and not stable_camera:
                text_rows=db.execute("SELECT c.id,c.document,c.text FROM chunks c JOIN documents d ON d.id=c.document WHERE d.done>0 AND c.page<=d.done AND c.kind='testo' ORDER BY c.document,c.id").fetchall()
                documents=db.execute('SELECT id,source,stored FROM documents WHERE done>0 ORDER BY id').fetchall()
        n=len(rows)
        if not n:return []
        check(stop)
        vectors=[]
        for r in rows:
            check(stop)
            try:vectors.append(unit(np.frombuffer(r['centroid'],dtype='<f4')))
            except (TypeError,ValueError) as e:
                raise ValueError('Vettore della mappa non valido per '+r['name']+'. Archivio conservato.') from e
        matrix=np.stack(vectors)
        explained=None;projection_name='PCA';point_contexts=['']*n
        if camera:
            if stable_camera:
                from camera_stable_map import describe
                thematic=describe(rows,self.data,matrix,scope,self._thematic_cache,lambda:check(stop),notify)
            else:
                from thematic_map import describe
                thematic=describe(rows,text_rows,documents,self.data,matrix,scope,clusters,self._thematic_cache,lambda:check(stop),notify)
            matrix=thematic['features'];coords=thematic['coords'];projection_name=thematic['projection'];explained=thematic['explained']
            labels=thematic['labels'];group_score=thematic['score'];group_sample=thematic['sample'];topics=thematic['topics'];point_contexts=thematic['contexts']
        elif n==1 or np.max(np.ptp(matrix,axis=0))<1e-7:
            coords=np.zeros((n,2))
        else:
            from sklearn.decomposition import PCA
            notify('Mappa: proiezione di tutti i punti in 2D…')
            projection=PCA(n_components=2,svd_solver='randomized',random_state=42)
            coords=projection.fit_transform(matrix)
            explained=float(projection.explained_variance_ratio_.sum())
        if not camera:
            from grouping import hierarchical_groups
            labels,group_score,group_sample,contexts=hierarchical_groups(matrix,clusters,lambda:check(stop),notify,return_context=True)
        examples={}
        for label in np.unique(labels):
            indices=np.flatnonzero(labels==label)
            center=matrix[indices].mean(axis=0)
            order=indices[np.argsort(-(matrix[indices]@center),kind='stable')]
            examples[int(label)]=list(dict.fromkeys(rows[index]['name'] for index in order))[:3]
        check(stop)
        if not camera:
            notify('Mappa: estrazione dei temi dai testi di ciascun gruppo…')
            from topics import cluster_topics
            assignments={r['chunk_id'] if scope=='passages' else r['id']:int(label) for r,label in zip(rows,labels)}
            references={group:{rows[i]['chunk_id'] if scope=='passages' else rows[i]['id'] for i in indices}
                        for group,indices in contexts.items()}
            def texts():
                with self.connect() as db:
                    for r in db.execute("SELECT c.id,c.document,c.text FROM chunks c JOIN documents d ON d.id=c.document WHERE d.done>0 AND c.page<=d.done AND c.kind='testo' ORDER BY c.document,c.id"):
                        check(stop)
                        key=r['id'] if scope=='passages' else r['document']
                        if key in assignments:yield assignments[key],r['document'],r['text'],key
            topics=cluster_topics(texts(),lambda:check(stop),titles={r['id']:r['name'] for r in rows},references=references)
        check(stop)
        points=[dict(id=r['id'],point_id=r['id'] if scope=='documents' else f"chunk:{r['chunk_id']}",chunk_id=r['chunk_id'],page=r['page'],text=r['text'][:900],scope=scope,
                     name=r['name'],path=str(self.data/r['stored']),partial=r['status']!='ready',done=r['done'],total_parts=r['total_parts'],x=float(xy[0]),y=float(xy[1]),cluster=int(label),topics=topics.get(int(label),[]),
                     topic_context=context,projection=projection_name,thematic=camera,topic_examples=examples[int(label)],explained=explained,group_score=group_score,group_sample=group_sample) for r,xy,label,context in zip(rows,coords,labels,point_contexts)]
        if camera and stable_camera:
            for point,weight,written in zip(points,thematic['weights'],thematic['written']):
                point.update(topic_words=weight,written=bool(written),stable_topics=True,topic_run=thematic.get('run'))
        return points
