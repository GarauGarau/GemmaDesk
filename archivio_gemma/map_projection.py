"""Cached, unsupervised 2D geometry: topic labels never determine positions."""
import hashlib
import json
import os
from pathlib import Path
import numpy as np

VERSION='umap-2d-graph-v1'


def project(matrix,identities,data_path,scope,artifact,check,notify):
    import importlib.metadata
    options=dict(version=VERSION,umap=importlib.metadata.version('umap-learn'),scope=scope,seed=42,min_dist=.08,epochs=250)
    digest=hashlib.sha256(json.dumps(options,sort_keys=True).encode())
    digest.update(memoryview(np.ascontiguousarray(matrix)));digest.update(np.asarray(identities,dtype=np.int64).tobytes())
    folder=Path(data_path)/'map-cache';folder.mkdir(exist_ok=True)
    path=folder/(digest.hexdigest()+'.npz')
    if path.exists():
        with np.load(path,allow_pickle=False) as saved:coords=saved['coordinates']
        if coords.shape==(len(matrix),2) and np.isfinite(coords).all():return coords
    check();notify('Mappa: calcolo UMAP 2D, poi riutilizzato da disco…')
    if len(matrix)<3 or np.max(np.ptp(matrix,axis=0))<1e-7:
        coords=np.zeros((len(matrix),2),dtype=np.float32)
        if len(matrix)==2:coords[:,0]=[-.5,.5]
    else:
        from umap import UMAP
        graph=None;geometry=None
        # The learned model already has the unsupervised neighborhood graph.
        # Reuse it, never inject the cluster labels as supervised coordinates.
        if scope=='passages' and artifact:
            artifact=Path(artifact)
            if (artifact/'assegnazioni.npz').exists() and (artifact/'geometria.joblib').exists():
                with np.load(artifact/'assegnazioni.npz') as assignments:
                    ids=assignments['chunk_ids'][assignments['labels']!=-2]
                if np.array_equal(ids,identities):
                    import joblib
                    geometry,graph=joblib.load(artifact/'geometria.joblib')
        if graph is not None:
            from umap.umap_ import simplicial_set_embedding,find_ab_params
            a,b=find_ab_params(1.,options['min_dist'])
            coords,_=simplicial_set_embedding(geometry,graph,2,1.,a,b,1.,5,options['epochs'],
                'spectral',np.random.RandomState(42),'euclidean',{},False,{},False,parallel=False)
        else:
            extra={};neighbors=min(15,len(matrix)-1)
            if len(matrix)>=4096:
                import numba
                from umap.umap_ import nearest_neighbors
                extra['precomputed_knn']=nearest_neighbors(matrix,neighbors,'cosine',{},True,np.random.RandomState(42),
                    low_memory=True,n_jobs=min(12,numba.config.NUMBA_NUM_THREADS),verbose=False)
            coords=UMAP(n_components=2,n_neighbors=neighbors,min_dist=options['min_dist'],metric='cosine',
                n_epochs=options['epochs'],random_state=42,n_jobs=1,**extra).fit_transform(matrix)
    check()
    if not np.isfinite(coords).all():raise ValueError('Proiezione UMAP non valida; cache precedente conservata')
    # Normalize only translation/uniform scale, keeping relative distances.
    coords=np.asarray(coords,dtype=np.float32);coords-=coords.mean(axis=0)
    scale=max(float(np.ptp(coords,axis=0).max()),1e-6);coords/=scale
    temp=path.with_suffix(f'.{os.getpid()}.part')
    with temp.open('wb') as stream:np.savez_compressed(stream,coordinates=coords,identities=identities,options=json.dumps(options))
    temp.replace(path)
    return coords
