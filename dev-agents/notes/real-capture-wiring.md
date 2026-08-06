# real-capture-wiring 交接物（真实抓包接线）

- 完成 Agent：real-capture-wiring  日期：2026-08-06
- 依据：`phase0` 探索确认的 3 处缺口（scope_resolver 接入 / Dispatcher 侧 CaptureProxyManager / mitm addon 写回 traffic_root）。
- 基线：520 passed → **540 passed / 1 skipped**（+20 新用例；1 skip = 真实 mitmdump 集成，环境无 mitmproxy）。
- 未 git commit。

---

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `dispatcher/runtime/containers.py` | `resolve_scope_policy(eid, scope_policy, *, default_capture_host=None) -> ContainerScope` | **缺口 1 核心**：scope_policy JSON（DDL §2.1）→ `ContainerScope`。`network_cap` 接受 bool（True→NET_RAW+NET_ADMIN）或 cap 列表；`resources.{mem,cpu,pids}`；`tools`；`capture_proxy={enabled,host,port,no_capture_hosts}`。host 缺省链：`capture_proxy.host` → `default_capture_host` → env `CAIRN_CAPTURE_HOST` → `172.17.0.1`；no_capture_hosts 缺省用默认豁免主机 |
| `dispatcher/runtime/__init__.py` | re-export `resolve_scope_policy` | — |
| `dispatcher/protocol/client.py` | **追加** `get_engagement_scope(eid) -> dict` | 取 `GET /engagements/{eid}` 的 `scope_policy`（只追加不改既有方法） |
| `dispatcher/scheduler/loop.py` | `_make_scope_resolver` / `_default_backend(config, *, client, project_to_eid)` / `run_dispatch_loop` 接线 / `DispatcherLoop.__init__(..., project_to_eid, capture_manager)` | **缺口 1+2 接线**：`project_to_eid` 由 loop 在 `_ensure_project` 填充；`_default_backend` 用它给 `ContainerBackend` 挂 `scope_resolver`。capture 生命周期：`_process_engagement`→`_ensure_capture`（幂等起代理）；`_handle_kill`→`stop_engagement`（C3）；`_run_periodic`→`_reconcile_capture_proxies`（非 active 停）；`run()` finally→`stop_all` |
| `dispatcher/capture/proxy.py`（新建） | `CaptureProxyManager` / `ProxyEngine` / `MitmProxyEngine` / `FakeProxyEngine` / `generate_ca` / `CaptureProxyState` | **缺口 2**：Dispatcher 侧代理编排（不 import server）。`start_engagement(eid, scope_policy, *, server_url, capture_token, traffic_root, allow_hosts, no_hosts)`：生成 CA `{ca_dir}/{eid}/ca.pem`+`ca.key`（0700）→ 起 `mitmdump -q --listen-host 0.0.0.0 --listen-port {port} -s addon.py`（env 注入 addon 参数）→ 记录运行态。`stop_engagement`/`is_running`/`running_ports`/`running_eids`/`stop_all` |
| `dispatcher/capture/addon.py`（新建） | `CairnCaptureAddon`（`request`/`response` 钩子、`capture_flow`、`_index`） | **缺口 3**：**纯 stdlib、不 import cairn/mitmproxy**（mitmdump 可独立加载）。写 `{traffic_root}/{eid}/{seq}/req.bin|resp.bin`（>100MB 分片 `.i`，统一 chunk_count 两侧写）；sha256=sha256(req‖resp)；F8 `POST /engagements/{eid}/traffic` Bearer capture token；失败记 stderr 不阻塞 |
| `dispatcher/capture/__init__.py` | re-export | client.py + proxy.py |
| `tests/test_capture_wiring.py`（新建） | 20 用例 + 1 skip | 缺口 1-3 验收 |

**不动**：`server/capture_proxy.py`（仅参照）、`server/services/capture.py`（读侧已完备）、`server/routers/traffic.py`（F8 读/写侧不改）。

---

## 2. 缺口 1：scope_resolver 契约

`ContainerBackend.__init__` 本已收 `scope_resolver: Callable[[str], Any]`（此前 loop 未传）。现在：

```python
resolver(project_id: str) -> ContainerScope
  eid = project_to_eid[str(project_id)]          # loop 在 _ensure_project 填充
  scope_policy = client.get_engagement_scope(eid) # GET /engagements/{eid}.scope_policy
  return resolve_scope_policy(eid, scope_policy)
```

效果：capture enabled 时 `_build_env` 注入 HTTPS_PROXY=host:port、HTTP_PROXY、
SSL_CERT_FILE/REQUESTS_CA_BUNDLE/NODE_EXTRA_CA_CERTS=/etc/cairn-capture/ca.pem、
NO_PROXY=no_capture_hosts；`_run_kwargs` 把 `{capture_ca_dir}/{eid}/ca.pem`
bind-mount 到 `/etc/cairn-capture/ca.pem`（ro）；`network_cap`/`mem/cpu/pids`/`tools`
随之生效；capture + `network_mode=host` → `ContainerBackendError`（C12）。

