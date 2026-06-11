#!/usr/bin/env python3
"""Spec-Tasks Organ.

A pure, stdlib-only decision organ extracted from discovery-engine's
``app/services/spec_tasks.py`` (Phase 3 of Matt's AI-driven spec pipeline —
task-level claim/complete sequencing).

CONTRACT (orchestrator pure-organ protocol)
--------------------------------------------
    decide(state, context) -> {"output", "rationale", "self_metric"}

  * Pure: no DB, no network, no filesystem, no env reads, no clock — every
    input arrives via ``state`` / ``context``.
  * Deterministic: same (state, context) always yields the same result.
  * Fail-safe: never raises. Bad/empty input returns a valid structure with a
    low ``confidence`` and an explanatory ``rationale``.
  * Stdlib-only: imports nothing outside the Python standard library.
  * ``self_metric.confidence`` is a float in ``[0.0, 1.0]``.

WHAT THIS ORGAN DECIDES
-----------------------
Given a technical spec's task graph (tasks with dependencies + per-task
state) and its approval flag, decide the SINGLE next action a worker should
take against the spec:

  * ``not_approved``   — the spec hasn't been approved; nothing is claimable.
    (Mirrors ``claim_next_task`` raising ``PermissionError`` on an unapproved
    spec.)
  * ``no_tasks``       — the spec defines no tasks.
  * ``claim``          — the next eligible task to claim: the first ``pending``
    task whose every dependency is in a terminal "done" state. (The exact
    eligibility rule from ``claim_next_task``.)
  * ``spec_complete``  — every task is in a terminal "done" state, so the
    parent spec should roll up to ``resolved``. (Mirrors the ``all_done``
    roll-up in ``complete_task``.)
  * ``in_progress``    — nothing claimable right now, but work is live (one or
    more tasks ``in_progress``); the caller should wait.
  * ``blocked``        — nothing claimable, nothing live, and the spec is not
    complete: a dependency is ``failed`` / unknown, or the remaining pending
    tasks form a dependency deadlock. Needs operator/reset intervention.

The load-bearing invariant carried over from the live wire (items 12640 /
12629): ``completed_pending_review`` counts as "done" for BOTH the dependency
check and the completion roll-up, so a dependent task unblocks and a spec
rolls up even when a task's result missed the acceptance heuristic (that miss
is tracked separately via a ``spec_acceptance_review`` surface).

The CLI (``python organ.py < input.json``) reads ``{state, context}`` on
stdin (or ``$ORGAN_INPUT``) and writes ``{output, rationale, self_metric}`` to
stdout, so the orchestrator can shell out to it like any other organ.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional

# A task is "done" for pipeline-mechanics purposes once it has been completed
# by an agent — whether or not it passed the acceptance heuristic.
# ``completed_pending_review`` means the work was delivered but the result text
# didn't match the acceptance criteria closely enough; that is the operator's
# oversight gate, orthogonal to whether downstream tasks may proceed. Treating
# it as not-done was the bug behind items 12640 / 12629. Kept identical to the
# live wire's ``_DONE_STATUSES`` so the organ's decision matches production.
DONE_STATUSES = frozenset({"completed", "completed_pending_review"})

# Statuses a task may be in. ``pending`` is the implicit default for any task
# that has no state slot yet.
_DEFAULT_STATUS = "pending"


# ---------------------------------------------------------------------------
# Pure helpers — no IO, deterministic, testable in isolation.
# ---------------------------------------------------------------------------

def _norm_tasks(tasks: Any) -> List[Dict[str, Any]]:
    """Return the well-formed task dicts (each with a truthy ``id``).

    Defensive: non-list ``tasks`` → ``[]``; non-dict / id-less entries are
    dropped so a malformed spec entry never crashes the decision.
    """
    if not isinstance(tasks, list):
        return []
    out: List[Dict[str, Any]] = []
    for t in tasks:
        if isinstance(t, dict) and t.get("id"):
            out.append(t)
    return out


def _status_of(states: Dict[str, Any], tid: Any) -> str:
    """Return the status string for ``tid`` (default ``pending``)."""
    if not isinstance(states, dict):
        return _DEFAULT_STATUS
    st = states.get(tid)
    if not isinstance(st, dict):
        return _DEFAULT_STATUS
    status = st.get("status")
    return status if isinstance(status, str) and status else _DEFAULT_STATUS


def deps_satisfied(task: Dict[str, Any], states: Dict[str, Any]) -> bool:
    """True iff every dependency of ``task`` is in a terminal "done" state.

    The exact eligibility predicate from ``claim_next_task``: a dependency
    counts as satisfied only when its status is in :data:`DONE_STATUSES`. An
    unknown dependency id (no task / no state) is NOT satisfied.
    """
    deps = task.get("dependencies") or []
    if not isinstance(deps, list):
        return False
    return all(_status_of(states, d) in DONE_STATUSES for d in deps)


def next_claimable_task(tasks: List[Dict[str, Any]],
                        states: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the first ``pending`` task whose deps are all done, else ``None``.

    Order-preserving: walks ``tasks`` in declaration order and returns the
    first eligible one — identical to ``claim_next_task``'s scan.
    """
    for task in tasks:
        tid = task.get("id")
        if _status_of(states, tid) != "pending":
            continue
        if deps_satisfied(task, states):
            return task
    return None


