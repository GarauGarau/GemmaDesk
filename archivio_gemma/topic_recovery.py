"""Recover density outliers from local neighbors, with document-held-out audit.

Cluster agreement is a diagnostic, not human accuracy. Unknowns are never
training seeds and semantic/lexical disagreement still permits abstention.
"""
import numpy as np
from scipy import sparse
from sklearn.preprocessing import normalize


def recover(matrix,bow,labels,seed_labels,graph,documents,lexical,groups,check=lambda:None):
    groups=np.asarray(groups,dtype=int)
    if len(groups)<2:return labels.copy(),dict(recovered=0)
    positions={g:j for j,g in enumerate(groups)};documents=np.asarray(documents)
    validation=(documents%5==0)&(seed_labels>=0)

    def evidence(seed):
        known=np.flatnonzero(seed);onehot=sparse.csr_matrix((np.ones(len(known),dtype=np.float32),
            (known,[positions[int(seed_labels[i])] for i in known])),shape=(len(labels),len(groups)))
        scores=(graph@onehot).tocsr();total=np.asarray(scores.sum(axis=1)).ravel()
        best=np.asarray(scores.argmax(axis=1)).ravel();top=np.asarray(scores.max(axis=1).toarray()).ravel()
        binary=graph.copy();binary.data[:]=1;counts=(binary@onehot).tocsr()
        support=np.asarray(counts[np.arange(len(labels)),best]).ravel()
        centers=[];floors=[]
        for group in groups:
            check();ids=np.flatnonzero(seed&(seed_labels==group))
            if not len(ids):centers.append(np.zeros(matrix.shape[1]));floors.append(1.);continue
            center=matrix[ids].mean(axis=0);center/=max(np.linalg.norm(center),1e-12)
            centers.append(center);floors.append(max(.35,float(np.quantile(matrix[ids]@center,.01))))
        centers=np.asarray(centers);floors=np.asarray(floors)
        similarity=np.einsum('ij,ij->i',matrix,centers[best])
        confidence=np.divide(top,total,out=np.zeros_like(top),where=total>0)
        # Local agreement alone can attach an unrelated topic; corroborate with
        # actual content vocabulary, tolerating related lexical alternatives.
        content=np.zeros(len(labels),dtype=bool)
        for begin in range(0,len(labels),4096):
            check();end=min(len(labels),begin+4096)
            lex=(normalize(bow[begin:end])@lexical.T).toarray()
            score=lex[np.arange(end-begin),best[begin:end]]
            content[begin:end]=(score>=.015)&(score>=.4*lex.max(axis=1))
        eligible=(support>=3)&(similarity>=floors[best])&content
        for i in np.flatnonzero(eligible):
            neighbors=graph.indices[graph.indptr[i]:graph.indptr[i+1]]
            matching=neighbors[seed[neighbors]&(seed_labels[neighbors]==groups[best[i]])]
            if len(np.unique(documents[matching]))<2:eligible[i]=False
        return best,confidence,eligible

    best,confidence,eligible=evidence((seed_labels>=0)&~validation)
    trials=[]
    for threshold in (.5,.6,.7,.75,.8,.85,.9,.95,.99):
        accepted=validation&eligible&(confidence>=threshold);n=int(accepted.sum())
        agreement=float(np.mean(groups[best[accepted]]==seed_labels[accepted])) if n else 0.
        trials.append(dict(threshold=threshold,accepted=n,cluster_agreement=agreement))
    viable=[r for r in trials if r['accepted']>=30 and r['cluster_agreement']>=.99]
    report=dict(document_holdout_members=int(validation.sum()),trials=trials,
        note='Accordo con temi di densità su documenti esclusi dai vicini/centroidi; diagnostica interna, non accuratezza su annotazioni umane.')
    if not viable:return labels.copy(),dict(report,recovered=0,abstained='Supporto di validazione insufficiente')
    threshold=viable[0]['threshold'];best,confidence,eligible=evidence(seed_labels>=0)
    accepted=(labels==-1)&eligible&(confidence>=threshold)
    result=labels.copy();result[accepted]=groups[best[accepted]]
    return result,dict(report,recovered=int(accepted.sum()),threshold=threshold)
