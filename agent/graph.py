"""
The execution agent: a LangGraph state machine from an order to a paper fill.

    plan ──► price (one branch per plan, in parallel) ──► critic ──► approve ──► execute
     ▲                                                      │  (pauses for a human)
     └──────────── revise, at most MAX_REVISIONS times ─────┘

- plan     the tool-calling advisor proposes how to work the order. On a revision it is
           handed the critic's findings.
- price    the advised plan and its alternatives (one clip, resting limit, TWAPs) are
           each priced against the same book, fanned out with Send so every plan in the
           round is priced before the critic sees any of them.
- critic   deterministic checks against the priced plans (agent/critic.py). A blocking
           finding sends the plan back; after MAX_REVISIONS, or once the run has used its
           time budget, it goes to the human with the findings unresolved and marked.
- approve  interrupt(): the run stops, its state is saved by the checkpointer, and it
           resumes only when a human approves or rejects. An approval older than
           APPROVAL_TTL_S is refused, since the book it was priced on has moved.
- execute  paper execution against the latest book (agent/execution.py).

Everything the graph needs from outside (the planner, the book feed) is injected, so the
tests and the evals drive the same graph the app does.
"""
import operator
import threading
import time
import uuid
from collections import deque
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from .critic import blocking, review
from .execution import execute
from .pricing import DEFAULT_SLICES, plans_to_price, price_plan  # noqa: F401  (re-exported)

MAX_REVISIONS = 2
APPROVAL_TTL_S = 120


class AgentState(TypedDict, total=False):
    order: dict
    book: dict
    started_at: float
    round: int
    advice: dict
    advisor: dict
    priced: Annotated[list, operator.add]   # every round's priced plans; the critic reads its round's
    findings: list
    rounds: Annotated[list, operator.add]   # one entry per round: the advice and what the critic said
    trace: Annotated[list, operator.add]
    planned_at: float
    decision: dict
    execution: dict
    status: str


def _step(node, started, detail=""):
    return {"node": node, "ms": round((time.perf_counter() - started) * 1e3, 1), "detail": detail}


def build_graph(planner, book_source=None, pace_s=0.0, deadline_s=None):
    """
    `planner(order, book, feedback)` returns {"advice": dict or None, ...metadata}; it is
    the advisor, with whatever transport and fallback the caller wants. `book_source()`
    returns the latest book for execution. Past `deadline_s` seconds from the start, a
    blocked plan goes to the human instead of going round again.
    """

    def plan(state):
        started = time.perf_counter()
        round_ = state.get("round", 0) + 1
        feedback = [f["message"] for f in blocking(state.get("findings") or [])] or None
        out = planner(state["order"], state["book"], feedback)
        advice = out.get("advice")
        meta = {k: v for k, v in out.items() if k != "advice"}
        update = {"round": round_, "advice": advice, "advisor": meta, "findings": [],
                  "trace": [_step("plan", started, f"round {round_}: {advice['strategy'] if advice else 'no advice'}"
                                  f" from {meta.get('model', '?')}")]}
        if advice is None:
            update["status"] = "failed"
        return update

    def fan_out(state):
        if state.get("advice") is None:
            return END
        return [Send("price", {"order": state["order"], "book": state["book"], "plan": p, "round": state["round"]})
                for p in plans_to_price(state["advice"])]

    def price(payload):
        started = time.perf_counter()
        priced = price_plan(payload["order"], payload["book"], payload["plan"])
        return {"priced": [{**priced, "round": payload["round"]}],
                "trace": [_step("price", started, f"{priced['label']}: {priced['cost_bps']:.2f} bps")]}

    def critic(state):
        started = time.perf_counter()
        priced = [p for p in state["priced"] if p["round"] == state["round"]]
        findings = review(state["order"], state["book"], state["advice"], priced)
        blocked = blocking(findings)
        return {"findings": findings,
                "rounds": [{"round": state["round"], "advice": state["advice"], "findings": findings}],
                "planned_at": time.time(),
                "trace": [_step("critic", started, f"{len(blocked)} blocking, {len(findings) - len(blocked)} to note")]}

    def after_critic(state):
        out_of_time = deadline_s is not None and time.time() - state.get("started_at", time.time()) > deadline_s
        if blocking(state["findings"]) and state["round"] <= MAX_REVISIONS and not out_of_time:
            return "plan"
        return "approve"

    def approve(state):
        # Everything before interrupt() re-runs on resume, so this node does nothing else first.
        decision = interrupt({"advice": state["advice"], "findings": state["findings"],
                              "unresolved": bool(blocking(state["findings"]))})
        started = time.perf_counter()
        approved = bool(decision.get("approved"))
        if approved and time.time() - state["planned_at"] > APPROVAL_TTL_S:
            return {"decision": decision, "status": "expired",
                    "trace": [_step("approve", started, f"refused: older than {APPROVAL_TTL_S}s, re-plan")]}
        return {"decision": decision, "status": "approved" if approved else "rejected",
                "trace": [_step("approve", started, "approved" if approved else "rejected")]}

    def after_approve(state):
        return "execute" if state["status"] == "approved" else END

    def run_execution(state):
        started = time.perf_counter()
        result = execute(state["order"], state["advice"], state["book"], book_source=book_source, pace_s=pace_s)
        return {"execution": result, "status": "executed" if result["status"] in ("filled", "partial", "no_trade")
                else "failed",
                "trace": [_step("execute", started, result["status"])]}

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan)
    graph.add_node("price", price)
    graph.add_node("critic", critic)
    graph.add_node("approve", approve)
    graph.add_node("execute", run_execution)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", fan_out, ["price", END])
    graph.add_edge("price", "critic")
    graph.add_conditional_edges("critic", after_critic, ["plan", "approve"])
    graph.add_conditional_edges("approve", after_approve, ["execute", END])
    graph.add_edge("execute", END)
    return graph


