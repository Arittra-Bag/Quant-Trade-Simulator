"""
Eval runner.

    python -m evals.runner                     # every candidate, every scenario, offline
    python -m evals.runner --candidates rules
    python -m evals.runner --markdown evals/RESULTS.md --json evals/results.json
    python -m evals.runner --live              # adds the real Gemini transport, needs a key

Offline is the default and needs no API key and no network: the replay candidates play
recorded scripts and the `rules` candidate is plain Python over the project's own cost
models. That is what CI runs. `--live` is the only path that touches the API.

Exit code is 1 when a candidate the harness treats as real regresses below its floor, so
this can gate a merge. The replay candidates are expected to fail their target graders,
so they never affect the exit code.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor.advisor import run_advisor  # noqa: E402
from evals.candidates import CANDIDATES, DESCRIPTIONS, REAL_CANDIDATES  # noqa: E402
from evals.graders import grade, ground_truth  # noqa: E402
from evals.scenarios import SCENARIOS, scenario_book  # noqa: E402

# A real candidate scoring below this is a regression worth failing CI over.
SCORE_FLOOR = 0.90

# Live runs are built for the Gemini free tier: 5 calls a minute per model.
# Seconds between live API calls, which keeps a run under that limit.
LIVE_CALL_INTERVAL = float(os.environ.get("GEMINI_CALL_INTERVAL", "13"))
# Seconds before one API call is abandoned, so an unanswered call cannot hang the run.
LIVE_CALL_TIMEOUT = float(os.environ.get("GEMINI_CALL_TIMEOUT", "60"))
# Seconds a whole live run may take. Scenarios not started by then are marked errored.
LIVE_RUN_BUDGET = float(os.environ.get("GEMINI_RUN_BUDGET", "900"))


def _fill_quote_placeholder(transport, scenario, book):
    """Replay scripts may ask for the true quoted cost with the '__QUOTE__' sentinel."""
    script = getattr(transport, "script", None)
    if not script:
        return
    quoted = ground_truth(scenario, book)["quote"]["net_cost_bps"]
    for turn in script:
        advice = turn.get("advice")
        if isinstance(advice, dict) and advice.get("expected_cost_bps") == "__QUOTE__":
            advice["expected_cost_bps"] = quoted


def run_one(candidate_name, scenario, live_transport=None):
    book = scenario_book(scenario)
    if live_transport is not None:
        transport = live_transport
    else:
        transport = CANDIDATES[candidate_name](scenario)
        _fill_quote_placeholder(transport, scenario, book)

    result = run_advisor(
        transport, book, scenario["side"], scenario["notional"],
        order_type=scenario.get("order_type", "Market"),
        fee_tier=scenario.get("fee_tier", "Tier 1"),
        volatility=scenario.get("volatility", 0.01),
        book_age="0.0s (recorded snapshot)",
    )
    checks, score, critical = grade(scenario, result, book)
    return {
        "candidate": candidate_name,
        "scenario": scenario["id"],
        "score": round(score, 4),
        "critical_failures": critical,
        "checks": [c.to_dict() for c in checks],
        "advice": result.advice,
        "tool_calls": [c["name"] for c in result.tool_calls],
        "turns": result.turns,
        "latency_ms": round(result.latency_ms, 2),
        "model": result.model,
        "models_used": result.models_used,
        "errored": result.upstream_error,
        "errors": result.errors,
    }


def _skipped(name, scenario, reason):
    return {"candidate": name, "scenario": scenario["id"], "score": 0.0, "critical_failures": [],
            "checks": [], "advice": None, "tool_calls": [], "turns": 0, "latency_ms": 0.0,
            "model": "", "models_used": [], "errored": True, "errors": [reason]}


def run_suite(candidate_names, live=False, on_row=None):
    """Run every candidate on every scenario. `on_row` is called with each row as it lands."""
    rows, live_transport_factory = [], None
    if live:
        live_transport_factory = _live_transport_factory()
        if live_transport_factory:
            candidate_names = list(candidate_names) + ["gemini_live"]
        else:
            print("No GEMINI_API_KEY; skipping the live candidate.", file=sys.stderr)

    for name in candidate_names:
        live_run = name == "gemini_live"
        # One live transport for the whole run, so call pacing carries across scenarios.
        transport = live_transport_factory() if live_run else None
        started = time.time()
        for i, scenario in enumerate(SCENARIOS, 1):
            if live_run and time.time() - started > LIVE_RUN_BUDGET:
                row = _skipped(name, scenario, f"not run: the {LIVE_RUN_BUDGET:.0f}s run budget was spent")
            else:
                row = run_one(name, scenario, live_transport=transport)
            rows.append(row)
            if live_run:
                state = "errored" if row["errored"] else f"{row['score'] * 100:.0f}%"
                print(f"[{i}/{len(SCENARIOS)}] gemini_live {scenario['id']}: {state} "
                      f"({row['latency_ms'] / 1000:.0f}s)", file=sys.stderr, flush=True)
            if on_row:
                on_row(rows)
    return rows


def _live_transport_factory():
    """Build a factory for the real Gemini transport, or None when no key is configured."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    from advisor.advisor import GeminiTransport
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(LIVE_CALL_TIMEOUT * 1000)))
    from gemini_integration import FALLBACK_MODELS, MODEL  # evaluate the models the app uses
    models = list(dict.fromkeys([MODEL] + FALLBACK_MODELS))
    return lambda: GeminiTransport(client, models, call_interval=LIVE_CALL_INTERVAL)


