"""pytest suite for the Spec-Tasks Organ.

Verifies the pure-organ contract (deterministic, fail-safe, stdlib-only) and
the task-sequencing decision logic ported from discovery-engine's
``app/services/spec_tasks.py``.
"""
import inspect
import json
from pathlib import Path

import pytest

from organ import (
    decide,
    decide_next_task,
    DONE_STATUSES,
    deps_satisfied,
    next_claimable_task,
    spec_complete,
    completion,
    blocked_pending_tasks,
    salient_tokens,
    evaluate_acceptance_criteria,
)


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------

def _assert_valid_report(result):
    assert isinstance(result, dict)
    assert set(result.keys()) == {"output", "rationale", "self_metric"}
    out = result["output"]
    assert isinstance(out, dict)
    assert "decision" in out
    assert "next_task" in out
    sm = result["self_metric"]
    assert 0.0 <= sm["confidence"] <= 1.0
    assert isinstance(result["rationale"], str)


def test_decide_signature_is_state_context():
    assert list(inspect.signature(decide).parameters.keys())[:2] == ["state", "context"]


def test_decide_next_task_is_alias():
    assert decide_next_task is decide


def test_empty_state_is_failsafe():
    result = decide({}, {})
    _assert_valid_report(result)
    assert result["output"]["decision"] == "not_approved"
    assert result["output"]["next_task"] is None


def test_none_state_is_failsafe():
    result = decide(None, None)
    _assert_valid_report(result)
    assert result["output"]["next_task"] is None


def test_determinism():
    state = {
        "approved": True,
        "tasks": [{"id": "T1"}, {"id": "T2", "dependencies": ["T1"]}],
        "task_states": {"T1": {"status": "pending"}},
    }
    assert decide(state, {}) == decide(state, {})


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------

def test_unapproved_spec_blocks_claim():
    state = {"approved": False, "tasks": [{"id": "T1"}]}
    out = decide(state, {})["output"]
    assert out["decision"] == "not_approved"
    assert out["next_task"] is None


def test_approved_but_no_tasks():
    out = decide({"approved": True, "tasks": []}, {})["output"]
    assert out["decision"] == "no_tasks"
    assert out["total_tasks"] == 0


# ---------------------------------------------------------------------------
# Claim sequencing
# ---------------------------------------------------------------------------

def test_claims_first_pending_with_no_deps():
    state = {"approved": True, "tasks": [{"id": "T1", "title": "First"}]}
    out = decide(state, {})["output"]
    assert out["decision"] == "claim"
    assert out["next_task"]["id"] == "T1"
    assert out["next_task"]["status"] == "pending"


def test_dependency_blocks_until_done():
    state = {
        "approved": True,
        "tasks": [
            {"id": "T1"},
            {"id": "T2", "dependencies": ["T1"]},
        ],
        "task_states": {"T1": {"status": "pending"}},
    }
    out = decide(state, {})["output"]
    # T1 is the only claimable task; T2 waits on T1.
    assert out["decision"] == "claim"
    assert out["next_task"]["id"] == "T1"


def test_dependency_unblocks_when_complete():
    state = {
        "approved": True,
        "tasks": [
            {"id": "T1"},
            {"id": "T2", "dependencies": ["T1"]},
        ],
        "task_states": {"T1": {"status": "completed"}},
    }
    out = decide(state, {})["output"]
    assert out["decision"] == "claim"
    assert out["next_task"]["id"] == "T2"


def test_completed_pending_review_unblocks_dependency():
    """Item 12640: completed_pending_review counts as done for deps."""
    state = {
        "approved": True,
        "tasks": [
            {"id": "T1"},
            {"id": "T2", "dependencies": ["T1"]},
        ],
        "task_states": {"T1": {"status": "completed_pending_review"}},
    }
    out = decide(state, {})["output"]
    assert out["decision"] == "claim"
    assert out["next_task"]["id"] == "T2"


def test_claim_order_preserves_declaration_order():
    state = {
        "approved": True,
        "tasks": [{"id": "A"}, {"id": "B"}, {"id": "C"}],
    }
    out = decide(state, {})["output"]
    assert out["next_task"]["id"] == "A"


