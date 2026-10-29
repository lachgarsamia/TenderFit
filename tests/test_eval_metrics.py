import math

from tenderfit.eval.metrics import (Item, blocker_recall, extraction_recall, false_blocker_rate, kappa, macro_f1,
                                    ndcg_at_k, paired_bootstrap, precision_at_k, set_prf)

ITEMS = [
    Item("a", "mandatory", "not_met", "not_met"),
    Item("b", "mandatory", "not_met", "needs_confirmation"),
    Item("c", "mandatory", "unknown", "not_met"),        # missing evidence wrongly called a blocker
    Item("d", "mandatory", "met", "met"),
    Item("e", "scored", "unknown", "unknown"),
    Item("f", "mandatory", "met", None),                 # not extracted
]


def test_blocker_metrics():
    assert blocker_recall(ITEMS) == 0.5
    assert false_blocker_rate(ITEMS) == 1 / 3


def test_extraction_recall_counts_unextracted():
    assert extraction_recall(ITEMS) == 0.8


def test_macro_f1_perfect_and_partial():
    assert macro_f1([Item("x", "mandatory", s, s) for s in ("met", "not_met", "unknown")]) == 1.0
    assert 0 < macro_f1(ITEMS) < 1


def test_ranking_metrics():
    assert precision_at_k(["a", "b", "c", "d"], {"a", "c"}, k=4) == 0.5
    assert ndcg_at_k(["a", "b"], {"a": 1.0, "b": 1.0}) == 1.0
    assert ndcg_at_k(["b", "a"], {"a": 2.0, "b": 0.0}) < 1.0


def test_set_prf_and_kappa():
    r = set_prf({1, 2, 3}, {2, 3, 4})
    assert (r["precision"], r["recall"], r["missed"], r["extra"]) == (2 / 3, 2 / 3, [4], [1])
    assert kappa(["m", "u", "m"], ["m", "u", "m"]) == 1.0
    assert kappa(["m", "u"], ["u", "m"]) < 0


def test_paired_bootstrap_detects_a_clear_improvement():
    units = list(range(40))
    worse = lambda u: Item(str(u), "mandatory", "met", "met" if u % 2 else "unknown")  # noqa: E731
    better = lambda u: Item(str(u), "mandatory", "met", "met")  # noqa: E731
    r = paired_bootstrap(units, lambda xs: sum(i.gold == i.pred for i in xs) / len(xs), worse, better, n=500)
    assert math.isclose(r["diff"], 0.5) and r["lo"] > 0.3
