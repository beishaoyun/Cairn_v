#!/usr/bin/env python3
"""Mock worker script (Agent 31) — deterministic contract validator.

This is the actual executable that ``MockDriver`` runs. It is a pure-stdlib
script (no cairn imports), so it can be launched with ``sys.executable`` from
the driver. It reads the rendered prompt from ``argv[1]`` (a temp file written
by the driver), detects the task phase via the ``mock-phase:`` marker, reads the
``MOCK_<PHASE>`` JSON env var (delay / outcomes / payload / rules), selects an
outcome (rules first, then weighted probability), and prints the phase's JSON
contract on stdout.

It never touches a real LLM, the network, or mitmproxy — it is a contract
validator for end-to-end regression (verify-mock-test-spec.md §1).

Usage (invoked by MockDriver):
    python3 _mock_script.py <prompt-file>
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time

_PHASE_RE = re.compile(r"mock-phase:\s*([A-Za-z_]+)")
_STAGE_RE = re.compile(r"mock-stage:\s*([A-Za-z_]+)")


def _load_defaults() -> dict:
    """Defaults table injected by the driver via CAIRN_MOCK_DEFAULTS (optional)."""
    raw = os.environ.get("CAIRN_MOCK_DEFAULTS", "")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


_DEFAULTS = _load_defaults()


def _phase_defaults(phase: str) -> dict:
    d = _DEFAULTS.get(phase) or {}
    return {
        "delay": d.get("delay", [0.0, 0.0]),
        "outcomes": d.get("outcomes", {}),
    }


def detect_phase(prompt: str) -> str | None:
    m = _PHASE_RE.search(prompt)
    if m:
        return m.group(1).lower()
    # Heuristic fallback (used only when the marker is absent).
    low = prompt.lower()
    if "explore" in low and "conclude" in low:
        return "explore_conclude"
    if "bootstrap" in low and "conclude" in low:
        return "bootstrap_conclude"
    if "observations" in low and "verdict" not in low:
        return "verify"
    if "verdict" in low:
        return "verify"
    if "replay" in low or "remediated" in low or "unchanged" in low:
        return "replay"
    if "recommend_finalize" in low or "coverage gap" in low:
        return "reason"
    if "audit" in low:
        return "audit"
    if "discoveries" in low:
        return "bootstrap"
    if "coverage" in low or "findings" in low:
        return "explore_execute"
    return None


def detect_stage(prompt: str) -> str | None:
    m = _STAGE_RE.search(prompt)
    if m:
        return m.group(1).lower()
    low = prompt.lower()
    if "observations" in low and "verdict" not in low:
        return "blind"
    if "verdict" in low or "stage" in low:
        return "comparison"
    return None


def rule_matches(rule: dict, prompt: str) -> bool:
    if "prompt_has" in rule:
        needle = rule["prompt_has"]
        if isinstance(needle, str):
            if needle not in prompt:
                return False
        elif isinstance(needle, list):
            if not all(n in prompt for n in needle):
                return False
        else:
            return False
    if "prompt_has_any" in rule:
        needles = rule["prompt_has_any"]
        if not any(n in prompt for n in needles):
            return False
    if "prompt_has_none" in rule:
        for n in rule["prompt_has_none"]:
            if n in prompt:
                return False
    return True


def select_outcome(phase: str, cfg: dict, prompt: str) -> str | None:
    """Apply rules first; otherwise weighted-random among the configured outcomes."""
    for rule in cfg.get("rules") or []:
        if rule_matches(rule, prompt):
            if "force" in rule:
                return rule["force"]
            return None  # matched without a force -> fall through to probability
    outcomes = cfg.get("outcomes") or _phase_defaults(phase)["outcomes"]
    if not outcomes:
        return None
    total = sum(float(v) for v in outcomes.values())
    if total <= 0:
        return None
    r = random.random() * total
    acc = 0.0
    for name, prob in outcomes.items():
        acc += float(prob)
        if r <= acc:
            return name
    return list(outcomes.keys())[-1]


def merge_payload(cfg: dict, prompt: str) -> dict:
    """Base payload shallow-merged with every matching rule's payload."""
    payload = dict(cfg.get("payload") or {})
    for rule in cfg.get("rules") or []:
        if rule_matches(rule, prompt) and rule.get("payload"):
            payload.update(rule["payload"])
    return payload


def apply_delay(cfg: dict, phase: str) -> None:
    delay = cfg.get("delay")
    if delay is None:
        delay = _phase_defaults(phase)["delay"]
    if not isinstance(delay, (list, tuple)) or len(delay) != 2:
        return
    try:
        lo, hi = float(delay[0]), float(delay[1])
    except (TypeError, ValueError):
        return
    if lo < 0 or hi < lo:
        return
    if hi > lo:
        time.sleep(random.uniform(lo, hi))
    elif hi > 0:
        time.sleep(hi)


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _emit_rejected() -> None:
    _emit({"accepted": False, "reason": "mock_rejected"})


def _emit_verify(outcome: str, payload: dict, stage: str | None) -> None:
    if stage == "blind":
        observations = payload.get("observations")
        if observations is None:
            observations = [
                {
                    "vuln": "mock blind observation",
                    "severity": "high",
                    "traffic_id": "tr-001",
                    "basis": "mock: digest shows request/response",
                }
            ]
        _emit(
            {
                "accepted": True,
                "data": {
                    "observations": observations,
                    "traffic_note": payload.get("traffic_note", ""),
                },
            }
        )
        return
    # comparison stage
    if outcome in ("confirmed", "rejected", "needs_more_evidence"):
        data = {"stage": "comparison", "verdict": outcome, **payload}
        _emit({"accepted": True, "data": data})
    else:
        # accepted_false and any unknown comparison outcome → accepted=false
        _emit_rejected()