def spec_complete(tasks: List[Dict[str, Any]], states: Dict[str, Any]) -> bool:
    """True iff there is at least one task and every task is in a done state.

    Mirrors the ``all_done`` roll-up in ``complete_task``: an empty task list
    is NOT complete (nothing to roll up).
    """
    if not tasks:
        return False
    return all(_status_of(states, t.get("id")) in DONE_STATUSES for t in tasks)


def completion(tasks: List[Dict[str, Any]],
               states: Dict[str, Any]) -> Dict[str, int]:
    """Return ``{total, completed, completion_pct}``.

    ``completed`` counts both terminal done statuses (``completed_pending_review``
    included), matching ``get_spec_status``.
    """
    total = len(tasks)
    completed = sum(
        1 for t in tasks if _status_of(states, t.get("id")) in DONE_STATUSES
    )
    pct = int(round((completed / total) * 100)) if total else 0
    return {"total": total, "completed": completed, "completion_pct": pct}


def blocked_pending_tasks(tasks: List[Dict[str, Any]],
                          states: Dict[str, Any]) -> List[str]:
    """Return ids of ``pending`` tasks whose dependencies are not all done.

    These are the tasks holding the spec back: they cannot be claimed until
    their dependencies finish (or are reset). A task is included regardless of
    *why* its deps are unmet (still in flight, failed, or unknown) — the caller
    distinguishes "wait" from "deadlock" via the live/failed signal.
    """
    out: List[str] = []
    for task in tasks:
        tid = task.get("id")
        if _status_of(states, tid) != "pending":
            continue
        if not deps_satisfied(task, states):
            out.append(tid)
    return out


def _serialise_task(task: Dict[str, Any], status: str) -> Dict[str, Any]:
    """Project a spec task definition + status into the organ's output shape."""
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "dependencies": task.get("dependencies") or [],
        "complexity": task.get("complexity"),
        "suggested_model": task.get("suggested_model"),
        "acceptance_criteria": task.get("acceptance_criteria") or [],
        "status": status,
    }


# ---------------------------------------------------------------------------
# Acceptance-criteria heuristic — pure, ported verbatim from spec_tasks.py.
# ---------------------------------------------------------------------------

_SALIENT_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{3,}")
_STOPWORDS = frozenset({
    "should", "must", "will", "have", "this", "that", "with", "from",
    "into", "when", "then", "shall", "also", "more", "than",
    "case", "such", "each", "some", "what", "after", "before",
})


