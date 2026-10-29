"""Semantic activity over all dated passages, independent of top-k retrieval."""
from collections import OrderedDict
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlparse
import numpy as np
from core import check, unit, ROOT


def available(archive):
    """Whether this archive has the Camera's dated session catalogue."""
    with archive.connect() as db:
        if db.execute("SELECT 1 FROM meta WHERE key='corpus_kind' AND value='essays'").fetchone():return False
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='camera_sessions'").fetchone())


def publication_date(value):
    """Keep the precision of the publication date; never use file/download times."""
    value=str(value or '').strip()
    if re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?',value):
        try:dt.date.fromisoformat(value+('-01-01' if len(value)==4 else '-01' if len(value)==7 else ''))
        except ValueError:return ''
        return value
    months='January February March April May June July August September October November December'.split()
    match=re.fullmatch(r'([A-Za-z]+)\s+(\d{4})',value)
    if match and match[1].lower() in [m.lower() for m in months]:
        return f'{match[2]}-{[m.lower() for m in months].index(match[1].lower())+1:02}'
    if match and match[1].lower() in {'spring','summer','fall','autumn','winter'}:
        return match[2]  # an approximate season supports annual aggregation only
    return ''


def essay_metadata(document):
    from metadata import read_metadata
    metadata=read_metadata(document['source'],document.get('sha'))
    if metadata:return metadata
    source=Path(document['source'])
    candidates=[source.parent.parent/'metadati'/(source.stem+'.json')]
    url=urlparse(document.get('url') or '')
    if url.hostname in {'paulgraham.com','www.paulgraham.com'}:
        candidates.append(ROOT/'data/paul_graham/metadati'/(Path(url.path).stem+'.json'))
    for path in dict.fromkeys(candidates):
        try:metadata=json.loads(path.read_text())
        except (OSError,ValueError):continue
        if not isinstance(metadata,dict):continue
        if metadata.get('sha256') and metadata['sha256']!=document.get('sha'):continue
        if path!=candidates[0] and metadata.get('url')!=document.get('url'):continue
        return metadata
    return {}


def catalog(archive,stop=None):
    """Dates and coverage for existing documents; no inference or database writes."""
    if stop is not None:check(stop)
    camera=available(archive)
    with archive.connect() as db:
        documents=[dict(r) for r in db.execute('SELECT * FROM documents ORDER BY id')]
        if camera:
            sessions=[dict(r) for r in db.execute('''SELECT s.*,d.status,d.stored,d.done,d.pages AS total_parts
                FROM camera_sessions s LEFT JOIN documents d ON d.id=s.document
                ORDER BY s.session_date,s.slug''')]
        else:
            sessions=[]
            for doc in documents:
                if stop is not None:check(stop)
                meta=essay_metadata(doc);date=publication_date(meta.get('date'))
                if not date:continue
                sessions.append(dict(document=doc['id'],session_date=date,status=doc['status'],stored=doc['stored'],
                    done=doc['done'],total_parts=doc['pages'],title=doc['title'] or doc['name'],url=doc['url'] or meta.get('url',''),
                    clean_source=doc['source'],kept_words=meta.get('words',1),date_label=meta.get('date','')))
    dated=[]
    for session in sessions:
        date=publication_date(session['session_date'])
        if not date:continue
        session.update(session_date=date,passages=[],partial=bool(session['done']) and session['status']!='ready')
        dated.append(session)
    ids={s['document'] for s in dated}
    return dict(sessions=dated,kind='camera' if camera else 'essays',
                undated_documents=sum(doc['id'] not in ids for doc in documents),
                annual_only_documents=sum(len(s['session_date'])==4 for s in dated))


def snapshot(archive, vector, stop, query=''):
    check(stop)
    query_vector = unit(vector) if vector is not None else None
    data=catalog(archive,stop);sessions=data['sessions']
    by_id={r['document']:r for r in sessions if r['document'] is not None and r['kept_words']>0}
    with archive.connect() as db:
        audited=bool(db.execute("SELECT 1 FROM meta WHERE (key='camera_preprocessing' AND value='camera-thematic-v2') OR (key='thematic_preprocessing' AND value='essays-no-overlap-v1')").fetchone())
        data['weighted']=audited
        join=' JOIN camera_passages p ON p.document=c.document AND p.page=c.page ' if audited else ''
        fields=',p.words' if audited else ',NULL AS words'
        restriction=" AND p.written=0 AND (p.topic IS NULL OR p.topic!=-2 OR p.version!=(SELECT value FROM meta WHERE key='camera_discovery_run'))" if audited else ''
        cursor=db.execute('SELECT c.id,c.document,c.page,c.text,c.vector'+fields+' FROM chunks c JOIN documents d ON d.id=c.document'+join+" WHERE d.done>0 AND c.page<=d.done AND c.kind='testo'"+restriction+' ORDER BY c.id')
        while rows := cursor.fetchmany(1024):
            check(stop)
            rows=[r for r in rows if r['document'] in by_id]
            if not rows:continue
            if query_vector is None:
                raise ValueError('Serve il vettore della ricerca per i passaggi indicizzati.')
            if any(len(r['vector']) != 768*4 for r in rows):
                raise ValueError('Embedding archiviato non valido. Archivio conservato.')
            matrix = np.stack([np.frombuffer(r['vector'], dtype='<f4') for r in rows])
            if not np.isfinite(matrix).all():
                raise ValueError('Embedding archiviato non valido. Archivio conservato.')
            scores = matrix @ query_vector
            for r, score in zip(rows, scores):
                by_id[r['document']]['passages'].append(dict(
                    id=r['id'], part=r['page'], score=float(score), text=r['text'][:900],words=r['words']))
    check(stop)
    return dict(data,query=query,captured_at=dt.datetime.now().astimezone().isoformat(timespec='seconds'))


