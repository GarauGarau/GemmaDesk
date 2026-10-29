"""Further residual topics must reproduce across two independent layouts."""
import numpy as np
from hdbscan import HDBSCAN


def discover_consensus(matrix,labels,documents,reduce,check=lambda:None,notify=lambda _:None):
    labels=labels.copy();documents=np.asarray(documents);reports=[];models=[]
    for stage,minimum in enumerate((25,20),2):
        indices=np.flatnonzero(labels==-1)
        if len(indices)<max(150,minimum*5):break
        notify(f'Residui: controllo di stabilità {stage}, {len(indices)} passaggi')
        partitions=[];coordinates=[]
        for seed in (42+stage*2,43+stage*2):
            coords,_=reduce(matrix[indices],10,seed);check()
            model=HDBSCAN(min_cluster_size=minimum,min_samples=4,core_dist_n_jobs=12)
            partitions.append(model.fit_predict(coords));coordinates.append(coords)
        first,second=partitions;accepted=0;topics=0;offset=int(labels.max())+1;stabilities=[]
        for group in sorted(set(first)-{-1}):
            check();a=np.flatnonzero(first==group);other=second[a];valid=other[other>=0]
            if not len(valid):continue
            best=np.bincount(valid).argmax();b=np.flatnonzero(second==best)
            intersection=np.intersect1d(a,b,assume_unique=True)
            overlap=len(intersection)/len(a);jaccard=len(intersection)/(len(a)+len(b)-len(intersection))
            if overlap<.8 or jaccard<.65 or len(intersection)<minimum:continue
            members=indices[intersection]
            if len(np.unique(documents[members]))<2:continue
            center=matrix[members].mean(axis=0);center/=max(np.linalg.norm(center),1e-12)
            cohesion=float(np.median(matrix[members]@center))
            if cohesion<.65:continue
            labels[members]=offset+topics;topics+=1;accepted+=len(members)
            stabilities.append(dict(topic=offset+topics-1,overlap=overlap,jaccard=jaccard,semantic_cohesion=cohesion,passages=len(members)))
        reports.append(dict(stage=stage,input=len(indices),minimum_support=minimum,topics=topics,recovered=accepted,stability=stabilities))
        models.append(dict(chunk_indices=indices,partitions=partitions,coordinates=coordinates))
        if not accepted:break
    return labels,reports,models
