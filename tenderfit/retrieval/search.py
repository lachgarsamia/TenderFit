import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

import psycopg

from tenderfit.config import settings

RRF_K = 60
STOP = {"the", "and", "for", "with", "shall", "must", "will", "any", "all", "are", "per", "from", "that", "this",
        "within", "of", "to", "in", "on", "or", "a", "an", "be", "by", "is", "as", "at"}


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


def _materialise(module) -> None:
    # torch 2.15 on macOS returns NaN from matmuls on memory-mapped safetensors weights; copy them into RAM.
    import torch
    with torch.no_grad():
        for t in list(module.parameters()) + list(module.buffers()):
            t.data = t.data.clone()


class MiniLM:
    def __init__(self, name: str):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(name, device="cpu")
        _materialise(self.model)

    def encode(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, batch_size=32, normalize_embeddings=True).tolist()


class CrossEncoderReranker:
    def __init__(self, name: str):
        from sentence_transformers import CrossEncoder
        self.model = CrossEncoder(name, device="cpu")
        _materialise(self.model.model)

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self.model.predict([(query, t) for t in texts])] if texts else []


@lru_cache(maxsize=1)
def embedder() -> Embedder:
    return MiniLM(settings.embedding_model)


@lru_cache(maxsize=1)
def reranker() -> Reranker:
    return CrossEncoderReranker(settings.rerank_model)


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def index_version(conn: psycopg.Connection, version_id: int, emb: Embedder | None = None) -> int:
    rows = conn.execute(
        """SELECT p.id, coalesce(p.section, '') || E'\n' || p.text AS t FROM passage p JOIN document d ON d.id = p.document_id
           WHERE d.version_id = %s AND p.embedding IS NULL""", (version_id,)).fetchall()
    if not rows:
        return 0
    vecs = (emb or embedder()).encode([r["t"] for r in rows])
    for r, v in zip(rows, vecs):
        conn.execute("UPDATE passage SET embedding = %s::vector WHERE id = %s", (_vec(v), r["id"]))
    conn.commit()
    return len(rows)


def _tsquery(text: str) -> str:
    terms = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in STOP]
    return " | ".join(dict.fromkeys(terms))


@dataclass
class Hit:
    id: int
    text: str
    section: str | None
    page: int | None
    document: str
    document_id: int
    score: float
    lexical_rank: int | None
    vector_rank: int | None
    why: list[str] = field(default_factory=list)


def search(conn: psycopg.Connection, version_id: int, query: str, k: int = 5, pool: int = 50, rerank_top: int = 20,
           emb: Embedder | None = None, rr: Reranker | None = None, exclude: set[int] | None = None,
           hybrid: bool = True, profile_id: int | None = None, graph_links: list[dict] | None = None) -> list[Hit]:
    """Lexical and vector candidates fused by RRF, reranked, top k. Only public passages and this supplier's own are searched.

    graph_links are passages the requirement explicitly points to. They join the candidates, and always survive reranking,
    because the requirement cannot be read correctly without them. Each hit records why it was retrieved."""
    access = "(d.owner_profile_id IS NULL OR d.owner_profile_id = %(p)s)"
    args = {"v": version_id, "p": profile_id, "pool": pool, "q": _tsquery(query) or "zzzz"}
    lexical = [] if not hybrid else [r["id"] for r in conn.execute(
        f"""SELECT p.id FROM passage p JOIN document d ON d.id = p.document_id
           WHERE d.version_id = %(v)s AND {access} AND p.tsv @@ to_tsquery('english', %(q)s)
           ORDER BY ts_rank_cd(p.tsv, to_tsquery('english', %(q)s)) DESC, p.id LIMIT %(pool)s""", args)]
    qv = _vec((emb or embedder()).encode([query])[0])
    vector = [r["id"] for r in conn.execute(
        f"""SELECT p.id FROM passage p JOIN document d ON d.id = p.document_id
           WHERE d.version_id = %(v)s AND {access} AND p.embedding IS NOT NULL
           ORDER BY p.embedding <=> %(qv)s::vector, p.id LIMIT %(pool)s""", {**args, "qv": qv})]
    links = graph_links or []
    linked = list(dict.fromkeys(g["id"] for g in links))
    fused: dict[int, float] = {}
    for ranks in (lexical, vector, linked):
        for i, pid in enumerate(ranks):
            fused[pid] = fused.get(pid, 0.0) + 1.0 / (RRF_K + i + 1)
    ranked = [pid for pid, _ in sorted(fused.items(), key=lambda x: -x[1]) if pid not in (exclude or set())]
    ids = ranked[:rerank_top] + [p for p in linked if p in ranked[rerank_top:]]
    if not ids:
        return []
    rows = {r["id"]: r for r in conn.execute(
        """SELECT p.id, p.text, p.section, p.page, p.document_id, coalesce(d.filename, 'document') AS document
           FROM passage p JOIN document d ON d.id = p.document_id WHERE p.id = ANY(%s)""", (ids,))}
    scores = (rr or reranker()).score(query, [rows[i]["text"] for i in ids]) if hybrid else [fused[i] for i in ids]
    reasons: dict[int, list[str]] = {}
    for g in links:
        word = "refers to" if g["kind"] == "REFERENCES" else "uses the defined term"
        reasons.setdefault(g["id"], []).append(f"this requirement {word} {g['label']}")
    hits = []
    for i, s in zip(ids, scores):
        why = list(reasons.get(i, []))
        if i in lexical:
            why.append(f"keyword match (rank {lexical.index(i) + 1})")
        if i in vector:
            why.append(f"similar meaning (rank {vector.index(i) + 1})")
        hits.append(Hit(i, rows[i]["text"], rows[i]["section"], rows[i]["page"], rows[i]["document"], rows[i]["document_id"], s,
                        lexical.index(i) + 1 if i in lexical else None, vector.index(i) + 1 if i in vector else None, why))
    hits.sort(key=lambda h: -h.score)
    top = hits[:k]
    return top + [h for h in hits[k:] if h.id in linked]


def corrective_query(req: dict, gaps: list[str]) -> str:
    """A second attempt asks for what the first could not find, not for the same words again."""
    parts = [*gaps, *req.get("defined_terms", []), *req.get("evidence_needed", []), req["statement"]]
    return " ".join(dict.fromkeys(p for p in parts if p))