def periods(first, last, grouping):
    if grouping == 'year':
        return [str(y) for y in range(int(first[:4]), int(last[:4])+1)]
    if grouping != 'month':
        raise ValueError('Raggruppamento non valido')
    year, month = map(int, first[:7].split('-'))
    end_year, end_month = map(int, last[:7].split('-'))
    result = []
    while (year, month) <= (end_year, end_month):
        result.append(f'{year}-{month:02}')
        year, month = (year+1, 1) if month == 12 else (year, month+1)
    return result


def aggregate(data, threshold=.70, grouping='month'):
    if not -1 <= threshold <= 1:
        raise ValueError('Soglia coseno non valida')
    sessions = [s for s in data['sessions'] if grouping=='year' or len(s['session_date'])>=7]
    if not sessions:
        return []
    dates = [r['session_date'] for r in sessions]
    bins = OrderedDict((p, dict(period=p, all_sessions=0, expected_sessions=0,
        indexed_sessions=0, complete_sessions=0, partial_sessions=0, matched_sessions=0, indexed_passages=0, matched_passages=0,
        passage_share=None, session_share=None, coverage=None,indexed_words=0,matched_words=0,word_share=None))
        for p in periods(min(dates), max(dates), grouping))
    for session in sessions:
        key = session['session_date'][:4 if grouping == 'year' else 7]
        row = bins[key]
        row['all_sessions'] += 1
        if not session['kept_words']:
            continue
        row['expected_sessions'] += 1
        if not session['passages']:
            continue
        row['indexed_sessions'] += 1
        row['complete_sessions' if session['status']=='ready' else 'partial_sessions'] += 1
        row['indexed_passages'] += len(session['passages'])
        matches = sum(p['score'] >= threshold for p in session['passages'])
        row['matched_passages'] += matches
        row['matched_sessions'] += matches > 0
        row['indexed_words']+=sum(p.get('words') or 0 for p in session['passages'])
        row['matched_words']+=sum(p.get('words') or 0 for p in session['passages'] if p['score']>=threshold)
    for row in bins.values():
        if row['expected_sessions']:
            # Partial sessions contribute their saved passages, but cannot make
            # coverage look complete while unprocessed passages are missing.
            row['coverage'] = row['complete_sessions'] / row['expected_sessions']
        if row['indexed_passages']:
            row['passage_share'] = row['matched_passages'] / row['indexed_passages']
        if row['indexed_words']:row['word_share']=row['matched_words']/row['indexed_words']
        if row['indexed_sessions']:
            row['session_share'] = row['matched_sessions'] / row['indexed_sessions']
    return list(bins.values())


def matching_passages(data, period, threshold=.70):
    rows = []
    for session in data['sessions']:
        if not session['kept_words'] or not session['session_date'].startswith(period):
            continue
        for passage in session['passages']:
            if passage['score'] >= threshold:
                rows.append(dict(**passage, title=session['title'], date=session['session_date'],
                                 url=session['url'], source=session['clean_source'],partial=session['status']!='ready',
                                 done=session.get('done'),total_parts=session.get('total_parts')))
    return sorted(rows, key=lambda r: (-r['score'], r['date'], r['part']))


def yearly_composition(data,points,scope='documents'):
    """Count each displayed document/chunk once, in its map group and real year."""
    if scope not in {'documents','passages'}:raise ValueError('Vista mappa non valida.')
    dates={s['document']:s['session_date'] for s in data['sessions'] if s['document'] is not None}
    years=[s['session_date'][:4] for s in data['sessions']]
    bins=OrderedDict((year,dict(period=year,total=0,groups={}))
        for year in periods(min(years),max(years),'year')) if years else OrderedDict()
    weighted=scope=='passages' and any(p.get('stable_topics') for p in points)
    for row in bins.values():row.update(expected_sessions=0,complete_sessions=0,coverage=None)
    for session in data['sessions']:
        if not session.get('kept_words'):continue
        row=bins[session['session_date'][:4]];row['expected_sessions']+=1
        row['complete_sessions']+=session.get('status')=='ready'
    for row in bins.values():
        if row['expected_sessions']:row['coverage']=row['complete_sessions']/row['expected_sessions']
    groups={};seen=set();undated=0
    for point in points:
        if weighted and (point.get('written') or point['cluster']==-2):continue
        key=point.get('chunk_id') if scope=='passages' else point['id']
        if key in seen:continue
        seen.add(key)
        group=point['cluster'];groups[group]=' · '.join(point.get('topics',[])) or f'Gruppo {group+1}'
        date=dates.get(point['id'])
        if not date:undated+=1;continue
        weight=point.get('topic_words',0) if weighted else 1
        row=bins[date[:4]];row['total']+=weight;row['groups'][group]=row['groups'].get(group,0)+weight
    return dict(rows=list(bins.values()),groups=groups,scope=scope,weighted=weighted,undated_points=undated,
                undated_documents=data.get('undated_documents',0),
                pending_documents=sum(s['kept_words']>0 and s['status']!='ready' for s in data['sessions']))
