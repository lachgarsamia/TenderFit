import hashlib
import re

from tenderfit.documents import service
from tenderfit.retrieval.search import index_version, search
from tests.test_document_service import _portal, _version


class BagEmbedder:
    def encode(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 384
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 384] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


class OverlapReranker:
    def score(self, query, texts):
        q = set(re.findall(r"[a-z0-9]+", query.lower()))
        return [len(q & set(re.findall(r"[a-z0-9]+", t.lower()))) for t in texts]


def test_hybrid_search_finds_the_right_passage(db, tmp_path, monkeypatch):
    vid = _version(db)
    service.process_version(db, vid, _portal(tmp_path, monkeypatch))
    assert index_version(db, vid, BagEmbedder()) > 0
    assert index_version(db, vid, BagEmbedder()) == 0
    hits = search(db, vid, "equivalent product standard BS EN 1729-2", k=3, emb=BagEmbedder(), rr=OverlapReranker())
    assert hits[0].section == "2.3 Product standard" and hits[0].page == 5
    assert hits[0].lexical_rank == 1 and hits[0].vector_rank is not None
    hits = search(db, vid, "public liability insurance", k=1, emb=BagEmbedder(), rr=OverlapReranker())
    assert hits[0].section == "Notice: conditions of participation"
