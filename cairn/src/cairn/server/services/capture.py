"""捕获子域服务层（Agent 23 所有 · capture-verify-progress-spec §2/§8/§9）。

真相源：捕获代理（mitmproxy）只写流量文件，索引经 ``POST /engagements/{id}/traffic``
回写本模块（F8 代理单写者）。Server 是唯一 DB 写者，本模块只落 ``traffic_entries``
与 ``finding_traffic_links`` 两张表；``finding_http_evidence`` 由 22 登记，本模块只
派生 captured 内容并经 22 的服务函数登记（不直写对方表）。

关键契约：
- F5 fail-closed：``assert_capture_allowed(host)`` —— host ∈ allow_capture_hosts 且 ∉
  no_capture_hosts，白名单之外透传不落盘；
- C11 热刷新：``allow_capture_hosts`` 由 authorized targets 派生，随 targets 增删即时
  刷新（dispatcher/capture/client.py 或 capture_proxy 轮询 targets）；kill/归档置空；
- F2 三层分离：全量文件（报告/审计/replay 用）→ digest（给模型，≤digest_budget，截断
  含 sha256 引用）→ DB 元数据；
- C12 归属：``client`` 由 ``client_ip`` 反查 worker（bridge 独立 IP）；host 网络共享 IP
  → client=NULL 记录 client_ip；
- C2 增强：``reconcile(eid)`` 声明数 vs 时间窗捕获数 → capture_gap 标记（落
  scheduler_state，供 30/40 读取）；``derive_http_from_capture`` 以捕获字节派生。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..db import next_id
from ..errors import CairnError, ErrorCode
from ..models import HttpEvidenceSource, TrafficLinkRole

#: F2：给模型的 digest 字节上限/会话（scope_policy.capture_proxy.digest_budget 默认值）
DEFAULT_DIGEST_BUDGET = 8192
#: F2：体前缀/后缀窗口（capture-verify-progress-spec §8.2）
DIGEST_BODY_PREFIX = 2048
DIGEST_BODY_SUFFIX = 512
#: F2：>100MB 分片存储阈值（DB 只记录 chunk_count，代理侧切分）
CHUNK_THRESHOLD_BYTES = 100 * 1024 * 1024

#: 捕获默认豁免主机（LLM API / Cairn Server / 健康检查；scope_policy 可覆盖）
DEFAULT_NO_CAPTURE_HOSTS = (
    "api.anthropic.com",
    "api.deepseek.com",
    "cairn-server",
)

_CORRUPT_MARKER = "traffic_corrupt"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rel(path: str) -> str:
    """B7/路径校验：traffic 文件路径必须相对、禁止穿越（.. / 绝对路径）。"""
    if not path or path.startswith("/") or "\\" in path:
        raise CairnError(ErrorCode.VALIDATION, message="traffic 文件路径必须为相对路径", detail={"path": path})
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise CairnError(ErrorCode.VALIDATION, message="traffic 文件路径非法", detail={"path": path})
    return path


# ---------------------------------------------------------------------------
# F5 白名单判定（纯函数，Server 与 Dispatcher/代理共用）
# ---------------------------------------------------------------------------


def _host_in_set(host: str, entries: set[str]) -> bool:
    """host 命中集合：精确匹配或 IP ∈ CIDR。域名不做子域通配（scope 语义要求精确授权）。"""
    if host in entries:
        return True
    for entry in entries:
        if "/" in entry:  # CIDR（如 10.0.0.0/8）
            try:
                if ipaddress.ip_address(host.split(":")[0]) in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
    return False


def assert_capture_allowed(
    host: str,
    *,
    allow_capture_hosts: set[str] | list[str] | tuple[str, ...] = (),
    no_capture_hosts: set[str] | list[str] | tuple[str, ...] = (),
) -> bool:
    """F5 fail-closed：``log ⇔ host ∈ allow_capture_hosts 且 ∉ no_capture_hosts``。

    白名单之外一律不落盘（默认安全）。``allow_capture_hosts`` 为空时所有 host 均拒绝。
    """
    if not host:
        return False
    allow = {h for h in (allow_capture_hosts or ()) if h}
    no = {h for h in (no_capture_hosts or ()) if h}
    if _host_in_set(host, no):
        return False
    if not _host_in_set(host, allow):
        return False
    return True


def _target_to_host(value: str, kind: str) -> str | None:
    """把 targets.value 规范化为白名单 host 词条。

    domain/hostname/ip 直用；url 取 hostname；cidr 保留（IP ∈ CIDR 由 ``_host_in_set`` 判定）。
    """
    if not value:
        return None
    if kind == "url":
        parsed = urlsplit(value if "://" in value else f"//{value}")
        return parsed.hostname or value
    return value


def derive_allow_hosts(conn: sqlite3.Connection, eid: str, scope_policy: dict) -> set[str]:
    """C11：由 authorized targets 派生 ``allow_capture_hosts``（叠加 scope_policy 显式白名单）。"""
    hosts: set[str] = set()
    rows = conn.execute(
        "SELECT value, kind FROM targets WHERE engagement_id = ? AND scope_status = 'authorized'",
        (eid,),
    ).fetchall()
    for row in rows:
        host = _target_to_host(row["value"], row["kind"])
        if host:
            hosts.add(host)
    cp = scope_policy.get("capture_proxy") or {}
    hosts.update(str(h) for h in (cp.get("allow_capture_hosts") or []))
    return hosts


def derive_no_hosts(scope_policy: dict) -> set[str]:
    """次级排除清单（scope_policy.capture_proxy.no_capture_hosts；缺省用默认豁免主机）。"""
    cp = scope_policy.get("capture_proxy") or {}
    explicit = cp.get("no_capture_hosts")
    if explicit:
        return {str(h) for h in explicit}
    return set(DEFAULT_NO_CAPTURE_HOSTS)


def _engagement_scope_policy(conn: sqlite3.Connection, eid: str) -> dict:
    row = conn.execute("SELECT scope_policy FROM engagements WHERE id = ?", (eid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="engagement 不存在", detail={"engagement_id": eid})
    try:
        return json.loads(row["scope_policy"] or "{}")
    except json.JSONDecodeError:
        return {}


def server_assert_capture_allowed(conn: sqlite3.Connection, eid: str, host: str) -> bool:
    """Server 侧 F5 兜底（index 时的双保险；代理已在本地判定，Server 拒绝即 fail-closed）。"""
    scope_policy = _engagement_scope_policy(conn, eid)
    allow = derive_allow_hosts(conn, eid, scope_policy)
    no = derive_no_hosts(scope_policy)
    return assert_capture_allowed(host, allow_capture_hosts=allow, no_capture_hosts=no)


# ---------------------------------------------------------------------------
# C12 归属：client_ip → worker
# ---------------------------------------------------------------------------


def resolve_client(client_ip: str | None, ip_to_worker: dict[str, str] | None) -> str | None:
    """C12：由 client_ip 反查 worker 名。

    - bridge 网络：每 worker 容器独立 IP，``ip_to_worker`` 命中 → 返回 worker 名；
    - host 网络/多 worker 共用端口（共享 IP）→ 归属不明，返回 None（调用方记录 client_ip）。
    """
    if not client_ip or not ip_to_worker:
        return None
    return ip_to_worker.get(client_ip)


# ---------------------------------------------------------------------------
# F2 文件读取 / digest
# ---------------------------------------------------------------------------


def _read_payload(rel_path: str, chunk_count: int, traffic_root: str) -> tuple[bytes, bool]:
    """按 chunk_count 依序读全量字节并拼接。

    返回 ``(data, missing)``：分片文件缺失 → ``missing=True``（F2：超大包分片校验后拼回，
    spec §8.1）。sha256 校验在调用方对「全量包」做（见 :func:`_package_sha256`）。
    """
    rel_path = _safe_rel(rel_path)
    if chunk_count and chunk_count > 1:
        paths = [f"{rel_path}.{i}" for i in range(chunk_count)]
    else:
        paths = [rel_path]
    data = bytearray()
    missing = False
    for p in paths:
        full = os.path.join(traffic_root, p)
        if not os.path.isfile(full):
            missing = True
            continue
        with open(full, "rb") as fh:
            data.extend(fh.read())
    return bytes(data), missing


def _package_sha256(request: bytes, response: bytes | None) -> str:
    """全量包校验和 = sha256(request ‖ response)（F2；单列 sha256 覆盖整包）。"""
    h = hashlib.sha256()
    h.update(request)
    if response is not None:
        h.update(response)
    return h.hexdigest()


def _split_http(raw: bytes) -> tuple[bytes, bytes]:
    """拆分原始 HTTP 字节为 (头块, 体块)。"""
    head, _, body = raw.partition(b"\r\n\r\n")
    return head, body


def _parse_head(head: bytes) -> tuple[str, list[str]]:
    """解析头块为 (请求行/状态行, 头行列表)。"""
    lines = head.split(b"\r\n")
    first = lines[0].decode("latin-1", "replace").strip() if lines else ""
    headers = [ln.decode("latin-1", "replace") for ln in lines[1:] if ln]
    return first, headers


def _body_window(
    body: bytes,
    sha256: str | None,
    prefix: int = DIGEST_BODY_PREFIX,
    suffix: int = DIGEST_BODY_SUFFIX,
) -> str:
    """体前缀 prefix 字节 + 后缀 suffix 字节；截断处标注 ``... [truncated, sha256=<全量校验和>]``
    （spec §8.2：模型可确认「所见与全量一致」而无需加载全量）。"""
    if len(body) <= prefix + suffix:
        return body.decode("utf-8", "replace")
    head = body[:prefix].decode("utf-8", "replace")
    tail = body[-suffix:].decode("utf-8", "replace")
    marker = f"... [truncated, sha256={sha256}]" if sha256 else f"... [truncated body: {len(body)} bytes]"
    return f"{head}\n{marker}\n{tail}"


def make_digest(
    request_bytes: bytes,
    response_bytes: bytes | None,
    sha256: str | None,
    *,
    budget: int = DEFAULT_DIGEST_BUDGET,
) -> str:
    """F2 digest：请求行+全部请求头+请求体前缀2KB+后缀512B；响应 status+头+体窗口。

    截断处标注 ``... [truncated, sha256=<全量校验和>]``（模型可确认「所见与全量一致」
    而无需加载全量）。总长 ≤ budget（超限整体截断并附 sha256 引用）。
    """
    req_head, req_body = _split_http(request_bytes)
    req_line, req_headers = _parse_head(req_head)
    req_digest = f"{req_line}\n" + "\n".join(req_headers)
    if req_body:
        req_digest += "\n" + _body_window(req_body, sha256)

    resp_digest = ""
    if response_bytes is not None:
        resp_head, resp_body = _split_http(response_bytes)
        resp_line, resp_headers = _parse_head(resp_head)
        resp_digest = f"{resp_line}\n" + "\n".join(resp_headers)
        if resp_body:
            resp_digest += "\n" + _body_window(resp_body, sha256)

    digest = (
        f"--- REQUEST ({len(request_bytes)} bytes) ---\n{req_digest}"
        + (f"\n\n--- RESPONSE ({len(response_bytes)} bytes) ---\n{resp_digest}" if response_bytes is not None else "")
    )
    if len(digest.encode("utf-8")) > budget:
        # 整体截断到 budget 内，末尾附全量校验和引用（spec §8.2）
        marker = f"\n... [truncated, sha256={sha256}]" if sha256 else "\n... [truncated]"
        keep = max(0, budget - len(marker.encode("utf-8")))
        digest = digest[:keep].rstrip() + marker
    return digest


# ---------------------------------------------------------------------------
# 服务签名（backend-module-skeleton §3 / services.capture）
# ---------------------------------------------------------------------------


def index_traffic(conn: sqlite3.Connection, eid: str, *, entry: dict) -> dict:
    """代理回写索引（F8 代理唯一写入口；Server 唯一 DB 写者）。

    ``entry`` 元数据：method/url/host/client/client_ip/status/req_path/resp_path/
    req_bytes/resp_bytes/sha256/chunk_count/content_type/captured_at/seq。
    F5 fail-closed：host 不在白名单 → 拒绝（SCOPE_DENIED），不落库。
    """
    _engagement_scope_policy(conn, eid)  # 校验 engagement 存在
    host = (entry.get("host") or "").strip()
    if host and not server_assert_capture_allowed(conn, eid, host):
        raise CairnError(
            ErrorCode.SCOPE_DENIED,
            message="捕获 fail-closed：host 不在捕获白名单",
            detail={"host": host, "engagement_id": eid},
        )

    method = (entry.get("method") or "").strip().upper()
    url = (entry.get("url") or "").strip()
    req_path = _safe_rel((entry.get("req_path") or "").strip())
    if not method or not url or not req_path:
        raise CairnError(ErrorCode.VALIDATION, message="traffic 索引必填：method/url/req_path")

    seq = entry.get("seq")
    if seq is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM traffic_entries WHERE engagement_id = ?", (eid,)
        ).fetchone()
        seq = int(row["m"]) + 1
    seq = int(seq)

    captured_at = entry.get("captured_at") or _utcnow()
    traffic_id = next_id(conn, "traffic", eid)
    conn.execute(
        """INSERT INTO traffic_entries
           (id, engagement_id, seq, captured_at, method, url, host, client, client_ip,
            status, req_path, resp_path, req_bytes, resp_bytes, content_type, sha256,
            chunk_count, finding_linked)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (
            traffic_id,
            eid,
            seq,
            captured_at,
            method,
            url,
            host or None,
            entry.get("client") or None,
            entry.get("client_ip") or None,
            entry.get("status"),
            req_path,
            (entry.get("resp_path") or "").strip() or None,
            int(entry.get("req_bytes") or 0),
            entry.get("resp_bytes"),
            entry.get("content_type"),
            entry.get("sha256"),
            int(entry.get("chunk_count") or 1),
        ),
    )
    conn.commit()
    return _entry_row_to_dict(conn, traffic_id)


