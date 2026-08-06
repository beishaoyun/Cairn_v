"""Dispatcher 侧捕获辅助（Agent 23 所有 · skeleton §1 dispatcher/capture/client.py）。

职责（capture-verify-progress-spec §2.2/§2.5）：
- 白名单刷新拉取（C11）：经 ``CairnClient`` 拉 ``GET /engagements/{eid}/targets`` 派生
  ``allow_capture_hosts``（fail-closed F5），随 targets 增删刷新（≤ runtime.interval）；
- ``capture_gap`` 判定辅助（C2）：explore 声明数 vs 捕获数阈值判定；
- C12 归属：``client_ip`` → worker 名（bridge 独立 IP；host 网络 → None）。

纯逻辑 + 依赖注入：不持有 Server 地址/凭据（C5 由调用方给 CairnClient）。
"""

from __future__ import annotations

from typing import Optional

from ...server.services.capture import (
    assert_capture_allowed,
    resolve_client,
    _target_to_host,
)

__all__ = [
    "CaptureWhitelist",
    "derive_whitelist",
    "CaptureClient",
    "reconcile_gap",
    "resolve_client",
    "assert_capture_allowed",
]


class CaptureWhitelist:
    """代理白名单（fail-closed F5）：``log ⇔ host ∈ allow ∧ ∉ no``。

    Dispatcher 注入到代理/本地判定；``allow`` 为空 → 任何 host 不记录（默认安全）。
    """

    def __init__(self, allow_capture_hosts=None, no_capture_hosts=None) -> None:
        self.allow_capture_hosts: set[str] = set(allow_capture_hosts or ())
        self.no_capture_hosts: set[str] = set(no_capture_hosts or ())

    def allowed(self, host: str) -> bool:
        return assert_capture_allowed(
            host,
            allow_capture_hosts=self.allow_capture_hosts,
            no_capture_hosts=self.no_capture_hosts,
        )

    def update_allow(self, hosts) -> None:
        self.allow_capture_hosts = {str(h) for h in hosts}

    def clear(self) -> None:
        """kill/归档 → 白名单置空（C3 kill 即停）。"""
        self.allow_capture_hosts.clear()
        self.no_capture_hosts.clear()

    def to_dict(self) -> dict:
        return {
            "allow_capture_hosts": sorted(self.allow_capture_hosts),
            "no_capture_hosts": sorted(self.no_capture_hosts),
        }


def derive_whitelist(targets: list[dict], scope_policy: dict) -> CaptureWhitelist:
    """C11：由 targets（authorized）派生白名单 + scope_policy 的 no_capture_hosts 次级排除。"""
    cp = scope_policy.get("capture_proxy") or {}
    allow: set[str] = set()
    for t in targets or []:
        if t.get("scope_status") != "authorized":
            continue
        host = _target_to_host(t.get("value", ""), t.get("kind", ""))
        if host:
            allow.add(host)
    allow.update(str(h) for h in (cp.get("allow_capture_hosts") or []))
    no = cp.get("no_capture_hosts")
    no_hosts = {str(h) for h in no} if no else {
        "api.anthropic.com", "api.deepseek.com", "cairn-server",
    }
    return CaptureWhitelist(allow_capture_hosts=allow, no_capture_hosts=no_hosts)


class CaptureClient:
    """Dispatcher 侧白名单刷新拉取（依赖 ``CairnClient``；不缓存 Server 数据）。"""

    def __init__(self, api) -> None:
        self.api = api

    def fetch_targets(self, eid: str) -> list[dict]:
        """拉取 targets（GET /engagements/{eid}/targets）。"""
        return self.api.list_targets(eid)

    def refresh_whitelist(self, eid: str, scope_policy: dict) -> CaptureWhitelist:
        """C11：拉 targets → 派生白名单（供代理本地判定 / 注入 capture_proxy）。"""
        targets = self.fetch_targets(eid)
        return derive_whitelist(targets, scope_policy)

    def resolve_attribution(self, client_ip: Optional[str], ip_to_worker: dict) -> Optional[str]:
        """C12：client_ip → worker 名；无法区分（host 网络共享 IP）→ None。"""
        return resolve_client(client_ip, ip_to_worker)


def reconcile_gap(
    declared_count: int,
    captured_count: int,
    *,
    min_capture_ratio: float = 2.0,
    min_capture_abs_diff: int = 3,
) -> bool:
    """C2 capture_gap 判定（阈值来自 dispatch-config-spec §7 tuning.min_capture_ratio/
    min_capture_abs_diff）：声明数远超捕获数 → 疑似缺抓，verify 应 needs_more + 报告标注缺口。"""
    if declared_count <= 0:
        return False
    if captured_count <= 0:
        return True
    return (
        declared_count >= min_capture_ratio * captured_count
        and (declared_count - captured_count) >= min_capture_abs_diff
    )
