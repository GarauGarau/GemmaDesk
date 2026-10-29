"""Corpus-driven parliamentary topics: BERTopic components, no seed taxonomy.

All dated passages share one fitted model. Word counts never depend on the
training sample, and density/semantic scores are not advertised as accuracy.
"""
from collections import Counter, defaultdict
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import numpy as np

VERSION = 'camera-discovery-v1'
UNASSIGNED = -1
PROCEDURAL = -2
SPECIAL = {-1: 'Tema da verificare', -2: 'Conduzione e tabulati'}
CONFIG = dict(method='BERTopic', version=VERSION, seed=42, neighbors=15,
              dimensions=8, min_cluster_floor=45, min_cluster_factor=.75, min_samples=8,
              residual_min_cluster_size=35, residual_min_samples=4,residual_min_npmi=.05,
              max_features=35000, max_fit_passages=None,residual_acceptance='density; NPMI is diagnostic')


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.part')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(path)


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS camera_topic_runs(
      id TEXT PRIMARY KEY,created TEXT NOT NULL,config TEXT NOT NULL,
      metrics TEXT NOT NULL,artifact TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS camera_discovered_topics(
      run TEXT NOT NULL REFERENCES camera_topic_runs(id),topic INTEGER NOT NULL,
      label TEXT NOT NULL,terms TEXT NOT NULL,centroid BLOB NOT NULL,
      details TEXT NOT NULL,PRIMARY KEY(run,topic));''')


def catalog(db):
    """Read only; an old supervised taxonomy is never displayed as discovery."""
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'camera_topic_runs' not in tables:return dict(SPECIAL),None,{}
    row=db.execute("SELECT value FROM meta WHERE key='camera_discovery_run'").fetchone()
    if not row:return dict(SPECIAL),None,{}
    run=row[0];labels=dict(SPECIAL)
    for r in db.execute('SELECT topic,label FROM camera_discovered_topics WHERE run=?',(run,)):
        labels[r[0]]=r[1]
    report=db.execute('SELECT metrics FROM camera_topic_runs WHERE id=?',(run,)).fetchone()
    return labels,run,json.loads(report[0]) if report else {}


def migrate(db):
    """Discard only obsolete labels, keeping all texts, vectors and checkpoints."""
    initialize(db)
    old=db.execute("SELECT value FROM meta WHERE key='camera_topic_method'").fetchone()
    if old and old[0]==VERSION:return
    db.execute('''UPDATE camera_passages SET topic=NULL,suggested=NULL,secondary=NULL,
      similarity=NULL,margin=NULL,evidence=NULL,method=NULL,version=NULL''')
    db.execute("DELETE FROM meta WHERE key IN ('camera_topics','camera_discovery_run')")
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('camera_topic_method',VERSION))


def exclusion_reason(text, role='unknown', heading=''):
    """Exact/structural exclusion, never speaker identity or length alone."""
    from camera_preprocess import strip_procedures
    from camera_procedure_fragments import reason as fragment_reason
    fragment=fragment_reason(text,role)
    if fragment:return fragment
    value=' '.join(text.split())
    if not value:return 'testo vuoto'
    # Time allocations and roll calls survived some versions of Camera HTML.
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    timed=[line for line in lines if '|' in line and re.search(r'\b(minuti|ore|ora|minuto)\b',line,re.I)]
    rest=[line for line in lines if line not in timed]
    table_caption=r'(?:Tempo complessivo\b.*|Seguito dell.esame\s*:.*|Discussione generale\s*:.*|\|?\s*Discussione generale\s*\|.*|(?:Misto|Gruppi)\s*[:|]*|\(\*\) Al tempo sopra indicato si aggiungono .*)'
    if len(timed)>=3 and all(re.fullmatch(table_caption,line,re.I) for line in rest):
        return 'tabella di ripartizione dei tempi'
    if role in ('chair','unknown'):
        names=re.sub(r'Hanno (?:risposto|votato) (?:sì|si|no)|Si sono astenuti|Sono in missione','',value,flags=re.I)
        tokens=re.findall(r'[^\W\d_]+',names)
        particles={'di','de','del','della','delle','dei','degli','d','la','detto','in','van','von'}
        if len(tokens)>=40 and all(t[0].isupper() or t in particles for t in tokens) and not re.search(r'[.!?]',names):
            return 'elenco nominale senza discorso'
    remaining,removed=strip_procedures(value,chair=role=='chair')
    if not remaining and removed:return 'sole formule di conduzione'
    # Additional full-sentence templates: a substantive sentence prevents the
    # exclusion of the entire passage, including in a speech by the President.
    if role=='chair':
        patterns=[
          r'(?:Passiamo|Procediamo) (?:ora |quindi )?alla votazione\b.+',
          r'(?:Avverto|Ricordo) che (?:i presentatori|il presentatore|come da prassi)\b.+',
          r'(?:Avverto|Comunico) che (?:il Presidente del Senato|nella seduta di oggi)\b.+',
          r'(?:Come da prassi,? )?(?:le mozioni|gli ordini del giorno) saranno post[ei] in votazione\b.+',
          r'(?:Estraggo a sorte|La chiama avrà inizio|Per agevolare le operazioni di voto)\b.+',
          r'(?:A seguito dell.approvazione del dispositivo|Analogamente a quanto già fatto)\b.+',
          r'(?:\(?Segue (?:la chiama|il sorteggio)\)?)[.!]?',
          r'(?:Colleghi,? )?(?:nel frattempo,? )?(?:salutiamo|saluto) .+assistono ai nostri lavori.+',
          r'(?:Ministro|Ministra|Onorevole|Deputat[oa])(?:,? [A-ZÀ-Ü][^.!?]{0,60})?,? (?:la prego|prego|concluda|accetta la riformulazione|insiste per la votazione)[.!?]?',
          r'(?:(?:Sì|Si|Bene),? )?(?:Passiamo|Procediamo|Iniziamo|Riprendiamo)(?:,? (?:ora|quindi|dunque),?)? (?:all.esame|alla votazione|all.ordine del giorno|ai voti|con l.ordine del giorno|il seguito della discussione)\b.*',
          r'Pongo (?:quindi |dunque )?in votazione\b.*',
          r'Se nessuno chiede (?:di parlare|di intervenire)\b.*',
          r'La Camera (?:approva|respinge) per [\d ]+ voti di differenza[.!]?',
          r'Le Commissioni .+hanno espresso i prescritti pareri.+distribuzione[.!]?',
          r'(?:Ricordo|Avverto) che,? a norma dell.articolo .+ (?:emendamenti|articoli aggiuntivi).+',
          r'Inoltre non sono pubblicati, in quanto non ricevibili, i nuovi emendamenti.+',
          r'Avverto che fuori dalla seduta le proposte emendative .+ sono state ritirate.+',
          r'Informo l.Assemblea che, in relazione al numero di emendamenti presentati, la Presidenza applicherà.+',
          r'A tal fine, .+è stat[oa] invitat[oa] a segnalare l.emendamento da porre comunque in votazione[.!]?',
          r'(?:Ricordo che )?nella parte (?:antimeridiana|pomeridiana) della seduta si è conclusa la discussione.+',
          r'Interrompiamo a questo punto l.esame del provvedimento che riprenderà.+',
          r'(?:Sospendo la seduta|La seduta riprenderà|Riprenderemo i lavori)\b.*',
          r'(?:È così esaurito l.esame|Sono così esaurite|Sono esaurite)\b.*',
          r'(?:Le lasci pure al banco della Presidenza e le faremo avere al Ministro|Sta bene)[.!]?',
        ]
        sentences=re.split(r'(?<=[.!?])\s+(?=[A-ZÀ-Ü(])',remaining)
        if sentences and all(any(re.fullmatch(p,s,re.I) for p in patterns) and
            not re.search(r'\b(?:ma|perché|ritengo|condanno|denuncio)\b',s,re.I) for s in sentences):
            return 'annunci tecnici senza intervento sostanziale'
    return ''


def vectorizer(min_df=3,max_features=35000,language="italian"):
    from sklearn.feature_extraction.text import CountVectorizer
    from parliamentary_topics import STOPWORDS
    # Linguistic and parliamentary register words, never lists of policy issues.
    stop=STOPWORDS|set('aren essere sono anche dopo prima solo tutti tutte ogni fino senza può più non per con che dal del dei degli della delle dello alla alle agli dalla dalle dagli dallo nelle nella negli nello nei nel una uno sul sulla sulle sugli allo all dell lei lui noi voi mia mio sue suoi loro lunedì martedì mercoledì giovedì venerdì sabato domenica ore'.split())
    if language=='english':
        from topics import STOP
        stop=STOP
    return CountVectorizer(stop_words=sorted(w for w in stop if len(w)>=3),
        token_pattern=r'(?u)\b[^\W\d_]{3,}\b',ngram_range=(1,2),min_df=min_df,
        max_df=.8,max_features=max_features,dtype=np.float32)


def reduce_embeddings(matrix,neighbors,seed,check=lambda:None):
    """Parallel neighbor search, deterministic layout; preserve the actual graph."""
    from umap import UMAP
    options={}
    if len(matrix)>=4096:
        from umap.umap_ import nearest_neighbors
        import numba
        workers=min(12,os.cpu_count() or 1,numba.config.NUMBA_NUM_THREADS)
        options['precomputed_knn']=nearest_neighbors(matrix,n_neighbors=neighbors,metric='cosine',
            metric_kwds={},angular=True,random_state=np.random.RandomState(seed),
            low_memory=True,n_jobs=workers,verbose=False)
        check()
    reducer=UMAP(n_neighbors=neighbors,n_components=CONFIG['dimensions'],min_dist=0,
        metric='cosine',random_state=seed,n_jobs=1,low_memory=True,**options)
    reduced=reducer.fit_transform(matrix);check()
    # No online transform is used: every new run discovers themes globally.
    # Keep the fitted coordinates/graph, not GB of redundant neighbor indexes.
    return reduced,reducer.graph_


def geometry_cache_path(matrix,neighbors,seed,output):
    import importlib.metadata
    digest=hashlib.sha256(json.dumps(dict(neighbors=neighbors,seed=seed,dimensions=CONFIG['dimensions'],
        metric='cosine',umap=importlib.metadata.version('umap-learn')),sort_keys=True).encode())
    digest.update(memoryview(np.ascontiguousarray(matrix)))
    folder=Path(output)/'geometria-cache';folder.mkdir(exist_ok=True)
    return folder/(digest.hexdigest()+'.joblib')


def cached_reduction(matrix,neighbors,seed,output,check=lambda:None):
    import joblib
    path=geometry_cache_path(matrix,neighbors,seed,output)
    if path.exists():check();return joblib.load(path)
    result=reduce_embeddings(matrix,neighbors,seed,check)
    temp=path.with_suffix(f'.{os.getpid()}.part');joblib.dump(result,temp,compress=3);temp.replace(path)
    return result


def lexical_topics(bow,labels,terms):
    """Sparse aggregation implements c-TF-IDF without concatenating long texts."""
    from scipy import sparse
    from bertopic.vectorizers import ClassTfidfTransformer
    from sklearn.preprocessing import normalize
    groups=sorted(set(int(x) for x in labels if x>=0));positions={g:i for i,g in enumerate(groups)}
    accepted=np.flatnonzero(labels>=0)
    aggregation=sparse.csr_matrix((np.ones(len(accepted),dtype=np.float32),
        ([positions[int(labels[i])] for i in accepted],accepted)),shape=(len(groups),len(labels)))
    counts=aggregation@bow
    transformer=ClassTfidfTransformer(reduce_frequent_words=True)
    # Terms found only in outliers have zero class frequency. They must not
    # become infinite IDF weights when reusing the whole-corpus vocabulary.
    with np.errstate(divide='ignore',invalid='ignore'):transformer.fit(counts)
    idf=transformer._idf_diag.diagonal();idf[~np.isfinite(idf)]=0
    transformer._idf_diag=sparse.diags(idf,format='csr')
    weights=transformer.transform(counts).tocsr()
    descriptions={}
    for index,group in enumerate(groups):
        row=weights[index];order=np.argsort(-row.data)
        ranked=[str(terms[row.indices[j]]) for j in order[:80]]
        chosen=[]
        for term in ranked:
            # Avoid labels consisting of overlapping variants of one phrase.
            if any(set(term.split())<=set(t.split()) or set(t.split())<=set(term.split()) for t in chosen):continue
            chosen.append(term)
            if len(chosen)==12:break
        descriptions[group]=dict(terms=chosen,label=' · '.join(chosen[:3]) or f'Tema {group+1}')
    return groups,normalize(weights),descriptions,transformer


def measure(bow,labels,descriptions,terms,words):
    """Diagnostic coherence, diversity and word-weighted coverage, not accuracy."""
    lookup={str(t):i for i,t in enumerate(terms)};presence=(bow>0).astype(np.float32)
    coherence=[];all_terms=[]
    for item in descriptions.values():
        tt=item['terms'][:10];all_terms.extend(tt);ids=[lookup[t] for t in tt if t in lookup]
        if len(ids)<2:continue
        x=presence[:,ids];p=np.asarray(x.mean(axis=0)).ravel();joint=(x.T@x).toarray()/max(1,bow.shape[0]);values=[]
        for i in range(len(ids)):
            for j in range(i):
                q=float(joint[i,j]);values.append(float(np.log(q/(p[i]*p[j]))/-np.log(q)) if 0<q<1 else -1.)
        item['npmi']=float(np.mean(values));coherence.append(item['npmi'])
    total=float(words.sum());count=Counter(int(x) for x in labels if x>=0)
    return dict(topics=len(descriptions),npmi=float(np.mean(coherence)) if coherence else None,
        topic_diversity=len(set(all_terms))/len(all_terms) if all_terms else None,
        assigned_word_share=float(words[labels>=0].sum()/total) if total else None,
        residual_word_share=float(words[labels==-1].sum()/total) if total else None,
        procedural_word_share=float(words[labels==-2].sum()/total) if total else None,
        largest_topic_passage_share=max(count.values(),default=0)/max(1,len(labels)),validated_accuracy=None)


def similar_pairs(groups,centroids,lexical,descriptions):
    """Flag shared subject terms as well as similarity, not generic register."""
    cosines=np.asarray(centroids)@np.asarray(centroids).T
    lex=(lexical@lexical.T).toarray();result=[]
    for i,j in np.argwhere(np.triu((cosines>.97)&(lex>.65),k=1)):
        first,second=groups[i],groups[j]
        shared=set(descriptions[first]['terms'][:8])&set(descriptions[second]['terms'][:8])
        if len(shared)<2:continue
        result.append(dict(first=first,second=second,cosine=float(cosines[i,j]),lexical=float(lex[i,j]),shared_terms=sorted(shared)))
    return sorted(result,key=lambda r:-r['lexical'])


def semantic_recovery(matrix,bow,labels,lexical,groups,check=lambda:None):
    """Recover only when semantic and lexical evidence agree with a margin.

    Thresholds are calibrated using held-out cluster members, not CAP labels.
    This estimates agreement with density clusters, not semantic correctness.
    """
    from sklearn.preprocessing import normalize
    if len(groups)<2:return labels.copy(),np.zeros(len(labels)),np.full(len(labels),-1),{},None
    centers=[];thresholds=[];lex_thresholds=[];validation=[]
    for j,g in enumerate(groups):
        check();ids=np.flatnonzero(labels==g);train=ids[np.arange(len(ids))%5!=0];test=ids[np.arange(len(ids))%5==0]
        if not len(train):train=ids
        center=matrix[train].mean(axis=0);center/=max(np.linalg.norm(center),1e-12);centers.append(center)
        similarities=matrix[test]@center
        thresholds.append(max(.35,float(np.quantile(similarities,.1))) if len(test) else 1.)
        scores=np.asarray(normalize(bow[test])@lexical[j].T.toarray()).ravel()
        lex_thresholds.append(max(.015,float(np.quantile(scores,.1))) if len(test) else 1.)
        validation.extend((int(i),j) for i in test)
    centers=np.asarray(centers,dtype=np.float32);thresholds=np.asarray(thresholds);lex_thresholds=np.asarray(lex_thresholds)
    assigned=labels.copy();strength=np.zeros(len(labels),dtype=np.float32);suggested=np.full(len(labels),-1,dtype=np.int32)
    accepted_validation=correct_validation=0
    validation_map=dict(validation)
    for begin in range(0,len(labels),4096):
        check();end=min(begin+4096,len(labels));cos=matrix[begin:end]@centers.T
        lex=(normalize(bow[begin:end])@lexical.T).toarray()
        best=np.argmax(cos,axis=1);top=np.max(cos,axis=1);second=np.partition(cos,-2,axis=1)[:,-2]
        lexbest=np.argmax(lex,axis=1);lexscore=lex[np.arange(end-begin),best]
        ok=(top>=thresholds[best]) & (top-second>=.025) & (lexbest==best) & (lexscore>=lex_thresholds[best])
        for offset in range(end-begin):
            i=begin+offset;g=groups[int(best[offset])]
            if labels[i]==-1:
                suggested[i]=g;strength[i]=top[offset]
                if ok[offset]:assigned[i]=g
            if i in validation_map and ok[offset]:
                accepted_validation+=1;correct_validation+=int(best[offset]==validation_map[i])
    report=dict(heldout_members=len(validation),accepted=accepted_validation,
        cluster_agreement=correct_validation/accepted_validation if accepted_validation else None,
        note='Accordo con cluster su membri esclusi dai centroidi; non accuratezza su annotazioni umane.')
    return assigned,strength,suggested,report,centers


def database_path(root):
    root=Path(root)
    return root/'archivio.sqlite3' if (root/'archivio.sqlite3').exists() else root/'database/archivio.sqlite3'


def read_snapshot(root,check=lambda:None,period=None):
    path=database_path(root)
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN')
        query='''FROM chunks c JOIN documents d ON d.id=c.document
          JOIN camera_passages p ON p.document=c.document AND p.page=c.page
          JOIN camera_sessions s ON s.document=c.document WHERE c.kind='testo' AND c.page<=d.done'''
        params=()
        if period is not None:
            query+=" AND substr(coalesce(s.session_date,''),1,4)=?";params=(period,)
        count=db.execute('SELECT count(*) '+query,params).fetchone()[0]
        matrix=np.empty((count,768),dtype=np.float32);rows=[]
        for i,r in enumerate(db.execute('SELECT c.id,c.text,c.vector,p.document,p.page,p.words,p.speaker,p.role,p.heading,p.written,s.slug,s.session_date,s.url '+query+' ORDER BY c.id',params)):
            if i%512==0:check()
            item=dict(r);raw=item.pop('vector');vector=np.frombuffer(raw,dtype='<f4')
            item['embedding_digest']=hashlib.sha256(raw+item['text'].encode()).digest()
            if vector.shape!=(768,) or not np.isfinite(vector).all():raise ValueError('Embedding non valido, dati conservati')
            matrix[i]=vector;rows.append(item)
        session_counts=dict(db.execute('SELECT status,count(*) FROM documents GROUP BY status'))
    norms=np.linalg.norm(matrix,axis=1)
    if np.any(norms<1e-8):raise ValueError('Embedding vuoto, dati conservati')
    matrix/=norms[:,None]
    return rows,matrix,session_counts


def discover(root,stop=None,notify=print,annual=None):
    """Fit all saved passages, checkpoint artifacts, then publish atomically."""
    import fcntl
    root=Path(root);output=root/'temi';output.mkdir(exist_ok=True)
    from core import check as check_stop
    check=(lambda:check_stop(stop)) if stop is not None else (lambda:None)
    with (output/'.discovery.lock').open('w') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            if annual is None:
                with closing(sqlite3.connect(database_path(root))) as db:
                    annual=dict(db.execute('SELECT key,value FROM meta')).get('topic_discovery_mode')=='annual-aligned-v1'
            if annual:
                from annual_topics import discover_annual
                return discover_annual(root,check,notify)
            return _discover(root,check,notify)
        except BaseException as error:
            from core import Cancelled
            atomic_json(output/'scoperta-avanzamento.json',dict(status='paused' if isinstance(error,(Cancelled,KeyboardInterrupt)) else 'error',error=str(error),updated_at=dt.datetime.now().astimezone().isoformat()))
            raise


def _discover(root,check,notify,period=None,prepare_only=False,config_overrides=None):
    from sklearn.preprocessing import normalize
    from hdbscan import HDBSCAN
    import joblib
    started=time.monotonic();output=root/'temi'
    with closing(sqlite3.connect(database_path(root))) as db:
        essays=dict(db.execute('SELECT key,value FROM meta')).get('corpus_kind')=='essays'
    config=dict(CONFIG,language='english' if essays else 'italian')
    if essays:config.update(min_cluster_floor=20,min_cluster_factor=.5,min_samples=5,residual_min_cluster_size=12,residual_min_samples=3)
    if config_overrides:config.update(config_overrides)
    progress_file=output/(f'scoperta-{period or 'senza-data'}.json' if prepare_only else 'scoperta-avanzamento.json')
    def progress(stage,**extra):
        check();notify(stage);atomic_json(progress_file,dict(stage=stage,status='running',updated_at=dt.datetime.now().astimezone().isoformat(),**extra))
    progress('Lettura degli embedding già salvati')
    rows,matrix,statuses=read_snapshot(root,check,period)
    if len(rows)<50:raise ValueError('Servono almeno 50 passaggi indicizzati per scoprire temi affidabili')
    reasons=['' if essays else exclusion_reason(r['text'],r['role'],r['heading']) for r in rows]
    eligible=np.array([not r for r in reasons]);ids=np.flatnonzero(eligible)
    if len(ids)<50:raise ValueError('Testo sostanziale insufficiente per scoprire i temi')
    implementation_source=Path(__file__).read_bytes()
    components={name:(Path(__file__).parent/name).read_bytes() for name in ('camera_procedure_fragments.py','topic_recovery.py','topic_residuals.py')}
    implementation=hashlib.sha256(implementation_source+b''.join(components.values())).hexdigest()
    digest=hashlib.sha256(json.dumps(config,sort_keys=True).encode()+implementation.encode())
    digest.update(memoryview(matrix))
    digest.update('\n'.join(f"{r['id']}:{r['document']}:{r['page']}:{r['words']}:{r['embedding_digest'].hex()}:{why}" for r,why in zip(rows,reasons)).encode())
    signature=digest.hexdigest()
    run=signature[:16];folder=output/'modelli'/run;folder.mkdir(parents=True,exist_ok=True)
    (folder/'implementation.py').write_bytes(implementation_source)
    for name,source in components.items():(folder/name).write_bytes(source)
    with closing(sqlite3.connect(database_path(root))) as db:
        labels_old,active,old_report=catalog(db)
        if prepare_only and (folder/'temi.json').exists() and (folder/'inputs.json').exists():
            notify('Modello annuale già disponibile');return json.loads((folder/'temi.json').read_text())['metrics']
        if active==run:
            notify('Modello già aggiornato')
            atomic_json(output/'scoperta-avanzamento.json',dict(status='completed',run=run,reused=True,updated_at=dt.datetime.now().astimezone().isoformat()))
            return old_report
    progress('Lessico del corpus e controllo dei contenuti',passages=len(rows),eligible=len(ids))
    vec=vectorizer(max_features=config['max_features'],language=config['language']);bow=vec.fit_transform(r['text'] for r in rows);terms=vec.get_feature_names_out()
    # Cache the expensive geometric step by the exact embedding snapshot.
    geometry=folder/'geometria.joblib'
    if geometry.exists():
        reduced,graph=joblib.load(geometry)
    else:
        progress('UMAP: struttura semantica dell’intero corpus',passages=len(ids))
        reduced,graph=cached_reduction(matrix[ids],config['neighbors'],config['seed'],output,check)
        temp=folder/'geometria.part';joblib.dump((reduced,graph),temp,compress=3);temp.replace(geometry)
    progress('HDBSCAN: scoperta dei temi senza categorie iniziali')
    minimum=max(config['min_cluster_floor'],round(config['min_cluster_factor']*len(ids)**.5))
    clusterer=HDBSCAN(min_cluster_size=minimum,min_samples=config['min_samples'],
        cluster_selection_method='eom',core_dist_n_jobs=min(12,os.cpu_count() or 1),prediction_data=True)
    local=clusterer.fit_predict(reduced);check()
    labels=np.full(len(rows),PROCEDURAL,dtype=np.int32);labels[ids]=local
    strength=np.zeros(len(rows),dtype=np.float32);strength[ids]=clusterer.probabilities_
    methods=np.full(len(rows),'procedural',dtype=object);methods[ids]='density'
    # A second independent manifold on the residuals can reveal themes hidden
    # by the density of large topics; it is not nearest-topic forced assignment.
    residual=np.flatnonzero(labels==-1);residual_info=dict(input=len(residual),topics=0)
    residual_model=None
    if len(residual)>=max(100,config['residual_min_cluster_size']*3):
        progress('Ricerca di temi nuovi nei residui',residual_passages=len(residual))
        residual_coords,residual_graph=cached_reduction(matrix[residual],10,43,output,check)
        residual_clusterer=HDBSCAN(min_cluster_size=config['residual_min_cluster_size'],min_samples=config['residual_min_samples'],core_dist_n_jobs=min(12,os.cpu_count() or 1))
        rr=residual_clusterer.fit_predict(residual_coords);offset=int(labels.max())+1
        found=rr>=0;labels[residual[found]]=rr[found]+offset;strength[residual[found]]=residual_clusterer.probabilities_[found];methods[residual[found]]='residual-density'
        residual_info.update(topics=len(set(rr)-{-1}),recovered=int(found.sum()))
        residual_model=dict(clusterer=residual_clusterer,coordinates=residual_coords,graph=residual_graph,chunk_indices=residual)
    from topic_residuals import discover_consensus
    extra_labels,consensus_report,consensus_models=discover_consensus(matrix,labels,[r['document'] for r in rows],
        lambda values,neighbors,seed:cached_reduction(values,neighbors,seed,output,check),check,progress)
    extra_changed=(labels==-1)&(extra_labels>=0);labels=extra_labels;methods[extra_changed]='residual-consensus'
    residual_info['consensus_rounds']=consensus_report
    if not np.any(labels>=0):raise ValueError('Nessun tema stabile trovato: mantenuto il modello precedente')
    progress('Rappresentazioni c-TF-IDF e recupero prudente dei residui')
    groups,lexical,descriptions,ctf=lexical_topics(bow,labels,terms)
    # A dense residual cluster can still be lexical noise. Reject weak new
    # candidates explicitly rather than boosting coverage with incoherent topics.
    words=np.array([r['words'] for r in rows])
    measure(bow,labels,descriptions,terms,words)
    candidates=sorted(set(int(g) for g in labels[methods=='residual-density'])-{-1,-2})
    # NPMI alone rejected clear semantic subjects (e.g. postal services),
    # especially paraphrases with rare terms. Density is the discovery gate;
    # weak lexical coherence remains explicit review information, not erasure.
    rejected=[]
    residual_info['weak_lexical_coherence_topics']=sum(descriptions[g].get('npmi',-1)<config['residual_min_npmi'] for g in candidates)
    residual_info['rejected_incoherent_topics']=0
    labels[np.isin(labels,rejected)]=-1
    groups,lexical,descriptions,ctf=lexical_topics(bow,labels,terms)
    density_labels=labels.copy()
    recovered,similarity,suggested,recovery,centers=semantic_recovery(matrix,bow,labels,lexical,groups,check)
    changed=(labels==-1)&(recovered>=0);methods[changed]='semantic-lexical';labels=recovered
    progress('Recupero locale: vicini, lessico e verifica su documenti esclusi')
    from topic_recovery import recover as neighbor_recovery
    neighbor_labels,neighbor_report=neighbor_recovery(matrix[ids],bow[ids],labels[ids],density_labels[ids],graph,
        [rows[i]['document'] for i in ids],lexical,groups,check)
    neighbor_changed=(labels[ids]==-1)&(neighbor_labels>=0)
    methods[ids[neighbor_changed]]='validated-neighbors';labels[ids]=neighbor_labels
    center_index={group:i for i,group in enumerate(groups)}
    for i in ids[neighbor_changed]:
        suggested[i]=labels[i];similarity[i]=float(matrix[i]@centers[center_index[int(labels[i])]])
    methods[labels==-1]='unassigned'
    groups,lexical,descriptions,ctf=lexical_topics(bow,labels,terms)
    metrics=measure(bow,labels,descriptions,terms,words)
    metrics.update(run=run,version=VERSION,implementation_sha256=implementation,indexed_passages=len(rows),indexed_words=int(words.sum()),
        complete_sessions=statuses.get('ready',0),total_sessions=sum(statuses.values()),
        provisional=any(k!='ready' and v for k,v in statuses.items()),
        residual_discovery=residual_info,recovery=recovery,recovered_by_similarity=int(changed.sum()),
        neighborhood_recovery=neighbor_report,
        min_cluster_size_used=minimum,
        exclusion_reasons=dict(Counter(r for r in reasons if r)),seconds=time.monotonic()-started,
        scope='Saggi di Paul Graham; date di pubblicazione' if essays else 'Assemblea della Camera; interventi orali e scritti distinti negli export')
    # Topic IDs are internal to an immutable run. Refit rebuilds every period;
    # never silently compare unrelated numeric IDs from different model runs.
    representatives=[];topic_rows=[];centroids=[]
    for group in groups:
        check();members=np.flatnonzero(labels==group);center=matrix[members].mean(axis=0);center/=max(np.linalg.norm(center),1e-12);centroids.append(center)
        candidates=members[np.argsort(-(matrix[members]@center))];examples=[];documents=set()
        for i in candidates:
            if rows[i]['document'] in documents:continue
            documents.add(rows[i]['document']);examples.append({k:rows[i][k] for k in ('id','slug','session_date','page','speaker','text','url')})
            if len(examples)==5:break
        info=descriptions[group];info.update(topic=group,passages=len(members),words=int(words[members].sum()),
            sessions=len({rows[i]['document'] for i in members}),years=dict(Counter(rows[i]['session_date'][:4] for i in members)),examples=examples)
        info['review_flags']=[]
        if info['sessions']<3:info['review_flags'].append('Sostegno limitato a pochi documenti')
        if info.get('npmi',0)<0:info['review_flags'].append('Coerenza lessicale debole')
        representatives.append(info)
        topic_rows.append((run,group,info['label'],json.dumps(info['terms'],ensure_ascii=False),center.astype('<f4').tobytes(),json.dumps(info,ensure_ascii=False)))
    metrics['near_duplicate_pairs']=similar_pairs(groups,centroids,lexical,descriptions)
    metrics['similarity_audit_version']=2
    progress('Salvataggio del modello, esempi e diagnostica',topics=len(groups),**{k:metrics[k] for k in ('assigned_word_share','residual_word_share')})
    np.savez_compressed(folder/'assegnazioni.npz',chunk_ids=np.array([r['id'] for r in rows]),labels=labels,density_labels=density_labels,strength=strength,similarity=similarity,suggested=suggested)
    joblib.dump(dict(vectorizer=vec,ctfidf=ctf,lexical=lexical,groups=groups,centroids=np.asarray(centroids),clusterer=clusterer,residual_model=residual_model,consensus_models=consensus_models),folder/'modello.joblib',compress=3)
    atomic_json(folder/'temi.json',dict(run=run,config=config,metrics=metrics,topics=representatives))
    atomic_json(folder/'esclusioni.json',[dict(chunk_id=r['id'],document=r['document'],page=r['page'],reason=why) for r,why in zip(rows,reasons) if why])
    if prepare_only:
        atomic_json(folder/'inputs.json',[dict(id=r['id'],document=r['document'],page=r['page'],words=r['words'],
            date=r['session_date'],digest=r['embedding_digest'].hex()) for r in rows])
        atomic_json(progress_file,dict(status='completed',run=run,topics=len(groups)))
        return metrics
    publish(root,rows,run,config,metrics,folder,topic_rows,labels,suggested,similarity,strength,reasons,methods,check)
    metrics['seconds']=time.monotonic()-started
    atomic_json(output/'temi-scoperti.json',dict(run=run,config=config,metrics=metrics,topics=representatives))
    atomic_json(output/'scoperta-avanzamento.json',dict(status='completed',run=run,topics=len(groups),updated_at=dt.datetime.now().astimezone().isoformat(),seconds=metrics['seconds']))
    notify(f"Scoperti {len(groups)} temi; {metrics['residual_word_share']:.1%} delle parole ancora da verificare")
    return metrics


def publish(root,rows,run,config,metrics,folder,topic_rows,labels,suggested,similarity,strength,reasons,methods,check,expected_run=None):
    # Publish model and assignments in one transaction. New chunks arriving
    # after the read snapshot remain explicitly pending (NULL).
    with closing(sqlite3.connect(database_path(root),timeout=60)) as db:
        initialize(db);db.execute('BEGIN IMMEDIATE')
        try:
            check()
            if expected_run is not None and catalog(db)[1]!=expected_run:
                raise ValueError('Un altro modello è stato pubblicato durante il calcolo: riprovare')
            expected={r['id']:(r['document'],r['page'],r['embedding_digest']) for r in rows};verified=0
            for cid,document,page,text,vector in db.execute('SELECT id,document,page,text,vector FROM chunks'):
                if cid not in expected:continue
                if (document,page,hashlib.sha256(vector+text.encode()).digest())!=expected[cid]:
                    raise ValueError('Archivio cambiato durante il calcolo: risultato non pubblicato')
                verified+=1
            if verified!=len(rows):raise ValueError('Archivio cambiato durante il calcolo: risultato non pubblicato')
            db.execute('INSERT OR REPLACE INTO camera_topic_runs VALUES(?,?,?,?,?)',(run,dt.datetime.now().astimezone().isoformat(),json.dumps(config),json.dumps(metrics),str(folder)))
            db.executemany('INSERT OR REPLACE INTO camera_discovered_topics VALUES(?,?,?,?,?,?)',topic_rows)
            db.execute('''UPDATE camera_passages SET topic=NULL,suggested=NULL,secondary=NULL,similarity=NULL,margin=NULL,evidence=NULL,method=NULL,version=NULL''')
            db.executemany('''UPDATE camera_passages SET topic=?,suggested=?,secondary='[]',similarity=?,margin=?,evidence=?,method=?,version=? WHERE document=? AND page=?''',
                [(int(labels[i]),int(suggested[i]) if suggested[i]>=0 else None,float(similarity[i]),float(strength[i]),
                  json.dumps(dict(exclusion=reasons[i],density_strength=float(strength[i])),ensure_ascii=False),str(methods[i]),run,r['document'],r['page']) for i,r in enumerate(rows)])
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('camera_topic_method',VERSION))
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('camera_discovery_run',run))
            if config.get('mode')=='annual-aligned-v1':
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('topic_discovery_mode','annual-aligned-v1'))
            db.execute("DELETE FROM meta WHERE key='camera_topics'");check();db.commit()
        except BaseException:db.rollback();raise
