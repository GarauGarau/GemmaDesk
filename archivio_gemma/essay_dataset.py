"""Audited English essays using the shared passage/model storage schema.

The camera_* SQL names are retained for compatibility, not for corpus policy:
corpus_kind selects English lexicon, cleaning, dates, and user-facing labels.
"""
from pathlib import Path
import hashlib
import json
import re
from camera_dataset import bounded_parts,initialize
from camera_preprocess import words
from core import check

VERSION='essays-no-overlap-v1'


def clean(text,title='',date=''):
    from topics import TRANSLATION_LINK
    kept=[];audit=[]
    for match in re.finditer(r'\S[\s\S]*?(?=\n\s*\n|\Z)',text):
        paragraph=match.group().strip();flat=' '.join(paragraph.split());reason=''
        if not kept and flat in {title,date}:reason='titolo o data nei metadati'
        elif TRANSLATION_LINK.fullmatch(flat):reason='collegamento a traduzione'
        elif re.fullmatch(r'(?:Thanks|Thank you) to .+ for (?:reading|reviewing|commenting on|their comments on|help with) (?:drafts? of |an earlier draft of |the draft of |this essay|this|it).*[.!]',flat,re.I):reason='ringraziamenti redazionali'
        if not reason:kept.append(paragraph)
        audit.append(dict(start=match.start(),end=match.end(),kept=not reason,reason=reason,words=words(paragraph)))
    return '\n\n'.join(kept),audit


def prepare(archive):
    """Prepare derivatives, preserving every source and its publication metadata."""
    from timeline import essay_metadata,publication_date
    folder=archive.data/'temi';(folder/'audit').mkdir(parents=True,exist_ok=True)
    (archive.data/'testi_tematici').mkdir(exist_ok=True)
    with archive.connect() as db:
        existing=dict(db.execute('SELECT key,value FROM meta'))
        if existing.get('camera_preprocessing'):return
        initialize(db)
        db.execute('''CREATE TABLE IF NOT EXISTS camera_sessions(slug TEXT PRIMARY KEY,session_date TEXT NOT NULL,
            document INTEGER REFERENCES documents(id) ON DELETE SET NULL,title TEXT NOT NULL,url TEXT NOT NULL,
            pdf TEXT NOT NULL,clean_source TEXT NOT NULL,original_source TEXT NOT NULL,kept_words INTEGER NOT NULL,removed_words INTEGER NOT NULL)''')
        documents=[dict(r) for r in db.execute('SELECT * FROM documents')]
        if existing.get('thematic_preprocessing')!=VERSION and db.execute('SELECT count(*) FROM chunks').fetchone()[0]:
            raise ValueError('La raccolta contiene embedding del vecchio metodo: ricalcolarli prima di preparare i saggi.')
        for doc in documents:
            if Path(doc['source']).suffix.lower() not in {'.txt','.md'}:
                raise ValueError('Il profilo saggi richiede testi TXT/MD; usare un archivio separato per PDF e immagini.')
            source=Path(doc['source']);raw=source.read_bytes()
            sha=hashlib.sha256(raw).hexdigest()
            if doc['sha'] and sha!=doc['sha']:raise ValueError('Originale modificato: '+str(source))
            if not doc['sha']:db.execute('UPDATE documents SET sha=? WHERE id=?',(sha,doc['id']));doc['sha']=sha
            meta=essay_metadata(doc);text,audit=clean(raw.decode('utf-8'),doc['title'] or doc['name'],meta.get('date',''))
            target=archive.data/'testi_tematici'/f'{doc["id"]}.txt'
            target.write_text(text,encoding='utf-8')
            (folder/'audit'/f'{doc["id"]}.json').write_text(json.dumps(dict(source=str(source),sha256=sha,clean_sha256=hashlib.sha256(text.encode()).hexdigest(),paragraphs=audit),ensure_ascii=False,indent=2))
            kept=words(text);removed=sum(a['words'] for a in audit if not a['kept'])
            db.execute('INSERT OR REPLACE INTO camera_sessions VALUES(?,?,?,?,?,?,?,?,?,?)',
                (str(doc['id']),publication_date(meta.get('date')),doc['id'],doc['title'] or doc['name'],doc['url'],'',str(target),str(source),kept,removed))
        db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', [('corpus_kind','essays'),('thematic_preprocessing',VERSION)])
    return len(documents)


def make_plan(doc,engine,stop,data_path):
    path=Path(data_path)/'testi_tematici'/f'{doc["id"]}.txt'
    text=path.read_text(encoding='utf-8')
    audit=json.loads((Path(data_path)/'temi/audit'/f'{doc["id"]}.json').read_text())
    if hashlib.sha256(text.encode()).hexdigest()!=audit['clean_sha256'] or doc['sha']!=audit['sha256']:
        raise ValueError('Testo o audit dei saggi modificato')
    prefix=f'title: {doc["title"][:160]} | text: '
    return [dict(text=body,prefix=prefix,start=start,end=end,words=words(body),speaker='Paul Graham',role='author',heading=doc['title'],written=0)
        for start,end,body in bounded_parts(text,lambda s:engine.tokens(s,stop),prefix,lambda:check(stop))]


