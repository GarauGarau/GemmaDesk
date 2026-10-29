"""Rebuild Camera derivatives, benchmark local hardware and resume indexing."""
from contextlib import closing
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import sqlite3
import threading
import time
from camera_preprocess import ROOT,VERSION,clean_html,words
from core import Archive,EnginePool,Cancelled,unit


def atomic_json(path,value):
    path=Path(path);temp=path.with_suffix(path.suffix+'.part')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2));temp.replace(path)


def prepare_one(args):
    root,stage,source=args
    raw=root/source['original'];text,audit=clean_html(raw.read_bytes())
    target=stage/'testi_tematici'/(source['slug']+'.txt')
    target.write_text(text+('\n' if text else ''),encoding='utf-8')
    counts=Counter()
    with gzip.open(stage/'temi/audit'/(source['slug']+'.jsonl.gz'),'wt',encoding='utf-8',compresslevel=1) as stream:
        for row in audit:
            stream.write(json.dumps(row,ensure_ascii=False)+'\n')
            if row['reason']:counts[row['reason']]+=row['input_words']
            else:
                for item in row.get('removed',[]):counts[item['reason']]+=words(item['text'])
    kept=sum(r['words'] for r in audit if r['kept']);total=sum(r['input_words'] for r in audit)
    return dict(slug=source['slug'],date=source['date'],title=source['title'],url=source['html_url'],
        pdf=str((root/source['pdf']).resolve()),original_source=str((root/source['text']).resolve()),
        source=str((root/'testi_tematici'/target.name).resolve()),sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        original_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),input_words=total,kept_words=kept,
        removed_words=total-kept,written_words=sum(r['words'] for r in audit if r['kept'] and r['written']),
        chair_words=sum(r['words'] for r in audit if r['kept'] and r['role']=='chair'),removed_by_reason=dict(counts))