class ExecutionAgent:
    """
    The compiled graph with an in-memory checkpointer, and the two calls the UI makes:
    `start` runs an order until it needs a human, `resume` hands it their decision.

    Runs are kept per thread id; the oldest are dropped past `max_runs`, so a public page
    cannot grow the checkpointer without bound. A run whose state was dropped, or that a
    restart lost, reports status "expired".
    """

    def __init__(self, planner, book_source=None, pace_s=0.0, max_runs=32, deadline_s=None):
        self.saver = InMemorySaver()
        self.graph = build_graph(planner, book_source, pace_s, deadline_s).compile(checkpointer=self.saver)
        self.max_runs = max_runs
        self._runs = deque()
        self._lock = threading.Lock()

    def _config(self, thread_id):
        return {"configurable": {"thread_id": thread_id}}

    def start(self, order, book):
        thread_id = uuid.uuid4().hex
        with self._lock:
            self._runs.append(thread_id)
            while len(self._runs) > self.max_runs:
                self.saver.delete_thread(self._runs.popleft())
        self.graph.invoke({"order": order, "book": book, "started_at": time.time()}, self._config(thread_id))
        return thread_id, self.view(thread_id)

    def resume(self, thread_id, approved):
        # One decision per run: a second click finds nothing waiting and gets the result.
        with self._lock:
            if self.view(thread_id)["status"] == "awaiting_approval":
                self.graph.invoke(Command(resume={"approved": bool(approved)}), self._config(thread_id))
        return self.view(thread_id)

    def view(self, thread_id):
        """What the UI shows: the run's state, and whether it is waiting on a human."""
        snapshot = self.graph.get_state(self._config(thread_id))
        values = dict(snapshot.values or {})
        if not values:
            return {"thread_id": thread_id, "status": "expired"}
        waiting = any(task.interrupts for task in snapshot.tasks)
        round_ = values.get("round", 0)
        return {
            "thread_id": thread_id,
            "status": "awaiting_approval" if waiting else values.get("status", "running"),
            "order": values.get("order"),
            "advice": values.get("advice"),
            "advisor": values.get("advisor", {}),
            "priced": [p for p in values.get("priced", []) if p["round"] == round_],
            "findings": values.get("findings", []),
            "rounds": values.get("rounds", []),
            "revisions": max(round_ - 1, 0),
            "trace": values.get("trace", []),
            "execution": values.get("execution"),
        }
