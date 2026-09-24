"""
The critic: deterministic checks on a proposed plan before a human is asked to approve it.

It is not a second model grading the first. Every finding is a comparison against the
book or against the plan's own priced cost, using the rules in advisor/policy.py that
the evals score, so a finding can be audited and a pass means something.

A finding is `block` when the plan should not reach a human as it stands (the planner is
asked to revise it) and `warn` when it is defensible but the approver should see it.
"""
from advisor.policy import (MAX_BOOK_MULTIPLE, SCHEDULED, SINGLE_CLIP, admits_depth, cost_matches,
                            side_drift)
from models import side_depth_usd

# An alternative has to beat the advised plan by this much before the approver is told.
CHEAPER_BY_BPS = 1.0
CHEAPER_BY_REL = 0.25


def _finding(code, severity, message):
    return {"code": code, "severity": severity, "message": message}


def review(order, book, advice, priced):
    """
    Findings on `advice` for `order`, given the plans priced against `book`.

    `priced` is the price step's output for this round: one entry per plan, each with
    `strategy`, `slices`, `cost_bps`, `complete` and `advised` (True for the plan the
    advice describes).
    """
    side, notional = order["side"], float(order["notional"])
    strategy = advice["strategy"]
    findings = []

    if advice.get("order_side") != side:
        findings.append(_finding("side", "block",
                                 f"order_side is {advice.get('order_side')!r} but the order is a {side}."))
    elif side_drift(advice, side):
        findings.append(_finding("side", "block",
                                 f"The analysis describes the other side of the market; this order is a {side}."))

    advised = next((p for p in priced if p.get("advised")), None)
    one_clip = next((p for p in priced if p["strategy"] == "immediate_market"), None)
    if advised is not None and strategy != "wait":
        # The plan's own price, or the one-clip price as the conservative figure: the same
        # two the cost_grounded grader accepts.
        claimed = float(advice.get("expected_cost_bps") or 0.0)
        accepted = [p for p in (advised, one_clip) if p is not None]
        if not any(cost_matches(claimed, p["cost_bps"]) for p in accepted):
            findings.append(_finding("cost", "block",
                                     f"expected_cost_bps is {claimed:.2f}, but the plan as advised "
                                     f"({advised['label']}) prices at {advised['cost_bps']:.2f} bps"
                                     + (f" and one clip at {one_clip['cost_bps']:.2f} bps." if one_clip
                                        and one_clip is not advised else ".")))

    oversized = one_clip is not None and not one_clip["complete"]
    depth = side_depth_usd(book, side)
    if depth and notional > MAX_BOOK_MULTIPLE * depth and strategy != "wait":
        findings.append(_finding("oversize", "block",
                                 f"The order is {notional / depth:,.0f}x the visible {('asks' if side == 'buy' else 'bids')} "
                                 f"(${depth:,.0f}); any cost for it is extrapolated past the book. Only 'wait' is defensible."))
    elif oversized and strategy in SINGLE_CLIP:
        findings.append(_finding("depth", "block",
                                 f"The order is larger than the visible book, so {strategy} cannot fill it in one clip."))
    if oversized and not admits_depth(advice):
        findings.append(_finding("depth", "block",
                                 "The order runs past the visible book and the advice never says so."))

    if advised is not None and strategy != "wait":
        # A plan the book cannot fill is no rival to one it can, however cheap its floor.
        rivals = [p for p in priced if not p.get("advised") and p["strategy"] != "wait"
                  and (p["complete"] or not advised["complete"])
                  and not (oversized and p["strategy"] in SINGLE_CLIP)]
        best = min(rivals, key=lambda p: p["cost_bps"], default=None)
        margin = max(CHEAPER_BY_BPS, abs(advised["cost_bps"]) * CHEAPER_BY_REL)
        if best is not None and advised["cost_bps"] - best["cost_bps"] > margin:
            findings.append(_finding("cheaper", "warn",
                                     f"{best['label']} prices at {best['cost_bps']:.2f} bps against "
                                     f"{advised['cost_bps']:.2f} bps for the advised plan."))
    if strategy in SCHEDULED:
        findings.append(_finding("schedule", "warn",
                                 "A sliced cost assumes the book refills between slices, so it is a best case."))
    return findings


def blocking(findings):
    return [f for f in findings if f["severity"] == "block"]