def prepare(root,workers=12,rebuild=False):
    root=root.resolve();database=root/'database'
    if not rebuild and (database/'archivio.sqlite3').exists():
        with closing(sqlite3.connect(database/'archivio.sqlite3')) as db:
            if db.execute('SELECT count(*) FROM chunks').fetchone()[0]:raise RuntimeError('Usare --rebuild per sostituire esplicitamente i derivati esistenti')
    stage=root/'.camera-v2-staging'
    if stage.exists():shutil.rmtree(stage)
    (stage/'testi_tematici').mkdir(parents=True);(stage/'temi/audit').mkdir(parents=True)
    sources=json.loads((root/'catalogo.json').read_text())['documents'];records=[];started=time.monotonic()
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for i,record in enumerate(pool.map(prepare_one,((root,stage,s) for s in sources),chunksize=4),1):
                records.append(record)
                if i%100==0:print(f'Pulizia {i}/{len(sources)} · {time.monotonic()-started:.1f}s',flush=True)
        counts=Counter()
        for record in records:counts.update(record['removed_by_reason'])
        report=dict(version=VERSION,documents=len(records),with_content=sum(r['kept_words']>0 for r in records),
            input_words=sum(r['input_words'] for r in records),kept_words=sum(r['kept_words'] for r in records),
            removed_words=sum(r['removed_words'] for r in records),written_words=sum(r['written_words'] for r in records),
            chair_words=sum(r['chair_words'] for r in records),removed_by_reason=dict(counts),
            technical_only=[r['slug'] for r in records if not r['kept_words']],seconds=time.monotonic()-started,workers=workers,
            policy='Formule riconosciute escluse con audit; interventi sostanziali di tutti gli oratori conservati. Titoli e nomi nei metadati. Interventi scritti distinti dalla serie orale. Nessuna sovrapposizione fra passaggi.',
            scope=json.loads((root/'catalogo.json').read_text())['scope'])
        atomic_json(stage/'temi/manifest.json',records);atomic_json(stage/'temi/rapporto.json',report)
        archive=Archive(stage/'database')
        from camera_dataset import initialize
        with archive.connect() as db:
            initialize(db)
            db.execute('''CREATE TABLE camera_sessions(slug TEXT PRIMARY KEY,session_date TEXT NOT NULL,
                document INTEGER REFERENCES documents(id) ON DELETE SET NULL,title TEXT NOT NULL,url TEXT NOT NULL,
                pdf TEXT NOT NULL,clean_source TEXT NOT NULL,original_source TEXT NOT NULL,kept_words INTEGER NOT NULL,removed_words INTEGER NOT NULL)''')
            seen={}
            for r in records:
                doc=None
                if r['kept_words']:
                    if r['sha256'] in seen:
                        # Do not silently collapse two actual dated sessions.
                        raise ValueError('Due sedute hanno testo tematico identico: '+r['slug']+' / '+seen[r['sha256']])
                    seen[r['sha256']]=r['slug']
                    doc=db.execute('INSERT INTO documents(source,name,sha,title,url) VALUES(?,?,?,?,?)',
                        (r['source'],r['title'],r['sha256'],r['title'],r['url'])).lastrowid
                db.execute('INSERT INTO camera_sessions VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (r['slug'],r['date'],doc,r['title'],r['url'],r['pdf'],r['source'],r['original_source'],r['kept_words'],r['removed_words']))
            db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',[
                ('camera_preprocessing',VERSION),('collection','Camera dei deputati · Assemblea'),
                ('source_catalog',str(root/'catalogo.json')),('corpus_path',str(root/'testi_tematici'))])
        # Close/checkpoint before renaming directories; originals never move.
        with closing(sqlite3.connect(stage/'database/archivio.sqlite3')) as db:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if (root/'temi/campione-revisione.csv').exists():shutil.copy2(root/'temi/campione-revisione.csv',stage/'temi/campione-revisione.csv')
        if (root/'temi/prestazioni').is_dir():shutil.copytree(root/'temi/prestazioni',stage/'temi/prestazioni')
        for name in ('preferenze.json',):
            if (database/name).exists():shutil.copy2(database/name,stage/'database'/name)
        if (database/'app.lock').exists():os.link(database/'app.lock',stage/'database/app.lock')
        moved=[];installed=[]
        try:
            for name in ('testi_tematici','temi','database'):
                previous=root/(name+'.previous-v1')
                if previous.exists():raise RuntimeError('Backup precedente presente: '+str(previous))
                if (root/name).exists():(root/name).rename(previous);moved.append(name)
                (stage/name).rename(root/name);installed.append(name)
        except BaseException:
            for name in reversed(installed):(root/name).rename(stage/name)
            for name in reversed(moved):(root/(name+'.previous-v1')).rename(root/name)
            raise
        (root/'database.previous-v1/app.lock').unlink(missing_ok=True)
        stage.rmdir()
        for name in moved:shutil.rmtree(root/(name+'.previous-v1'))
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
        return report
    except BaseException:
        print('Preparazione interrotta; gli originali sono conservati.',flush=True);raise