def export(archive):
    import csv
    import gzip
    from collections import defaultdict
    from topic_discovery import catalog,atomic_json
    from timeline import periods
    with archive.connect() as db:
        labels,run,metrics=catalog(db)
        rows=[dict(r) for r in db.execute('''SELECT p.*,s.slug,s.session_date,s.url,c.text FROM camera_passages p JOIN camera_sessions s ON s.document=p.document
            JOIN documents d ON d.id=p.document JOIN chunks c ON c.document=p.document AND c.page=p.page AND c.kind='testo' WHERE p.page<=d.done''')]
        counts=dict(db.execute('SELECT status,count(*) FROM documents GROUP BY status'))
        sessions=[dict(r) for r in db.execute('SELECT s.*,d.status FROM camera_sessions s JOIN documents d ON d.id=s.document')]
    output=archive.data/'temi'
    labels.pop(-2,None);labels[-3]='In attesa di scoperta'
    for precision,name in ((4,'annuale'),(7,'mensile')):
        bins=defaultdict(lambda:defaultdict(int));documents=defaultdict(list)
        for s in sessions:
            if len(s['session_date'])>=precision:documents[s['session_date'][:precision]].append(s)
        if documents:
            for period in periods(min(documents),max(documents),'year' if precision==4 else 'month'):documents.setdefault(period,[])
        for r in rows:
            if len(r['session_date'])<precision:continue
            topic=r['topic'] if r['version']==run and r['topic'] is not None else -3
            bins[r['session_date'][:precision]][topic]+=r['words']
        target=output/f'andamento-{name}.csv';temp=target.with_suffix('.part')
        with temp.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream,delimiter=';');writer.writerow(['model_run','period','topic','label','words','thematic_words','share_indexed_words','expected_words','word_coverage','expected_documents','complete_documents','document_coverage','review_share'])
            for period,docs in sorted(documents.items()):
                groups=bins[period];total=sum(groups.values());expected=sum(s['kept_words'] for s in docs);complete=sum(s['status']=='ready' for s in docs)
                for topic,label in sorted(labels.items()):
                    count=groups.get(topic,0)
                    writer.writerow([run,period,topic,label,count,total,count/total if total else None,expected,total/expected if expected else None,len(docs),complete,complete/len(docs) if docs else None,groups.get(-1,0)/total if total else None])
        temp.replace(target)
    atomic_json(output/'copertura.json',dict(run=run,documents=counts,undated_passages=sum(not r['session_date'] for r in rows),metrics=metrics))
    atomic_json(output/'qualita.json',dict(model_run=run,discovery=metrics,total_documents=len(sessions),complete_documents=counts.get('ready',0),undated_documents=sum(not s['session_date'] for s in sessions),indexed_passages=len(rows),indexed_words=sum(r['words'] for r in rows),review_words=sum(r['words'] for r in rows if r['topic']==-1),validated_accuracy=None))
    atomic_json(output/'rapporto.json',dict(version=VERSION,documents=len(sessions),kept_words=sum(s['kept_words'] for s in sessions),removed_words=sum(s['removed_words'] for s in sessions),policy='Titolo/data nei metadati; traduzioni e ringraziamenti redazionali riconosciuti esclusi con audit. Note e codice conservati; nessun overlap.'))
    target=output/'passaggi-classificati.csv.gz';temp=target.with_suffix('.part')
    with gzip.open(temp,'wt',encoding='utf-8-sig',newline='') as stream:
        fields=['document','slug','session_date','page','start','end','words','topic','suggested','similarity','margin','method','version','url']
        writer=csv.DictWriter(stream,fieldnames=fields,delimiter=';',extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    temp.replace(target)
    from camera_quality import residual_groups,write_csv
    residual_groups([r for r in rows if r['topic']==-1],output,language='english')
    strata=defaultdict(list)
    for row in rows:strata[(row['session_date'][:4],row['topic']==-1)].append(row)
    sample=[];fields=['slug','session_date','page','topic','version','words','text','url','human_topic','human_useful','notes']
    for group,members in sorted(strata.items()):
        for row in sorted(members,key=lambda r:hashlib.sha256(f"{r['slug']}:{r['page']}".encode()).digest())[:12]:
            sample.append({key:row.get(key,'') for key in fields})
    path=output/'campione-revisione.csv'
    if path.exists():
        with path.open(encoding='utf-8-sig',newline='') as stream:
            previous=[r for r in csv.DictReader(stream,delimiter=';') if any(r.get(k) for k in ('human_topic','human_useful','notes'))]
        key=lambda r:(str(r['slug']),str(r['page']),r['text'])
        lookup={key(r):r for r in sample}
        for old in previous:
            if key(old) in lookup:lookup[key(old)].update({k:old.get(k,'') for k in ('human_topic','human_useful','notes')})
            else:sample.append({k:old.get(k,'') for k in fields})
    write_csv(path,sample,fields)
