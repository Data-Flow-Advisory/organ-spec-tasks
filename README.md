# organ-spec-tasks

A pure, stdlib-only **decision organ** extracted from discovery-engine's
`app/services/spec_tasks.py` (Phase 3 of Matt's AI-driven spec pipeline —
task-level claim/complete sequencing).

## What is an organ?

A small, self-contained decision-maker conforming to the orchestrator
pure-organ contract:

```
decide(state, context) -> {"output", "rationale", "self_metric"}
```

- **Pure** — no DB, network, filesystem, env reads, or clock. Everything
  arrives via `state` / `context`.
- **Deterministic** — same input always yields the same output.
- **Fail-safe** — never raises; bad/empty input returns a valid structure with
  low `confidence` and an explanatory `rationale`.
- **Stdlib-only** — Python standard library only.
- **`self_metric.confidence`** is a float in `[0.0, 1.0]`.

## What this organ decides

Given a technical spec's task graph (tasks with dependencies + per-task state)
and its approval flag, it decides the **single next action** a worker should
take against the spec. `output.decision` is one of:

| decision        | meaning |
|-----------------|---------|
| `not_approved`  | Spec isn't approved — nothing is claimable. (Mirrors `claim_next_task` raising `PermissionError`.) |
| `no_tasks`      | The spec defines no tasks. |
| `claim`         | The next task to claim: first `pending` task whose every dependency is in a terminal "done" state. `output.next_task` carries it. |
| `spec_complete` | Every task is in a terminal "done" state → the parent spec should roll up to `resolved`. |
| `in_progress`   | Nothing claimable right now, but work is live (≥1 task `in_progress`) — wait. |
| `blocked`       | Nothing claimable, nothing live, spec incomplete: a dependency is `failed`/unknown, or the remaining pending tasks form a dependency deadlock. Needs reset/operator intervention. |

### Load-bearing invariant carried over from the live wire

`completed_pending_review` counts as **done** for both the dependency check
*and* the completion roll-up. A task whose result missed the acceptance
heuristic still unblocks its dependents and still lets the spec roll up — the
miss is tracked separately via a `spec_acceptance_review` surface. Treating it
as not-done was the bug behind discovery-engine items **12640 / 12629**, so the
organ pins it with `DONE_STATUSES = {"completed", "completed_pending_review"}`
and a regression test.

## Usage

### As a library

```python
from organ import decide

state = {
    "approved": True,
    "tasks": [
        {"id": "T1", "dependencies": []},
        {"id": "T2", "dependencies": ["T1"]},
    ],
    "task_states": {"T1": {"status": "completed"}},
}
result = decide(state, {})
# result["output"]["decision"] == "claim"
# result["output"]["next_task"]["id"] == "T2"
```

### As a CLI

```bash
python organ.py < samples/claim_next.json
# or, per the orchestrator CONTRACT, point ORGAN_INPUT at the payload file:
ORGAN_INPUT=samples/claim_next.json python organ.py
```

Reads the `{state, context}` payload from the file named by `$ORGAN_INPUT`
when that env var is set (it takes precedence), otherwise from stdin, and
writes `{output, rationale, self_metric}` to stdout — so the orchestrator can
shell out to it like any other organ.

## State schema

| key           | type | default | meaning |
|---------------|------|---------|---------|
| `approved`    | bool | `False` | Has the spec been approved (the hard gate). |
| `tasks`       | list | `[]`    | Task definitions: `{id, title, dependencies, acceptance_criteria, complexity, suggested_model, ...}`. Entries without a truthy `id` are dropped. |
| `task_states` | dict | `{}`    | `{task_id: {"status": ...}}`. A task with no slot defaults to `pending`. Statuses: `pending`, `in_progress`, `completed`, `completed_pending_review`, `failed`. |

## Connection ports (the Lego stud)

Beyond the uniform `decide(...)` *shape*, this organ declares a typed
`ports.json` manifest so the composer can wire it to other organs **by type**,
not by hand-written adapter (see the orchestrator's `CONNECTORS.md`). Each
port's `name` is the literal wiring address — an input name is the key `decide`
reads under `state`, an output name the key it writes under `output` — and each
`type` is a name from the shared vocabulary (`types.json`). Two ports connect
iff their `type` matches.

| direction | name          | type               | required |
|-----------|---------------|--------------------|----------|
| input     | `approved`    | `ApprovalGate`     | yes      |
| input     | `tasks`       | `SpecTaskGraph`    | yes      |
| input     | `task_states` | `TaskStateMap`     | no       |
| output    | `decision`    | `SpecTaskDecision` | —        |

`decide` produces one logical product — the next-action decision — whose flat
output keys (`decision`, `next_task`, `total_tasks`, `completed_tasks`,
`completion_pct`, `blocked_task_ids`) are facets of that single product,
modelled as one output port `decision` typed `SpecTaskDecision` (its schema
enumerates the full shape).

**The spec-task pipeline domain was absent from the seed vocabulary**, so this
PR mints four new types — `SpecTaskGraph`, `TaskStateMap`, `ApprovalGate`,
`SpecTaskDecision` — listed under `types.json._proposed` and **proposed for
review/upstreaming** into the orchestrator vocabulary. `types.json` here is a
pinned copy of the orchestrator vocabulary plus these additions, so the
`conformance` Action can validate ports **offline** (stdlib-only, no network).

`check_ports.py` is the offline port check (also run by the `conformance`
Action): it asserts `ports.json` parses, every declared port type exists in
`types.json`, and `decide` actually reads each declared input name and writes
each declared output name (sampled against the organ's own samples).

```bash
python check_ports.py
```

## Exported pure helpers

Besides `decide`, the module exports the building blocks (all pure):

- `deps_satisfied(task, states)` — are all of a task's deps terminal-done?
- `next_claimable_task(tasks, states)` — first eligible pending task, or `None`.
- `spec_complete(tasks, states)` — are all tasks done (and ≥1 task)?
- `completion(tasks, states)` — `{total, completed, completion_pct}`.
- `blocked_pending_tasks(tasks, states)` — pending task ids with unmet deps.
- `evaluate_acceptance_criteria(criteria, result_text)` — the acceptance-criteria
  heuristic ported verbatim (`{criteria_count, addressed, missed,
  fraction_addressed, passed}`), with its `salient_tokens(text)` tokenizer.

## Tests & conformance

```bash
python -m pip install pytest
python -m pytest test_organ.py -v
```

The GitHub `conformance` Action runs the suite across Python 3.10–3.12, checks
the `decide(state, context)` signature, fail-safe behaviour, determinism,
stdlib-only imports, the ports manifest (`check_ports.py` — see "Connection
ports" above), and renders each sample into the job summary.

## Provenance

Extracted from `Data-Flow-Advisory/discovery-engine`
`app/services/spec_tasks.py`. The live wire mutates a
`PendingWidgetAction.context_json` SQLAlchemy row in place; this organ isolates
the **pure sequencing decision** (which task next / complete / blocked) from
all the persistence, audit, and rubric side effects so it can be unit-tested
and composed in orchestrator pipelines.
