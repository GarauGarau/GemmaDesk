"""Export discovered topic trajectories with explicit coverage and residual audit."""
from contextlib import closing
import csv
from collections import Counter,defaultdict
import datetime as dt
import gzip
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import numpy as np
from topic_discovery import catalog as topic_catalog,CONFIG
from camera_rebuild import atomic_json


def write_csv(path,rows,fields):
    temporary=path.with_suffix(path.suffix+'.part')
    with temporary.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,delimiter=';');writer.writeheader();writer.writerows(rows)
    temporary.replace(path)


def residual_groups(rows,output,language='italian'):
    if len(rows)<10:
        atomic_json(output/'temi-da-verificare.json',dict(passages=len(rows),groups=[]));return
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.cluster import MiniBatchKMeans
    from parliamentary_topics import STOPWORDS
    # A deterministic hash sample spans documents and years, not just the start.
    sample=sorted(rows,key=lambda r:hashlib.sha256(f"{r['slug']}:{r['page']}".encode()).digest())[:12000]
    speaker_names={token.lower() for row in rows for token in re.findall(r'[^\W\d_]{3,}',row.get('speaker',''))}
    vec=TfidfVectorizer(stop_words=sorted({w for w in STOPWORDS if w.isalpha()}|speaker_names|{'aren','essere','sono','anche','dopo','prima','solo','tutti','tutte','ogni','fino','senza','può','più','non','per','con','che','dal','del','dei','degli','della','delle','dello','alla','alle','agli','dalla','dalle','dagli','dallo','nelle','nella','negli','nello','nei','nel','una','uno','sul','sulla','sulle','sugli','allo','all','dell'}),token_pattern=r'(?u)\b[^\W\d_]{3,}\b',max_features=15000,min_df=2,max_df=.85,ngram_range=(1,2),sublinear_tf=True,dtype=np.float32)
    if language=='english':
        from topics import STOP
        vec.set_params(stop_words=sorted(STOP|{'aren'}))
    try:matrix=vec.fit_transform(r['text'] for r in sample)
    except ValueError:
        atomic_json(output/'temi-da-verificare.json',dict(passages=len(rows),groups=[],note='Lessico insufficiente per gruppi affidabili'));return
    n=min(24,max(2,int(len(sample)**.5/2)),len(sample))
    model=MiniBatchKMeans(n_clusters=n,random_state=42,n_init=3,batch_size=1024)
    labels=model.fit_predict(matrix);terms=vec.get_feature_names_out();groups=[]
    for label in sorted(set(labels)):
        indices=np.flatnonzero(labels==label);centroid=model.cluster_centers_[label]
        similarities=np.asarray(matrix[indices]@centroid).ravel();order=indices[np.argsort(-similarities)[:5]]
        groups.append(dict(group=int(label),sample_passages=len(indices),sample_words=sum(sample[i]['words'] for i in indices),
            terms=terms[np.argsort(-centroid)[:12]].tolist(),years=dict(Counter(sample[i]['session_date'][:4] for i in indices)),
            examples=[{k:sample[i][k] for k in ('slug','session_date','page','speaker','heading','suggested','text','url')} for i in order]))
    atomic_json(output/'temi-da-verificare.json',dict(passages=len(rows),sample=len(sample),groups=sorted(groups,key=lambda g:-g['sample_words']),
        note='Gruppi esplorativi dei soli passaggi incerti; non vengono convertiti automaticamente in etichette affidabili.'))