def _entry_row_to_dict(conn: sqlite3.Connection, traffic_id: str) -> dict:
    row = conn.execute("SELECT * FROM traffic_entries WHERE id = ?", (traffic_id,)).fetchone()
    if row is None:  # pragma: no cover
        raise CairnError(ErrorCode.NOT_FOUND, message="traffic 不存在", detail={"traffic_id": traffic_id})
    return dict(row)


def resolve_traffic(
    conn: sqlite3.Connection,
    eid: str,
    traffic_id: str,
    *,
    for_model: bool = False,
    traffic_root: str | None = None,
    digest_budget: int | None = None,
) -> dict:
    """还原原始请求/响应。``for_model=False`` → 全量；``for_model=True`` → digest（F2）。

    - 全量：分片按 chunk_count 拼回，sha256 校验失败 → ``corrupt`` + 字段 ``traffic_corrupt``；
    - digest：≤ digest_budget，截断处含 ``sha256`` 引用；报告/审计/replay 永远走全量。
    """
    row = conn.execute(
        "SELECT * FROM traffic_entries WHERE id = ? AND engagement_id = ?", (traffic_id, eid)
    ).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="traffic 不存在", detail={"traffic_id": traffic_id, "engagement_id": eid})

    root = traffic_root
    if root is None:
        from ...config import DEFAULT_TRAFFIC_ROOT

        root = DEFAULT_TRAFFIC_ROOT
    budget = digest_budget
    if budget is None:
        scope_policy = _engagement_scope_policy(conn, eid)
        cp = scope_policy.get("capture_proxy") or {}
        budget = int(cp.get("digest_budget") or DEFAULT_DIGEST_BUDGET)

    req_bytes_all, req_missing = _read_payload(row["req_path"], row["chunk_count"], root)
    resp_bytes_all = b""
    resp_missing = False
    if row["resp_path"]:
        resp_bytes_all, resp_missing = _read_payload(row["resp_path"], row["chunk_count"], root)

    corrupt = req_missing or resp_missing
    if not corrupt and row["sha256"]:
        actual = _package_sha256(req_bytes_all, resp_bytes_all if row["resp_path"] else None)
        if actual != row["sha256"]:
            corrupt = True
    meta = dict(row)
    if for_model:
        digest = make_digest(
            req_bytes_all,
            resp_bytes_all if row["resp_path"] else None,
            row["sha256"],
            budget=budget,
        )
        meta.update({"mode": "digest", "digest_budget": budget, "digest": digest, "corrupt": corrupt})
    else:
        meta.update(
            {
                "mode": "full",
                "request": req_bytes_all.decode("utf-8", "replace"),
                "response": resp_bytes_all.decode("utf-8", "replace") if row["resp_path"] else None,
                "corrupt": corrupt,
            }
        )
    if corrupt:
        meta[_CORRUPT_MARKER] = True
    return meta


