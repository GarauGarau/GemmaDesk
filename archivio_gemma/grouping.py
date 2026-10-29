"""Deterministic nested topic partitions in the original embedding space.

Every extra group splits one existing leaf. Unchanged leaves keep their IDs;
the larger child inherits its parent's ID. No final reassignment across leaves
is allowed, since that would destroy continuity between successive cuts.
"""
import numpy as np
from sklearn.cluster import KMeans, MiniBatchKMeans
from sklearn.metrics import silhouette_score

MAX_GROUPS = 24
SAMPLE_SIZE = 1024


def hierarchical_groups(matrix, clusters=0, check=lambda: None, notify=lambda message: None, return_context=False, min_separation=.10):
    if not 0 <= clusters <= MAX_GROUPS:
        raise ValueError('Numero di gruppi non valido.')
    n = len(matrix)
    if not n:
        result = (np.empty(0, dtype=int), None, 0)
        return (*result, {}) if return_context else result
    labels = np.zeros(n, dtype=int)
    sample_indices = np.sort(np.random.default_rng(42).choice(n, min(n, SAMPLE_SIZE), replace=False))
    sample = matrix[sample_indices]
    distances = np.clip(1 - sample @ sample.T, 0, 2)
    np.fill_diagonal(distances, 0)

    def score(current):
        sampled = current[sample_indices]
        count = len(np.unique(sampled))
        return float(silhouette_score(distances, sampled, metric='precomputed')) if 1 < count < len(sample) else None

    def propose(indices):
        check()
        values = matrix[indices]
        if len(indices) < 2 or np.max(np.ptp(values, axis=0)) < 1e-7:
            return None
        # Full K-means avoids minibatch instability on ordinary desktop libraries.
        estimator = (KMeans(n_clusters=2, n_init=10, random_state=42)
                     if len(indices) <= 4096 else
                     MiniBatchKMeans(n_clusters=2, n_init=10, random_state=42, batch_size=1024))
        assigned = estimator.fit_predict(values)
        check()
        if len(np.unique(assigned)) != 2:
            return None
        # Refine the binary split with cosine similarity and unit-length centers.
        for _ in range(20):
            check()
            centers = np.stack([values[assigned == side].mean(axis=0) for side in (0, 1)])
            centers /= np.maximum(np.linalg.norm(centers, axis=1, keepdims=True), 1e-12)
            refined = (values @ centers.T).argmax(axis=1)
            if len(np.unique(refined)) != 2 or np.array_equal(refined, assigned):
                break
            assigned = refined
        children = [indices[assigned == side] for side in (0, 1)]
        children.sort(key=lambda child: (-len(child), int(child.min())))
        # Reduction in cosine dispersion determines the next branch to refine.
        precise = values.astype(np.float64)
        gain = sum(np.linalg.norm(precise[assigned == side].sum(axis=0)) for side in (0, 1))
        gain -= np.linalg.norm(precise.sum(axis=0))
        return children, float(gain)

    leaves = {0: np.arange(n)}
    # Each leaf's naming reference is its sibling at creation, even if that
    # sibling is subsequently subdivided. Unchanged leaves keep the same names.
    contexts = {0: np.empty(0, dtype=int)}
    candidates = {}
    best_labels, best_score = labels.copy(), min_separation
    best_contexts = contexts.copy()
    target = min(n, clusters or MAX_GROUPS)
    if not clusters and n < 4:
        target = 1
    for new_id in range(1, target):
        check()
        notify(f'Mappa: suddivisione gerarchica in {new_id + 1} gruppi…')
        for ident, indices in leaves.items():
            if ident not in candidates:
                candidates[ident] = propose(indices)
        eligible = [ident for ident, proposal in candidates.items() if proposal is not None]
        if not eligible:
            break
        parent = max(eligible, key=lambda ident: (candidates[ident][1], -ident))
        (retained, added), _ = candidates.pop(parent)
        leaves[parent], leaves[new_id] = retained, added
        contexts[parent], contexts[new_id] = added, retained
        labels[added] = new_id
        if not clusters:
            value = score(labels)
            if value is not None and value > best_score + 1e-6:
                best_labels, best_score = labels.copy(), value
                best_contexts = contexts.copy()
    check()
    if not clusters:
        labels, contexts = best_labels, best_contexts
    result = (labels, score(labels), len(sample_indices))
    return (*result, contexts) if return_context else result
