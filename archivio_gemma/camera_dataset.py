"""Audited, nonoverlapping Camera passages; topics are learned after embedding."""
import gzip
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from camera_preprocess import words, VERSION
from core import check

PLAN_VERSION='camera-turns-1024-no-overlap-v3'


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS camera_passages(
      document INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      page INTEGER NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
      words INTEGER NOT NULL,speaker TEXT NOT NULL,role TEXT NOT NULL,
      heading TEXT NOT NULL,written INTEGER NOT NULL,
      topic INTEGER,suggested INTEGER,secondary TEXT,similarity REAL,margin REAL,
      evidence TEXT,method TEXT,version TEXT,
      PRIMARY KEY(document,page));
    CREATE INDEX IF NOT EXISTS camera_topic ON camera_passages(topic,document);
    ''')
    from topic_discovery import migrate
    migrate(db)


def bounded_parts(text,count,prefix,check=lambda:None):
    """One token check per typical passage, no overlap and no lost characters.

    A character target bounds tokenizer work. The actual tokenizer, not the
    heuristic, enforces the 1024-token ceiling even for unusual Unicode input.
    """
    offset=0
    while offset<len(text):
        check();rest=text[offset:];cut=min(2600,len(rest))
        if cut<len(rest):
            for pattern in (r'\n\s*\n',r'[.!?][»”\")]*\s+',r'\s+'):
                ends=[m.end() for m in re.finditer(pattern,rest[:cut]) if m.end()>cut*.55]
                if ends:cut=ends[-1];break
        while count(prefix+rest[:cut])>1024:
            cut=int(cut*.8)
            if cut<1:raise ValueError('Prefisso o carattere oltre il contesto')
            ends=[m.end() for m in re.finditer(r'\s+',rest[:cut])]
            if ends:cut=ends[-1]
        yield offset,offset+cut,rest[:cut]
        offset+=cut


def make_plan(doc,engine,stop):
    from parliamentary_topics import substantive_heading
    source=Path(doc['source']);text=source.read_text(encoding='utf-8')
    if hashlib.sha256(source.read_bytes()).hexdigest()!=doc['sha']:
        raise ValueError('Testo tematico modificato: rigenerare il piano')
    audit=source.parent.parent/'temi/audit'/(source.stem+'.jsonl.gz')
    with gzip.open(audit,'rt',encoding='utf-8') as f:records=[json.loads(line) for line in f]
    kept=[row for row in records if row['kept']]
    # Merge only adjacent paragraphs of the same turn, agenda and written status.
    units=[]
    for row in kept:
        check(stop);body=text[row['start']:row['end']]
        if hashlib.sha256(body.encode()).hexdigest()!=row['sha256']:raise ValueError('Audit non coerente con il testo')
        heading=substantive_heading(row.get('subsection','')) or substantive_heading(row['section'])
        key=(row['turn'],heading,row['written'])
        if units and units[-1]['key']==key:units[-1]['end']=row['end']
        else:units.append(dict(key=key,start=row['start'],end=row['end'],speaker=row['speaker'],role=row['role'],heading=heading,written=row['written']))
    plan=[]
    for u in units:
        # A subject heading helps search without injecting dates, legislature or
        # the speaker's name into every embedding. It is never counted as speech.
        title=u['heading'][:160] or 'Dibattito parlamentare'
        prefix=f'title: {title} | text: '
        for start,end,body in bounded_parts(text[u['start']:u['end']],lambda x:engine.tokens(x,stop),prefix,lambda:check(stop)):
            plan.append(dict(text=body,prefix=prefix,start=u['start']+start,end=u['start']+end,
                             words=words(body),speaker=u['speaker'],role=u['role'],heading=u['heading'],written=int(u['written'])))
    if sum(p['words'] for p in plan)!=sum(words(text[u['start']:u['end']]) for u in units):
        raise ValueError('La segmentazione ha alterato il conteggio delle parole')
    return plan


def save_plan(db,doc,plan):
    old=db.execute('SELECT count(*) FROM camera_passages WHERE document=?',(doc['id'],)).fetchone()[0]
    if old and old!=len(plan):raise ValueError('Piano cambiato durante la ripresa')
    db.executemany('''INSERT OR IGNORE INTO camera_passages(document,page,start,end,words,speaker,role,heading,written)
        VALUES(?,?,?,?,?,?,?,?,?)''',[(doc['id'],i,p['start'],p['end'],p['words'],p['speaker'],p['role'],p['heading'],p['written']) for i,p in enumerate(plan,1)])


def validate_commit(db,doc,page):
    if db.execute('SELECT 1 FROM camera_passages WHERE document=? AND page=?',(doc['id'],page)).fetchone() is None:
        raise ValueError('Metadati del passaggio mancanti')