def list_traffic(
    conn: sqlite3.Connection,
    eid: str,
    *,
    client: str | None = None,
    since: str | None = None,
    host: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> list[dict]:
    """捕获索引检索（C12：按 worker 归属过滤）。"""
    clauses = ["engagement_id = ?"]
    params: list = [eid]
    if client is not None:
        clauses.append("client = ?")
        params.append(client)
    if since is not None:
        clauses.append("captured_at >= ?")
        params.append(since)
    if host is not None:
        clauses.append("host = ?")
        params.append(host)
    rows = conn.execute(
        f"SELECT * FROM traffic_entries WHERE {' AND '.join(clauses)} "
        "ORDER BY seq ASC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [dict(r) for r in rows]


def link_finding_traffic(
    conn: sqlite3.Connection,
    fid: str,
    traffic_ids: list[str],
    *,
    role: str = TrafficLinkRole.trigger.value,
    source: str = HttpEvidenceSource.captured.value,
) -> list[str]:
    """关联流量（role ∈ trigger/related/verification/replay；source ∈ captured/agent_typed）。

    写入 ``finding_traffic_links``（ftl-###）+ 置 ``traffic_entries.finding_linked=1``。
    traffic 必须与 finding 同 engagement；同 (finding, traffic, role) 幂等跳过。
    """
    finding = conn.execute("SELECT id, engagement_id FROM findings WHERE id = ?", (fid,)).fetchone()
    if finding is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在", detail={"finding_id": fid})
    role = role if role in (r.value for r in TrafficLinkRole) else TrafficLinkRole.trigger.value
    source = source if source in (s.value for s in HttpEvidenceSource) else HttpEvidenceSource.captured.value

    linked: list[str] = []
    created_at = _utcnow()
    for tid in traffic_ids:
        tr = conn.execute(
            "SELECT id FROM traffic_entries WHERE id = ? AND engagement_id = ?", (tid, finding["engagement_id"])
        ).fetchone()
        if tr is None:
            raise CairnError(
                ErrorCode.VALIDATION,
                message="traffic 不属于该 finding 的 engagement",
                detail={"traffic_id": tid, "engagement_id": finding["engagement_id"]},
            )
        exists = conn.execute(
            "SELECT 1 FROM finding_traffic_links WHERE finding_id = ? AND traffic_id = ? AND role = ?",
            (fid, tid, role),
        ).fetchone()
        if exists:
            continue
        link_id = next_id(conn, "finding_traffic_link", finding["engagement_id"])
        conn.execute(
            "INSERT INTO finding_traffic_links (id, finding_id, traffic_id, role, source, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (link_id, fid, tid, role, source, created_at),
        )
        conn.execute("UPDATE traffic_entries SET finding_linked = 1 WHERE id = ?", (tid,))
        linked.append(link_id)
    conn.commit()
    return linked


def get_linked_traffic(conn: sqlite3.Connection, fid: str) -> list[dict]:
    """finding 已关联的流量（role/source 原样；report/verify 引用）。"""
    rows = conn.execute(
        """SELECT ftl.id AS link_id, ftl.role, ftl.source, ftl.created_at, te.*
           FROM finding_traffic_links ftl
           JOIN traffic_entries te ON te.id = ftl.traffic_id
           WHERE ftl.finding_id = ?
           ORDER BY te.seq ASC""",
        (fid,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# C2 派生：以捕获字节填充 finding_http_evidence（22 登记，本模块填充内容）
# ---------------------------------------------------------------------------

#: finding_http_evidence 派生后 response/request body 的内嵌上限（全量在 traffic 文件）
HTTP_EVIDENCE_BODY_CAP = 64 * 1024


def derive_http_from_capture(
    conn: sqlite3.Connection,
    fid: str,
    traffic_id: str,
    *,
    traffic_root: str | None = None,
) -> dict:
    """C2：以捕获字节派生 ``finding_http_evidence(source='captured')``。

    内容（method/url/request_headers/request_body/response_status/response_headers/
    response_body）全部取自 traffic 文件字节，agent 不逐字录入。登记动作走 22 的
    ``services.findings.add_http_evidence``（不直写对方表）；同时建立
    ``role='trigger'`` 关联（finding_traffic_links 属本域）。
    """
    row = conn.execute(
        "SELECT * FROM traffic_entries WHERE id = ?", (traffic_id,)
    ).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="traffic 不存在", detail={"traffic_id": traffic_id})
    finding = conn.execute("SELECT id, engagement_id FROM findings WHERE id = ?", (fid,)).fetchone()
    if finding is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在", detail={"finding_id": fid})
    if row["engagement_id"] != finding["engagement_id"]:
        raise CairnError(
            ErrorCode.VALIDATION,
            message="traffic 与 finding 不属同一 engagement",
            detail={"traffic_id": traffic_id, "finding_id": fid},
        )

    root = traffic_root
    if root is None:
        from ...config import DEFAULT_TRAFFIC_ROOT

        root = DEFAULT_TRAFFIC_ROOT

    req_bytes_all, _ = _read_payload(row["req_path"], row["chunk_count"], root)
    req_head, req_body = _split_http(req_bytes_all)
    _req_line, req_headers = _parse_head(req_head)
    method = (row["method"] or _req_line.split(" ")[0] if _req_line else row["method"] or "").upper()

    response_status = None
    response_headers: list[str] = []
    response_body = ""
    if row["resp_path"]:
        resp_bytes_all, _ = _read_payload(row["resp_path"], row["chunk_count"], root)
        resp_head, resp_body = _split_http(resp_bytes_all)
        resp_line, response_headers = _parse_head(resp_head)
        m = re.match(r"HTTP/\S+\s+(\d{3})", resp_line)
        if m:
            response_status = int(m.group(1))
        response_body = resp_body[:HTTP_EVIDENCE_BODY_CAP].decode("utf-8", "replace")

    http_obj = {
        "source": HttpEvidenceSource.captured.value,
        "traffic_id": traffic_id,
        "method": method,
        "url": row["url"],
        "request_headers": "\r\n".join(req_headers) or None,
        "request_body": req_body[:HTTP_EVIDENCE_BODY_CAP].decode("utf-8", "replace"),
        "response_status": response_status,
        "response_headers": "\r\n".join(response_headers) or None,
        "response_body": response_body or None,
        "note": f"captured=1 sha256={row['sha256']} chunk_count={row['chunk_count']}",
        "captured_at": row["captured_at"],
    }

    # 同 finding 已派生的 evidence 去重（同一 traffic_id 不重复登记；
    # 同时打断 22 add_http_evidence → _derive_http 的互递归环路）
    dup = conn.execute(
        "SELECT 1 FROM finding_http_evidence WHERE finding_id = ? AND traffic_id = ? AND source = 'captured'",
        (fid, traffic_id),
    ).fetchone()
    if not dup:
        link_finding_traffic(conn, fid, [traffic_id], role=TrafficLinkRole.trigger.value, source=HttpEvidenceSource.captured.value)
        # 登记动作走 22 的服务函数（不直写对方表；22 未就绪时抛清晰错误）
        _findings_svc = _load_findings_service()
        _findings_svc.add_http_evidence(conn, fid, http_obj=http_obj)

    conn.commit()  # 保证 http evidence 行持久（22 的 add_http_evidence 内部不 commit）
    return http_obj


def _load_findings_service():
    """懒加载 22 的 services.findings（并行期依赖守卫）。用 importlib 以 sys.modules 为准。"""
    try:
        import importlib

        return importlib.import_module("cairn.server.services.findings")
    except Exception as exc:  # noqa: BLE001 —— 并行开发：22 未实现
        raise CairnError(
            ErrorCode.INTERNAL,
            message="services.findings.add_http_evidence 未就绪（依赖 22-findings）",
            detail={"error": str(exc)},
        ) from exc


# ---------------------------------------------------------------------------
# C2 增强：捕获完整性对账（防「一致地错」）
# ---------------------------------------------------------------------------


def reconcile(
    conn: sqlite3.Connection,
    eid: str,
    *,
    min_capture_ratio: float = 2.0,
    min_capture_abs_diff: int = 3,
    unverified_count: int | None = None,
    total_connections: int | None = None,
    unverified_threshold: float = 0.5,
) -> dict:
    """C2 增强：explore 声明 http[]/traffic_ids 数 vs 时间窗捕获数 → capture_gap 标记。

    - ``declared_count``：本 engagement finding 已关联 traffic 的链接数 + agent_typed
      http 证据数（Agent 声称的流量/请求量）；
    - ``captured_count``：本 engagement 实际捕获的 traffic_entries 数；
    - 若 ``declared >= ratio × captured`` 且 ``差 ≥ abs_diff``（或 captured=0 而 declared>0）
      → ``capture_gap=true``（静默缺抓：verify 应默认 needs_more，报告标注证据缺口）；
    - ``unverified`` 占比超阈 → ``downgrade_command_evidence=true``（同 C6/F10）；
    - 结果落 ``scheduler_state``（key ``capture_gap:{eid}``，spec §2.5），供 30/40 周期对账读取。
    """
    captured_count = conn.execute(
        "SELECT COUNT(*) FROM traffic_entries WHERE engagement_id = ?", (eid,)
    ).fetchone()[0]

    linked = conn.execute(
        """SELECT COUNT(*) FROM finding_traffic_links ftl
           JOIN findings f ON f.id = ftl.finding_id
           WHERE f.engagement_id = ?""",
        (eid,),
    ).fetchone()[0]
    agent_typed = conn.execute(
        """SELECT COUNT(*) FROM finding_http_evidence he
           JOIN findings f ON f.id = he.finding_id
           WHERE f.engagement_id = ? AND he.source = 'agent_typed'""",
        (eid,),
    ).fetchone()[0]
    declared_count = int(linked) + int(agent_typed)

    gap = False
    if declared_count > 0 and captured_count == 0:
        gap = True
    elif captured_count > 0 and declared_count >= min_capture_ratio * captured_count:
        if (declared_count - captured_count) >= min_capture_abs_diff:
            gap = True

    unverified_ratio = None
    downgrade_command_evidence = False
    if total_connections and total_connections > 0 and unverified_count is not None:
        unverified_ratio = unverified_count / total_connections
        downgrade_command_evidence = unverified_ratio >= unverified_threshold

    result = {
        "engagement_id": eid,
        "declared_count": declared_count,
        "captured_count": captured_count,
        "min_capture_ratio": min_capture_ratio,
        "min_capture_abs_diff": min_capture_abs_diff,
        "capture_gap": gap,
        "unverified_count": unverified_count,
        "total_connections": total_connections,
        "unverified_ratio": unverified_ratio,
        "downgrade_command_evidence": downgrade_command_evidence,
        "reconciled_at": _utcnow(),
    }
    conn.execute(
        """INSERT INTO scheduler_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (f"capture_gap:{eid}", json.dumps(result, ensure_ascii=False), result["reconciled_at"]),
    )
    conn.commit()
    return result


def read_capture_gap(conn: sqlite3.Connection, eid: str) -> dict | None:
    """读取最近一次对账结果（供 30/40 周期对账 / verify needs_more 判定）。"""
    row = conn.execute(
        "SELECT value FROM scheduler_state WHERE key = ?", (f"capture_gap:{eid}",)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:  # pragma: no cover
        return None


def capture_gap_findings(conn: sqlite3.Connection, eid: str) -> list[dict]:
    """列出「声称有 HTTP 请求证据但捕获缺失」的 open finding（verify 应 needs_more 的候选）。

    判定：finding 只有 ``agent_typed`` http 证据且无 captured 关联（linked=0）→ 声明了
    HTTP 请求却无捕获会话 → capture_gap 候选。非 HTTP finding（无 http 证据、纯命令证据）
    不算缺抓，不在此列。
    """
    rows = conn.execute(
        """SELECT f.id, f.title, f.target_id, f.status,
                  (SELECT COUNT(*) FROM finding_traffic_links ftl WHERE ftl.finding_id = f.id) AS linked,
                  (SELECT COUNT(*) FROM finding_http_evidence he
                    WHERE he.finding_id = f.id AND he.source = 'agent_typed') AS agent_typed
           FROM findings f
           WHERE f.engagement_id = ? AND f.status IN ('open','pending_verify')
           ORDER BY f.created_at ASC""",
        (eid,),
    ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        if row["linked"] == 0 and row["agent_typed"] > 0:
            row["gap_reason"] = "agent_typed_without_captured"
            result.append(row)
    return result
