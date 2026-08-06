"""任务公共层（Agent 30）——契约校验器 / 任务上下文 / 执行编排 / 写回重试 / 进度上报。

本模块是 ``dispatcher/tasks/*`` 与 ``findings/coverage/replay`` 写回层的公共底座：

- 校验器清单对齐 ``backend-module-skeleton.md`` §4（``validate_*_payload``）；
  **``complete`` 字段一律拒绝**（黄金不变量 5；bootstrap 用 ``sweep_complete`` 表初探完成）。
- ``TaskContext`` / ``TaskResult`` 供 Agent 40 主循环装配与消费；
- ``run_worker_phase`` 实现单阶段执行编排（13 §7）：
  prepare_session → build_execute → backend.build_exec_process → communicate →
  extract_session → extract_response_text；``cancellation.attach_process`` 全程挂载。
- ``with_retry``：findings/coverage 写回失败退避 1 次再放弃（tuning.writeback_retries），
  仍失败只记日志（不做无界重试）。
- ``emit_event``：进度上报（摘要 ≤512B 落 append_event，原始流分片写文件归 40）。

硬约束（CLAUDE.md / dev-agents/30）：
- Agent 容器不持 token（C5）：本模块只经 ``CairnClient`` 写回，绝不把凭据放进 prompt。
- 不实现调度循环 / worker 选择 / 心跳（40）——这里只提供单任务纯函数/类。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import httpx

from ..errors import (
    COVERAGE_ALREADY_COVERED,
    COVERAGE_NOT_APPLICABLE,
    ENGAGEMENT_INVALID_STATE,
    FINDING_DUP,
    KILL_SWITCH_ON,
    LEASE_CONFLICT,
    NOT_FOUND,
    OUT_OF_AUTHORIZATION_WINDOW,
    SCOPE_DENIED,
    VALIDATION,
    CairnClientError,
)

# ---------------------------------------------------------------------------
# 枚举常量（对齐 DDL CHECK / prompts 契约；黄金不变量 7）
# ---------------------------------------------------------------------------

SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low", "info")
COVERAGE_OUTCOMES: tuple[str, ...] = ("no_issue", "finding_created", "not_applicable")
DEPTHS: tuple[str, ...] = ("baseline", "standard", "deep")
WAIVER_KINDS: tuple[str, ...] = ("not_applicable", "out_of_scope", "risk_accepted")
VERDICTS: tuple[str, ...] = ("confirmed", "rejected", "needs_more_evidence")
REPLAY_RESULTS: tuple[str, ...] = ("unchanged", "remediated", "ambiguous", "error")
HTTP_METHODS: frozenset[str] = frozenset({
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT",
})
FINDING_CATEGORIES: tuple[str, ...] = ("recon", "scan", "webapp", "network", "config", "osint", "auth", "other")
VERIFY_INDEPENDENCE: tuple[str, ...] = ("cross_worker", "cross_model", "cross_run", "human")
VERIFY_STAGES: tuple[str, ...] = ("blind", "comparison")

#: 服务端错误码中**确定性拒绝**（重试无意义）的集合 —— 写回重试对它们直接抛出。
NON_RETRYABLE_CODES: frozenset[str] = frozenset({
    FINDING_DUP,
    SCOPE_DENIED,
    VALIDATION,
    NOT_FOUND,
    LEASE_CONFLICT,
    COVERAGE_ALREADY_COVERED,
    COVERAGE_NOT_APPLICABLE,
    ENGAGEMENT_INVALID_STATE,
    KILL_SWITCH_ON,
    OUT_OF_AUTHORIZATION_WINDOW,
})

_CWE_RE = re.compile(r"CWE-\d+")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class TaskError(Exception):
    """任务执行失败（非写回失败）。"""

    def __init__(self, message: str, *, error_code: str | None = None, escalate: bool = False) -> None:
        self.error_code = error_code
        self.escalate = escalate
        super().__init__(message)


class PayloadError(TaskError):
    """模型输出契约校验失败（等价 skeleton §4 的 rejected / VALIDATION）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=VALIDATION)


