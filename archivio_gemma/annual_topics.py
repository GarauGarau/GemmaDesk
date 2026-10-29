"""Independent yearly discovery and symmetric, auditable topic harmonization.

Calendar slices never predict through an earlier year's fitted clustering.
Only shared semantic/lexical representations are compared after local fitting.
Complete linkage prevents transitive chains from swallowing unrelated topics.
"""
from collections import Counter
from contextlib import closing
from concurrent.futures import ProcessPoolExecutor,wait,FIRST_COMPLETED
import datetime as dt
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import time
import numpy as np
from topic_discovery import (VERSION,atomic_json,database_path,read_snapshot,catalog,
    lexical_topics,measure,vectorizer,publish,semantic_recovery)

CONFIG=dict(mode='annual-aligned-v1',semantic_floor=.86,lexical_floor=.10,
    semantic_weight=.75,complete_link_distance=.30,alignment_terms=100,workers=2,
    small_semantic_floor=.68,small_lexical_floor=.04,small_distance=.40)
LOCAL_CAMERA=dict(min_cluster_floor=25,min_cluster_factor=.35,min_samples=5,
    residual_min_cluster_size=20,residual_min_samples=3,calendar_slice=True)
LOCAL_ESSAYS=dict(min_cluster_floor=5,min_cluster_factor=.35,min_samples=2,
    residual_min_cluster_size=5,residual_min_samples=2,calendar_slice=True)


def align(centers,lexical,keys,config=CONFIG):
    """Permutation-invariant families; no base year, target count or chain merge."""
    from scipy.cluster.hierarchy import linkage,fcluster
    from scipy.spatial.distance import squareform
    from sklearn.preprocessing import normalize
    order=np.array(sorted(range(len(keys)),key=lambda i:keys[i]),dtype=int)
    centers=centers[order];lexical=lexical[order].tocsr(copy=True)
    # Common register spread over thousands of low-weight features can make
    # unrelated parliamentary subjects appear lexically similar. Compare the
    # most distinctive terms, not that shared background vocabulary.
    limit=config['alignment_terms']
    for i in range(lexical.shape[0]):
        start,end=lexical.indptr[i:i+2];values=lexical.data[start:end]
        if len(values)>limit:values[np.argsort(values)[:-limit]]=0
    lexical.eliminate_zeros();lexical=normalize(lexical)
    semantic=np.clip(centers@centers.T,-1,1)
    lex=np.clip((lexical@lexical.T).toarray(),0,1)
    weight=config['semantic_weight']
    distance=weight*(1-semantic)+(1-weight)*(1-lex)
    distance[(semantic<config['semantic_floor'])|(lex<config['lexical_floor'])]=2
    distance=(distance+distance.T)/2;np.fill_diagonal(distance,0)
    if len(order)>1:
        raw=fcluster(linkage(squareform(distance,checks=False),method='complete'),
            t=config['complete_link_distance'],criterion='distance')
    else:raw=np.ones(len(order),dtype=int)
    families=sorted((np.flatnonzero(raw==g).tolist() for g in set(raw)),key=lambda members:keys[order[members[0]]])
    mapped=np.empty(len(order),dtype=np.int32);audits=[]
    for group,members in enumerate(families):
        mapped[order[members]]=group
        if len(members)>1:
            pairs=np.triu_indices(len(members),1);ix=np.ix_(members,members)
            audits.append(dict(topic=group,local_topics=len(members),
                minimum_semantic=float(semantic[ix][pairs].min()),
                minimum_lexical=float(lex[ix][pairs].min()),
                maximum_distance=float(distance[ix][pairs].max())))
    return mapped,audits


def temporal_descriptions(bow,labels,dates,groups,lexical,descriptions,terms):
    """A multi-year family must not acquire a name found only in a late year."""
    for position,group in enumerate(groups):
        members=np.flatnonzero(labels==group);years=sorted(set(dates[members])-{''})
        if len(years)<2:continue
        support=np.zeros(bow.shape[1],dtype=np.int16)
        for year in years:
            ids=members[dates[members]==year]
            support+=np.asarray(bow[ids].sum(axis=0)).ravel()>=3
        row=lexical[position];scores=row.data*np.sqrt(support[row.indices]/len(years))
        minimum=max(2,int(np.ceil(len(years)/2)))
        chosen=[];counts={}
        for j in np.argsort(-scores):
            feature=row.indices[j]
            if support[feature]<minimum:continue
            term=str(terms[feature])
            if any(set(term.split())<=set(t.split()) or set(t.split())<=set(term.split()) for t in chosen):continue
            chosen.append(term);counts[term]=int(support[feature])
            if len(chosen)==12:break
        info=descriptions[group]
        if chosen:
            info['raw_label']=info['label'];info['terms']=chosen;info['label']=' · '.join(chosen[:3])
            info['label_year_support']=counts


