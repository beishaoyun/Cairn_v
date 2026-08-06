"""mitmdump addon —— 真实抓包写回 traffic_root + 索引回写（Agent 23 · F8）。

被 ``dispatcher/capture/proxy.py`` 的 ``MitmProxyEngine`` 以 ``mitmdump -s addon.py``
挂载。**纯 stdlib、不 import cairn / mitmproxy** —— 保证 mitmdump 环境可独立加载
（无需把 cairn 装进 mitmproxy 环境）。mitmproxy 的 flow 对象按鸭子类型消费。

契约（与 ``server/services/capture.py`` 读取侧严格对齐）：

- 写 ``{traffic_root}/{eid}/{seq}/req.bin`` / ``resp.bin``（原始 HTTP 字节，
  头/体 ``\r\n\r\n`` 分隔，供 ``_read_payload``/``make_digest`` 解析）；
- 大包（>100MB，与 ``CHUNK_THRESHOLD_BYTES`` 一致）按 ``chunk_count`` 分片：
  ``req.bin.{0..n-1}``/``resp.bin.{0..n-1}``；req/resp 统一用同一 chunk_count
  （``server.services.capture._read_payload`` 用同一 chunk_count 读 req/resp 两侧）；
- sha256 = sha256(req ‖ resp)（``_package_sha256`` 语义）；
- ``POST {server_url}/engagements/{eid}/traffic``，Bearer ``CAIRN_CAPTURE_TOKEN``，
  body 字段与 ``TrafficIndexRequest``（routers/traffic.py）完全一致（extra=forbid）；
  失败记 stderr，不阻塞代理。

参数经环境变量（proxy.py 注入）：

- ``CAIRN_TRAFFIC_ROOT`` / ``CAIRN_EID`` / ``CAIRN_SERVER_URL`` / ``CAIRN_CAPTURE_TOKEN``
- ``CAIRN_ALLOW_HOSTS``（fail-closed 白名单，逗号分隔） / ``CAIRN_NO_HOSTS``
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from typing import Any, Optional

#: 与 server/services/capture.CHUNK_THRESHOLD_BYTES 对齐（>100MB 分片）
CHUNK_THRESHOLD_BYTES = 100 * 1024 * 1024


def _header_pairs(headers: Any) -> list[tuple[str, str]]:
    """兼容 mitmproxy ``Headers``（``items(multi=True)``）与普通 dict/列表。"""
    items_fn = getattr(headers, "items", None)
    if items_fn is None:
        return []
    try:
        multi = items_fn(multi=True)
        return [(str(k), str(v)) for k, v in multi]
    except TypeError:
        return [(str(k), str(v)) for k, v in headers.items()]


def _host_of(flow: Any) -> str:
    """代理连接的目标 host（hostname，不含端口）。"""
    host = getattr(flow.request, "host", None) or ""
    return str(host)


def _pretty_url(flow: Any) -> str:
    pretty = getattr(flow.request, "pretty_url", None)
    if pretty:
        return str(pretty)
    host = _host_of(flow)
    path = getattr(flow.request, "path", "") or ""
    return f"http://{host}{path}"


def _raw_request(flow: Any) -> bytes:
    """重建原始请求字节（头块 \r\n\r\n + 体；供读取侧 ``_split_http`` 解析）。"""
    req = flow.request
    method = str(getattr(req, "method", "") or "GET")
    path = str(getattr(req, "path", "") or "/")
    http_version = str(getattr(req, "http_version", "") or "1.1")
    head = "\r\n".join(
        [f"{method} {path} HTTP/{http_version}"]
        + [f"{k}: {v}" for k, v in _header_pairs(getattr(req, "headers", {}))]
    ) + "\r\n\r\n"
    content = getattr(req, "content", None) or b""
    return head.encode("latin-1", "replace") + bytes(content)


def _raw_response(flow: Any) -> bytes:
    """重建原始响应字节。"""
    resp = flow.response
    http_version = str(getattr(resp, "http_version", "") or "1.1")
    status = int(getattr(resp, "status_code", 0) or 0)
    reason = str(getattr(resp, "reason", "") or "")
    head = "\r\n".join(
        [f"HTTP/{http_version} {status} {reason}".rstrip()]
        + [f"{k}: {v}" for k, v in _header_pairs(getattr(resp, "headers", {}))]
    ) + "\r\n\r\n"
    content = getattr(resp, "content", None) or b""
    return head.encode("latin-1", "replace") + bytes(content)


class CairnCaptureAddon:
    """mitmdump addon：fail-closed 白名单 + 落盘 + F8 索引回写。"""

    def __init__(self, *, index_fn: Optional[Any] = None) -> None:
        self.traffic_root = os.environ.get("CAIRN_TRAFFIC_ROOT", "")
        self.eid = os.environ.get("CAIRN_EID", "")
        self.server_url = os.environ.get("CAIRN_SERVER_URL", "").rstrip("/")
        self.capture_token = os.environ.get("CAIRN_CAPTURE_TOKEN", "")
        self.allow_hosts: set[str] = set(
            h for h in os.environ.get("CAIRN_ALLOW_HOSTS", "").split(",") if h
        )
        self.no_hosts: set[str] = set(
            h for h in os.environ.get("CAIRN_NO_HOSTS", "").split(",") if h
        )
        self._index_fn = index_fn  # 测试注入（替代 HTTP 回写）
        self._seq = self._init_seq()

    # ------------------------------------------------------------- mitm hooks

    def request(self, flow: Any) -> None:
        """request 钩子：fail-closed 预判。实际落盘在 response 钩子（再复核）。"""
        if not self._allowed(_host_of(flow)):
            meta = getattr(flow, "metadata", None)
            if meta is not None:
                meta["cairn_skip"] = True

    def response(self, flow: Any) -> None:
        try:
            self.capture_flow(flow)
        except Exception as exc:  # noqa: BLE001 —— 单条流量失败不阻塞代理
            sys.stderr.write(f"cairn-capture: response hook failed: {exc!r}\n")

    # ------------------------------------------------------------- capture

    def _allowed(self, host: str) -> bool:
        """F5 fail-closed：记录 ⇔ host ∈ allow_capture_hosts 且 ∉ no_capture_hosts。"""
        if not host:
            return False
        return host in self.allow_hosts and host not in self.no_hosts

    def _init_seq(self) -> int:
        """从 ``{traffic_root}/{eid}`` 现有 seq 目录推断下一个序号（mitmdump 重启不覆盖）。"""
        seq = 0
        base = os.path.join(self.traffic_root, self.eid)
        if os.path.isdir(base):
            for name in os.listdir(base):
                if name.isdigit():
                    seq = max(seq, int(name))
        return seq + 1

    def _next_seq(self) -> int:
        cur = self._seq
        self._seq += 1
        return cur

    def _write_chunks(self, rel_base: str, data: bytes, chunk_count: int) -> str:
        """写 ``{traffic_root}/{rel_base}[.{i}]``。chunk_count>1 → 统一 .i 后缀。"""
        full_base = os.path.join(self.traffic_root, rel_base)
        os.makedirs(os.path.dirname(full_base), exist_ok=True)
        if chunk_count <= 1:
            with open(full_base, "wb") as fh:
                fh.write(data)
            return rel_base
        for i in range(chunk_count):
            chunk = data[i * CHUNK_THRESHOLD_BYTES:(i + 1) * CHUNK_THRESHOLD_BYTES]
            with open(f"{full_base}.{i}", "wb") as fh:
                fh.write(chunk)
        return rel_base

    def _write_pair(self, base: str, req: bytes, resp: bytes) -> tuple[str, str, int]:
        """统一分片写 req/resp：chunk_count>1 时两侧都写 .i 后缀（读取侧同 count 拼回）。"""
        req_count = max(1, (len(req) + CHUNK_THRESHOLD_BYTES - 1) // CHUNK_THRESHOLD_BYTES)
        resp_count = max(1, (len(resp) + CHUNK_THRESHOLD_BYTES - 1) // CHUNK_THRESHOLD_BYTES)
        chunk_count = max(req_count, resp_count)
        req_path = self._write_chunks(f"{base}/req.bin", req, chunk_count)
        resp_path = self._write_chunks(f"{base}/resp.bin", resp, chunk_count)
        return req_path, resp_path, chunk_count

    def capture_flow(self, flow: Any) -> Optional[dict]:
        """对一次完整 flow 落盘 + 回写索引。白名单外 → 返回 None（不落盘）。"""
        if getattr(flow, "response", None) is None:
            return None
        host = _host_of(flow)
        if not self._allowed(host):
            return None
        req = _raw_request(flow)
        resp = _raw_response(flow)
        seq = self._next_seq()
        req_path, resp_path, chunk_count = self._write_pair(
            os.path.join(self.eid, str(seq)), req, resp
        )
        sha256 = hashlib.sha256(req + resp).hexdigest()

        client_ip = None
        conn = getattr(flow, "client_conn", None)
        peername = getattr(conn, "peername", None) if conn is not None else None
        if peername:
            client_ip = str(peername[0])

        entry = {
            "method": str(getattr(flow.request, "method", "") or "").upper(),
            "url": _pretty_url(flow),
            "host": host,
            "client_ip": client_ip,
            "status": int(getattr(flow.response, "status_code", 0) or 0),
            "req_path": req_path,
            "resp_path": resp_path,
            "req_bytes": len(req),
            "resp_bytes": len(resp),
            "content_type": self._content_type(flow),
            "sha256": sha256,
            "chunk_count": chunk_count,
            "seq": seq,
        }
        self._index(entry)
        return entry

    @staticmethod
    def _content_type(flow: Any) -> Optional[str]:
        ct = None
        headers = getattr(flow.response, "headers", {})
        items_fn = getattr(headers, "get", None)
        if items_fn is not None:
            try:
                ct = items_fn("Content-Type") or items_fn("content-type")
            except Exception:  # noqa: BLE001
                ct = None
        if ct is None:
            for k, v in _header_pairs(headers):
                if k.lower() == "content-type":
                    ct = v
                    break
        return str(ct) if ct else None

    # ------------------------------------------------------------- F8 回写

    def _index(self, entry: dict) -> None:
        """POST 索引回写（Bearer CAIRN_CAPTURE_TOKEN）。失败记 stderr 不阻塞。"""
        if self._index_fn is not None:
            self._index_fn(entry)
            return
        if not self.server_url or not self.eid or not self.capture_token:
            sys.stderr.write("cairn-capture: 索引回写缺 server_url/eid/capture_token，跳过\n")
            return
        body = json.dumps(entry).encode("utf-8")
        req = urllib.request.Request(
            f"{self.server_url}/engagements/{self.eid}/traffic",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.capture_token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                resp.read()
        except Exception as exc:  # noqa: BLE001 —— 回写失败不阻塞代理
            sys.stderr.write(f"cairn-capture: 索引回写失败: {exc!r}\n")


#: mitmdump 自动发现的 addon 实例
addons = [CairnCaptureAddon()]