def _emit_replay(outcome: str, payload: dict) -> None:
    matched = payload.get("matched_original")
    try:
        matched = int(matched) if matched is not None else 0
    except (TypeError, ValueError):
        matched = 0
    data = {"result": outcome, "matched_original": matched, **payload}
    _emit({"accepted": True, "data": data})


def _coverage_outcome() -> str:
    """Pick the explore coverage_result.outcome from MOCK_EXPLORE_COVERAGE_OUTCOME
    (spec prompts §9) when the explore payload does not pin it."""
    raw = os.environ.get("MOCK_EXPLORE_COVERAGE_OUTCOME", "")
    if raw:
        try:
            cfg = json.loads(raw)
        except ValueError:
            cfg = {}
        outcomes = cfg.get("outcomes") or {}
        if outcomes:
            total = sum(float(v) for v in outcomes.values())
            if total > 0:
                r = random.random() * total
                acc = 0.0
                for name, prob in outcomes.items():
                    acc += float(prob)
                    if r <= acc:
                        return name
                return list(outcomes)[-1]
    return "no_issue"


def _emit_explore(outcome: str, payload: dict) -> None:
    data = {"description": payload.get("description", "mock explore"), **payload}
    if "coverage" not in data:
        data["coverage"] = {
            "covered_items": payload.get("covered_items", ["c-001"]),
            "depth_achieved": payload.get("depth_achieved", "standard"),
            "outcome": payload.get("coverage_outcome") or _coverage_outcome(),
        }
    _emit({"accepted": True, "data": data})


def _emit_reason(outcome: str, payload: dict) -> None:
    if outcome == "finalize":
        data = {
            "intents": [],
            "coverage": {
                "recommend_finalize": True,
                "reason": payload.get("reason", "mock: remaining gaps are low-value"),
                "waivers": payload.get("waivers", []),
            },
        }
        data.update(payload)
        _emit({"accepted": True, "data": data})
        return
    data = {
        "intents": payload.get(
            "intents",
            [{"from": ["f001"], "description": "mock intent", "coverage_item_ids": ["c-001"]}],
        ),
        "coverage": {"recommend_finalize": False, "reason": ""},
    }
    data.update(payload)
    _emit({"accepted": True, "data": data})


def _emit_bootstrap(outcome: str, payload: dict) -> None:
    data = {
        "fact": payload.get("fact", {"description": payload.get("fact_description", "mock recon")}),
        "sweep_complete": {"description": "initial sweep done, attack surface recorded"},
        "discoveries": payload.get("discoveries", []),
        "coverage": {"outcome": "no_issue"},
    }
    _emit({"accepted": True, "data": data})


def _emit_audit(outcome: str, payload: dict) -> None:
    data = {
        "verdict": "covered" if outcome == "covered" else "discrepancy",
        "reason": payload.get("reason", "mock audit"),
        **payload,
    }
    _emit({"accepted": True, "data": data})


def _emit_healthcheck(outcome: str, payload: dict) -> None:
    status = "ok" if outcome != "fail" else "fail"
    _emit({"accepted": True, "data": {"status": status}})


def main() -> int:
    if len(sys.argv) < 2:
        print("mock: missing prompt file argument", file=sys.stderr)
        return 2
    prompt_file = sys.argv[1]
    try:
        with open(prompt_file, "r", encoding="utf-8") as fh:
            prompt = fh.read()
    except OSError as exc:
        print(f"mock: cannot read prompt file: {exc}", file=sys.stderr)
        return 2
    finally:
        # best-effort cleanup of the driver-written prompt file
        try:
            os.remove(prompt_file)
        except OSError:
            pass

    phase = detect_phase(prompt)
    if phase is None:
        print("mock: cannot detect phase from prompt", file=sys.stderr)
        return 2

    key = f"MOCK_{phase.upper()}"
    raw = os.environ.get(key, "")
    if raw:
        try:
            cfg = json.loads(raw)
        except ValueError as exc:
            print(f"mock: invalid {key} JSON: {exc}", file=sys.stderr)
            return 2
    else:
        cfg = {}

    outcome = select_outcome(phase, cfg, prompt)
    if outcome is None:
        defaults = _phase_defaults(phase)["outcomes"] or {"ok": "1.0"}
        outcome = next(iter(defaults))

    payload = merge_payload(cfg, prompt)

    # meta outcomes shared by every phase
    if outcome == "invalid_json":
        print("{invalid json")
        return 0
    if outcome == "empty":
        return 0
    if outcome == "command_fail":
        print("mock: command failure", file=sys.stderr)
        return 1
    if outcome == "rejected" and phase not in ("verify", "replay"):
        _emit_rejected()
        return 0

    apply_delay(cfg, phase)

    if phase == "verify":
        _emit_verify(outcome, payload, detect_stage(prompt) or "comparison")
    elif phase == "replay":
        _emit_replay(outcome, payload)
    elif phase in ("explore_execute", "explore_conclude"):
        _emit_explore(outcome, payload)
    elif phase == "reason":
        _emit_reason(outcome, payload)
    elif phase in ("bootstrap", "bootstrap_conclude"):
        _emit_bootstrap(outcome, payload)
    elif phase == "audit":
        _emit_audit(outcome, payload)
    elif phase == "healthcheck":
        _emit_healthcheck(outcome, payload)
    else:
        _emit({"accepted": True, "data": {}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
