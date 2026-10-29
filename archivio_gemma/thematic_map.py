"""Derived parliamentary map: immutable saved embeddings, reusable projections."""
from collections import defaultdict
import hashlib
from pathlib import Path
import numpy as np
from grouping import hierarchical_groups
from parliamentary_topics import (passage_contexts,content_features,procedural_mask,topic_names)


def project(matrix,check=lambda:None,notify=lambda message:None):
    """Project content vectors without using the group labels or moving clusters."""
    from sklearn.decomposition import PCA
    unique,inverse=np.unique(matrix,axis=0,return_inverse=True)
    check()
    if len(unique)<2 or np.max(np.ptp(unique,axis=0))<1e-7:
        return np.zeros((len(matrix),2)),'PCA',None
    if len(unique)<10:
        projection=PCA(n_components=min(2,*unique.shape),random_state=42)
        coords=projection.fit_transform(unique)
        if coords.shape[1]<2:coords=np.pad(coords,((0,0),(0,1)))
        return coords[inverse],'PCA',float(projection.explained_variance_ratio_.sum())
    from sklearn.manifold import TSNE
    notify('Mappa: disposizione per vicinanza dei contenuti…')
    coords=TSNE(n_components=2,perplexity=min(40,(len(unique)-1)/3),metric='cosine',
                init='pca',random_state=42,max_iter=750,learning_rate='auto',n_jobs=4).fit_transform(unique)
    check()
    coords-=coords.mean(axis=0)
    coords/=max(float(np.percentile(np.linalg.norm(coords,axis=1),95)),1e-9)
    return coords[inverse],'t-SNE',None


def signature(rows,text_rows,documents,data_path,scope,check):
    result=hashlib.sha256(scope.encode())
    for row in rows:
        check();result.update(str((row['id'],row['chunk_id'])).encode());result.update(row['centroid'])
    for row in text_rows:
        result.update(str(row['id']).encode());result.update(row['text'].encode())
    for doc in documents:
        source=Path(doc['source'])
        paths=[source.parent.parent/'temi'/'audit'/(source.stem+'.jsonl.gz'),Path(data_path)/doc['stored'] if doc['stored'] else source]
        for path in paths:
            result.update(str(path).encode())
            try:stat=path.stat();result.update(str((stat.st_size,stat.st_mtime_ns)).encode())
            except OSError:result.update(b'missing')
    return result.digest()


def describe(rows,text_rows,documents,data_path,matrix,scope,clusters,cache,check,notify):
    key=signature(rows,text_rows,documents,data_path,scope,check)
    entry=cache.get(scope)
    if entry is None or entry['key']!=key:
        notify('Mappa: ricostruzione degli argomenti dai resoconti verificati…')
        headings=passage_contexts(text_rows,documents,data_path,check)
        by_chunk={row['id']:i for i,row in enumerate(text_rows)}
        if scope=='passages':
            texts=[row['text'] for row in rows]
            contexts=[headings[by_chunk[row['chunk_id']]] for row in rows]
            residual=procedural_mask(texts)
        else:
            by_doc=defaultdict(list)
            for index,row in enumerate(text_rows):by_doc[row['document']].append(index)
            texts=['\n\n'.join(text_rows[i]['text'] for i in by_doc[row['id']]) for row in rows]
            contexts=['\n'.join(dict.fromkeys(headings[i] for i in by_doc[row['id']] if headings[i])) for row in rows]
            residual=np.zeros(len(rows),dtype=bool)
        features=content_features(texts,matrix,check,notify,contexts=contexts)
        coords,projection,explained=project(features,check,notify)
        entry=dict(key=key,features=features,coords=coords,projection=projection,explained=explained,
                   residual=residual,contexts=contexts,headings=headings,names={})
        check();cache[scope]=entry
    labels=np.full(len(rows),-1,dtype=int)
    labels[~entry['residual']],score,sample=hierarchical_groups(entry['features'][~entry['residual']],clusters,
        check,notify,min_separation=.03)
    label_key=labels.tobytes()
    names=entry['names'].get(label_key)
    if names is None:
        notify('Mappa: nomi dei temi dai contenuti ricorrenti del dibattito…')
        assignments={row['chunk_id'] if scope=='passages' else row['id']:int(label) for row,label in zip(rows,labels)}
        naming_labels=[assignments.get(row['id'] if scope=='passages' else row['document'],-1) for row in text_rows]
        names=topic_names([row['text'] for row in text_rows],naming_labels,check,entry['headings'])
        # Only a few recent cuts are retained; vectors and coordinates are shared.
        if len(entry['names'])>=4:entry['names'].pop(next(iter(entry['names'])))
        check();entry['names'][label_key]=names
    return dict(entry,labels=labels,topics=names,score=score,sample=sample)
