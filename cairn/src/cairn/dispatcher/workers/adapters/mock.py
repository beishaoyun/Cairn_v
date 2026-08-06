"""Mock worker driver (Agent 31) — deterministic contract validator.

The mock driver is a **test / dry-run double**: it simulates every task phase
with a pure Python script driven by ``MOCK_<PHASE>`` JSON environment variables,
with no real LLM, no network and no mitmproxy (verify-mock-test-spec.md §1).

Config schema for each ``MOCK_<PHASE>`` value (JSON):

.. code-block:: json

    {
      "delay": [0.05, 0.2],              // seconds window; script sleeps inside [lo, hi]
      "outcomes": {"confirmed": "1.0"},  // weighted outcomes; MUST sum to exactly 1.0 (rule 24)
      "payload": {"verified_severity": "high"},   // merged into the emitted data
      "rules": [                          // first matching rule wins (rule payload over base)
        {"prompt_has": "verdict", "force": "rejected",
         "payload": {"verified_severity": "low"}}
      ]
    }

Phase registry (v1 §8.4 + v2 skeleton §3 TaskType extension + verify-mock-test-spec §2):

* ``healthcheck`` / ``bootstrap`` / ``bootstrap_conclude`` / ``reason`` /
  ``explore_execute`` / ``explore_conclude`` — original six phases;
* ``verify`` — verdict injection (confirmed / rejected / needs_more_evidence /
  accepted_false / invalid_json / empty / command_fail) + ``payload``
  (verified_severity / verified_traffic_ids / suggested_action / reason /
  observations / traffic_note);
* ``replay`` — deterministic replay-engine result injection (remediated /
  unchanged / ambiguous / error) + ``payload`` (matched_original / result);
* ``audit`` — coverage sampling review (covered / discrepancy).

``MOCK_ALLOWED_ENV_KEYS`` is auto-derived from ``MOCK_ALLOWED_OUTCOMES`` so
``MOCK_VERIFY`` / ``MOCK_REPLAY`` are automatically legal (contract A).

Hard constraints:
* no business logic — mock only *emits* the contract shape that Agent 30's
  validators consume; if a test exposes a 30/21/22 bug, record it in the
  handoff instead of patching the mock to mask it;
* the driver is a seed driver: ``prepare_session`` returns a generated id,
  ``required_env_keys=()`` so container-mode construction needs no LLM keys
  (13-dispatcher-runtime §7).
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import ClassVar

from ..base import WorkerCommand, WorkerDriver, WorkerDriverError

__all__ = [
    "MockDriver",
    "MockConfigError",
    "MOCK_PHASES",
    "MOCK_ALLOWED_OUTCOMES",
    "MOCK_DEFAULT_BEHAVIOR",
    "MOCK_ALLOWED_ENV_KEYS",
    "MOCK_EXTRA_KEYS",
    "COVERAGE_OUTCOMES",
    "mock_env_key",
    "validate_mock_config",
    "validate_mock_extra_key",
]

#: All phases the mock driver can simulate.
MOCK_PHASES: frozenset[str] = frozenset(
    {
        "healthcheck",
        "bootstrap",
        "bootstrap_conclude",
        "reason",
        "explore_execute",
        "explore_conclude",
        "verify",
        "replay",
        "audit",
    }
)

#: Contract A — allowed outcomes per phase (verify-mock-test-spec §2.1).
#: ``invalid_json`` / ``empty`` / ``command_fail`` are the crash-injection meta
#: outcomes shared by every phase.
MOCK_ALLOWED_OUTCOMES: dict[str, frozenset[str]] = {
    # ``fail`` kept for backward compatibility with the v1 healthcheck shape
    # (``MOCK_HEALTHCHECK: {"outcomes": {"ok": ..., "fail": ...}}``).
    "healthcheck": frozenset({"ok", "fail", "invalid_json", "empty", "command_fail"}),
    "bootstrap": frozenset(
        {"ok", "rejected", "invalid_json", "empty", "command_fail"}
    ),
    "bootstrap_conclude": frozenset(
        {"ok", "rejected", "invalid_json", "empty", "command_fail"}
    ),
    "reason": frozenset(
        {"intents", "finalize", "rejected", "invalid_json", "empty", "command_fail"}
    ),
    "explore_execute": frozenset(
        {"fact", "rejected", "invalid_json", "empty", "command_fail"}
    ),
    "explore_conclude": frozenset(
        {"fact", "rejected", "invalid_json", "empty", "command_fail"}
    ),
    "verify": frozenset(
        {
            "confirmed",
            "rejected",
            "needs_more_evidence",
            "accepted_false",
            "invalid_json",
            "empty",
            "command_fail",
        }
    ),
    "replay": frozenset(
        {
            "remediated",
            "unchanged",
            "ambiguous",
            "error",
            "invalid_json",
            "empty",
            "command_fail",
        }
    ),
    "audit": frozenset(
        {"covered", "discrepancy", "rejected", "invalid_json", "empty", "command_fail"}
    ),
}

#: Contract A — default behavior per phase (delay window + deterministic default
#: outcome). Used when no ``MOCK_<PHASE>`` env is supplied, and as the driver-side
#: reference the worker script falls back to via ``CAIRN_MOCK_DEFAULTS``.
_MOCK_DEFAULT_OUTCOMES: dict[str, dict[str, str]] = {
    "healthcheck": {"ok": "1.0"},
    "bootstrap": {"ok": "1.0", "rejected": "0.0", "invalid_json": "0.0", "empty": "0.0", "command_fail": "0.0"},
    "bootstrap_conclude": {"ok": "1.0", "rejected": "0.0", "invalid_json": "0.0", "empty": "0.0", "command_fail": "0.0"},
    "reason": {"intents": "1.0", "finalize": "0.0", "rejected": "0.0", "invalid_json": "0.0", "empty": "0.0", "command_fail": "0.0"},
    "explore_execute": {"fact": "1.0", "rejected": "0.0", "invalid_json": "0.0", "empty": "0.0", "command_fail": "0.0"},
    "explore_conclude": {"fact": "1.0", "rejected": "0.0", "invalid_json": "0.0", "empty": "0.0", "command_fail": "0.0"},
    "verify": {
        "confirmed": "1.0", "rejected": "0.0", "needs_more_evidence": "0.0",
        "accepted_false": "0.0", "invalid_json": "0.0", "empty": "0.0", "command_fail": "0.0",
    },
    "replay": {
        "remediated": "1.0", "unchanged": "0.0", "ambiguous": "0.0",
        "error": "0.0", "invalid_json": "0.0", "empty": "0.0", "command_fail": "0.0",
    },
    "audit": {"covered": "1.0", "discrepancy": "0.0", "rejected": "0.0", "invalid_json": "0.0", "empty": "0.0", "command_fail": "0.0"},
}

MOCK_DEFAULT_BEHAVIOR: dict[str, dict] = {
    phase: {"delay": [0.05, 0.2], "outcomes": dict(outcomes)}
    for phase, outcomes in _MOCK_DEFAULT_OUTCOMES.items()
}

#: Contract A — ``MOCK_<PHASE>`` env keys auto-derived from the allowed outcomes,
#: so ``MOCK_VERIFY`` / ``MOCK_REPLAY`` are automatically legal.
MOCK_ALLOWED_ENV_KEYS: frozenset[str] = frozenset(
    f"MOCK_{phase.upper()}" for phase in MOCK_ALLOWED_OUTCOMES
)

#: Non-phase ``MOCK_*`` keys defined by the spec (prompts §9). ``MOCK_EXTRA_KEYS``
#: are validated alongside the phase keys and auto-legal in ``MOCK_ALLOWED_ENV_KEYS``.
#:
#: * ``MOCK_EXPLORE_COVERAGE_OUTCOME`` — probabilities for the explore
#:   ``coverage_result.outcome`` (no_issue / finding_created / not_applicable)
#:   when the explore payload does not pin it.
MOCK_EXTRA_KEYS: frozenset[str] = frozenset({"MOCK_EXPLORE_COVERAGE_OUTCOME"})

#: Auto-derived allowed env keys = phase keys + spec-defined extra keys.
MOCK_ALLOWED_ENV_KEYS = MOCK_ALLOWED_ENV_KEYS | MOCK_EXTRA_KEYS

#: ``MOCK_<PHASE>``-looking env key pattern (validated in ``__init__``).
_MOCK_KEY_RE = re.compile(r"^MOCK_[A-Z0-9_]+$")

#: Coverage-result outcomes (coverage spec §3) used by MOCK_EXPLORE_COVERAGE_OUTCOME.
COVERAGE_OUTCOMES: frozenset[str] = frozenset(
    {"no_issue", "finding_created", "not_applicable"}
)


def mock_env_key(phase: str) -> str:
    """Return the env key for a mock phase, e.g. ``mock_env_key("verify")``."""
    return f"MOCK_{phase.upper()}"


class MockConfigError(WorkerDriverError):
    """A ``MOCK_*`` env value failed static validation (rule 24)."""


def validate_mock_config(phase: str, cfg: object) -> None:
    """Strictly validate a parsed ``MOCK_<PHASE>`` config dict (rule 24).

    Rejects: non-object config, unknown phase, unknown outcome, probability
    values that are not numbers / negative, outcome probabilities not summing
    to exactly 1.0, a negative or inverted delay window, a non-dict payload,
    and malformed rules (non-dict / forced outcome not in the phase's allowed
    set).
    """
    key = mock_env_key(phase)
    if phase not in MOCK_ALLOWED_OUTCOMES:
        raise MockConfigError(f"{key}: unknown phase {phase!r}")
    allowed = MOCK_ALLOWED_OUTCOMES[phase]
    if not isinstance(cfg, dict):
        raise MockConfigError(f"{key}: config must be a JSON object")
    if not cfg:
        return  # empty config → the worker script falls back to defaults

    outcomes = cfg.get("outcomes")
    if outcomes is not None:
        if not isinstance(outcomes, dict) or not outcomes:
            raise MockConfigError(f"{key}: 'outcomes' must be a non-empty object")
        total = 0.0
        for name, prob in outcomes.items():
            if name not in allowed:
                raise MockConfigError(
                    f"{key}: unknown outcome {name!r} (allowed: {sorted(allowed)})"
                )
            try:
                p = float(prob)
            except (TypeError, ValueError):
                raise MockConfigError(
                    f"{key}: outcome {name!r} probability is not a number: {prob!r}"
                ) from None
            if p < 0:
                raise MockConfigError(
                    f"{key}: outcome {name!r} probability must be >= 0"
                )
            total += p
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise MockConfigError(
                f"{key}: outcome probabilities must sum to 1.0, got {total:.6f}"
            )

    delay = cfg.get("delay")
    if delay is not None:
        if not (isinstance(delay, (list, tuple)) and len(delay) == 2):
            raise MockConfigError(f"{key}: 'delay' must be a two-element list [lo, hi]")
        try:
            lo, hi = float(delay[0]), float(delay[1])
        except (TypeError, ValueError):
            raise MockConfigError(
                f"{key}: 'delay' entries must be numbers, got {delay!r}"
            ) from None
        if lo < 0 or hi < lo:
            raise MockConfigError(
                f"{key}: 'delay' must be non-negative and [0] <= [1], got {delay!r}"
            )

    if "payload" in cfg and not isinstance(cfg["payload"], dict):
        raise MockConfigError(f"{key}: 'payload' must be an object")

    rules = cfg.get("rules")
    if rules is not None:
        if not isinstance(rules, list):
            raise MockConfigError(f"{key}: 'rules' must be a list")
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise MockConfigError(f"{key}: rules[{i}] must be an object")
            if "force" in rule and rule["force"] not in allowed:
                raise MockConfigError(
                    f"{key}: rules[{i}].force {rule['force']!r} not allowed "
                    f"(allowed: {sorted(allowed)})"
                )
            if "payload" in rule and not isinstance(rule["payload"], dict):
                raise MockConfigError(f"{key}: rules[{i}].payload must be an object")


def validate_mock_extra_key(key: str, cfg: object) -> None:
    """Validate a spec-defined non-phase ``MOCK_*`` key (e.g. MOCK_EXPLORE_COVERAGE_OUTCOME).

    Schema: ``{"outcomes": {"no_issue": ..., "finding_created": ..., "not_applicable": ...}}``
    — outcome names must be in ``COVERAGE_OUTCOMES`` and weights must sum to 1.0.
    """
    if key not in MOCK_EXTRA_KEYS:
        raise MockConfigError(f"unknown MOCK_* key: {key!r}")
    if not isinstance(cfg, dict):
        raise MockConfigError(f"{key}: config must be a JSON object")
    if not cfg:
        return
    outcomes = cfg.get("outcomes")
    if not isinstance(outcomes, dict) or not outcomes:
        raise MockConfigError(f"{key}: 'outcomes' must be a non-empty object")
    total = 0.0
    for name, prob in outcomes.items():
        if name not in COVERAGE_OUTCOMES:
            raise MockConfigError(
                f"{key}: unknown coverage outcome {name!r} (allowed: {sorted(COVERAGE_OUTCOMES)})"
            )
        try:
            total += float(prob)
        except (TypeError, ValueError):
            raise MockConfigError(f"{key}: probability for {name!r} is not a number: {prob!r}") from None
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise MockConfigError(f"{key}: probabilities must sum to 1.0, got {total:.6f}")


class MockDriver(WorkerDriver):
    """Seed-session mock driver.

    ``required_env_keys=()`` — construction never demands LLM env keys. Any
    ``MOCK_*`` key present in the effective env is validated against
    ``MOCK_ALLOWED_ENV_KEYS`` and its config against ``validate_mock_config``.

    ``build_execute`` / ``build_conclude`` write the rendered prompt to a temp
    file and run ``python3 _mock_script.py <prompt-file>`` with the effective
    env (MOCK_* configs + ``CAIRN_MOCK_DEFAULTS``). The worker script detects
    the phase from the prompt's ``mock-phase:`` marker (the driver may also be
    told the phase via ``kw["phase"]``, which is prepended as a marker to be
    robust against callers that do not render the marker).
    """

    driver_type: ClassVar[str] = "mock"
    required_env_keys: ClassVar[tuple[str, ...]] = ()
    local_binary: ClassVar[str | None] = None
    base_url_env: ClassVar[str] = ""
    health_path: ClassVar[str] = ""

    #: path to the standalone worker script (same package directory)
    _script_path: ClassVar[str] = str(Path(__file__).parent / "_mock_script.py")

    def __init__(
        self,
        *,
        execution: str = "container",
        common_env: dict[str, str] | None = None,
        worker_env: dict[str, str] | None = None,
        binary_path: str | None = None,
    ) -> None:
        super().__init__(
            execution=execution,
            common_env=common_env,
            worker_env=worker_env,
            binary_path=binary_path,
        )
        self._tmpdir = tempfile.TemporaryDirectory(prefix="cairn-mock-")
        self._prompt_counter = 0
        self._defaults_json = json.dumps(MOCK_DEFAULT_BEHAVIOR, ensure_ascii=False)
        # strict env validation (rule 24): unknown MOCK_* key → fail construction
        for key, value in self.env.items():
            if _MOCK_KEY_RE.match(key):
                if key not in MOCK_ALLOWED_ENV_KEYS:
                    raise MockConfigError(
                        f"unknown MOCK_* key: {key!r} "
                        f"(allowed: {sorted(MOCK_ALLOWED_ENV_KEYS)})"
                    )
                try:
                    cfg = json.loads(value)
                except ValueError as exc:
                    raise MockConfigError(
                        f"{key}: invalid JSON: {exc}"
                    ) from exc
                if key in MOCK_EXTRA_KEYS:
                    validate_mock_extra_key(key, cfg)
                else:
                    phase = key[len("MOCK_"):].lower()
                    validate_mock_config(phase, cfg)

    # -- session lifecycle -------------------------------------------------

    def prepare_session(self) -> str:
        """Seed driver: generate a stable session id (never extracted from output)."""
        return f"mock-{uuid.uuid4().hex[:12]}"

    extract_session = WorkerDriver.extract_session

    # -- command construction ---------------------------------------------

    def _write_prompt(self, prompt: str, *, phase: str | None, stage: str | None) -> str:
        """Write the rendered prompt to a temp file and return its path.

        If ``phase``/``stage`` are supplied by the caller, prepend the mock
        markers so the worker script can detect them even if the prompt text
        lacks the marker (robustness for Agent 30 callers).
        """
        header = ""
        if phase is not None:
            header += f"<!-- mock-phase: {phase} -->"
        if stage is not None:
            header += f"<!-- mock-stage: {stage} -->"
        content = (header + "\n" + prompt) if header else prompt
        self._prompt_counter += 1
        path = os.path.join(
            self._tmpdir.name,
            f"prompt_{self._prompt_counter:04d}_{uuid.uuid4().hex[:6]}.md",
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def build_execute(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        """Build the first-phase command (mock worker script)."""
        path = self._write_prompt(prompt, phase=kw.get("phase"), stage=kw.get("stage"))
        env = dict(self.env)
        env["CAIRN_MOCK_DEFAULTS"] = self._defaults_json
        return WorkerCommand([sys.executable, self._script_path, path], env)

    def build_conclude(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        """Conclude phase: same script; phase detected from the prompt marker."""
        return self.build_execute(prompt, session_id=session_id, **kw)

    def supports_conclude(self) -> bool:
        return True

    def extract_response_text(self, stdout: str) -> str | None:
        return stdout.strip() or None

    # -- health ------------------------------------------------------------

    def check_health(self, *, timeout: float | None = None) -> bool:
        """Mock workers are always healthy (no real endpoint to probe)."""
        return True
