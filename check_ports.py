#!/usr/bin/env python3
"""Port-conformance check for organ-spec-tasks (the Connection Standard).

Extends the pure-organ contract check with the Lego *stud* check from
``CONNECTORS.md``: an organ must declare a typed ``ports.json`` manifest, every
declared type must exist in the shared vocabulary (``types.json``), and the
organ's ``decide`` must actually read each declared input name from ``state``
and write each declared output name under ``output``.

Stdlib-only, no network — validates against the repo's pinned ``types.json``
copy so it runs offline in CI. Exit 0 = green, non-zero = the organ is
connectable-by-luck-only (CONNECTORS.md flags this amber/red).

Usage:  python check_ports.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
PORTS_PATH = ROOT / "ports.json"
TYPES_PATH = ROOT / "types.json"
ORGAN_PATH = ROOT / "organ.py"
SAMPLES_DIR = ROOT / "samples"


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def _load_json(path: Path, label: str) -> Any:
    if not path.exists():
        _fail(f"{label} not found at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"{label} does not parse as JSON: {exc}")


def _check_port_list(ports: Any, label: str) -> List[Dict[str, Any]]:
    if not isinstance(ports, list):
        _fail(f"ports.json '{label}' must be a list")
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(ports):
        if not isinstance(p, dict):
            _fail(f"ports.json {label}[{i}] is not an object")
        name = p.get("name")
        ptype = p.get("type")
        if not isinstance(name, str) or not name:
            _fail(f"ports.json {label}[{i}] missing a non-empty string 'name'")
        if not isinstance(ptype, str) or not ptype:
            _fail(f"ports.json {label}[{i}] ({name!r}) missing a non-empty string 'type'")
        out.append(p)
    return out


def main() -> int:
    # 1. ports.json parses and is structurally a {inputs:[], outputs:[]} manifest.
    ports = _load_json(PORTS_PATH, "ports.json")
    if not isinstance(ports, dict):
        _fail("ports.json top-level must be an object")
    inputs = _check_port_list(ports.get("inputs", []), "inputs")
    outputs = _check_port_list(ports.get("outputs", []), "outputs")
    if not outputs:
        _fail("ports.json declares no outputs — an organ with no output port is unconnectable")
    print(f"OK ports.json parses: {len(inputs)} input(s), {len(outputs)} output(s)")

    # 2. types.json parses and every declared port type exists in the vocabulary.
    types_doc = _load_json(TYPES_PATH, "types.json")
    vocab = types_doc.get("types") if isinstance(types_doc, dict) else None
    if not isinstance(vocab, dict) or not vocab:
        _fail("types.json has no 'types' object")
    vocab_names = set(vocab.keys())
    for label, port_list in (("input", inputs), ("output", outputs)):
        for p in port_list:
            if p["type"] not in vocab_names:
                _fail(
                    f"{label} port {p['name']!r} declares type {p['type']!r} "
                    f"which is not in the vocabulary (types.json). "
                    f"Add it to types.json (and propose it upstream) or pick an existing type."
                )
    print(f"OK every port type exists in the vocabulary ({len(vocab_names)} types)")

    # 3. decide reads each declared input name from `state`.
    #    Evidence: the organ source accesses the key off `state`, AND the name
    #    appears in at least one sample's state (sampled against the organ's
    #    own samples, per CONNECTORS.md).
    organ_src = ORGAN_PATH.read_text(encoding="utf-8") if ORGAN_PATH.exists() else ""
    if not organ_src:
        _fail("organ.py not found — cannot verify the contract")

    sample_states: List[Dict[str, Any]] = []
    sample_files = sorted(SAMPLES_DIR.glob("*.json")) if SAMPLES_DIR.exists() else []
    for sf in sample_files:
        data = _load_json(sf, f"sample {sf.name}")
        if isinstance(data, dict) and isinstance(data.get("state"), dict):
            sample_states.append(data["state"])

    for p in inputs:
        name = p["name"]
        reads_in_source = (
            f'state.get("{name}"' in organ_src
            or f"state.get('{name}'" in organ_src
            or f'state["{name}"]' in organ_src
            or f"state['{name}']" in organ_src
        )
        if not reads_in_source:
            _fail(
                f"input port {name!r} is declared but decide() never reads "
                f"state[{name!r}] in organ.py"
            )
        if sample_states and not any(name in s for s in sample_states):
            _fail(
                f"input port {name!r} is declared but appears in no sample's "
                f"state — add a sample exercising it"
            )
    print(f"OK decide() reads every declared input name ({[p['name'] for p in inputs]})")

    # 4. decide writes each declared output name under `output`.
    #    Runtime evidence: run decide on empty state + every sample and assert
    #    the declared output names are present in result['output'].
    sys.path.insert(0, str(ROOT))
    try:
        from organ import decide  # noqa: E402
    except Exception as exc:  # pragma: no cover - import failure is a hard fail
        _fail(f"could not import decide from organ.py: {exc}")

    runs: List[Dict[str, Any]] = [decide({}, {})]
    for sf in sample_files:
        data = _load_json(sf, f"sample {sf.name}")
        st = data.get("state") if isinstance(data, dict) else None
        ctx = data.get("context") if isinstance(data, dict) else None
        runs.append(decide(st or {}, ctx or {}))

    for r in runs:
        out = r.get("output")
        if not isinstance(out, dict):
            _fail(f"decide() returned a non-dict output: {out!r}")
        for p in outputs:
            if p["name"] not in out:
                _fail(
                    f"output port {p['name']!r} is declared but decide() did "
                    f"not write it under output (keys: {sorted(out)})"
                )
    print(f"OK decide() writes every declared output name ({[p['name'] for p in outputs]})")

    print("\nPORTS CONFORMANCE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
