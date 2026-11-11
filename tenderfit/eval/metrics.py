import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

STATUSES = ("met", "not_met", "needs_confirmation", "unknown")


@dataclass(frozen=True)
class Item:
    """One gold requirement paired with one configuration's verdict (None if the requirement was not extracted)."""
    req: str
    obligation: str
    gold: str
    pred: str | None
    hard: bool = False


def safe_div(a: float, b: float) -> float:
    return a / b if b else float("nan")


def blocker_recall(items: Sequence[Item]) -> float:
    gold = [i for i in items if i.obligation == "mandatory" and i.gold == "not_met"]
    return safe_div(sum(i.pred == "not_met" for i in gold), len(gold))


def false_blocker_rate(items: Sequence[Item]) -> float:
    """Missing evidence labelled NOT MET: mandatory requirements whose gold verdict is not NOT MET."""
    neg = [i for i in items if i.obligation == "mandatory" and i.gold != "not_met"]
    return safe_div(sum(i.pred == "not_met" for i in neg), len(neg))


def false_met_rate(items: Sequence[Item]) -> float:
    """The dangerous error: a mandatory requirement called MET when the correct answer is anything else."""
    neg = [i for i in items if i.obligation == "mandatory" and i.gold != "met"]
    return safe_div(sum(i.pred == "met" for i in neg), len(neg))


def per_class(items: Sequence[Item]) -> dict[str, dict[str, float]]:
    out = {}
    for s in STATUSES:
        tp = sum(i.gold == s and i.pred == s for i in items)
        fp = sum(i.gold != s and i.pred == s for i in items)
        fn = sum(i.gold == s and i.pred != s for i in items)
        if tp + fp + fn:
            out[s] = {"precision": safe_div(tp, tp + fp), "recall": safe_div(tp, tp + fn), "support": tp + fn}
    return out


def macro_f1(items: Sequence[Item]) -> float:
    f1s = []
    for s in STATUSES:
        tp = sum(i.gold == s and i.pred == s for i in items)
        fp = sum(i.gold != s and i.pred == s for i in items)
        fn = sum(i.gold == s and i.pred != s for i in items)
        if tp + fp + fn == 0:
            continue
        p, r = safe_div(tp, tp + fp), safe_div(tp, tp + fn)
        f1s.append(0.0 if not (p + r) or math.isnan(p) or math.isnan(r) else 2 * p * r / (p + r))
    return safe_div(sum(f1s), len(f1s))


def extraction_recall(items: Sequence[Item], obligation: str = "mandatory") -> float:
    gold = [i for i in items if i.obligation == obligation]
    return safe_div(sum(i.pred is not None for i in gold), len(gold))


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int = 10) -> float:
    top = list(ranked)[:k]
    return safe_div(sum(r in relevant for r in top), len(top))


def recall_at_k(ranked: Sequence, relevant: set, k: int = 5) -> float:
    return safe_div(len(set(list(ranked)[:k]) & relevant), len(relevant))


def ndcg_at_k(ranked: Sequence[str], gains: dict[str, float], k: int = 10) -> float:
    dcg = sum(gains.get(r, 0.0) / math.log2(i + 2) for i, r in enumerate(list(ranked)[:k]))
    ideal = sorted(gains.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return safe_div(dcg, idcg)


def set_prf(pred: set, gold: set) -> dict:
    tp = len(pred & gold)
    return {"precision": safe_div(tp, len(pred)), "recall": safe_div(tp, len(gold)), "tp": tp,
            "missed": sorted(gold - pred), "extra": sorted(pred - gold)}


def kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Cohen's kappa between two annotators."""
    n = len(a)
    if not n:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    cats = set(a) | set(b)
    pe = sum((list(a).count(c) / n) * (list(b).count(c) / n) for c in cats)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def paired_bootstrap(units: Sequence, metric: Callable[[Sequence], float], a: Callable, b: Callable,
                     n: int = 2000, seed: int = 0, cluster: Callable | None = None) -> dict:
    """CI for metric(b) - metric(a), resampling the same units for both configurations (Efron & Tibshirani).

    Requirements of one notice are not independent. Pass cluster to resample whole notices, or the interval is too narrow."""
    rng = random.Random(seed)
    def diff(sample):
        return metric([b(u) for u in sample]) - metric([a(u) for u in sample])
    groups: dict = {}
    for u in units:
        groups.setdefault(cluster(u) if cluster else id(u), []).append(u)
    keys = list(groups)
    point = diff(units)
    stats = []
    for _ in range(n):
        s = [u for _ in keys for u in groups[keys[rng.randrange(len(keys))]]]
        d = diff(s)
        if not math.isnan(d):
            stats.append(d)
    stats.sort()
    if not stats:
        return {"diff": point, "lo": float("nan"), "hi": float("nan"), "n": 0}
    lo, hi = stats[int(0.025 * len(stats))], stats[min(len(stats) - 1, int(0.975 * len(stats)))]
    return {"diff": point, "lo": lo, "hi": hi, "n": len(stats)}