def summarise(rows):
    """
    Per-candidate totals. A scenario where the provider failed before the model answered
    (quota, overload) is counted as errored and left out of the score: it measured the
    provider, not the advisor. `score` is None when nothing was scored.
    """
    by_candidate = {}
    for row in rows:
        entry = by_candidate.setdefault(row["candidate"], {
            "scenarios": 0, "errored": 0, "score_sum": 0.0, "critical": 0, "latency_ms": 0.0,
            "failed_checks": {}})
        if row.get("errored"):
            entry["errored"] += 1
            continue
        entry["scenarios"] += 1
        entry["score_sum"] += row["score"]
        entry["critical"] += 1 if row["critical_failures"] else 0
        entry["latency_ms"] += row["latency_ms"]
        for check in row["checks"]:
            if not check["passed"]:
                entry["failed_checks"][check["name"]] = entry["failed_checks"].get(check["name"], 0) + 1
    for entry in by_candidate.values():
        n = max(entry["scenarios"], 1)
        entry["score"] = round(entry["score_sum"] / n, 4) if entry["scenarios"] else None
        entry["mean_latency_ms"] = round(entry["latency_ms"] / n, 2)
        entry["clean_scenarios"] = entry["scenarios"] - entry["critical"]
    return by_candidate


def _check_names(rows):
    names, seen = [], set()
    for row in rows:
        for check in row["checks"]:
            if check["name"] not in seen:
                seen.add(check["name"])
                names.append(check["name"])
    return names


def _rank(item):
    score = item[1]["score"]
    return -1.0 if score is None else -score


def _pct(score, width=0):
    return f"{'n/a':>{width}}" if score is None else f"{score * 100:{width}.1f}%"