class CancelledError(TaskError):
    """任务被取消（kill switch / 超时 / 外部 cancel）。"""

    def __init__(self, reason: str = "cancelled") -> None:
        super().__init__(reason, error_code="CANCELLED")


class WritebackError(TaskError):
    """写回（findings/coverage/图）失败且重试耗尽。"""

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message, error_code=error_code)


# ---------------------------------------------------------------------------
# 数据对象
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    """单任务执行结果（Agent 40 消费）。

    ``status`` ∈ success | failed | cancelled | rejected | retryable。
    - ``success``   任务完成，写回成功；
    - ``failed``    契约/运行时失败（``error_code``/``error`` 说明；reason 收敛失败带
      ``escalate=True`` 升级人工，C8）；
    - ``cancelled`` 被取消（kill switch / 超时）；
    - ``rejected``  模型 ``accepted=false``（不落任何写回）；
    - ``retryable`` 临时失败（格子忙/他人认领），下轮重排（不升级人工）。
    """

    status: str
    data: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    outcome_note: Optional[str] = None
    escalate: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"


def _null_log(_msg: str) -> None:  # pragma: no cover - trivial
    pass


@dataclass
class TaskContext:
    """一次任务执行的上下文（40 主循环装配后传入任务函数）。

    - ``client``：12 的 CairnClient（唯一写回通道，C5：容器不持 token）；
    - ``config``：12 的 DispatcherConfig（tasks.*.timeout / tuning.*）；
    - ``cancellation``：13 的 TaskCancellation（attach 进程、kill switch）；
    - ``worker`` / ``eid`` / ``project_id`` / ``run_id``：派发元数据（进度/心跳用）。
    """

    client: Any  # CairnClient
    config: Any  # DispatcherConfig
    cancellation: Any = None  # TaskCancellation | None
    worker: str = ""
    eid: str = ""
    project_id: Optional[str] = None
    run_id: Optional[str] = None
    log: Callable[[str], None] = field(default_factory=lambda: _null_log)


# ---------------------------------------------------------------------------
# JSON 提取 / accepted 包装
# ---------------------------------------------------------------------------


