"""Stage 2 — embedding similarity within category buckets.

Each market's text (question + rules) is embedded once with a small sentence
transformer (all-MiniLM-L6-v2). Within each canonical category we take the
cosine-similarity matrix, blank out pairs the structural filter rejects (close
times too far apart), and keep, per Polymarket market, its top-k Kalshi matches
above a threshold. Work is chunked by rows so memory stays bounded even for
large categories.
"""
from collections import defaultdict

import numpy as np
from sentence_transformers import SentenceTransformer

from filters import close_seconds, window_mask

_model = None
ROW_CHUNK = 512


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _text(m: dict) -> str:
    return f"{m['question']} {m['rules_text']}".strip()


def top_k_candidates(poly: list[dict], kalshi: list[dict], k: int = 1,
                     min_sim: float = 0.80, window_hours: float = 48) -> list[tuple]:
    """poly/kalshi must already carry `canonical_category`.

    Returns (poly_id, kalshi_id, category, similarity) tuples, at most k per
    Polymarket market, each passing the structural filter and >= min_sim.
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
        cp, ck = close_seconds(ps), close_seconds(ks)
        kk = min(k, len(ks))
        for start in range(0, len(ps), ROW_CHUNK):
            sims = p_emb[start:start + ROW_CHUNK] @ k_emb.T
            sims[~window_mask(cp[start:start + ROW_CHUNK], ck, window_hours)] = -np.inf
            top = np.argpartition(-sims, kk - 1, axis=1)[:, :kk]
            for r, cols in enumerate(top):
                for j in cols[np.argsort(-sims[r, cols])]:
                    if sims[r, j] >= min_sim:
                        out.append((ps[start + r]["market_id"], ks[j]["market_id"],
                                    cat, float(sims[r, j])))
    return out
