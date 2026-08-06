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
    """Preload ``traffic_entries`` with the exact ids the scenarios reference.

    The production proxy write-back endpoint (``POST /engagements/{eid}/traffic``,
    F8) auto-generates ids, so tests seed exact ids (``tr-001`` etc.) directly into
    the temp DB. When ``client`` exposes ``db_path``/``traffic_root`` the rows are
    inserted + payload files written directly (E2E seeding only); otherwise it
    falls back to the capture-token API.
    """
    import datetime

    if not traffic_ids:
        traffic_ids = ("tr-001", "tr-002")
    base = dict(
        payload
        or {
            "request_bytes": b"GET /login HTTP/1.1\r\nHost: 10.0.0.5\r\n\r\n",
            "response_bytes": b"HTTP/1.1 200 OK\r\n\r\nSQL error near 'OR 1=1'",
            "method": "GET",
            "url": "http://10.0.0.5:8080/login",
            "status": 200,
        }
    )
    db_path = getattr(client, "db_path", None)
    traffic_root = getattr(client, "traffic_root", None)
    if db_path is not None:
        import sqlite3
        from pathlib import Path

        req = base.get("request_bytes", b"GET /login HTTP/1.1\r\nHost: 10.0.0.5\r\n\r\n")
        resp = base.get("response_bytes", b"HTTP/1.1 200 OK\r\n\r\nSQL error near 'OR 1=1'")
        if isinstance(req, str):
            req = req.encode("utf-8")
        if isinstance(resp, str):
            resp = resp.encode("utf-8")
        root = Path(traffic_root or ".")
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(db_path)
        try:
            for i, tid in enumerate(traffic_ids):
                req_path = f"e2e/{tid}.req"
                resp_path = f"e2e/{tid}.resp"
                if root is not None:
                    (root / req_path).parent.mkdir(parents=True, exist_ok=True)
                    (root / req_path).write_bytes(req)
                    (root / resp_path).parent.mkdir(parents=True, exist_ok=True)
                    (root / resp_path).write_bytes(resp)
                conn.execute(
                    "INSERT OR REPLACE INTO traffic_entries "
                    "(id, engagement_id, seq, captured_at, method, url, host, client, client_ip, "
                    " status, req_path, resp_path, req_bytes, resp_bytes, content_type, sha256, "
                    " chunk_count, archived, archived_path, finding_linked) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,0)",
                    (
                        tid, eid, i + 1, now,
                        base.get("method", "GET"), base.get("url", "http://10.0.0.5:8080/login"),
                        base.get("host"), base.get("client") or "proxy", base.get("client_ip"),
                        base.get("status"), req_path, resp_path, len(req), len(resp),
                        base.get("content_type"), base.get("sha256"), 1,
                    ),
                )
        finally:
            conn.commit()
            conn.close()
        return
    if hasattr(client, "_request"):
        # CairnClient（捕获代理受限 token 客户端，F8）—— id 由服务端自动生成。
        idx = {
            "method": base.get("method", "GET"),
            "url": base.get("url", "http://10.0.0.5:8080/login"),
            "status": base.get("status"),
            "req_path": f"/{traffic_ids[0]}.req",
            "resp_path": f"/{traffic_ids[0]}.resp",
            "req_bytes": len(base.get("request_bytes") or b""),
            "resp_bytes": len(base.get("response_bytes") or b""),
            "client": "proxy",
        }
        if base.get("sha256"):
            idx["sha256"] = base["sha256"]
        client._request("POST", f"/engagements/{eid}/traffic", json=idx)
    else:
        resp = client.post(f"/engagements/{eid}/traffic", json={"traffic_id": traffic_ids[0], "role": role, **base})
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
    """Create an open finding via ``POST /engagements/{eid}/findings``.

    ``detected_by`` goes in the JSON body (the findings router reads it from the
    body, not a query param — a wiring fix for the TV matrix, 2026-08-06).
    """
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
    body.setdefault("detected_by", detected_by)
    body.setdefault("actor", "agent")
    if hasattr(client, "create_finding"):
        # CairnClient — the JSON body matches the findings router DTO.
        resp = client.create_finding(eid, body, detected_by=detected_by, actor="agent")
        assert resp.get("id"), resp
        return resp["id"]
    resp = client.post(f"/engagements/{eid}/findings", json=body)
    assert resp.status_code < 400, resp.text
    return resp.json()["id"]


def _count_tasks(client: Any, eid: str) -> int:
    """Number of task_runs for an engagement (via the CairnClient)."""
    try:
        rows = client._request("GET", f"/engagements/{eid}/tasks") or []
    except Exception:  # noqa: BLE001 —— pump 期间任务列表查询失败按 0 处理
        return 0
    return len(rows)


def _drain_steps(dispatch: Any, loop: Any, timeout: float) -> None:
    """Step a DispatcherLoop until two consecutive steps create no task_run."""
    import time

    client = getattr(dispatch, "_client", None)
    eid = getattr(dispatch, "_eid", None)
    deadline = time.monotonic() + timeout
    idle_streak = 0
    steps = 0
    while time.monotonic() < deadline and steps < 2000:
        steps += 1
        before = _count_tasks(client, eid) if (client is not None and eid) else None
        loop.step()
        after = _count_tasks(client, eid) if (client is not None and eid) else None
        if before is not None and after is not None and after == before:
            idle_streak += 1
            if idle_streak >= 2:
                break
        else:
            idle_streak = 0
        time.sleep(0.005)


