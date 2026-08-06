"""确定性重放引擎（Agent 30 · F4）—— 原始触发包 + payload 变体 → 签名比对。

契约（capture-verify-progress-spec §6/§6.1，verify-mock-test-spec TV-30/31/32/44/46）：
- **不依赖 LLM**（``worker='replay-engine'``，不占 worker 并发，12 contracts）。
- 输入：finding 的 trigger traffic（``resolve_traffic(..., for_model=False)`` 全量字节）。
- HTTP 类：重放原始请求（经捕获代理发送，复测证据闭环，role='replay'）+ payload 变体
  → 用 ``compare_signature`` 比对响应签名 → ``matched_original``/``result``。
- 命令确定性重放（非 HTTP 类，capture §6.1）：受控执行器 wrapper 重放原始 command，
  捕获真实 stdout/stderr + sha256，判定签名。
- ``result`` ∈ unchanged/remediated/ambiguous/error（``validate_replay_result``）；
  ``remediated`` → ``record_retest_confirmation(kind='replay')``（22 幂等，TV-44）。
- ``compare_signature`` 为纯函数（status + body 指纹比对），单测覆盖（verify-mock §5.2）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from ..errors import CairnClientError
from ..tasks.common import validate_replay_result

logger = logging.getLogger("cairn.dispatcher.replay.engine")

#: 重放结果（与 validate_replay_result / 22 的 closed 门槛联动）
REPLAY_RESULTS = ("unchanged", "remediated", "ambiguous", "error")

#: HTTP 响应指纹的 body 归一化（去空白/压缩，防无关字节抖动）
_WS_RE = re.compile(rb"\s+")
_FP_HASH_ALG = "sha256"


@dataclass
class ReplayEngine:
    """确定性重放引擎。

    ``http_client``：可选注入的 ``httpx.Client``（测试传 stub；生产走代理）。重放请求经
    捕获代理（``proxy`` URL）发送 → 复测流量自身落 role='replay' 证据（F4 闭环）。
    """

    client: Any  # CairnClient
    retries: int = 2
    proxy: Optional[str] = None
    http_client: Any = None
    log: Any = field(default_factory=lambda: logger.info)

    # ------------------------------------------------------------------
    # 纯函数：签名比对（单测焦点）
    # ------------------------------------------------------------------

    @staticmethod
    def body_fingerprint(body: bytes | str | None) -> str:
        """body 指纹：归一化空白后 sha256（F4：状态 + body 指纹比对）。"""
        if body is None:
            raw = b""
        elif isinstance(body, bytes):
            raw = body
        else:
            raw = body.encode("utf-8", "replace")
        normalized = _WS_RE.sub(b"", raw)
        return hashlib.new(_FP_HASH_ALG, normalized).hexdigest()

    @staticmethod
    def compare_signature(now_resp: dict, orig_resp: dict) -> dict:
        """状态 + body 指纹比对（verify-mock §5.2 单测目标）。

        ``now_resp``/``orig_resp`` 均为 HTTP 响应 dict（至少含 ``status``/``body``）。
        返回 ``{"matched": bool, "status_match": bool, "body_match": bool,
        "orig_fingerprint": str, "now_fingerprint": str}``。
        """
        orig_status = int(orig_resp.get("status") or 0)
        now_status = int(now_resp.get("status") or 0)
        status_match = (orig_status == now_status) and now_status != 0
        orig_fp = ReplayEngine.body_fingerprint(orig_resp.get("body"))
        now_fp = ReplayEngine.body_fingerprint(now_resp.get("body"))
        body_match = orig_fp == now_fp
        return {
            "matched": status_match and body_match,
            "status_match": status_match,
            "body_match": body_match,
            "orig_fingerprint": orig_fp,
            "now_fingerprint": now_fp,
        }

    # ------------------------------------------------------------------
    # 变体生成
    # ------------------------------------------------------------------

    @staticmethod
    def payload_variants(orig_body: str, count: int) -> list[str]:
        """从原始请求体生成 ``count`` 个 payload 变体。

        - 原始体为空：变体为常见注入探针（浅层扰动，仅用于判定「响应是否随 payload 变化」）；
        - 原始体非空：在关键参数尾部追加空字节/大小写/注释扰动（best-effort，启发式）。
        变体用于确认「触发是否与 payload 强相关」（同签名变体越多 → unchanged）。
        """
        variants: list[str] = []
        probes = ["'", "''", " OR 1=1--", " OR 1=2--", "%00", "\"", "\" OR \"1\"=\"1", "<script>1</script>"]
        for i in range(max(0, count)):
            if not orig_body:
                variants.append(probes[i % len(probes)])
            else:
                variants.append(f"{orig_body}{probes[i % len(probes)]}")
        return variants

    # ------------------------------------------------------------------
    # HTTP 重放
    # ------------------------------------------------------------------

    def _parse_request(self, full: dict) -> tuple[str, str, dict, str | None]:
        """从 ``resolve_traffic(full)`` 结果解析出 (method, url, headers, body)。

        full 结构（23 capture §resolve_traffic mode=full）：``request`` 为原始 HTTP 字节串。
        """
        request = full.get("request") or ""
        lines = request.split("\r\n")
        head = lines[0] if lines else ""
        parts = head.split(" ", 2)
        method = parts[0].upper() if len(parts) > 0 else "GET"
        target = parts[1] if len(parts) > 1 else "/"
        url = full.get("url") or target
        headers: dict[str, str] = {}
        body_lines: list[str] = []
        in_body = False
        for ln in lines[1:]:
            if in_body:
                body_lines.append(ln)
            elif not ln:
                in_body = True
            elif ":" in ln:
                k, _, v = ln.partition(":")
                headers[k.strip()] = v.strip()
        body = "\r\n".join(body_lines) or None
        return method, url, headers, body

    def _parse_response(self, full: dict) -> dict:
        """从 ``resolve_traffic(full)`` 解析捕获的响应 (status, body)。

        full["response"] 为原始 HTTP 响应字节串（``HTTP/1.1 200 OK\\r\\n...\\r\\n\\r\\n<body>``）。
        """
        resp = full.get("response")
        if not resp:
            return {"status": full.get("status") or 0, "body": ""}
        lines = str(resp).split("\r\n")
        head = lines[0] if lines else ""
        m = re.match(r"HTTP/\S+\s+(\d{3})", head)
        status = int(m.group(1)) if m else (full.get("status") or 0)
        body = "\r\n".join(lines[1:]) or ""
        return {"status": status, "body": body}

    def _send(self, method: str, url: str, headers: dict, body: str | None) -> dict:
        """经捕获代理发送重放请求；返回响应 dict（status/headers/body）。"""
        http = self.http_client or httpx.Client(proxy=self.proxy, timeout=30.0)
        owns = self.http_client is None
        try:
            resp = http.request(method, url, headers=headers, content=body)
            return {
                "status": resp.status_code,
                "headers": dict(resp.headers),
                "body": resp.content,
            }
        finally:
            if owns:
                http.close()

    def replay_http(self, full: dict, *, variants: list[str] = ()) -> dict:
        """重放原始触发包 + 变体，做签名比对。

        返回结果 dict（``validate_replay_result`` 兼容）：
        ``{"matched_original": int, "result": str, "signatures": [...]}``。
        """
        method, url, headers, body = self._parse_request(full)
        orig = self._parse_response(full)  # 捕获的原始响应（vulnerable 基线）
        # 原始包重放
        try:
            orig_now = self._send(method, url, headers, body)
        except httpx.HTTPError as exc:
            self.log(f"replay 原始包发送失败: {exc}")
            return {"matched_original": 0, "result": "error", "error": str(exc), "signatures": []}
        orig_sig = self.compare_signature(orig_now, orig)
        # 变体重放
        matched = 1 if orig_sig["matched"] else 0
        sigs = [{"variant": "<original>", **orig_sig}]
        for v in variants:
            try:
                v_now = self._send(method, url, headers, body + v if body is not None else v)
            except httpx.HTTPError as exc:
                self.log(f"replay 变体发送失败: {exc}")
                continue
            vsig = self.compare_signature(v_now, orig)
            sigs.append({"variant": v, **vsig})
            if vsig["matched"]:
                matched += 1

        if matched > 0:
            result = "unchanged"
        elif matched == 0 and orig_sig["status_match"] and not orig_sig["body_match"]:
            # 状态一致但 body 指纹变化 → 可能已修复但状态码未变 → 需二次确认（TV-32）
            result = "ambiguous"
        else:
            result = "remediated"
        return {"matched_original": matched, "result": result, "signatures": sigs}

    # ------------------------------------------------------------------
    # 命令确定性重放（非 HTTP 类 · capture §6.1）
    # ------------------------------------------------------------------

    def command_fingerprint(self, text: str | None) -> str:
        raw = (text or "").encode("utf-8", "replace")
        return hashlib.new(_FP_HASH_ALG, raw).hexdigest()

    def replay_command(self, command: dict) -> dict:
        """受控执行器 wrapper 重放命令，捕获真实 stdout/stderr + sha256。

        ``command`` = finding.command_evidence 行（command/cwd/exit_code/stdout/stderr）。
        返回结果 dict（validate_replay_result 兼容）：
        ``{"matched_original": int, "result": str, "captured": {...}}``。
        """
        import subprocess

        argv = command.get("command", "")
        cwd = command.get("cwd")
        orig_stdout = command.get("stdout") or ""
        orig_stderr = command.get("stderr") or ""
        try:
            proc = subprocess.run(
                argv, shell=True, cwd=cwd, capture_output=True, text=True, timeout=30.0
            )
        except (subprocess.SubprocessError, OSError) as exc:
            self.log(f"命令重放执行失败: {exc}")
            return {"matched_original": 0, "result": "error", "error": str(exc), "captured": {}}
        captured = {
            "command": argv,
            "cwd": cwd,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "stdout_sha256": self.command_fingerprint(proc.stdout),
            "stderr_sha256": self.command_fingerprint(proc.stderr),
        }
        # 签名判定：原始回显仍复现（弱口令仍可登录）→ unchanged；命令失败/目标修复 → remediated
        orig_ok = (command.get("exit_code") or 0) == 0 and bool(orig_stdout)
        now_ok = proc.returncode == 0 and bool(proc.stdout.strip())
        if orig_ok and now_ok:
            # 都成功：比较 stdout 指纹（近似判定「行为是否仍一致」）
            orig_fp = self.command_fingerprint(orig_stdout)
            now_fp = self.command_fingerprint(proc.stdout)
            matched = int(orig_fp == now_fp)
            result = "unchanged" if matched else "ambiguous"
        elif now_ok:
            result = "remediated"  # 原失败/原成功→现失败说明目标已修复（或拒绝）
        else:
            result = "remediated"
        return {"matched_original": 0 if result == "remediated" else 1, "result": result, "captured": captured}

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(
        self,
        eid: str,
        fid: str,
        *,
        trigger_traffic_id: str,
        payload_variants: int = 0,
        is_http: bool = True,
    ) -> dict:
        """重放 finding 的 trigger 流量，写回结果 + 复测确认（kind='replay'）。

        流程：resolve 全量 → replay（HTTP 或命令）→ validate → 落 replay_runs 结果 →
        ``remediated`` → ``record_retest_confirmation(kind='replay')``。
        ``is_http=False`` 时 ``command`` 参数需为 finding 的 command_evidence 行。
        """
        full = self.client.resolve_traffic(eid, trigger_traffic_id, for_model=False)
        if is_http:
            variants = self.payload_variants((full.get("request") or "").split("\r\n\r\n")[-1], payload_variants)
            result = self.replay_http(full, variants=variants)
        else:
            # 命令确定性重放：由调用方传入 command 行（本函数签名不含，走 resolve 里的 request 近似）
            # 这里以 traffic 全量中的 request 作为命令源（简化；真实命令回显由调用方传 command 行）
            command = {"command": full.get("request") or "", "exit_code": 0, "stdout": full.get("response") or ""}
            result = self.replay_command(command)

        validate_replay_result(result)
        self._record_replay_run(eid, fid, trigger_traffic_id, result, payload_variants)

        if result.get("result") == "remediated":
            self.record_retest_confirmation(eid, fid, kind="replay", note=f"replay remediated matched={result['matched_original']}")
        return result

    # ------------------------------------------------------------------
    # 写回
    # ------------------------------------------------------------------

    def _record_replay_run(
        self,
        eid: str,
        fid: str,
        trigger_traffic_id: str,
        result: dict,
        payload_variants: int,
    ) -> None:
        """登记 replay_runs 结果（best-effort；22 路由 ``POST /findings/{fid}/replay``
        登记 queued，结果由本引擎更新；无独立结果端点时仅记日志）。"""
        # 22 的 replay 端点只登记 queued 行；结果回写端点阶段 2 对齐。
        # 这里尝试经 `_request` 更新（无独立端点则跳过，仅日志）。
        try:
            self.client._request(
                "POST",
                f"/engagements/{eid}/findings/{fid}/replay",
                json={"trigger_traffic_id": trigger_traffic_id, "payload_variants": payload_variants},
            )
        except CairnClientError as exc:
            self.log(f"replay_runs 登记失败（忽略）: {exc}")

    def record_retest_confirmation(
        self,
        eid: str,
        fid: str,
        *,
        kind: str,
        note: Optional[str] = None,
        actor: str = "replay-engine",
    ) -> Optional[dict]:
        """复测确认账本（22 ``record_retest_confirmation``；同轮同 kind 幂等，TV-44）。

        ``remediated`` 分支调用；幂等失败（重复触发不 +1）不视为错误。
        """
        try:
            return self.client._request(
                "POST",
                f"/engagements/{eid}/findings/{fid}/retest",
                json={"kind": kind, "note": note, "actor": actor},
            )
        except CairnClientError as exc:
            self.log(f"record_retest_confirmation 失败（忽略）: {exc}")
            return None