def benchmark(root,output):
    import numpy as np
    import statistics
    stop=threading.Event();inputs=[]
    manifest=json.loads((root/'temi/manifest.json').read_text())
    # Stratified across years, genuine Italian passages of production length.
    for record in manifest[::max(1,len(manifest)//12)][:12]:
        text=Path(record['source']).read_text();start=min(len(text)//3,5000)
        inputs.append('title: Dibattito parlamentare | text: '+text[start:start+2200])
    rows=[];baseline=None
    for backend,workers,batch in [('cpu',1,4),('cpu',2,4),('vulkan',1,4),('vulkan',2,4)]:
        engine=EnginePool(output/f'{backend}-{workers}',backend,workers,batch,text_only=True)
        try:
            engine.start(stop,lambda s:print(s,flush=True))
            def run():return np.stack([v for i in range(0,len(inputs),engine.batch_size) for v in engine.encode_many(inputs[i:i+engine.batch_size],stop)])
            warm=run()
            if baseline is None:baseline=warm
            cosine=float(np.min(np.sum(baseline*warm,axis=1)))
            if cosine<.999:raise ValueError('Vettori batch non equivalenti')
            elapsed=[]
            for _ in range(3):
                started=time.monotonic();run();elapsed.append(time.monotonic()-started)
            seconds=statistics.median(elapsed)
            row=dict(backend=backend,workers=workers,batch_size=batch,seconds=seconds,
                embeddings_per_second=len(inputs)/seconds,samples=elapsed,min_cosine=cosine,resources=engine.resources())
            rows.append(row);print(json.dumps(row),flush=True)
        except Exception as e:rows.append(dict(backend=backend,workers=workers,batch_size=batch,error=str(e)))
        finally:engine.close()
    good=[r for r in rows if 'seconds' in r]
    if not good:raise RuntimeError('Nessuna configurazione valida')
    best=min(good,key=lambda r:r['seconds']);result=dict(rows=rows,selected={k:best[k] for k in ('backend','workers','batch_size')},inputs=len(inputs))
    atomic_json(output/'benchmark.json',result)
    return result


def index(root,config):
    archive=Archive(root/'database');stop=threading.Event();last=[0.];exported=[time.monotonic(),None]
    quality_log=(root/'temi/qualita-progressiva.log').open('a')
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    engine=EnginePool(archive.data,config['backend'],config['workers'],config['batch_size'],text_only=True)
    def notify(value):
        if time.monotonic()-last[0]>10:
            status=dict(value) if isinstance(value,dict) else dict(stage=str(value))
            status.update(updated_at=dt.datetime.now().astimezone().isoformat(),pid=os.getpid(),config=config)
            atomic_json(root/'temi/avanzamento.json',status);print(json.dumps(status,ensure_ascii=False),flush=True);last[0]=time.monotonic()
            if time.monotonic()-exported[0]>180 and (exported[1] is None or exported[1].poll() is not None):
                exported[:]=[time.monotonic(),subprocess.Popen([sys.executable,'-s',str(Path(__file__).with_name('camera_quality.py')),'--directory',str(root)],stdout=quality_log,stderr=quality_log)]
    try:
        report=archive.index(engine,stop,notify)
        atomic_json(root/'temi/avanzamento.json',dict(status='completed' if not report['errors'] else 'error',completed=report['completed'],errors=report['errors'],updated_at=dt.datetime.now().astimezone().isoformat()))
        if report['errors']:raise RuntimeError(f"{report['errors']} documenti non completati")
    except Cancelled:
        atomic_json(root/'temi/avanzamento.json',dict(status='paused',updated_at=dt.datetime.now().astimezone().isoformat()))
    finally:
        engine.close()
        if exported[1] is not None:exported[1].wait()
        quality_log.close()
    return not stop.is_set()


def discover_topics(root):
    from topic_discovery import discover
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    return discover(root,stop=stop,notify=lambda message:print(message,flush=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=ROOT);parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--rebuild',action='store_true');parser.add_argument('--workers',type=int,default=min(12,os.cpu_count() or 1))
    parser.add_argument('--benchmark',action='store_true');parser.add_argument('--index',action='store_true');parser.add_argument('--export',action='store_true')
    parser.add_argument('--discover','--reclassify',dest='discover',action='store_true',help='Scopri i temi dai vettori salvati, senza categorie predefinite')
    parser.add_argument('--backend',choices=['cpu','vulkan'],default='vulkan');args=parser.parse_args();root=args.directory.resolve()
    # A stable lock outside swappable derived directories serializes rebuilds.
    import fcntl
    with (root/'.pipeline.lock').open('w') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        from PySide6.QtCore import QLockFile
        app_lock=QLockFile(str(root/'database/app.lock'));app_lock.setStaleLockTime(0)
        if not app_lock.tryLock(0):raise RuntimeError('La Camera è aperta o è già in elaborazione')
        try:
            if args.prepare:
                prepare(root,args.workers,args.rebuild)
            output=root/'temi/prestazioni';output.mkdir(exist_ok=True)
            config=dict(backend=args.backend,workers=1,batch_size=4)
            prefs=root/'database/preferenze.json'
            if prefs.exists():config.update(json.loads(prefs.read_text()).get(args.backend,{}))
            if args.benchmark:config=benchmark(root,output)['selected']
            elif (output/'benchmark.json').exists():config=json.loads((output/'benchmark.json').read_text())['selected']
            completed=index(root,config) if args.index else True
            if completed and (args.index or args.discover):discover_topics(root)
            if args.export:
                from camera_quality import export
                export(root)
        finally:app_lock.unlock()


if __name__=='__main__':main()