def pump_until_idle(dispatch: Any, timeout: float = 60.0) -> None:
    """Pump a DispatcherLoop until it reports no pending work or ``timeout``.

    ``dispatch`` may be a :class:`DispatchView` (loop + client + eid), a
    :class:`DispatcherLoop` with its own ``pump_until_idle``/``step()``, or
    ``None``. Idle is detected by two consecutive steps that create no new
    task_run.
    """
    if dispatch is None:
        return
    loop = getattr(dispatch, "_loop", None)
    if loop is not None:
        # DispatchView —— 直接 drain，避免 hasattr 无限递归
        _drain_steps(dispatch, loop, timeout)
        return
    if hasattr(dispatch, "pump_until_idle"):
        dispatch.pump_until_idle(timeout=timeout)
        return
    _drain_steps(dispatch, dispatch, timeout)


class DispatchView:
    """Thin wrapper around a ``DispatcherLoop`` exposing the E2ECtx accessors.

    The 46 TV scenarios rely on ``task_run(run_id)`` / ``events(run_id)`` and a
    ``pump_until_idle`` that drains the loop. The 40 ``DispatcherLoop`` only has
    ``step()``/``run()``, so this view adapts the loop + client to the harness
    contract (2026-08-06, P1-1 wiring).
    """

    def __init__(self, loop: Any, client: Any, eid: str, *, db_path: str | None = None) -> None:
        self._loop = loop
        self._client = client
        self._eid = eid
        self.db_path = db_path

    # --- E2ECtx accessors -------------------------------------------------
    def task_run(self, run_id: str) -> dict:
        return self._client._request("GET", f"/tasks/{run_id}")

    def events(self, run_id: str) -> list[dict]:
        resp = self._client._request("GET", f"/tasks/{run_id}/events")
        return resp.get("items") if isinstance(resp, dict) else resp or []

    def step(self) -> None:
        return self._loop.step()

    def pump_until_idle(self, timeout: float = 60.0) -> None:
        pump_until_idle(self, timeout=timeout)

    def find_verify_run_id(self, fid: str) -> str:
        """Map a finding to its latest verify task_run id via the DB.

        ``task_runs`` has no ``finding_id`` column, so the mapping is recovered
        from ``verify_runs.task_run_id`` (the loop passes ``ctx.run_id`` there).
        When the verify task failed *before* applying a verdict (contract /
        exception scenarios such as TV-13/16/17), no ``verify_runs`` row exists,
        so we fall back to the latest ``task_type=verify`` task_run.
        """
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT task_run_id FROM verify_runs WHERE finding_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (fid,),
            ).fetchone()
            if row is not None:
                return str(row[0])
        finally:
            conn.close()
        rows = self._client._request("GET", f"/engagements/{self._eid}/tasks") or []
        verify = [r for r in rows if r.get("task_type") == "verify"]
        if not verify:
            raise AssertionError(f"no verify run for {fid}")
        verify.sort(key=lambda r: r.get("started_at") or "")
        return str(verify[-1]["id"])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loop, name)


class _Resp:
    """Minimal Response-like object so harness helpers work against a CairnClient."""

    def __init__(self, status_code: int, data: Any) -> None:
        self.status_code = status_code
        self._data = data
        self.text = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data

    def json(self) -> Any:
        return self._data


class E2EHttpClient:
    """Wrap a :class:`CairnClient` to expose raw-HTTP-style ``get/post/put``.

    The 46 TV scenarios and the harness assertion helpers call
    ``client.get(f"/engagements/{eid}/...")`` and expect a Response-like object
    with ``status_code`` / ``json()`` / ``text``, while the DispatcherLoop needs
    the full ``CairnClient`` method surface. This wrapper provides both: path
    requests return ``_Resp``; every other attribute (``create_finding``,
    ``_request``, ``list_items``, ...) is delegated to the underlying client.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def _call(self, method: str, path: str, **kw: Any) -> _Resp:
        try:
            data = self._client._request(method, path, **kw)
            return _Resp(200, data)
        except Exception as exc:  # noqa: BLE001 —— CairnClientError surface as _Resp
            from cairn.dispatcher.errors import CairnClientError

            if isinstance(exc, CairnClientError):
                return _Resp(exc.http_status or 500, {
                    "error_code": exc.error_code, "message": str(exc), "detail": exc.detail,
                })
            raise

    def get(self, path: str, **kw: Any) -> _Resp:
        return self._call("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> _Resp:
        return self._call("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> _Resp:
        return self._call("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> _Resp:
        return self._call("DELETE", path, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


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
    resp = client.get(f"/engagements/{eid}/findings/{fid}/replay")
    if hasattr(resp, "json"):
        data = resp.json()
    else:
        data = resp
    runs = data.get("items") if isinstance(data, dict) and "items" in data else data
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
        details = {r.get("kind") for r in f.get("retest", {}).get("details", [])}
        assert kinds <= details, details


def assert_no_new_traffic_after(client: Any, eid: str, since: str) -> None:
    rows = client.get(f"/engagements/{eid}/traffic").json()
    for r in rows:
        assert r["created_at"] <= since, r


def assert_worker_exclusion(dispatch: Any, run_id: str, creator: str) -> None:
    run = dispatch.task_run(run_id)
    assert run["worker"] != creator, run


def assert_audit_run(client: Any, eid: str, *, item_id: str, verdict: str) -> None:
    resp = client.get(f"/engagements/{eid}/coverage/audit")
    if hasattr(resp, "json"):
        data = resp.json()
    else:
        data = resp
    audits = data.get("items") if isinstance(data, dict) and "items" in data else data
    assert any(
        a.get("coverage_item_id") == item_id and a.get("verdict") == verdict for a in audits
    ), audits