def small_slice(root,period):
    """Sparse years: conservative complete linkage in original vectors, no UMAP.

    At least two mutually corroborating passages are needed. Singletons remain
    uncertain, and limited documentary support is retained in the final audit.
    """
    from scipy.cluster.hierarchy import linkage,fcluster
    from scipy.spatial.distance import squareform
    from sklearn.preprocessing import normalize
    rows,matrix,_=read_snapshot(root,period=period)
    with closing(sqlite3.connect(database_path(root))) as db:
        essays=dict(db.execute('SELECT key,value FROM meta')).get('corpus_kind')=='essays'
    from topic_discovery import exclusion_reason
    reasons=['' if essays else exclusion_reason(r['text'],r['role'],r['heading']) for r in rows]
    ids=np.array([i for i,r in enumerate(reasons) if not r],dtype=int)
    labels=np.full(len(rows),-2,dtype=np.int32);labels[ids]=-1
    if len(ids)>=2:
        vec=vectorizer(min_df=1,language='english' if essays else 'italian');vec.set_params(max_df=1.)
        try:bow=normalize(vec.fit_transform(rows[i]['text'] for i in ids))
        except ValueError:bow=None
        if bow is not None:
            sem=np.clip(matrix[ids]@matrix[ids].T,-1,1);lex=(bow@bow.T).toarray()
            distance=.8*(1-sem)+.2*(1-lex)
            distance[(sem<CONFIG['small_semantic_floor'])|(lex<CONFIG['small_lexical_floor'])]=2
            distance=(distance+distance.T)/2;np.fill_diagonal(distance,0)
            raw=fcluster(linkage(squareform(distance,checks=False),method='complete'),CONFIG['small_distance'],criterion='distance')
            count=0
            for group in sorted(set(raw)):
                members=ids[raw==group]
                if len(members)>=2:labels[members]=count;count+=1
    inputs=[dict(id=r['id'],document=r['document'],page=r['page'],words=r['words'],date=r['session_date'],digest=r['embedding_digest'].hex()) for r in rows]
    signature=hashlib.sha256(Path(__file__).read_bytes()+json.dumps(inputs,sort_keys=True).encode()).hexdigest()[:16]
    folder=Path(root)/'temi/modelli'/signature;folder.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(folder/'assegnazioni.npz',chunk_ids=[r['id'] for r in rows],labels=labels,
        density_labels=labels,strength=np.zeros(len(rows)),similarity=np.zeros(len(rows)),suggested=np.full(len(rows),-1))
    atomic_json(folder/'inputs.json',inputs)
    atomic_json(folder/'esclusioni.json',[dict(chunk_id=r['id'],reason=why) for r,why in zip(rows,reasons) if why])
    atomic_json(folder/'temi.json',dict(run=signature,config=dict(method='small-slice-complete-link'),
        metrics=dict(run=signature,topics=len(set(labels)-{-1,-2}),indexed_passages=len(rows))))
    return signature


def fit_year(root,period,essays):
    # Separate process, independent numerical thread budget for each year.
    from threadpoolctl import threadpool_limits
    from topic_discovery import _discover
    import numba
    numba.set_num_threads(min(6,numba.config.NUMBA_NUM_THREADS))
    with closing(sqlite3.connect(database_path(root))) as db:
        n=db.execute("SELECT count(*) FROM chunks c JOIN camera_sessions s ON s.document=c.document WHERE c.kind='testo' AND substr(coalesce(s.session_date,''),1,4)=?",(period,)).fetchone()[0]
    with threadpool_limits(limits=2):
        if n<50:return period,small_slice(root,period)
        try:
            result=_discover(Path(root),lambda:None,lambda s:print(f'[{period or "senza data"}] {s}',flush=True),
                period=period,prepare_only=True,config_overrides=LOCAL_ESSAYS if essays else LOCAL_CAMERA)
            return period,result['run']
        except ValueError as error:
            if not str(error).startswith(('Nessun tema stabile trovato','Testo sostanziale insufficiente')):raise
            return period,small_slice(root,period)


