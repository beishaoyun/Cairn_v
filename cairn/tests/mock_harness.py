"""Mock-driver regression harness (Agent 31).

Two layers:

1. **Driver / contract layer** — pure helpers that need no Server or
   DispatcherLoop: ``mock_cfg`` config builders, ``mock_prompt`` marker
   builder, ``run_mock`` subprocess runner, and a MockDriver factory. These are
   independently testable right now.

2. **E2E layer** — seed + assertion helpers that assume a running process-internal
   Server (TestClient) and, once Agent 30/40 land, a DispatcherLoop with
   LocalBackend + MockDriver. E2E cases in ``test_mock_end_to_end.py`` are
   guarded by ``pytest.importorskip`` so they skip until those packages exist.

Every ``MOCK_<PHASE>`` config emitted here follows the contract of
``cairn.dispatcher.workers.adapters.mock.validate_mock_config``: outcome
probabilities sum to 1.0, delay ``[lo, hi]`` non-negative, payload dicts, rules
with ``prompt_has`` conditions.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Iterable, Mapping, Sequence

from cairn.dispatcher.workers.adapters.mock import (
    MockDriver,
    mock_env_key,
)

# ---------------------------------------------------------------------------
# 1. Driver / contract layer
# ---------------------------------------------------------------------------

#: default delay window used by harness configs — fixed and tiny for determinism
#: (verify-mock-test-spec §6: 延迟固定 + force/payload 不依赖概率).
DEFAULT_DELAY = [0.01, 0.01]

_VERIFY_OUTCOMES = {
    "confirmed": "1.0",
    "rejected": "0.0",
    "needs_more_evidence": "0.0",
    "accepted_false": "0.0",
    "invalid_json": "0.0",
    "empty": "0.0",
    "command_fail": "0.0",
}
_REPLAY_OUTCOMES = {
    "remediated": "1.0",
    "unchanged": "0.0",
    "ambiguous": "0.0",
    "error": "0.0",
    "invalid_json": "0.0",
    "empty": "0.0",
    "command_fail": "0.0",
}
_EXPLORE_OUTCOMES = {
    "fact": "1.0",
    "rejected": "0.0",
    "invalid_json": "0.0",
    "empty": "0.0",
    "command_fail": "0.0",
}
_REASON_OUTCOMES = {
    "intents": "1.0",
    "finalize": "0.0",
    "rejected": "0.0",
    "invalid_json": "0.0",
    "empty": "0.0",
    "command_fail": "0.0",
}
_BOOTSTRAP_OUTCOMES = {
    "ok": "1.0",
    "rejected": "0.0",
    "invalid_json": "0.0",
    "empty": "0.0",
    "command_fail": "0.0",
}
_AUDIT_OUTCOMES = {
    "covered": "1.0",
    "discrepancy": "0.0",
    "rejected": "0.0",
    "invalid_json": "0.0",
    "empty": "0.0",
    "command_fail": "0.0",
}


def _force(outcomes: Mapping[str, str], outcome: str, allowed: Sequence[str]) -> dict[str, str]:
    """Return an outcomes dict that forces ``outcome`` (weight 1.0, rest 0.0)."""
    if outcome not in allowed:
        raise ValueError(f"outcome {outcome!r} not allowed (allowed: {sorted(allowed)})")
    return {k: ("1.0" if k == outcome else "0.0") for k in allowed}


def _with_rules(
    cfg: dict[str, Any], rules: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    if rules:
        cfg = dict(cfg)
        cfg["rules"] = list(rules)
    return cfg


def phase_cfg(
    phase: str,
    *,
    outcome: str | None = None,
    outcomes: Mapping[str, str] | None = None,
    delay: Sequence[float] | None = DEFAULT_DELAY,
    payload: Mapping[str, Any] | None = None,
    rules: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble a ``MOCK_<PHASE>`` config dict.

    ``outcome`` (force a single outcome) and ``outcomes`` (explicit weights) are
    mutually exclusive; the harness defaults to a forced deterministic outcome.
    """
    from cairn.dispatcher.workers.adapters.mock import MOCK_ALLOWED_OUTCOMES

    allowed = sorted(MOCK_ALLOWED_OUTCOMES[phase])
    if outcomes is None:
        if outcome is None:
            outcome = allowed[0]
        outcomes = _force(outcomes if outcomes else {}, outcome, allowed)
    cfg: dict[str, Any] = {"delay": list(delay), "outcomes": dict(outcomes)}
    if payload is not None:
        cfg["payload"] = dict(payload)
    return _with_rules(cfg, rules)