def to_markdown(rows, summary):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        "# Advisor eval results",
        "",
        f"Generated by `python -m evals.runner --markdown evals/RESULTS.md` on {ts}.",
        f"{len(SCENARIOS)} scenarios x {len(summary)} candidates. Scores are weighted pass rates over "
        "the graders in `evals/graders.py`.",
        "",
        "## Scoreboard",
        "",
        "| Candidate | Kind | Score | Scenarios without a critical failure | Mean latency | What it is |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for name, entry in sorted(summary.items(), key=_rank):
        kind = "measured" if name in REAL_CANDIDATES or name == "gemini_live" else "replay fixture"
        out.append(
            f"| `{name}` | {kind} | {_pct(entry['score'])} | "
            f"{entry['clean_scenarios']}/{entry['scenarios']} | {entry['mean_latency_ms']:.1f} ms | "
            f"{DESCRIPTIONS.get(name, 'Live Gemini via the tool-calling loop.')} |"
        )

    out += ["", "## Which grader caught what", "",
            "Counts are scenarios where the check failed, out of "
            f"{len(SCENARIOS)}. A replay fixture is supposed to light up its own column.", ""]
    names = _check_names(rows)
    out.append("| Candidate | " + " | ".join(f"`{n}`" for n in names) + " |")
    out.append("| --- | " + " | ".join("---:" for _ in names) + " |")
    for candidate, _ in sorted(summary.items(), key=_rank):
        failed = summary[candidate]["failed_checks"]
        cells = [str(failed.get(n, 0)) if failed.get(n) else "." for n in names]
        out.append(f"| `{candidate}` | " + " | ".join(cells) + " |")

    out += ["", "## Per-scenario scores", "",
            "| Scenario | " + " | ".join(f"`{c}`" for c in sorted(summary)) + " |",
            "| --- | " + " | ".join("---:" for _ in summary) + " |"]
    for scenario in SCENARIOS:
        cells = []
        for candidate in sorted(summary):
            row = next((r for r in rows if r["candidate"] == candidate and r["scenario"] == scenario["id"]), None)
            cells.append("-" if row is None else f"{row['score'] * 100:.0f}%")
        out.append(f"| `{scenario['id']}` | " + " | ".join(cells) + " |")

    out += ["", "## Scenarios", ""]
    for scenario in SCENARIOS:
        out.append(f"- **`{scenario['id']}`** - {scenario['side']} ${scenario['notional']:,.0f} "
                   f"into `{scenario['book']}`. {scenario['note']}")

    out += [
        "",
        "## What these numbers do and do not say",
        "",
        "- `rules` is measured. It is the deterministic baseline in `advisor/advisor.py` running the "
        "real tools against the scenario books, so its score is a property of code in this repository.",
        "- Read the baseline's score with the obvious caveat: the graders and the baseline were written "
        "together, so a perfect score is a consistency check, not an independent result. Its use is as a "
        "floor. A model that cannot beat a page of thresholds on this set is not adding anything, and a "
        "change that drops the baseline below its floor fails CI.",
        "- Every other offline candidate is a **replay fixture**: a hand-written script standing in for "
        "one way a model can be wrong. They are not captured API responses and say nothing about how "
        "any model behaves. They exist so that each grader is shown firing on the failure it targets, "
        "which is the only way to know the graders are not vacuous.",
        "- `legacy_prose` is the output contract of the single-prompt path this work replaced, read off "
        "the old prompt: six string fields, no tools, no order side, no cost figure. Its failures are "
        "structural consequences of that contract rather than a judgement about the model behind it.",
        "- **No row here was produced against the live Gemini API.** There is no key in this repository's "
        "test or CI environment and the live path has never been exercised. `python -m evals.runner --live` "
        "adds a `gemini_live` row when `GEMINI_API_KEY` is set; until someone runs it, the tool-calling "
        "loop against the real API is untested.",
        "- The books are synthetic ladders from `evals/scenarios.py`, not venue captures. Drop recorded "
        "snapshots into `evals/books/` with the same shape and the scenarios use them instead.",
        "",
    ]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the advisor eval suite.")
    parser.add_argument("--candidates", default=",".join(CANDIDATES),
                        help="comma-separated candidate names (default: all)")
    parser.add_argument("--scenario", help="run a single scenario by id")
    parser.add_argument("--live", action="store_true",
                        help="also run the real Gemini transport; needs GEMINI_API_KEY")
    parser.add_argument("--json", dest="json_path", help="write the full per-check results here")
    parser.add_argument("--markdown", dest="md_path", help="write the results table here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    names = [n.strip() for n in args.candidates.split(",") if n.strip()]
    unknown = [n for n in names if n not in CANDIDATES]
    if unknown:
        parser.error(f"unknown candidate(s): {', '.join(unknown)}")

    global SCENARIOS
    if args.scenario:
        from evals.scenarios import get_scenario
        SCENARIOS = [get_scenario(args.scenario)]

    if args.live and not args.json_path:
        # A live run costs money and is hard to repeat; always keep the raw rows.
        args.json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")

    def save(rows):
        if args.json_path:
            # Write a temp file, then swap it in, so a cut-off save keeps the last good results.
            tmp = args.json_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                           "scenarios": [s["id"] for s in SCENARIOS],
                           "summary": summarise(rows), "rows": rows}, fh, indent=2)
            os.replace(tmp, args.json_path)

    if args.live:
        print(f"Live run: {len(SCENARIOS)} scenarios, calls {LIVE_CALL_INTERVAL:.0f}s apart for the free tier, "
              f"stops starting new scenarios after {LIVE_RUN_BUDGET / 60:.0f} min.", file=sys.stderr, flush=True)
    rows = run_suite(names, live=args.live, on_row=save if args.live else None)
    summary = summarise(rows)
    save(rows)
    if args.md_path:
        with open(args.md_path, "w") as fh:
            fh.write(to_markdown(rows, summary))

    if not args.quiet:
        width = max(len(n) for n in summary) if summary else 10
        print(f"{'candidate':<{width}}  score   clean  mean latency")
        for name, entry in sorted(summary.items(), key=_rank):
            errored = f"  ({entry['errored']} errored upstream, not scored)" if entry["errored"] else ""
            print(f"{name:<{width}}  {_pct(entry['score'], 5)}  "
                  f"{entry['clean_scenarios']}/{entry['scenarios']}    {entry['mean_latency_ms']:7.1f} ms{errored}")

    live = [r for r in rows if r["candidate"] == "gemini_live"]
    for row in live:
        if row["errors"]:
            kind = "errored" if row["errored"] else "failed"
            print(f"gemini_live {row['scenario']} {kind}: {row['errors'][0]}", file=sys.stderr)
    if live:
        used = sorted({m for r in live for m in r.get("models_used", [])})
        print(f"gemini_live answered by: {', '.join(used) or 'no model'}", file=sys.stderr)
    if args.live and args.json_path:
        print(f"full results in {args.json_path}", file=sys.stderr)

    real = [n for n in summary if n in REAL_CANDIDATES or n == "gemini_live"]
    unmeasured = [n for n in real if summary[n]["score"] is None]
    regressed = [n for n in real if summary[n]["score"] is not None and summary[n]["score"] < SCORE_FLOOR]
    if unmeasured:
        print(f"not measured, every scenario errored upstream: {', '.join(unmeasured)}", file=sys.stderr)
    if regressed:
        print(f"below the {SCORE_FLOOR:.0%} floor: {', '.join(regressed)}", file=sys.stderr)
    return 1 if unmeasured or regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