def prepare_years(root,periods,essays,check,progress):
    # Spawn (not fork) is safe when invoked from the Qt worker thread.
    pool=ProcessPoolExecutor(max_workers=CONFIG['workers'],mp_context=multiprocessing.get_context('spawn'))
    pending={pool.submit(fit_year,str(root),year,essays):year for year in periods};runs={}
    try:
        while pending:
            check();done,_=wait(pending,timeout=.5,return_when=FIRST_COMPLETED)
            for future in done:
                period,run=future.result();pending.pop(future);runs[period]=run
                progress(f'Anni completati: {len(runs)}/{len(periods)}',completed_years=sorted(runs),total_years=len(periods))
    except BaseException:
        for future in pending:future.cancel()
        # Running workers have only private derived artifacts; terminating them
        # cannot modify the live archive. Do not delay a user's cancellation.
        for process in list(getattr(pool,'_processes',{}).values()):process.terminate()
        pool.shutdown(wait=True,cancel_futures=True);raise
    pool.shutdown(wait=True)
    return runs


def discover_annual(root,check=lambda:None,notify=print,prepare_only=False,stage_only=False):
    import joblib
    started=time.monotonic();root=Path(root);output=root/'temi';output.mkdir(exist_ok=True)
    def progress(stage,**extra):
        check();notify(stage);atomic_json(output/'scoperta-avanzamento.json',dict(stage=stage,status='running',mode=CONFIG['mode'],updated_at=dt.datetime.now().astimezone().isoformat(),**extra))
    with closing(sqlite3.connect(database_path(root))) as db:
        essays=dict(db.execute('SELECT key,value FROM meta')).get('corpus_kind')=='essays'
        previous_run=catalog(db)[1]
        periods=[r[0] for r in db.execute("SELECT DISTINCT substr(coalesce(session_date,''),1,4) FROM camera_sessions s WHERE EXISTS(SELECT 1 FROM chunks c WHERE c.document=s.document) ORDER BY 1")]
    if not periods:raise ValueError('Nessun passaggio disponibile')
    progress('Scoperta indipendente dei temi per anno',years=periods)
    runs=prepare_years(root,periods,essays,check,progress)
    atomic_json(output/'modelli-annuali.json',dict(runs=runs))
    if prepare_only:return runs
    progress('Armonizzazione simmetrica dei temi annuali: embedding e lessico')
    rows,matrix,statuses=read_snapshot(root,check)
    positions={r['id']:i for i,r in enumerate(rows)};local=np.full(len(rows),-3,dtype=np.int32)
    local_ids=np.full(len(rows),-1,dtype=np.int32);strength=np.zeros(len(rows),dtype=np.float32)
    methods=np.full(len(rows),'annual-density',dtype=object)
    reasons=['']*len(rows);keys=[];sources=[]
    for period,local_run in sorted(runs.items()):
        folder=output/'modelli'/local_run
        inputs=json.loads((folder/'inputs.json').read_text())
        values=np.load(folder/'assegnazioni.npz');indices=[]
        for item,cid in zip(inputs,values['chunk_ids']):
            i=positions.get(int(cid));r=rows[i] if i is not None else None
            expected=None if r is None else dict(id=r['id'],document=r['document'],page=r['page'],words=r['words'],date=r['session_date'],digest=r['embedding_digest'].hex())
            if expected!=item:raise ValueError('Archivio cambiato tra i modelli annuali: riprovare')
            indices.append(i)
        indices=np.asarray(indices);labels=values['labels'];local_ids[indices]=labels
        local[indices]=labels;strength[indices]=values['strength']
        local_config=json.loads((folder/'temi.json').read_text())['config']
        if local_config.get('method')=='small-slice-complete-link':methods[indices]='annual-small-support'
        else:methods[indices[(values['density_labels']==-1)&(labels>=0)]]='annual-local-recovery'
        for item in json.loads((folder/'esclusioni.json').read_text()):reasons[positions[item['chunk_id']]]=item['reason']
        for group in sorted(set(int(g) for g in labels if g>=0)):
            global_id=len(keys);keys.append((period,group));sources.append(local_run)
            local[indices[labels==group]]=global_id
    if np.any(local==-3):raise ValueError('Nuovi passaggi dopo la scoperta annuale: riprovare prima di pubblicare')
    if not keys:raise ValueError('Nessun tema annuale supportato: dati precedenti conservati')
    config=dict(CONFIG,language='english' if essays else 'italian',local=LOCAL_ESSAYS if essays else LOCAL_CAMERA)
    digest=hashlib.sha256(Path(__file__).read_bytes()+json.dumps(dict(config=config,runs=runs),sort_keys=True).encode())
    run=digest.hexdigest()[:16];folder=output/'modelli'/run;folder.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(database_path(root))) as db:
        _,active,old=catalog(db)
    if active==run:
        atomic_json(output/'scoperta-avanzamento.json',dict(status='completed',run=run,reused=True));return old
    cache_key=hashlib.sha256(json.dumps(dict(runs=runs,language=config['language'],vocabulary=35000),sort_keys=True).encode()).hexdigest()
    cache=output/'armonizzazione-cache'/f'{cache_key}.joblib';cache.parent.mkdir(exist_ok=True)
    if cache.exists():vec,bow,terms,groups,lexical,descriptions,centers=joblib.load(cache)
    else:
        vec=vectorizer(max_features=35000,language=config['language']);bow=vec.fit_transform(r['text'] for r in rows);terms=vec.get_feature_names_out()
        groups,lexical,descriptions,_=lexical_topics(bow,local,terms)
        centers=np.asarray([matrix[local==g].mean(axis=0) for g in groups],dtype=np.float32)
        centers/=np.maximum(np.linalg.norm(centers,axis=1)[:,None],1e-12)
        temporary=cache.with_suffix('.part');joblib.dump((vec,bow,terms,groups,lexical,descriptions,centers),temporary,compress=3);temporary.replace(cache)
    local_lexical=lexical
    family,audits=align(centers,lexical,keys,config)
    labels=local.copy();assigned=local>=0;labels[assigned]=family[local[assigned]]
    words=np.asarray([r['words'] for r in rows]);local_report=[]
    for g,(period,ident) in enumerate(keys):
        members=np.flatnonzero(local==g);info=descriptions[g]
        local_report.append(dict(year=period,local_topic=ident,topic=int(family[g]),local_run=sources[g],
            label=info['label'],terms=info['terms'],passages=len(members),words=int(words[members].sum()),
            documents=len({rows[i]['document'] for i in members})))
    # Residuals may agree with a family found in another year, but cannot serve
    # as training seeds or invent a local topic. Preserve that provenance.
    groups,lexical,descriptions,ctf=lexical_topics(bow,labels,terms)
    density_labels=labels.copy()
    labels,similarity,suggested,recovery,_=semantic_recovery(matrix,bow,labels,lexical,groups,check)
    methods[(density_labels==-1)&(labels>=0)]='annual-family-recovery'
    methods[labels==-1]='unassigned';methods[labels==-2]='procedural'
    groups,lexical,descriptions,ctf=lexical_topics(bow,labels,terms)
    dates=np.asarray([(r['session_date'] or '')[:4] for r in rows])
    temporal_descriptions(bow,labels,dates,groups,lexical,descriptions,terms)
    # Coherence selects columns repeatedly: CSC avoids scanning every passage
    # for every topic. Results are numerically the same as CSR.
    metrics=measure(bow.tocsc(),labels,descriptions,terms,words)
    by_year=[]
    for period in periods:
        ids=np.flatnonzero(dates==period);total=int(words[ids].sum());useful=ids[labels[ids]!=-2]
        by_year.append(dict(year=period,passages=len(ids),local_topics=sum(k[0]==period for k in keys),
            families=len(set(labels[ids])-{-1,-2}),indexed_words=total,
            unassigned_passages=int(np.sum(labels[ids]==-1)),
            unassigned_words=int(words[ids[labels[ids]==-1]].sum()),useful_words=int(words[useful].sum())))
    metrics.update(run=run,version=VERSION,mode=CONFIG['mode'],local_topics=len(keys),years=by_year,
        indexed_passages=len(rows),indexed_words=int(words.sum()),complete_sessions=statuses.get('ready',0),
        total_sessions=sum(statuses.values()),provisional=any(k!='ready' and v for k,v in statuses.items()),
        recovery=recovery,recovered_by_similarity=int(np.sum((density_labels==-1)&(labels>=0))),
        alignment_audit=audits,exclusion_reasons=dict(Counter(r for r in reasons if r)),
        seconds=time.monotonic()-started,validated_alignment_accuracy=None)
    topic_rows=[];representatives=[];centroids=[]
    for group in groups:
        check();members=np.flatnonzero(labels==group);center=matrix[members].mean(axis=0);center/=max(np.linalg.norm(center),1e-12);centroids.append(center)
        examples=[];documents=set()
        for i in members[np.argsort(-(matrix[members]@center))]:
            if rows[i]['document'] in documents:continue
            documents.add(rows[i]['document']);examples.append({k:rows[i][k] for k in ('id','slug','session_date','page','speaker','text','url')})
            if len(examples)==5:break
        info=descriptions[group];info.update(topic=group,passages=len(members),words=int(words[members].sum()),
            sessions=len({rows[i]['document'] for i in members}),years=dict(Counter(dates[members])),examples=examples,
            annual_topics=[r for r in local_report if r['topic']==group],review_flags=[])
        if info['sessions']<3:info['review_flags'].append('Sostegno limitato a pochi documenti')
        if info.get('npmi',0)<0:info['review_flags'].append('Coerenza lessicale debole')
        representatives.append(info);topic_rows.append((run,group,info['label'],json.dumps(info['terms'],ensure_ascii=False),center.astype('<f4').tobytes(),json.dumps(info,ensure_ascii=False)))
    progress('Salvataggio del modello annuale e delle corrispondenze',topics=len(groups),local_topics=len(keys))
    (folder/'annual_topics.py').write_bytes(Path(__file__).read_bytes())
    (folder/'topic_discovery.py').write_bytes(Path(__file__).with_name('topic_discovery.py').read_bytes())
    np.savez_compressed(folder/'assegnazioni.npz',chunk_ids=[r['id'] for r in rows],labels=labels,
        density_labels=density_labels,strength=strength,similarity=similarity,suggested=suggested,
        local_topic=local_ids,local_index=local,year=dates)
    joblib.dump(dict(vectorizer=vec,ctfidf=ctf,lexical=lexical,groups=groups,centroids=np.asarray(centroids),
        annual_runs=runs,local_centroids=centers,local_lexical=local_lexical,local_to_family=family),folder/'modello.joblib',compress=3)
    atomic_json(folder/'temi-annuali.json',dict(run=run,topics=local_report,alignment=audits))
    result=dict(run=run,config=config,metrics=metrics,topics=representatives)
    atomic_json(folder/'temi.json',result)
    atomic_json(folder/'esclusioni.json',[dict(chunk_id=r['id'],document=r['document'],page=r['page'],reason=why) for r,why in zip(rows,reasons) if why])
    if stage_only:
        atomic_json(output/'scoperta-avanzamento.json',dict(status='prepared',run=run,topics=len(groups),local_topics=len(keys)))
        notify(f"Preparato {run}: {len(keys)} temi annuali, {len(groups)} armonizzati; residui {metrics['residual_word_share']:.1%}")
        return metrics
    expected_metadata=[(r['id'],r['document'],r['page'],r['words'],r['role'],r['heading'],r['written'],r['session_date']) for r in rows]
    def verify_publication():
        # publish calls this while holding BEGIN IMMEDIATE. Readers are allowed
        # and other writers cannot change dates, weights, exclusions or the
        # active run between this check and the atomic commit.
        check()
        with closing(sqlite3.connect(f'file:{database_path(root)}?mode=ro',uri=True)) as db:
            if catalog(db)[1]!=previous_run:raise ValueError('Modello attivo cambiato durante il calcolo')
            actual=list(db.execute("""SELECT c.id,c.document,c.page,p.words,p.role,p.heading,p.written,s.session_date
                FROM chunks c JOIN documents d ON d.id=c.document
                JOIN camera_passages p ON p.document=c.document AND p.page=c.page
                JOIN camera_sessions s ON s.document=c.document
                WHERE c.kind='testo' AND c.page<=d.done ORDER BY c.id"""))
            if actual!=expected_metadata:raise ValueError('Date o metadati cambiati durante il calcolo: risultato non pubblicato')
    publish(root,rows,run,config,metrics,folder,topic_rows,labels,suggested,similarity,strength,reasons,methods,verify_publication,expected_run=previous_run)
    atomic_json(output/'temi-scoperti.json',result)
    atomic_json(output/'temi-annuali.json',dict(run=run,topics=local_report,alignment=audits))
    atomic_json(output/'scoperta-avanzamento.json',dict(status='completed',run=run,mode=CONFIG['mode'],topics=len(groups),seconds=time.monotonic()-started))
    notify(f"{len(keys)} temi annuali, {len(groups)} temi armonizzati; {metrics['residual_word_share']:.1%} parole da verificare")
    return metrics


def main():
    import argparse
    import threading
    import signal
    from core import check as check_stop
    parser=argparse.ArgumentParser();parser.add_argument('root',type=Path);parser.add_argument('--prepare-only',action='store_true');parser.add_argument('--stage',action='store_true');args=parser.parse_args()
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    if args.prepare_only or args.stage:
        import fcntl
        (args.root/'temi').mkdir(exist_ok=True)
        with (args.root/'temi/.discovery.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            discover_annual(args.root,lambda:check_stop(stop),lambda s:print(s,flush=True),prepare_only=args.prepare_only,stage_only=args.stage)
    else:
        from topic_discovery import discover
        result=discover(args.root,stop,lambda s:print(s,flush=True),annual=True)
        print(json.dumps({k:v for k,v in result.items() if k not in ('alignment_audit','years')},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