> **host 取值注意**：scope_policy.capture_proxy 无 `host` 字段（DDL §2.1）→ 用
> `CAIRN_CAPTURE_HOST` env 或默认 Docker bridge 网关 `172.17.0.1`。生产若用
> docker-compose 自定义网络，务必设 `scope_policy.capture_proxy.host`
> （或 dispatcher env `CAIRN_CAPTURE_HOST`）为 worker 可达的 dispatcher 地址。

---

## 3. 缺口 2：代理生命周期接线点

| 时机 | 位置 | 动作 |
|---|---|---|
| active engagement 首次派发前 | `_process_engagement` → `_ensure_capture(eng)` | 幂等 `start_engagement`（scope_policy.capture_proxy.enabled 且未运行） |
| 熔断 kill | `_handle_kill(eid)` | `stop_engagement`（C3） |
| periodic 对账 | `_run_periodic` → `_reconcile_capture_proxies` | 非 active（kill/expire/archive/paused）→ stop |
| loop 关停 | `run()` finally | `stop_all` |

`_ensure_capture` 白名单：`dispatcher/capture/client.py::derive_whitelist`
（targets authorized → allow + scope_policy.no_capture_hosts）。白名单热刷新由服务端
`server_assert_capture_allowed`（index_traffic 双保险）兜底，代理本地白名单重启即更新。

---

## 4. 缺口 3：addon 文件约定（与读取侧严格对齐）

- 路径：`{traffic_root}/{eid}/{seq}/req.bin` / `resp.bin`；
- 分片：`>100MB`（`CHUNK_THRESHOLD_BYTES`，与 `server/services/capture.py`
  `CHUNK_THRESHOLD_BYTES` 同值）→ `req.bin.{0..n-1}`/`resp.bin.{0..n-1}`；
  **req/resp 统一同一 chunk_count**（`_read_payload` 用同一 chunk_count 读两侧，
  否则小侧缺 `.i` 文件 → missing → corrupt）；
- sha256 = sha256(req ‖ resp)（`_package_sha256` 语义）；
- F8 body：`method/url/host/client_ip/status/req_path/resp_path/req_bytes/resp_bytes/
  content_type/sha256/chunk_count/seq` —— 与 `TrafficIndexRequest`（extra=forbid）完全一致；
- addon env（proxy.py 注入）：`CAIRN_TRAFFIC_ROOT`/`CAIRN_EID`/`CAIRN_SERVER_URL`/
  `CAIRN_CAPTURE_TOKEN`/`CAIRN_ALLOW_HOSTS`/`CAIRN_NO_HOSTS`；
- addon 独立可加载：纯 stdlib + urllib，不 import cairn / mitmproxy；
  mitmdump 经 `addons = [CairnCaptureAddon()]` 发现。

---

## 5. 未做项 / 说明

- **真实 mitmdump 集成测试**：`test_addon_integration_real_mitmdump` 已写好，
  当前环境无 `mitmdump` → skip。有权限环境装 mitmproxy 后自动生效。
- **白名单热刷新（addon 侧）**：addon 的 allow_hosts 在进程启动时固定；热刷新依赖
  服务端 index_traffic 的 F5 兜底（C11），addon 侧未做动态重载。
- **client_ip → worker 归属**：addon 只记 `client_ip`（bridge 独立 IP）；worker 名映射
  由服务端 `resolve_client`/`ip_to_worker` 在后续消费时处理（C12），addon 不持映射。
- **seq 重启衔接**：addon 从 `{traffic_root}/{eid}` 现有 seq 目录推断下一个序号，
  mitmdump 重启不覆盖旧文件。

---

## 6. 真实运行演示使用说明

dispatch 配置（`dispatch.yaml`）关键字段：

```yaml
security:
  capture_ca_dir: /var/cairn/capture-ca   # 容器 CA bind-mount 源
  traffic_root: /var/cairn/traffic        # addon 写流量文件根
  capture_token_env: CAIRN_CAPTURE_TOKEN  # F8 回写受限 token
  api_token_env: CAIRN_API_TOKEN
runtime:
  execution: container
container:
  network_mode: bridge                    # capture 必须 bridge（C12）
```

创建 engagement 时 scope_policy 需含：

```json
{
  "network_cap": false,
  "resources": {"mem_limit": "2g", "cpu_quota": 100000, "pids_limit": 512},
  "capture_proxy": {
    "enabled": true,
    "port": 8080,
    "host": "172.17.0.1",
    "no_capture_hosts": ["api.anthropic.com", "api.deepseek.com", "cairn-server"]
  }
}
```

`host` 必须是 worker 容器可达的 Dispatcher 地址（自定义 docker 网络请改 dispatcher 服务名/IP）。

addon env 由 `CaptureProxyManager.start_engagement` 自动注入（`CAIRN_*`），无需手工设置。
Dispatcher 侧需设：`CAIRN_API_TOKEN`、`CAIRN_CAPTURE_TOKEN`；容器镜像需预装
`mitmproxy`（`mitmdump`）与 `openssl`（CA 生成）。

验证：全量 `uv run --project cairn pytest -q` → 540 passed / 1 skipped。