# ---------------------------------------------------------------------------
# Completion roll-up
# ---------------------------------------------------------------------------

def test_spec_complete_when_all_done():
    state = {
        "approved": True,
        "tasks": [{"id": "T1"}, {"id": "T2"}],
        "task_states": {
            "T1": {"status": "completed"},
            "T2": {"status": "completed_pending_review"},
        },
    }
    result = decide(state, {})
    out = result["output"]
    assert out["decision"] == "spec_complete"
    assert out["completion_pct"] == 100
    assert out["completed_tasks"] == 2


def test_completion_pct_partial():
    state = {
        "approved": True,
        "tasks": [{"id": "T1"}, {"id": "T2"}, {"id": "T3"}],
        "task_states": {"T1": {"status": "completed"}},
    }
    out = decide(state, {})["output"]
    assert out["completion_pct"] == 33
    assert out["completed_tasks"] == 1


# ---------------------------------------------------------------------------
# In-progress vs blocked
# ---------------------------------------------------------------------------

def test_in_progress_when_work_live_and_rest_blocked():
    state = {
        "approved": True,
        "tasks": [
            {"id": "T1"},
            {"id": "T2", "dependencies": ["T1"]},
        ],
        "task_states": {"T1": {"status": "in_progress"}},
    }
    out = decide(state, {})["output"]
    assert out["decision"] == "in_progress"
    assert "T2" in out["blocked_task_ids"]


def test_blocked_on_failed_dependency():
    state = {
        "approved": True,
        "tasks": [
            {"id": "T1"},
            {"id": "T2", "dependencies": ["T1"]},
        ],
        "task_states": {"T1": {"status": "failed"}},
    }
    out = decide(state, {})["output"]
    assert out["decision"] == "blocked"
    assert out["blocked_task_ids"] == ["T2"]


def test_blocked_on_unknown_dependency():
    state = {
        "approved": True,
        "tasks": [{"id": "T2", "dependencies": ["ghost"]}],
    }
    out = decide(state, {})["output"]
    assert out["decision"] == "blocked"
    assert "T2" in out["blocked_task_ids"]


def test_blocked_on_circular_dependency():
    state = {
        "approved": True,
        "tasks": [
            {"id": "T1", "dependencies": ["T2"]},
            {"id": "T2", "dependencies": ["T1"]},
        ],
    }
    out = decide(state, {})["output"]
    assert out["decision"] == "blocked"
    assert set(out["blocked_task_ids"]) == {"T1", "T2"}


# ---------------------------------------------------------------------------
# Helper-level coverage
# ---------------------------------------------------------------------------

def test_deps_satisfied_empty_deps():
    assert deps_satisfied({"id": "T1"}, {}) is True


def test_deps_satisfied_partial():
    states = {"A": {"status": "completed"}, "B": {"status": "pending"}}
    assert deps_satisfied({"id": "T", "dependencies": ["A", "B"]}, states) is False


def test_next_claimable_none_when_all_in_progress():
    tasks = [{"id": "T1"}]
    states = {"T1": {"status": "in_progress"}}
    assert next_claimable_task(tasks, states) is None


def test_spec_complete_empty_is_false():
    assert spec_complete([], {}) is False


def test_completion_counts_both_done_statuses():
    tasks = [{"id": "A"}, {"id": "B"}]
    states = {"A": {"status": "completed"}, "B": {"status": "completed_pending_review"}}
    assert completion(tasks, states) == {"total": 2, "completed": 2, "completion_pct": 100}


def test_blocked_pending_excludes_claimable():
    tasks = [{"id": "T1"}, {"id": "T2", "dependencies": ["T1"]}]
    # T1 pending+claimable (no deps) → not blocked; T2 pending+blocked.
    assert blocked_pending_tasks(tasks, {}) == ["T2"]


def test_done_statuses_frozenset():
    assert DONE_STATUSES == frozenset({"completed", "completed_pending_review"})


