"""Embedding similarity within category buckets.

We embed each market's text once (question + rules) with a small sentence
transformer, then within each canonical category take the cosine-similarity
matrix and keep, per Polymarket market, its top-k Kalshi matches above a
threshold. Bucketing by category keeps each matrix small and avoids embedding
the full 84M-pair cross product.
"""
from collections import defaultdict

import numpy as np
from sentence_transformers import SentenceTransformer

_model = None


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _text(m: dict) -> str:
    return f"{m['question']} {m['rules_text']}".strip()


def top_k_candidates(poly: list[dict], kalshi: list[dict],
                     k: int = 3, min_sim: float = 0.55) -> list[tuple]:
    """poly/kalshi must already carry `canonical_category`.

    Returns (poly_id, kalshi_id, category, similarity) tuples.
    """
    model = get_model()

    poly_by_cat: dict[str, list[dict]] = defaultdict(list)
    kalshi_by_cat: dict[str, list[dict]] = defaultdict(list)
    for m in poly:
        poly_by_cat[m["canonical_category"]].append(m)
    for m in kalshi:
        kalshi_by_cat[m["canonical_category"]].append(m)

    out = []
    for cat, ps in poly_by_cat.items():
        ks = kalshi_by_cat.get(cat, [])
        if not ks:
            continue
        p_emb = model.encode([_text(m) for m in ps], normalize_embeddings=True,
                             batch_size=64, show_progress_bar=False)
        k_emb = model.encode([_text(m) for m in ks], normalize_embeddings=True,
                             batch_size=64, show_progress_bar=False)
        sims = p_emb @ k_emb.T
        for i, row in enumerate(sims):
            for j in np.argsort(row)[::-1][:k]:
                if row[j] >= min_sim:
                    out.append((ps[i]["market_id"], ks[j]["market_id"],
                                cat, float(row[j])))
    return out
