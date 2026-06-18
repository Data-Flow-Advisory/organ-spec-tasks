#!/usr/bin/env python3
"""Connection-Standard conformance for organ-spec-tasks.

Validates this organ's typed-port manifest (``ports.json``) against the
orchestrator Connection Standard (CONNECTORS.md):

  1. ``ports.json`` parses and has the declared structure
     (``inputs`` with name/type/required, ``outputs`` with name/type).
  2. Every port ``type`` exists in the shared vocabulary (``types.json``).
  3. ``decide`` actually **reads** each declared input ``name`` (under
     ``state``) and **writes** each declared output ``name`` (under
     ``output``) — the latter sampled against the organ's own samples,
     per the standard ("sampled against the organ's own samples").

Pure stdlib, no network. Run directly (``python check_ports.py``) — exits
non-zero with an explanatory message on the first failure — or import
``verify_ports()`` from a test.
"""
from __future__ import annotations

import ast
import json
import os
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_PORTS_PATH = os.path.join(_HERE, "ports.json")
_TYPES_PATH = os.path.join(_HERE, "types.json")
_ORGAN_PATH = os.path.join(_HERE, "organ.py")
_SAMPLES_DIR = os.path.join(_HERE, "samples")


class PortError(AssertionError):
    """Raised when the ports manifest violates the Connection Standard."""


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _vocabulary_type_names() -> set:
    voc = _load_json(_TYPES_PATH)
    if not isinstance(voc, dict) or not isinstance(voc.get("types"), dict):
        raise PortError("types.json must be an object with a 'types' map")
    return set(voc["types"].keys())


def _state_names_read_by_decide() -> set:
    """Return the literal keys ``decide`` reads off ``state`` via ``state.get(...)``.

    Parses ``organ.py`` (no import / no execution needed) and collects every
    ``<something>.get("KEY"...)`` string-literal first argument. The organ
    funnels all state access through a local ``state`` dict and the helper
    ``states`` (task_states), so this captures the declared input names
    directly from source — the standard's "reads each declared input name".
    """
    tree = ast.parse(open(_ORGAN_PATH, encoding="utf-8").read())
    names: set = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def _output_keys_over_samples() -> set:
    """Return the union of keys written under ``output`` across all samples.

    Runs the real ``decide`` on every sample so the check reflects actual
    behaviour, not a static promise.
    """
    from organ import decide  # local import: organ.py is pure + stdlib-only

    keys: set = set()
    samples = sorted(
        f for f in os.listdir(_SAMPLES_DIR) if f.endswith(".json")
    )
    if not samples:
        raise PortError("no samples found to validate output ports against")
    for fname in samples:
        payload = _load_json(os.path.join(_SAMPLES_DIR, fname))
        state = payload.get("state") or {}
        context = payload.get("context") or {}
        result = decide(state, context)
        out = result.get("output")
        if not isinstance(out, dict):
            raise PortError(f"sample {fname}: decide().output is not a dict")
        keys |= set(out.keys())
    return keys


def verify_ports() -> Dict[str, Any]:
    """Run every port check. Raise :class:`PortError` on the first failure.

    Returns a small summary dict on success (handy for tests / logging).
    """
    ports = _load_json(_PORTS_PATH)
    if not isinstance(ports, dict):
        raise PortError("ports.json must be a JSON object")

    inputs = ports.get("inputs")
    outputs = ports.get("outputs")
    if not isinstance(inputs, list) or not isinstance(outputs, list):
        raise PortError("ports.json must have list 'inputs' and 'outputs'")
    if not inputs or not outputs:
        raise PortError("ports.json must declare at least one input and output")

    vocab = _vocabulary_type_names()

    def _check_ports(ports_list: List[Any], kind: str, need_required: bool):
        seen = set()
        for p in ports_list:
            if not isinstance(p, dict):
                raise PortError(f"{kind} port is not an object: {p!r}")
            name, typ = p.get("name"), p.get("type")
            if not isinstance(name, str) or not name:
                raise PortError(f"{kind} port missing string 'name': {p!r}")
            if name in seen:
                raise PortError(f"duplicate {kind} port name: {name!r}")
            seen.add(name)
            if not isinstance(typ, str) or not typ:
                raise PortError(f"{kind} port {name!r} missing string 'type'")
            if typ not in vocab:
                raise PortError(
                    f"{kind} port {name!r} type {typ!r} not in the vocabulary "
                    f"(types.json). Add it to the shared vocabulary or map to an "
                    f"existing type."
                )
            if need_required and not isinstance(p.get("required"), bool):
                raise PortError(
                    f"input port {name!r} must declare a boolean 'required'"
                )
        return seen

    input_names = _check_ports(inputs, "input", need_required=True)
    output_names = _check_ports(outputs, "output", need_required=False)

    # (3a) decide reads each declared input name off `state`.
    reads = _state_names_read_by_decide()
    missing_reads = sorted(n for n in input_names if n not in reads)
    if missing_reads:
        raise PortError(
            f"ports.json declares input(s) {missing_reads} that decide() never "
            f"reads via state.get(...). Reads found: {sorted(reads)}"
        )

    # (3b) decide writes each declared output name under `output` (sampled).
    writes = _output_keys_over_samples()
    missing_writes = sorted(n for n in output_names if n not in writes)
    if missing_writes:
        raise PortError(
            f"ports.json declares output(s) {missing_writes} that decide() never "
            f"writes under output across the samples. Writes found: "
            f"{sorted(writes)}"
        )

    return {
        "inputs": sorted(input_names),
        "outputs": sorted(output_names),
        "vocabulary_size": len(vocab),
    }


def main() -> int:
    try:
        summary = verify_ports()
    except (PortError, OSError, json.JSONDecodeError) as exc:
        print(f"FAIL ports conformance: {exc}")
        return 1
    print(
        "OK ports conformance — "
        f"inputs={summary['inputs']} outputs={summary['outputs']} "
        f"(vocabulary: {summary['vocabulary_size']} types)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