def test_malformed_tasks_are_dropped():
    state = {"approved": True, "tasks": [{"no_id": 1}, "junk", None, {"id": "T1"}]}
    out = decide(state, {})["output"]
    assert out["total_tasks"] == 1
    assert out["next_task"]["id"] == "T1"


# ---------------------------------------------------------------------------
# Acceptance-criteria heuristic (ported verbatim)
# ---------------------------------------------------------------------------

def test_acceptance_empty_criteria_passes():
    r = evaluate_acceptance_criteria([], "anything")
    assert r["passed"] is True
    assert r["criteria_count"] == 0


def test_acceptance_empty_result_misses_all():
    r = evaluate_acceptance_criteria(["migrate the database schema"], "")
    assert r["passed"] is False
    assert r["missed"] == ["migrate the database schema"]


def test_acceptance_addressed_by_overlap():
    r = evaluate_acceptance_criteria(
        ["migrate the database schema"],
        "I ran the migration on the database and updated the schema.",
    )
    assert r["passed"] is True
    assert r["fraction_addressed"] == 1.0


def test_acceptance_partial_miss():
    r = evaluate_acceptance_criteria(
        ["add the rate limiter middleware", "write integration tests"],
        "I added the rate limiter middleware.",
    )
    assert r["passed"] is False
    assert len(r["addressed"]) == 1
    assert len(r["missed"]) == 1


def test_salient_tokens_strips_stopwords():
    toks = salient_tokens("This should migrate the database")
    assert "should" not in toks
    assert "migrate" in toks
    assert "database" in toks


def test_salient_tokens_empty():
    assert salient_tokens("") == []
    assert salient_tokens(None) == []


# ---------------------------------------------------------------------------
# Samples round-trip
# ---------------------------------------------------------------------------

def test_samples_produce_valid_reports():
    sample_dir = Path(__file__).parent / "samples"
    files = sorted(sample_dir.glob("*.json"))
    assert files, "no sample files found"
    for f in files:
        data = json.loads(f.read_text())
        result = decide(data.get("state") or {}, data.get("context") or {})
        _assert_valid_report(result)


def test_sample_claim_next():
    data = json.loads((Path(__file__).parent / "samples" / "claim_next.json").read_text())
    out = decide(data["state"], data.get("context"))["output"]
    assert out["decision"] == "claim"
    assert out["next_task"]["id"] == "T2"


def test_sample_spec_complete():
    data = json.loads((Path(__file__).parent / "samples" / "spec_complete.json").read_text())
    out = decide(data["state"], data.get("context"))["output"]
    assert out["decision"] == "spec_complete"


def test_sample_blocked():
    data = json.loads((Path(__file__).parent / "samples" / "blocked_failed_dep.json").read_text())
    out = decide(data["state"], data.get("context"))["output"]
    assert out["decision"] == "blocked"


# ---------------------------------------------------------------------------
# CLI entrypoint — the ORGAN_INPUT file contract (exercises main(), not just
# decide()). Regression guard: the runner and the conformance workflow invoke
# the organ as ``ORGAN_INPUT=<path> python3 organ.py``; reading ORGAN_INPUT as
# inline JSON (or only as a stdin fallback) made every sample CLI run error.
# ---------------------------------------------------------------------------

import os
import subprocess
import sys

_ORGAN = str(Path(__file__).parent / "organ.py")