def _export(root):
    root=Path(root);output=root/'temi';output.mkdir(exist_ok=True)
    with closing(sqlite3.connect(f'file:{root}/database/archivio.sqlite3?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        TOPICS,FINGERPRINT,discovery=topic_catalog(db)
        sessions=[dict(r) for r in db.execute('SELECT s.*,d.status,d.done,d.pages FROM camera_sessions s LEFT JOIN documents d ON d.id=s.document ORDER BY session_date')]
        # One read snapshot; concurrent WAL indexing cannot mix two checkpoints.
        rows=[dict(r) for r in db.execute('''SELECT p.*,s.slug,s.session_date,s.url,c.text,d.status FROM camera_passages p
          JOIN camera_sessions s ON s.document=p.document JOIN documents d ON d.id=p.document
          JOIN chunks c ON c.document=p.document AND c.page=p.page AND c.kind='testo'
          WHERE c.page<=d.done ORDER BY s.session_date,s.slug,p.page''')]
    for r in rows:
        if not FINGERPRINT or r['version']!=FINGERPRINT:r['topic']=None
    TOPICS[-3]='In attesa di scoperta'
    manifest=json.loads((output/'manifest.json').read_text());expected={r['slug']:r for r in manifest}
    warnings=[];coverage=[]
    fields=['period','stream','topic','label','words','passages','share_indexed_words','indexed_words','expected_words',
        'word_coverage','expected_sessions','complete_sessions','session_coverage','review_words','review_share','model_run','thematic_words','excluded_words','pending_words']
    for precision,name in ((7,'mensile'),(4,'annuale')):
        buckets=defaultdict(list);catalog=defaultdict(list)
        for r in rows:buckets[(r['session_date'][:precision],r['written'])].append(r)
        for s in sessions:catalog[s['session_date'][:precision]].append(s)
        # Represent empty months explicitly, including recesses and data gaps.
        if precision==7 and catalog:
            first=min(catalog);last=max(catalog);year,month=map(int,first.split('-'))
            while f'{year:04}-{month:02}'<=last:
                catalog.setdefault(f'{year:04}-{month:02}',[])
                year,month=(year+1,1) if month==12 else (year,month+1)
        results=[]
        for period,ss in sorted(catalog.items()):
            for written,stream in ((0,'orale'),(1,'scritto')):
                rr=buckets[(period,written)];wc=Counter();pc=Counter()
                for r in rr:wc[r['topic'] if r['topic'] is not None else -3]+=r['words'];pc[r['topic'] if r['topic'] is not None else -3]+=1
                raw_total=sum(wc.values());total=raw_total-wc[-2];exp=sum(expected[s['slug']]['written_words'] if written else expected[s['slug']]['kept_words']-expected[s['slug']]['written_words'] for s in ss)
                eligible=[s for s in ss if (expected[s['slug']]['written_words'] if written else expected[s['slug']]['kept_words']-expected[s['slug']]['written_words'])>0]
                complete=sum(s['status']=='ready' for s in eligible)
                common=dict(period=period,stream=stream,indexed_words=raw_total,expected_words=exp,word_coverage=raw_total/exp if exp else None,
                    expected_sessions=len(eligible),complete_sessions=complete,session_coverage=complete/len(eligible) if eligible else None,
                    review_words=wc[-1],review_share=wc[-1]/total if total else None,model_run=FINGERPRINT,
                    thematic_words=total,excluded_words=wc[-2],pending_words=wc[-3])
                if name=='annuale':
                    coverage.append(common)
                    if total and wc[-1]/total>.15:warnings.append(f'{period} {stream}: {wc[-1]/total:.1%} delle parole da verificare')
                for topic,label in sorted(TOPICS.items()):
                    results.append(dict(common,topic=topic,label=label,words=wc[topic],passages=pc[topic],share_indexed_words=wc[topic]/total if total and topic!=-2 else None))
        write_csv(output/f'andamento-{name}.csv',results,fields)

    fields=['slug','session_date','page','start','end','words','speaker','role','heading','written','topic','suggested','secondary','similarity','margin','evidence','method','version','url']
    path=output/'passaggi-classificati.csv.gz';temp=path.with_suffix('.part')
    with gzip.open(temp,'wt',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,delimiter=';',extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    temp.replace(path)
    review=[r for r in rows if r['topic']==-1]
    residual_groups(review,output)
    # Inspection sheet includes accepted AND uncertain classifications each year.
    strata=defaultdict(list)
    for r in rows:strata[(r['session_date'][:4], 'pending' if r['topic'] is None else ('residual' if r['topic']==-1 else ('excluded' if r['topic']==-2 else 'assigned')))].append(r)
    sample=[]
    for group,rr in sorted(strata.items()):
        sample.extend(sorted(rr,key=lambda r:hashlib.sha256(f"{r['slug']}:{r['page']}".encode()).digest())[:20])
    sample_fields=fields+['text','human_topic','human_useful','notes']
    sample_rows=[dict({k:r.get(k,'') for k in sample_fields},human_topic='',human_useful='',notes='') for r in sample]
    sample_path=output/'campione-revisione.csv'
    if sample_path.exists():
        with sample_path.open(encoding='utf-8-sig',newline='') as stream:
            annotated=[r for r in csv.DictReader(stream,delimiter=';') if any(r.get(k,'') for k in ('human_topic','human_useful','notes'))]
        key=lambda r:(r['slug'],str(r['page']),r['text'])
        by_key={key(r):r for r in sample_rows}
        for old in annotated:
            if key(old) in by_key:
                by_key[key(old)].update({k:old.get(k,'') for k in ('human_topic','human_useful','notes')})
            else:sample_rows.append({k:old.get(k,'') for k in sample_fields})
    write_csv(sample_path,sample_rows,sample_fields)
    counts=Counter(r['topic'] for r in rows)
    report=dict(generated_at=dt.datetime.now().astimezone().isoformat(),method=CONFIG,model_run=FINGERPRINT,discovery=discovery,
        labels=TOPICS,indexed_passages=len(rows),indexed_words=sum(r['words'] for r in rows),review_passages=len(review),
        review_words=sum(r['words'] for r in review),excluded_words=sum(r['words'] for r in rows if r['topic']==-2),pending_words=sum(r['words'] for r in rows if r['topic'] is None),topic_passages={str(k):v for k,v in counts.items()},coverage=coverage,warnings=warnings,
        complete_sessions=sum(s['status']=='ready' for s in sessions),total_sessions=len(sessions),
        error_sessions=[s['slug'] for s in sessions if s['status']=='error'],
        validated_accuracy=None,notes=['Accuratezza non ancora misurata con annotazioni umane indipendenti.',
        'Quote sul testo indicizzato: gli anni con copertura incompleta non sono confrontabili come serie definitive.',
        'Andamento: tema principale; denominatore = parole indicizzate meno conduzione/tabulati esclusi, inclusi incerti e passaggi in attesa. La colonna share_indexed_words usa questo denominatore tematico, esplicitato in thematic_words. Parole dei passaggi, non attribuzione parola per parola; non minuti parlati. Un passaggio può discutere più temi: nessuna accuratezza umana già dimostrata.',
        'Corpus: Assemblea della Camera, non Senato né commissioni.'])
    atomic_json(output/'qualita.json',report)
    # Obsolete supervised exports must not remain visible as current data.
    for obsolete in ['tassonomia.json','presenza-multitema-mensile.csv','presenza-multitema-annuale.csv']:
        (output/obsolete).unlink(missing_ok=True)
    print(json.dumps({k:report[k] for k in ('indexed_passages','review_passages','complete_sessions','warnings')},ensure_ascii=False),flush=True)
    return report


def export(root):
    import fcntl
    root=Path(root);(root/'temi').mkdir(exist_ok=True)
    with (root/'temi/.quality.lock').open('w') as guard:
        try:fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(skipped=True,reason='Esportazione già in corso')
        return _export(root)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);export(p.parse_args().directory)