def salient_tokens(text: Optional[str]) -> List[str]:
    """Return the 4-letter-and-up tokens worth matching against.

    Strips stopwords so 'must' / 'should' don't dominate the signal. Order- and
    duplicate-preserving-removal identical to ``spec_tasks._salient_tokens``.
    """
    if not text:
        return []
    out: List[str] = []
    seen = set()
    for m in _SALIENT_TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        if tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def evaluate_acceptance_criteria(acceptance_criteria: Optional[List[str]],
                                 result_text: Optional[str]) -> Dict[str, Any]:
    """Heuristic check: which criteria does the result text address?

    Per criterion, build a set of "salient" word tokens (4+ chars, lowercased,
    stopword-stripped) and look for at least 50% in the result text. Catches
    "criterion mentioned the database migration" matching a result that talks
    about "running the migration on the database" without an LLM call.

    Returns ``{criteria_count, addressed, missed, fraction_addressed,
    passed}``. Empty criteria → ``passed=True``. Empty result → all criteria
    missed. Ported verbatim from ``spec_tasks.evaluate_acceptance_criteria``.
    """
    criteria = list(acceptance_criteria or [])
    if not criteria:
        return {
            "criteria_count": 0,
            "addressed": [],
            "missed": [],
            "fraction_addressed": 1.0,
            "passed": True,
        }

    text = (result_text or "").lower()
    addressed: List[str] = []
    missed: List[str] = []

    for crit in criteria:
        if not isinstance(crit, str) or not crit.strip():
            missed.append(crit)
            continue
        salient = salient_tokens(crit)
        if not salient:
            # Nothing testable — don't punish a vague criterion.
            addressed.append(crit)
            continue
        hits = sum(1 for tok in salient if tok in text)
        if hits >= max(1, len(salient) // 2):
            addressed.append(crit)
        else:
            missed.append(crit)

    fraction = len(addressed) / len(criteria) if criteria else 1.0
    return {
        "criteria_count": len(criteria),
        "addressed": addressed,
        "missed": missed,
        "fraction_addressed": fraction,
        "passed": not missed,
    }


# ---------------------------------------------------------------------------
# The decision — the pure organ entry point.
# ---------------------------------------------------------------------------

def _empty_output() -> Dict[str, Any]:
    return {
        "decision": "no_tasks",
        "next_task": None,
        "total_tasks": 0,
        "completed_tasks": 0,
        "completion_pct": 0,
        "blocked_task_ids": [],
    }


def decide(state: Optional[Dict[str, Any]],
           context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Decide the single next action for a spec's task graph.

    The canonical pure-organ contract function: ``decide(state, context)``.

    ``state`` keys (all optional; defensive defaults applied):
      - ``approved`` (bool)      — has the spec been approved; default False.
      - ``tasks`` (list)         — task definitions, each
        ``{"id", "title", "dependencies", "acceptance_criteria", ...}``;
        default [].
      - ``task_states`` (dict)   — ``{task_id: {"status": ...}}``; default {}.
        A task with no slot defaults to ``pending``.

    Returns ``{output, rationale, self_metric}``. Never raises.
    ``output.decision`` is one of: ``not_approved``, ``no_tasks``, ``claim``,
    ``spec_complete``, ``in_progress``, ``blocked``.
    """
    try:
        if not isinstance(state, dict):
            state = {}

        approved = bool(state.get("approved", False))
        tasks = _norm_tasks(state.get("tasks"))
        states = state.get("task_states")
        if not isinstance(states, dict):
            states = {}

        prog = completion(tasks, states)
        output = _empty_output()
        output.update({
            "total_tasks": prog["total"],
            "completed_tasks": prog["completed"],
            "completion_pct": prog["completion_pct"],
        })

        # Gate 1 — approval is the hard gate (claim_next_task PermissionError).
        if not approved:
            output["decision"] = "not_approved"
            return _report(
                output,
                "Spec is not approved; no task is claimable until approve_spec runs.",
                confidence=0.97,
            )

        # Gate 2 — no tasks defined.
        if not tasks:
            output["decision"] = "no_tasks"
            return _report(
                output,
                "Spec defines no tasks; nothing to sequence.",
                confidence=0.9,
            )

        # Gate 3 — next claimable task (the core sequencing decision).
        chosen = next_claimable_task(tasks, states)
        if chosen is not None:
            output["decision"] = "claim"
            output["next_task"] = _serialise_task(
                chosen, _status_of(states, chosen.get("id"))
            )
            return _report(
                output,
                (
                    f"Next claimable task is {chosen.get('id')!r} "
                    f"({(chosen.get('title') or '').strip() or 'untitled'}); "
                    f"all its dependencies are done "
                    f"({prog['completed']}/{prog['total']} tasks complete)."
                ),
                confidence=0.95,
            )

        # Gate 4 — nothing claimable: complete, waiting, or deadlocked?
        if spec_complete(tasks, states):
            output["decision"] = "spec_complete"
            return _report(
                output,
                f"All {prog['total']} task(s) are done; spec should roll up to resolved.",
                confidence=0.97,
            )

        blocked = blocked_pending_tasks(tasks, states)
        output["blocked_task_ids"] = blocked
        has_inprogress = any(
            _status_of(states, t.get("id")) == "in_progress" for t in tasks
        )

        if has_inprogress:
            output["decision"] = "in_progress"
            return _report(
                output,
                (
                    "Nothing claimable right now; work is live "
                    f"({prog['completed']}/{prog['total']} done, "
                    f"{len(blocked)} pending task(s) waiting on dependencies). Wait."
                ),
                confidence=0.9,
            )

        # No claimable task, none in flight, not complete → deadlock / failed dep.
        output["decision"] = "blocked"
        return _report(
            output,
            (
                "Spec is blocked: no task is claimable, none in flight, and the "
                f"spec is incomplete ({prog['completed']}/{prog['total']} done). "
                f"Pending task(s) {blocked} depend on a failed/unknown/unfinished "
                "task — needs reset or operator intervention."
            ),
            confidence=0.85,
        )

    except Exception as exc:  # fail-safe — a broken decision must never raise.
        out = _empty_output()
        out["decision"] = "error"
        return {
            "output": out,
            "rationale": f"decide() failed open: {exc}",
            "self_metric": {
                "confidence": 0.0,
                "decision": "error",
                "error": str(exc),
            },
        }


def _report(output: Dict[str, Any], rationale: str,
            confidence: float) -> Dict[str, Any]:
    """Assemble the contract triple with a consistent ``self_metric``."""
    return {
        "output": output,
        "rationale": rationale,
        "self_metric": {
            "confidence": confidence,
            "decision": output["decision"],
            "total_tasks": output["total_tasks"],
            "completed_tasks": output["completed_tasks"],
            "completion_pct": output["completion_pct"],
            "blocked_count": len(output.get("blocked_task_ids") or []),
        },
    }


# Backward-compatible aliases for callers expecting domain-specific names.
decide_next_task = decide


# ---------------------------------------------------------------------------
# CLI adapter — stdin JSON in, stdout JSON out. Not part of the pure contract.
# ---------------------------------------------------------------------------

def _read_input() -> Dict[str, Any]:
    # Per the orchestrator CONTRACT, ORGAN_INPUT names the *file* to read
    # the JSON payload from, and takes precedence over stdin. (The
    # conformance workflow invokes the organ as
    # ``ORGAN_INPUT="$s" python3 organ.py`` with ``$s`` a sample path — so
    # treating ORGAN_INPUT as inline JSON, or only consulting it when stdin
    # is empty, made every file-based invocation fail to parse.)
    import os

    path = os.getenv("ORGAN_INPUT")
    if path:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    if not text.strip():
        return {}
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def main() -> int:
    try:
        data = _read_input()
        result = decide(data.get("state") or {}, data.get("context") or {})
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        out = _empty_output()
        out["decision"] = "error"
        json.dump({
            "output": out,
            "rationale": f"CLI fatal error: {exc}",
            "self_metric": {"confidence": 0.0, "decision": "error", "error": str(exc)},
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