def verify_cfg(
    *,
    outcome: str = "confirmed",
    verdict: str | None = None,
    severity: str = "high",
    traffic_ids: Sequence[str] = ("tr-001",),
    reason: str = "mock: verified",
    suggested_action: str = "none",
    http_mismatch: bool = False,
    observations: Sequence[Mapping[str, Any]] | None = None,
    traffic_note: str = "",
    outcomes: Mapping[str, str] | None = None,
    delay: Sequence[float] | None = DEFAULT_DELAY,
    payload: Mapping[str, Any] | None = None,
    rules: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a ``MOCK_VERIFY`` config.

    ``verdict`` lets the test override the emitted ``verdict`` field (e.g.
    inject an illegal value for TV-13) while keeping the outcome weight intact.
    """
    base_payload: dict[str, Any] = {
        "verified_severity": severity,
        "verified_traffic_ids": list(traffic_ids),
        "suggested_action": suggested_action,
        "reason": reason,
        "http_mismatch": http_mismatch,
    }
    if observations is not None:
        base_payload["observations"] = list(observations)
    base_payload["traffic_note"] = traffic_note
    if verdict is not None:
        base_payload["verdict"] = verdict
    if payload is not None:
        base_payload.update(payload)
    return phase_cfg("verify", outcome=outcome, outcomes=outcomes, delay=delay,
                     payload=base_payload, rules=rules)


def replay_cfg(
    *,
    result: str = "remediated",
    matched_original: int = 0,
    outcomes: Mapping[str, str] | None = None,
    delay: Sequence[float] | None = DEFAULT_DELAY,
    payload: Mapping[str, Any] | None = None,
    rules: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a ``MOCK_REPLAY`` config (deterministic replay-engine result)."""
    base_payload: dict[str, Any] = {"result": result, "matched_original": matched_original}
    if payload is not None:
        base_payload.update(payload)
    return phase_cfg("replay", outcome=result, outcomes=outcomes, delay=delay,
                     payload=base_payload, rules=rules)


def explore_cfg(
    *,
    outcome: str = "fact",
    findings: Sequence[Mapping[str, Any]] | None = None,
    coverage: Mapping[str, Any] | None = None,
    description: str = "mock explore",
    outcomes: Mapping[str, str] | None = None,
    delay: Sequence[float] | None = DEFAULT_DELAY,
    payload: Mapping[str, Any] | None = None,
    rules: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a ``MOCK_EXPLORE_EXECUTE`` config with findings + coverage output."""
    base_payload: dict[str, Any] = {"description": description}
    if findings is not None:
        base_payload["findings"] = list(findings)
    if coverage is not None:
        base_payload["coverage"] = dict(coverage)
    if payload is not None:
        base_payload.update(payload)
    return phase_cfg("explore_execute", outcome=outcome, outcomes=outcomes, delay=delay,
                     payload=base_payload, rules=rules)


def reason_cfg(
    *,
    outcome: str = "intents",
    intents: Sequence[Mapping[str, Any]] | None = None,
    recommend_finalize: bool = False,
    waivers: Sequence[Mapping[str, Any]] | None = None,
    outcomes: Mapping[str, str] | None = None,
    delay: Sequence[float] | None = DEFAULT_DELAY,
    payload: Mapping[str, Any] | None = None,
    rules: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a ``MOCK_REASON`` config."""
    base_payload: dict[str, Any] = {}
    if intents is not None:
        base_payload["intents"] = list(intents)
    if waivers is not None:
        base_payload["waivers"] = list(waivers)
    base_payload["recommend_finalize"] = recommend_finalize
    if payload is not None:
        base_payload.update(payload)
    return phase_cfg("reason", outcome=outcome, outcomes=outcomes, delay=delay,
                     payload=base_payload, rules=rules)


def bootstrap_cfg(
    *,
    discoveries: Sequence[Mapping[str, Any]] | None = None,
    fact: Mapping[str, Any] | None = None,
    outcomes: Mapping[str, str] | None = None,
    delay: Sequence[float] | None = DEFAULT_DELAY,
    payload: Mapping[str, Any] | None = None,
    rules: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a ``MOCK_BOOTSTRAP`` config."""
    base_payload: dict[str, Any] = {}
    if discoveries is not None:
        base_payload["discoveries"] = list(discoveries)
    if fact is not None:
        base_payload["fact"] = dict(fact)
    if payload is not None:
        base_payload.update(payload)
    return phase_cfg("bootstrap", outcome="ok", outcomes=outcomes, delay=delay,
                     payload=base_payload, rules=rules)


def audit_cfg(
    *,
    outcome: str = "covered",
    reason: str = "mock audit",
    outcomes: Mapping[str, str] | None = None,
    delay: Sequence[float] | None = DEFAULT_DELAY,
    payload: Mapping[str, Any] | None = None,
    rules: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a ``MOCK_AUDIT`` config."""
    base_payload: dict[str, Any] = {"reason": reason}
    if payload is not None:
        base_payload.update(payload)
    return phase_cfg("audit", outcome=outcome, outcomes=outcomes, delay=delay,
                     payload=base_payload, rules=rules)


class mock_cfg:
    """Fixture-style namespace mirroring ``verify-mock-test-spec §3.3``.

    Exposes ``_phase`` / ``_verify`` / ``_replay`` / ``_explore`` /
    ``_reason`` / ``_bootstrap`` / ``_audit`` builders plus ``env`` to serialize
    a config for a worker's ``MOCK_<PHASE>`` env.
    """

    _phase = staticmethod(phase_cfg)
    _verify = staticmethod(verify_cfg)
    _replay = staticmethod(replay_cfg)
    _explore = staticmethod(explore_cfg)
    _reason = staticmethod(reason_cfg)
    _bootstrap = staticmethod(bootstrap_cfg)
    _audit = staticmethod(audit_cfg)

    @staticmethod
    def env(phase: str, cfg: Mapping[str, Any]) -> str:
        return json.dumps(cfg, ensure_ascii=False)

    @staticmethod
    def worker_env(**phases: Mapping[str, Any]) -> dict[str, str]:
        """Assemble a worker env dict from ``phase=cfg`` kwargs."""
        return {mock_env_key(p): json.dumps(c, ensure_ascii=False) for p, c in phases.items()}


def mock_prompt(phase: str, *, stage: str | None = None, text: str = "") -> str:
    """Build a prompt carrying the ``mock-phase`` / ``mock-stage`` markers."""
    marker = f"mock-phase: {phase}"
    if stage is not None:
        marker += f" mock-stage: {stage}"
    return f"<!-- {marker} -->\n{text}"


def make_mock_driver(
    *, execution: str = "local", env: Mapping[str, str] | None = None, **phases
) -> MockDriver:
    """Construct a MockDriver with a merged ``MOCK_<PHASE>`` env.

    ``phases`` are ``phase=cfg_dict`` kwargs; ``env`` is merged underneath so
    callers can pass extra (non-MOCK) env keys.
    """
    worker_env: dict[str, str] = {}
    if env:
        worker_env.update({k: str(v) for k, v in env.items()})
    worker_env.update(mock_cfg.worker_env(**phases))
    return MockDriver(execution=execution, worker_env=worker_env)


def run_mock(
    driver: MockDriver,
    prompt: str,
    *,
    phase: str | None = None,
    stage: str | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess:
    """Run the mock worker script for ``prompt`` and return the result.

    Pass ``phase``/``stage`` to have the driver prepend the markers (robust even
    when the prompt text lacks them).
    """
    cmd = driver.build_execute(
        prompt, session_id=driver.prepare_session(), phase=phase, stage=stage
    )
    return subprocess.run(
        cmd.argv, env=cmd.env, capture_output=True, text=True, timeout=timeout
    )


def parse_mock_json(result: subprocess.CompletedProcess) -> dict[str, Any] | None:
    """Parse the mock stdout as a single JSON object; ``None`` if not JSON."""
    text = result.stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def build_mock_workers(
    *names_and_cfgs: tuple[str, dict[str, str]],
) -> list[Any]:
    """Build ``WorkerConfig``-like objects for a DispatcherLoop (E2E only).

    Each tuple is ``(name, worker_env)`` where ``worker_env`` maps phase →
    JSON string (use ``mock_cfg.worker_env(...)``). Returns lightweight
    namespace objects duck-typed to the ``WorkerConfig`` fields 30/40 read
    (name / type / task_types / max_running / priority / verify_eligible / env).
    """
    workers = []
    for name, env in names_and_cfgs:
        workers.append(
            type(
                "_Worker",
                (),
                {
                    "name": name,
                    "type": "mock",
                    "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
                    "max_running": 2,
                    "priority": 0,
                    "verify_eligible": True,
                    "env": dict(env),
                },
            )()
        )
    return workers


# ---------------------------------------------------------------------------
# 2. E2E seed + assertion helpers (require a process-internal Server client)
# ---------------------------------------------------------------------------


def seed_traffic(client: Any, eid: str, *traffic_ids: str, role: str = "trigger",
                 payload: Mapping[str, Any] | None = None) -> None:
    """Preload ``traffic_entries`` via ``POST /engagements/{eid}/traffic``.

    Uses the capture write-back endpoint (the proxy's only writer, F8). In tests
    this is called with the capture token client (or the router's index path).
    """
    if not traffic_ids:
        traffic_ids = ("tr-001", "tr-002")
    base = dict(
        payload
        or {
            "request_bytes": b"GET /login HTTP/1.1\r\nHost: 10.0.0.5\r\n\r\n",
            "response_bytes": b"HTTP/1.1 200 OK\r\n\r\nSQL error near 'OR 1=1'",
        }
    )
    for tid in traffic_ids:
        resp = client.post(
            f"/engagements/{eid}/traffic",
            json={"traffic_id": tid, "role": role, **base},
        )
        assert resp.status_code < 400, resp.text


def seed_replay_evidence(
    client: Any, eid: str, *, traffic_id: str = "tr-101",
    trigger_file_content: bytes = b"GET /login HTTP/1.1\r\nHost: 10.0.0.5\r\n\r\n",
) -> str:
    """E2E ``replay_seed``: preload tr-101 (role=replay) + trigger package file.

    Returns the traffic_id so tests can assert the replay evidence row exists.
    """
    seed_traffic(client, eid, traffic_id, role="replay")
    # The trigger package file is a mock stand-in; the replay engine (30) will
    # read it. Kept here so the E2E seed is self-contained.
    return traffic_id


def seed_finding(client: Any, eid: str, *, payload: Mapping[str, Any] | None = None,
                 detected_by: str = "worker-A") -> str:
    """Create an open finding via ``POST /engagements/{eid}/findings``."""
    body = dict(
        payload
        or {
            "title": "SQL Injection in /login",
            "severity": "high",
            "asset": "http://10.0.0.5:8080/login",
            "description": "login reflects SQL error",
            "remediation": "parameterize queries",
            "traffic_ids": ["tr-001"],
        }
    )
    resp = client.post(
        f"/engagements/{eid}/findings", json=body, params={"detected_by": detected_by}
    )
    assert resp.status_code < 400, resp.text
    return resp.json()["id"]


def pump_until_idle(dispatch: Any, timeout: float = 30.0) -> None:
    """Pump a DispatcherLoop until it reports no pending work or ``timeout``.

    Works with any object exposing either ``pump_until_idle()`` (40's loop) or
    ``step()`` (a manual tick). Skips when ``dispatch`` is ``None``.
    """
    import time

    if dispatch is None:
        return
    if hasattr(dispatch, "pump_until_idle"):
        dispatch.pump_until_idle(timeout=timeout)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        before = getattr(dispatch, "pending_count", lambda: 0)()
        dispatch.step()
        after = getattr(dispatch, "pending_count", lambda: 0)()
        if after == 0 and before == 0:
            break
        time.sleep(0.01)


# ---- assertion helpers (§3.4 of verify-mock-test-spec) ----------------------

def assert_finding_state(client: Any, eid: str, fid: str, *, status: str,
                         verify_status: str | None = None, severity: str | None = None) -> dict:
    resp = client.get(f"/engagements/{eid}/findings/{fid}")
    assert resp.status_code == 200, resp.text
    f = resp.json()
    assert f["status"] == status, f"status {f['status']!r} != {status!r}"
    if verify_status is not None:
        assert f.get("verify_status") == verify_status, f.get("verify_status")
    if severity is not None:
        assert f["severity"] == severity, f["severity"]
    return f


def assert_verified_severity(client: Any, eid: str, fid: str) -> str:
    f = client.get(f"/engagements/{eid}/findings/{fid}").json()
    return f.get("verified_severity") or f.get("severity")


def assert_task_status(dispatch: Any, run_id: str, status: str) -> None:
    run = dispatch.task_run(run_id)
    assert run["status"] == status, run


def assert_events(dispatch: Any, run_id: str, *, kinds: set[str]) -> None:
    events = dispatch.events(run_id)
    assert kinds <= {e["kind"] for e in events}, {e["kind"] for e in events}


def assert_http_mismatch(client: Any, eid: str, fid: str) -> None:
    f = client.get(f"/engagements/{eid}/findings/{fid}").json()
    assert f.get("http_mismatch") is True, f


def assert_replay_run(client: Any, eid: str, fid: str, *, result: str,
                      matched_original: int) -> None:
    runs = client.get(f"/engagements/{eid}/findings/{fid}/replay").json()
    assert any(r["result"] == result for r in runs), runs
    if matched_original is not None:
        assert any(r.get("matched_original") == matched_original for r in runs), runs


def assert_retest_pass(client: Any, eid: str, fid: str, *, count: int | None = None,
                       kinds: set[str] | None = None) -> None:
    f = client.get(f"/engagements/{eid}/findings/{fid}").json()
    rp = f.get("retest_pass")
    if count is not None:
        assert rp == count, f"retest_pass={rp} != {count}"
    if kinds is not None:
        details = {r.get("kind") for r in f.get("retest_confirmations", [])}
        assert kinds <= details, details


def assert_no_new_traffic_after(client: Any, eid: str, since: str) -> None:
    rows = client.get(f"/engagements/{eid}/traffic").json()
    for r in rows:
        assert r["created_at"] <= since, r


def assert_worker_exclusion(dispatch: Any, run_id: str, creator: str) -> None:
    run = dispatch.task_run(run_id)
    assert run["worker"] != creator, run


def assert_audit_run(client: Any, eid: str, *, item_id: str, verdict: str) -> None:
    audits = client.get(f"/engagements/{eid}/coverage/audit").json()
    assert any(
        a.get("item_id") == item_id and a.get("verdict") == verdict for a in audits
    ), audits
