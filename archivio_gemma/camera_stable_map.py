"""One corpus model supplies topic identities for every year and map view."""
from collections import Counter,defaultdict
from contextlib import closing
import hashlib
import sqlite3
import numpy as np
from topic_discovery import catalog


def describe(rows,data_path,matrix,scope,cache,check,notify):
    with closing(sqlite3.connect(data_path/'archivio.sqlite3')) as db:
        db.row_factory=sqlite3.Row
        topics,run,report=catalog(db);topics[-3]='In attesa di scoperta'
        artifact=db.execute('SELECT artifact FROM camera_topic_runs WHERE id=?',(run,)).fetchone() if run else None
        meta=[dict(r) for r in db.execute('''SELECT p.* FROM camera_passages p JOIN documents d ON d.id=p.document
            WHERE p.page<=d.done ORDER BY p.document,p.page''')]
        for r in meta:
            if not run or r['version']!=run or r['topic'] is None:r['topic']=-3
    by_part={(r['document'],r['page']):r for r in meta};by_doc=defaultdict(list)
    for r in meta:by_doc[r['document']].append(r)
    labels=[];contexts=[];weights=[];written=[]
    for row in rows:
        check()
        if scope=='passages':
            m=by_part.get((row['id'],row['page']),{})
            labels.append(m.get('topic',-3));contexts.append(m.get('heading',''));weights.append(m.get('words',0));written.append(m.get('written',0))
        else:
            members=by_doc[row['id']];counts=Counter()
            for m in members:
                if m['topic']!=-2:counts[m['topic']]+=m['words']
            labels.append(counts.most_common(1)[0][0] if counts else -1)
            contexts.append('\n'.join(dict.fromkeys(m['heading'] for m in members if m['heading'])))
            weights.append(sum(counts.values()));written.append(0)
    digest=hashlib.sha256(memoryview(matrix));digest.update(scope.encode());key=digest.digest();entry=cache.get('stable_'+scope)
    if not entry or entry['key']!=key:
        from map_projection import project
        identities=[r['chunk_id'] if scope=='passages' else r['id'] for r in rows]
        coords=project(matrix,identities,data_path,scope,artifact[0] if artifact else None,check,notify)
        entry=dict(key=key,coords=coords,explained=None);cache['stable_'+scope]=entry
    return dict(features=matrix,coords=entry['coords'],explained=entry['explained'],projection='UMAP',
        labels=np.array(labels),topics={code:[label] for code,label in topics.items()},contexts=contexts,
        score=None,sample=0,weights=weights,written=written,stable=True,run=run)