def _run_cli(env=None, stdin_text=""):
    proc = subprocess.run(
        [sys.executable, _ORGAN],
        input=stdin_text,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return proc


def test_cli_reads_organ_input_file_path():
    """ORGAN_INPUT is a FILE PATH; every sample must run clean (no error)."""
    sample_dir = Path(__file__).parent / "samples"
    for f in sorted(sample_dir.glob("*.json")):
        proc = _run_cli(env={"ORGAN_INPUT": str(f)})
        assert proc.returncode == 0, f"{f.name}: rc={proc.returncode} err={proc.stderr}"
        report = json.loads(proc.stdout)
        _assert_valid_report(report)
        assert report["output"]["decision"] != "error", (
            f"{f.name} produced an error decision via ORGAN_INPUT: "
            f"{report['rationale']}"
        )


def test_cli_organ_input_matches_direct_decide():
    """CLI output via ORGAN_INPUT must equal a direct decide() call."""
    f = Path(__file__).parent / "samples" / "claim_next.json"
    proc = _run_cli(env={"ORGAN_INPUT": str(f)})
    data = json.loads(f.read_text())
    expected = decide(data["state"], data.get("context"))
    assert json.loads(proc.stdout) == expected


def test_cli_organ_input_precedes_stdin():
    """A set ORGAN_INPUT wins over piped stdin (file is the explicit override)."""
    f = Path(__file__).parent / "samples" / "spec_complete.json"
    proc = _run_cli(env={"ORGAN_INPUT": str(f)}, stdin_text='{"state": {}}')
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["output"]["decision"] == "spec_complete"


def test_cli_reads_stdin_when_no_organ_input():
    f = Path(__file__).parent / "samples" / "claim_next.json"
    proc = _run_cli(env={"ORGAN_INPUT": ""}, stdin_text=f.read_text())
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["output"]["decision"] == "claim"


# ---------------------------------------------------------------------------
# Connection Standard — ports.json + the type vocabulary (CONNECTORS.md).
# The Lego *stud* check: an organ declares typed ports, every type exists in
# the shared vocabulary, and decide() actually reads each declared input name
# and writes each declared output name.
# ---------------------------------------------------------------------------

_PORTS = Path(__file__).parent / "ports.json"
_TYPES = Path(__file__).parent / "types.json"


def test_ports_json_parses_and_is_a_manifest():
    ports = json.loads(_PORTS.read_text())
    assert isinstance(ports, dict)
    assert isinstance(ports.get("inputs"), list)
    assert isinstance(ports.get("outputs"), list)
    assert ports["outputs"], "an organ with no output port is unconnectable"
    for label in ("inputs", "outputs"):
        for p in ports[label]:
            assert isinstance(p.get("name"), str) and p["name"]
            assert isinstance(p.get("type"), str) and p["type"]


def test_every_declared_type_exists_in_vocabulary():
    ports = json.loads(_PORTS.read_text())
    vocab = json.loads(_TYPES.read_text())["types"]
    for label in ("inputs", "outputs"):
        for p in ports[label]:
            assert p["type"] in vocab, (
                f"{label} port {p['name']!r} type {p['type']!r} not in types.json"
            )


def test_decide_reads_every_declared_input_name():
    ports = json.loads(_PORTS.read_text())
    src = (Path(__file__).parent / "organ.py").read_text()
    for p in ports["inputs"]:
        name = p["name"]
        assert (
            f'state.get("{name}"' in src
            or f'state["{name}"]' in src
        ), f"decide() never reads state[{name!r}]"


def test_decide_writes_every_declared_output_name():
    ports = json.loads(_PORTS.read_text())
    sample_dir = Path(__file__).parent / "samples"
    runs = [decide({}, {})]
    for f in sorted(sample_dir.glob("*.json")):
        data = json.loads(f.read_text())
        runs.append(decide(data.get("state") or {}, data.get("context") or {}))
    for r in runs:
        out = r["output"]
        for p in ports["outputs"]:
            assert p["name"] in out, f"decide() did not write output[{p['name']!r}]"


def test_proposed_types_are_listed_for_review():
    """Types this organ minted must be flagged as proposed (not silently added)."""
    types_doc = json.loads(_TYPES.read_text())
    proposed = types_doc.get("_proposed", [])
    vocab = types_doc["types"]
    ports = json.loads(_PORTS.read_text())
    declared = {p["type"] for p in ports["inputs"] + ports["outputs"]}
    # Every declared type the seed vocabulary lacked is announced in _proposed.
    for name in declared:
        entry = vocab[name]
        if isinstance(entry, dict) and str(entry.get("_status", "")).startswith("PROPOSED"):
            assert name in proposed, f"{name} is PROPOSED but missing from _proposed"


def test_check_ports_script_passes():
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "check_ports.py")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"check_ports.py failed:\n{proc.stdout}\n{proc.stderr}"