def extract_json(text: str) -> dict:
    """从模型输出提取 JSON 对象（宽容：剥 markdown 围栏，找第一个平衡 ``{}``）。"""
    if not text:
        raise PayloadError("模型输出为空，无 JSON 可解析")
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # 兜底：找第一个平衡的 {...} 块
    start = t.find("{")
    if start == -1:
        raise PayloadError("输出中未找到 JSON 对象")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(t[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise PayloadError(f"JSON 解析失败: {exc}") from exc
                if isinstance(obj, dict):
                    return obj
                break
    raise PayloadError("输出中未找到完整 JSON 对象")


def _reject_complete(obj: Any, *, path: str = "data") -> None:
    """递归拒绝 ``complete`` 字段（黄金不变量 5：渗透场景无完成判定）。"""
    if isinstance(obj, dict):
        if "complete" in obj:
            raise PayloadError(
                f"字段 'complete' 被禁止（无完成判定；bootstrap 用 sweep_complete）：{path}"
            )
        for k, v in obj.items():
            _reject_complete(v, path=f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_complete(v, path=f"{path}[{i}]")


def parse_accepted(payload: dict) -> dict:
    """校验 ``{accepted, data}`` 包装并拒绝 ``complete``，返回 ``data``。"""
    if not isinstance(payload, dict):
        raise PayloadError("payload 不是 JSON 对象")
    if not payload.get("accepted"):
        raise TaskError(
            payload.get("reason") or "模型拒绝任务（accepted=false）",
            error_code="MODEL_REJECTED",
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PayloadError("data 缺失或非对象")
    _reject_complete(data)
    return data


# ---------------------------------------------------------------------------
# 校验器（backend-module-skeleton §4）
# ---------------------------------------------------------------------------


def _is_rel_path(path: str) -> bool:
    """相对证据路径：非空、无 ``..``/绝对前缀/盘符/反斜杠。"""
    if not isinstance(path, str) or not path:
        return False
    p = path.replace("\\", "/").strip()
    if p.startswith("/") or p.startswith("\\") or re.match(r"^[A-Za-z]:", p):
        return False
    if not p or p in (".", ".."):
        return False
    parts = [seg for seg in p.split("/") if seg]
    return all(seg not in (".", "..") for seg in parts)


def _validate_http_entry(h: Any, where: str) -> None:
    if not isinstance(h, dict):
        raise PayloadError(f"{where} 不是对象")
    method = h.get("method")
    if method is not None and str(method).upper() not in HTTP_METHODS:
        raise PayloadError(f"{where}.method 非法: {method!r}")
    url = h.get("url")
    if url is not None and not re.match(r"^https?://", str(url)):
        raise PayloadError(f"{where}.url 必须为绝对 URL")
    status = h.get("response_status")
    if status is not None and not (isinstance(status, int) and 100 <= status <= 599):
        raise PayloadError(f"{where}.response_status 必须在 [100, 599]")


def validate_findings_payload(findings: Any) -> list[dict]:
    """findings[] 白名单校验（severity/cvss/cwe/evidence_refs/http/commands）。"""
    if not isinstance(findings, list):
        raise PayloadError("findings 必须是数组")
    _reject_complete(list(findings), path="data.findings")
    out: list[dict] = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            raise PayloadError(f"findings[{i}] 不是对象")
        sev = f.get("severity")
        if sev not in SEVERITIES:
            raise PayloadError(f"findings[{i}].severity 非法: {sev!r}")
        cvss = f.get("cvss_score")
        if cvss is not None and not (isinstance(cvss, (int, float)) and 0 <= cvss <= 10):
            raise PayloadError(f"findings[{i}].cvss_score 必须在 [0,10]")
        cwe = f.get("cwe_id")
        if cwe is not None and not _CWE_RE.fullmatch(str(cwe)):
            raise PayloadError(f"findings[{i}].cwe_id 格式必须为 CWE-\\d+")
        asset = f.get("asset")
        if not isinstance(asset, str) or not asset.strip():
            raise PayloadError(f"findings[{i}].asset 必填")
        evrefs = f.get("evidence_refs") or []
        if not isinstance(evrefs, list) or not all(_is_rel_path(r) for r in evrefs):
            raise PayloadError(f"findings[{i}].evidence_refs 必须为相对路径数组")
        http = f.get("http") or []
        if not isinstance(http, list):
            raise PayloadError(f"findings[{i}].http 必须是数组")
        for j, h in enumerate(http):
            _validate_http_entry(h, f"findings[{i}].http[{j}]")
        commands = f.get("commands") or []
        if not isinstance(commands, list):
            raise PayloadError(f"findings[{i}].commands 必须是数组")
        for j, c in enumerate(commands):
            if not isinstance(c, dict) or not c.get("command"):
                raise PayloadError(f"findings[{i}].commands[{j}].command 必填")
        f = dict(f)
        f["http"] = list(http)
        f["commands"] = list(commands)
        out.append(f)
    return out


def validate_coverage_result(
    coverage: Any,
    *,
    known_item_ids: Iterable[str] = (),
    claimed_item_ids: Iterable[str] = (),
) -> dict:
    """coverage_result 校验（covered_items ∈ engagement / 未覆盖；outcome/depth 枚举）。

    - ``covered_items`` 非空且 ⊆ ``known_item_ids``（本 engagement 覆盖项）；
    - ``claimed_item_ids`` 提供时校验引用格子 ⊆ 本次 intent 认领集合（B1 前置提示）；
    - ``outcome=no_issue`` 必须声明 ``tested_scope``（C9 充分性，与 21 服务端一致）。
    返回规范化 dict（含 ``partial`` 布尔）。
    """
    if not isinstance(coverage, dict):
        raise PayloadError("coverage 必须是对象")
    covered = coverage.get("covered_items")
    if not isinstance(covered, list) or not covered or not all(isinstance(c, str) and c for c in covered):
        raise PayloadError("coverage.covered_items 必须为非空字符串数组")
    known = set(known_item_ids or ())
    if known and not known.issuperset(covered):
        bad = [c for c in covered if c not in known]
        raise PayloadError(f"coverage.covered_items 引用了本 engagement 之外/未覆盖覆盖项: {bad}")
    claimed = set(claimed_item_ids or ())
    if claimed and not claimed.issuperset(covered):
        bad = [c for c in covered if c not in claimed]
        raise PayloadError(f"coverage.covered_items 未全部由本次 intent 认领（B1）: {bad}")
    depth = coverage.get("depth_achieved")
    if depth not in DEPTHS:
        raise PayloadError(f"coverage.depth_achieved 非法: {depth!r}")
    outcome = coverage.get("outcome")
    if outcome not in COVERAGE_OUTCOMES:
        raise PayloadError(f"coverage.outcome 非法: {outcome!r}")
    ts = coverage.get("tested_scope")
    if isinstance(ts, dict) and "partial" in ts and not isinstance(ts["partial"], bool):
        raise PayloadError("coverage.tested_scope.partial 必须是布尔")
    if outcome == "no_issue" and not ts:
        raise PayloadError("coverage.outcome=no_issue 必须声明 tested_scope（C9 充分性）")
    return {
        "covered_items": list(covered),
        "depth_achieved": depth,
        "outcome": outcome,
        "tested_scope": ts,
        "partial": bool((ts or {}).get("partial", False)) or bool(coverage.get("partial")),
    }


def validate_explore_payload(
    payload: dict,
    *,
    known_item_ids: Iterable[str] = (),
    claimed_item_ids: Iterable[str] = (),
) -> dict:
    """explore 输出校验（description + findings[] + coverage 必填）。"""
    data = parse_accepted(payload)
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise PayloadError("data.description 必须非空")
    findings = validate_findings_payload(data.get("findings") or [])
    coverage = validate_coverage_result(
        data.get("coverage"),
        known_item_ids=known_item_ids,
        claimed_item_ids=claimed_item_ids,
    )
    if findings and coverage["outcome"] != "finding_created":
        raise PayloadError("findings 非空时 coverage.outcome 应为 finding_created")
    data["findings"] = findings
    data["coverage"] = coverage
    return data


def validate_reason_payload(
    payload: dict,
    *,
    gap_item_ids: Iterable[str] = (),
    valid_fact_ids: Iterable[str] = (),
    max_intents: Optional[int] = None,
    high_priority_gaps: bool = False,
) -> dict:
    """reason 输出校验（intents 引用覆盖项 / coverage / 禁 complete / 收敛硬约束）。

    - ``gap_item_ids``：本次喂给模型的缺口覆盖项 id 集合（exclude_in_progress=True，
      B1）；``intents[].coverage_item_ids`` 必须 ⊆ 该集合；
    - ``valid_fact_ids``：图合法 fact id（from 引用不得含 ``goal``）；
    - ``high_priority_gaps``：存在 priority ≥ 阈值的缺口（收敛硬约束前提）。
    """
    data = parse_accepted(payload)
    intents = data.get("intents") or []
    if not isinstance(intents, list):
        raise PayloadError("data.intents 必须是数组")
    if max_intents is not None and len(intents) > max_intents:
        intents = intents[:max_intents]
        data["intents"] = intents
    gap_ids = set(gap_item_ids or ())
    facts = set(valid_fact_ids or ())
    for i, it in enumerate(intents):
        if not isinstance(it, dict):
            raise PayloadError(f"intents[{i}] 不是对象")
        frm = it.get("from") or []
        if not isinstance(frm, list) or not frm or not all(isinstance(f, str) and f for f in frm):
            raise PayloadError(f"intents[{i}].from 必须为非空字符串数组")
        if facts and any(f not in facts for f in frm):
            bad = [f for f in frm if f not in facts]
            raise PayloadError(f"intents[{i}].from 引用了非法 fact id: {bad}")
        desc = it.get("description")
        if not isinstance(desc, str) or not desc.strip():
            raise PayloadError(f"intents[{i}].description 必须非空")
        cids = it.get("coverage_item_ids") or []
        if not isinstance(cids, list) or not cids or not all(isinstance(c, str) and c for c in cids):
            raise PayloadError(f"intents[{i}].coverage_item_ids 必须为非空字符串数组")
        if gap_ids and not gap_ids.issuperset(cids):
            bad = [c for c in cids if c not in gap_ids]
            raise PayloadError(f"intents[{i}].coverage_item_ids 引用了未覆盖范围之外覆盖项: {bad}")
    coverage = data.get("coverage") or {}
    if not isinstance(coverage, dict):
        raise PayloadError("data.coverage 必须是对象")
    if "recommend_finalize" in coverage and not isinstance(coverage["recommend_finalize"], bool):
        raise PayloadError("data.coverage.recommend_finalize 必须是布尔")
    if "reason" in coverage and not isinstance(coverage["reason"], str):
        raise PayloadError("data.coverage.reason 必须是字符串")
    waivers = coverage.get("waivers") or []
    if not isinstance(waivers, list):
        raise PayloadError("data.coverage.waivers 必须是数组")
    for i, w in enumerate(waivers):
        if not isinstance(w, dict):
            raise PayloadError(f"coverage.waivers[{i}] 不是对象")
        if w.get("kind") not in WAIVER_KINDS:
            raise PayloadError(f"coverage.waivers[{i}].kind 非法: {w.get('kind')!r}")
        if not isinstance(w.get("reason"), str) or not w["reason"].strip():
            raise PayloadError(f"coverage.waivers[{i}].reason 必填")
    # 收敛硬约束（coverage spec §3.1 规则 3）：高优先缺口存在 → 必须出 intent 或 finalize
    if high_priority_gaps and not intents and not coverage.get("recommend_finalize"):
        raise PayloadError(
            "覆盖未满：存在高优先缺口但既无 intents 也无 recommend_finalize=true（收敛约束）"
        )
    data["coverage"] = coverage
    return data


def validate_bootstrap_payload(payload: dict) -> dict:
    """bootstrap 输出校验（fact + sweep_complete + discoveries）。``complete`` 被拒。"""
    data = parse_accepted(payload)
    fact = data.get("fact")
    if not isinstance(fact, dict) or not isinstance(fact.get("description"), str) or not fact["description"].strip():
        raise PayloadError("data.fact.description 必填")
    discoveries = data.get("discoveries") or []
    if not isinstance(discoveries, list):
        raise PayloadError("data.discoveries 必须是数组")
    for i, d in enumerate(discoveries):
        if not isinstance(d, dict) or not d.get("target"):
            raise PayloadError(f"discoveries[{i}].target 必填")
    cov = data.get("coverage") or {}
    if not isinstance(cov, dict):
        raise PayloadError("data.coverage 必须是对象")
    if "outcome" in cov and cov["outcome"] not in COVERAGE_OUTCOMES:
        raise PayloadError(f"data.coverage.outcome 非法: {cov['outcome']!r}")
    sc = data.get("sweep_complete")
    if sc is not None and not (isinstance(sc, dict) or isinstance(sc, bool)):
        raise PayloadError("data.sweep_complete 必须为对象或布尔")
    return data


def validate_verify_blind_payload(payload: dict) -> dict:
    """verify 阶段一（盲审）输出校验：``observations`` 必须存在且为数组（可为空，
    诚实负面是合法答案，prompts §4.1）。"""
    data = parse_accepted(payload)
    obs = data.get("observations")
    if obs is None:
        raise PayloadError("data.observations 必填")
    if not isinstance(obs, list):
        raise PayloadError("data.observations 必须是数组")
    for i, o in enumerate(obs):
        if not isinstance(o, dict):
            raise PayloadError(f"observations[{i}] 不是对象")
        if "vuln" not in o:
            raise PayloadError(f"observations[{i}].vuln 必填")
        if "severity" in o and o["severity"] not in SEVERITIES:
            raise PayloadError(f"observations[{i}].severity 非法: {o['severity']!r}")
    return data


def validate_verify_compare_payload(
    payload: dict,
    *,
    traffic_ids: Iterable[str] = (),
) -> dict:
    """verify 阶段二（对照）输出校验：stage=comparison + verdict/severity/reason/
    verified_traffic_ids（⊆ engagement 流量）/http_mismatch。"""
    data = parse_accepted(payload)
    if data.get("stage") != "comparison":
        raise PayloadError("data.stage 必须为 comparison")
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise PayloadError(f"data.verdict 非法: {verdict!r}")
    sev = data.get("verified_severity")
    if sev not in SEVERITIES:
        raise PayloadError(f"data.verified_severity 非法: {sev!r}")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise PayloadError("data.reason 必填")
    vtids = data.get("verified_traffic_ids") or []
    if not isinstance(vtids, list) or not all(isinstance(t, str) for t in vtids):
        raise PayloadError("data.verified_traffic_ids 必须是字符串数组")
    known = set(traffic_ids or ())
    if known and any(t not in known for t in vtids):
        bad = [t for t in vtids if t not in known]
        raise PayloadError(f"data.verified_traffic_ids 引用了不存在的流量: {bad}")
    if "http_mismatch" in data and not isinstance(data["http_mismatch"], bool):
        raise PayloadError("data.http_mismatch 必须是布尔")
    return data


def validate_replay_result(result: Any) -> dict:
    """replay 引擎结果校验：matched_original 整数 + result ∈ 枚举。"""
    if not isinstance(result, dict):
        raise PayloadError("replay result 必须是对象")
    matched = result.get("matched_original")
    if not isinstance(matched, int) or matched < 0:
        raise PayloadError("replay result.matched_original 必须为非负整数")
    r = result.get("result")
    if r not in REPLAY_RESULTS:
        raise PayloadError(f"replay result.result 非法: {r!r}")
    return result


# ---------------------------------------------------------------------------
# 写回重试（G：findings/coverage 写失败退避 1 次再放弃）
# ---------------------------------------------------------------------------


def with_retry(
    fn: Callable[[], Any],
    *,
    retries: int = 1,
    backoff: float = 0.5,
    log: Optional[Callable[[str], None]] = None,
) -> Any:
    """执行 ``fn``；临时失败（网络/5xx）退避 ``backoff`` 秒重试至多 ``retries`` 次。

    确定性拒绝（``NON_RETRYABLE_CODES``）与模型拒绝直接抛出，不重试。仍失败抛原异常。
    """
    attempt = 0
    while True:
        try:
            return fn()
        except CairnClientError as exc:
            if exc.error_code in NON_RETRYABLE_CODES or exc.http_status and 400 <= exc.http_status < 500:
                raise
            if attempt >= retries:
                raise
            attempt += 1
            if log:
                log(f"写回重试 {attempt}/{retries}: {exc}")
            time.sleep(backoff)
        except (httpx.HTTPError, OSError) as exc:
            if attempt >= retries:
                raise
            attempt += 1
            if log:
                log(f"写回重试 {attempt}/{retries}: {exc}")
            time.sleep(backoff)


# ---------------------------------------------------------------------------
# 进度上报（F9：摘要 ≤512B 落 append_event）
# ---------------------------------------------------------------------------


def summarize_event(message: str, *, max_bytes: int = 512) -> str:
    """截断事件摘要到 ``max_bytes``（默认 512，tuning.event_summary_max_bytes）。

    截断时为省略号预留字节，保证编码后 ≤ ``max_bytes``。
    """
    if message is None:
        return ""
    data = message.encode("utf-8", "replace")
    if len(data) <= max_bytes:
        return message
    marker = "…".encode("utf-8")
    keep = max(0, max_bytes - len(marker))
    return data[:keep].decode("utf-8", "replace") + "…"


def emit_event(
    ctx: TaskContext,
    kind: str,
    level: str,
    message: str,
    *,
    raw_path: Optional[str] = None,
) -> None:
    """进度事件上报（经 CairnClient.append_event；失败只记日志不阻断任务）。"""
    if not ctx.run_id or ctx.client is None:
        return
    max_bytes = 512
    if ctx.config is not None:
        tuning = getattr(ctx.config, "tuning", None)
        if tuning is not None and getattr(tuning, "event_summary_max_bytes", None):
            max_bytes = tuning.event_summary_max_bytes
    try:
        ctx.client.append_event(
            ctx.run_id,
            kind=kind,
            level=level,
            message=summarize_event(message, max_bytes=max_bytes),
            raw_path=raw_path,
        )
    except Exception as exc:  # noqa: BLE001 —— 进度上报失败不应使任务失败
        ctx.log(f"append_event 失败（忽略）: {exc}")


# ---------------------------------------------------------------------------
# 执行编排（13 §7：单阶段 LLM 调用；取消全程挂载）
# ---------------------------------------------------------------------------


def run_worker_phase(
    ctx: TaskContext,
    *,
    driver: Any,
    backend: Any,
    prompt: str,
    timeout: Optional[float] = None,
    session_id: Optional[str] = None,
    phase: Optional[str] = None,
    stage: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """执行一次驱动阶段，返回 ``(response_text, session_id)``。

    编排：prepare_session → build_execute → backend.build_exec_process →
    communicate → extract_session → extract_response_text。``cancellation.attach_process``
    在进程创建后立即挂载，取消/kill switch 即杀进程。超时由调用方经
    ``backend.build_exec_process(timeout=...)`` 传入（来源 tasks.*.timeout）。

    ``phase``/``stage`` 转发给 ``driver.build_execute``（31 mock 驱动用
    ``mock-phase:``/``mock-stage:`` 标记识别阶段；claude/codex/pi 驱动忽略）。
    """
    if ctx.cancellation is not None and ctx.cancellation.cancelled:
        raise CancelledError(ctx.cancellation.reason or "cancelled")
    if session_id is None:
        session_id = driver.prepare_session()
    cmd = driver.build_execute(prompt, session_id=session_id, phase=phase, stage=stage)
    proc = backend.build_exec_process(cmd.argv, env=cmd.env, timeout=timeout)
    if ctx.cancellation is not None:
        ctx.cancellation.attach_process(proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        if ctx.cancellation is not None:
            ctx.cancellation.detach_process(proc)
    if ctx.cancellation is not None and ctx.cancellation.cancelled:
        raise CancelledError(ctx.cancellation.reason or "cancelled")
    sid = driver.extract_session(stdout, stderr) or session_id
    text = driver.extract_response_text(stdout) or ""
    if not text.strip():
        raise PayloadError("驱动输出为空（empty response）")
    return text, sid


def run_conclude_phase(
    ctx: TaskContext,
    *,
    driver: Any,
    backend: Any,
    prompt: str,
    timeout: Optional[float] = None,
    session_id: Optional[str] = None,
    phase: Optional[str] = None,
    stage: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """执行 conclude（二阶段收尾）驱动调用（同 execute，但走 ``build_conclude``）。"""
    if ctx.cancellation is not None and ctx.cancellation.cancelled:
        raise CancelledError(ctx.cancellation.reason or "cancelled")
    if session_id is None:
        session_id = driver.prepare_session()
    cmd = driver.build_conclude(prompt, session_id=session_id, phase=phase, stage=stage)
    proc = backend.build_exec_process(cmd.argv, env=cmd.env, timeout=timeout)
    if ctx.cancellation is not None:
        ctx.cancellation.attach_process(proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        if ctx.cancellation is not None:
            ctx.cancellation.detach_process(proc)
    if ctx.cancellation is not None and ctx.cancellation.cancelled:
        raise CancelledError(ctx.cancellation.reason or "cancelled")
    sid = driver.extract_session(stdout, stderr) or session_id
    text = driver.extract_response_text(stdout) or ""
    if not text.strip():
        raise PayloadError("conclude 驱动输出为空")
    return text, sid


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """ISO8601 UTC 时间戳（黄金不变量 8）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def writeback_retries(ctx: TaskContext, *, default: int = 1) -> int:
    """写回重试次数（tuning.writeback_retries，默认 1；G 契约）。"""
    try:
        tuning = getattr(ctx.config, "tuning", None)
        if tuning is not None:
            return int(getattr(tuning, "writeback_retries", default))
    except (AttributeError, TypeError, ValueError):  # pragma: no cover
        pass
    return default
