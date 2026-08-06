# 12-dispatcher-config 交接物

- 完成 Agent：12  日期：2026-08-06
- 交付：`cairn/src/cairn/dispatcher/{__init__,config,contracts,errors}.py`、`cairn/src/cairn/dispatcher/protocol/{__init__,client}.py`、`cairn/tests/test_dispatcher_config.py`、`cairn/tests/test_protocol_client.py`。另补 `cairn/src/cairn/__init__.py`（顶层包 __init__ 此前不存在，包无法导入，仅放 `__version__`；如 10 已建同名，内容相同无冲突）。

## 1. 实现清单

- `dispatcher/__init__.py`：re-export `load/loads/load_dict`、全部配置 dataclass、`CairnClient`、`CairnClientError`、`TaskType` 等。
- `dispatcher/contracts.py`：`TaskType`/`WorkerTaskType` Literal + 枚举元组（`WORKER_TASK_TYPES` 不含 `replay`）。
- `dispatcher/errors.py`：v2 §7.3 全部 error_code 常量、`STATUS_FALLBACK_CODES`、`CairnClientError`（带 error_code/http_status/detail）、子类 `AuthError`/`ScopeDeniedError`、`raise_for_error(resp)`。
- `dispatcher/config.py`：`ConfigError`；`ServerConfig/RuntimeConfig/TaskConfig/TasksConfig/SecurityConfig/ScopeConfig/TuningConfig/ContainerConfig/WorkerConfig/LocalConfig/DispatcherConfig`；`WorkerConfig.effective_env(common_env)`（per-worker 优先）；`expand_env`（全字符串 `${VAR}` 展开）；`load(path, *, env=None)` / `loads(text, ...)` / `load_dict(raw, ...)`。
- `dispatcher/protocol/client.py`：`CairnClient(base_url, token, *, timeout=30, client=None)`，每请求显式带 `Authorization: Bearer`，非 2xx 抛 `CairnClientError`。`client` 参数可注入已有 `httpx.Client`（测试传 FastAPI TestClient）。
- 测试：`test_dispatcher_config.py` 20 例 + `test_protocol_client.py` 10 例。

## 2. config 默认值表（对齐 dispatch-config-spec §1-§9；缺省即用）

| 段/字段 | 默认值 |
|---|---|
| `server.url` / `server.api_token` | 必填；`api_token` **必须 `${ENV_VAR}`** 且展开后非空（仓库禁明文） |
| `common_env` | `{}` |
| `runtime.execution` | `container`（`local` 亦可） |
| `runtime.interval` / `max_workers` / `max_running_projects` / `max_project_workers` / `healthcheck_timeout` | `3` / `8` / `3` / `4` / `20` |
| `runtime.worker_healthcheck` | `startup_only`（`startup_and_task`/`startup_only`/`disabled`） |
| `runtime.prompt_group` | `default` |
| `tasks.bootstrap` / `reason` / `explore` / `verify` / `audit` / `replay` | `{timeout:300,conclude_timeout:90}` / `{timeout:300,max_intents:2}` / `{timeout:300,conclude_timeout:90}` / `{timeout:300}` / `{timeout:300}` / `{timeout:60}` |
| `security.api_token_env` / `capture_token_env` | `CAIRN_API_TOKEN` / `CAIRN_CAPTURE_TOKEN` |
| `security.capture_ca_dir` / `evidence_root` / `traffic_root` / `archive_root` | `/var/cairn/capture-ca` / `/var/cairn/evidence` / `/var/cairn/traffic` / `/var/cairn/archive` |
| `security.static_encryption` / `archive_encryption` / `executor_url` | `true` / `true` / `""` |
| `scope.*` | `enforce_scope_guard`/`enforce_auth_window`/`enforce_kill_switch`=`true`，`default_scope_policy`=`"{}"` |
| `tuning.writeback_retries` / `reconcile_intent_timeout_multiplier` | `1` / `2` |
| `tuning.min_capture_ratio` / `min_capture_abs_diff` | `2.0` / `3` |
| `tuning.event_summary_max_bytes` / `command_evidence_max_bytes` / `event_raw_retain_days` | `512` / `1048576` / `7` |
| `tuning.sse_heartbeat_seconds` / `longpoll_hold_seconds` | `15` / `20` |
| `tuning.worker_rejected_cooldown_seconds` / `worker_unhealthy_cooldown_seconds` | `5` / `5` |
| `container.image` / `network_mode` / `completed_action` / `cap_add` | `ghcr.io/oritera/cairn-worker-container:latest` / `bridge` / `stop` / `[]` |
| `workers[].max_running` / `priority` / `verify_eligible` / `env` | `1` / `0` / `true` / `{}` |
| `local.workspace_root` / `local.completed_action` | `None` / `keep`（本地示例扩展，不在 spec §0 顶层列表内） |

必填段：`server` / `runtime` / `workers`。其余段缺省走默认。校验失败一律抛 `ConfigError`（不静默）：缺必填段、顶层未知段、非法 `execution`/`network_mode`/`worker_healthcheck`/`task_type`（含 `replay` 进 workers）、workers 重名、`${VAR}` 未设（报变量名）、api_token 明文/展开后空。

## 3. 客户端方法清单 = skeleton §3 映射表

> `eid`/`pid`/`fid` 一律字符串；标注「路径假设」的端点在 skeleton §2 无显式条目，需服务端子域（10/20-24）在阶段 2 对齐本表或反馈改名。

| 客户端方法 | HTTP | 端点 | skeleton §3 服务签名 |
|---|---|---|---|
| `list_active()` | GET | `/engagements?status=active` | §2.2 GET /engagements（含 scope/kill 状态） |
| `get(eid)` | GET | `/engagements/{eid}` | §2.2 GET /engagements/{id} |
| `set_status(eid,status,retest=False)` | PUT | `/engagements/{eid}/status` | `scope.transition_status(conn,eid,new_status,*,retest)` |
| `kill(eid)` | POST | `/engagements/{eid}/kill` | 熔断开关 |
| `list_targets(eid)` | GET | `/engagements/{eid}/targets` | §2.2 |
| `create_target(eid,value,**extra)` | POST | `/engagements/{eid}/targets` | `scope.resolve_target`/`check_scope_allowed` 相关 |
| `check_scope(eid,value)` | GET | `/engagements/{eid}/scope/check?value=` **路径假设** | `scope.check_scope_allowed(conn,eid,target_value)`（403 SCOPE_DENIED） |
| `get_coverage(eid)` | GET | `/engagements/{eid}/coverage` | `coverage.coverage_summary` |
| `get_gaps(eid,exclude_in_progress=False,threshold=,limit=)` | GET | `/engagements/{eid}/coverage/gaps` | `coverage.compute_gaps(conn,eid,*,threshold,exclude_in_progress,limit)` |
| `list_items(eid)` | GET | `/engagements/{eid}/coverage/items` | §2.3 |
| `waive(eid,item_id,kind,reason,by=)` | POST | `/engagements/{eid}/coverage/items/{cid}/waive` | `coverage.waive_item(conn,eid,item_id,*,kind,reason,by)` |
| `write_coverage_result(eid,...)` | POST | `/engagements/{eid}/coverage/result` **路径假设** | `coverage.write_coverage_result(conn,eid,*,item_ids,depth_achieved,outcome,fact_id,intent_id,evidence_refs,tested_scope,partial)`；`Idempotency-Key` 头做幂等键（coverage spec §3） |
| `export_yaml(pid)` | GET | `/projects/{pid}/export?format=yaml`（返回纯文本） | `graph.export_graph_yaml(conn,pid)` |
| `claim_intent(pid,iid,worker)` | POST | `/projects/{pid}/intents/{iid}/claim` **路径假设** | `graph.claim_intent(conn,pid,iid,*,worker)`（他人持有 409 LEASE_CONFLICT） |
| `heartbeat_intent(pid,iid,worker)` | POST | `/projects/{pid}/intents/{iid}/heartbeat` | `graph.heartbeat_intent` |
| `release_intent(pid,iid,worker)` | POST | `/projects/{pid}/intents/{iid}/release` | `graph.release_intent` |
| `conclude_intent(pid,iid,worker,facts=)` | POST | `/projects/{pid}/intents/{iid}/conclude` | `graph.conclude_intent` |
| `create_finding(eid,payload,detected_by=,actor='agent')` | POST | `/engagements/{eid}/findings` | `findings.create_finding(conn,eid,*,payload,detected_by,actor='agent')` |
| `upload_evidence(eid,fid,file,kind)` | POST | `/engagements/{eid}/findings/{fid}/evidence` | `findings.attach_evidence(conn,fid,*,kind,path,mime,size)`（multipart） |
| `add_http_evidence(eid,fid,http_obj)` | POST | `/engagements/{eid}/findings/{fid}/http` | `findings.add_http_evidence(conn,fid,*,http_obj)` |
| `add_command_evidence(eid,fid,cmd)` | POST | `/engagements/{eid}/findings/{fid}/commands` | §2.5 POST /commands |
| `link_traffic(eid,fid,traffic_ids,role,source=)` | POST | `/engagements/{eid}/findings/{fid}/traffic` | `capture.link_finding_traffic(conn,fid,traffic_ids,*,role,source)` |
| `list_traffic(eid,client=,since=)` | GET | `/engagements/{eid}/traffic` | capture 索引查询 |
| `resolve_traffic(eid,tid,for_model=True)` | GET | `/engagements/{eid}/traffic/{tid}?for_model=` | `capture.resolve_traffic(conn,eid,traffic_id,*,for_model)`（服务端默认 False=全量；**客户端默认 True=digest**，供 LLM 消费，见 §5） |
| `open_task_run(eid,task_type,worker,project_id=)` | POST | `/engagements/{eid}/task_runs` **路径假设** | `progress.open_task_run(conn,*,engagement_id,project_id,task_type,worker)`（project_id 可空） |
| `append_event(run_id,kind,level,message,raw_path=)` | POST | `/tasks/{run_id}/events` **路径假设** | `progress.append_event(conn,run_id,*,kind,level,message,raw_path)` |
| `finish_task_run(run_id,status,outcome_note=)` | POST | `/tasks/{run_id}/finish` **路径假设** | TaskRun 状态收尾（§2.5 无显式写端点） |
| `trigger_audit(eid,item_id)` | POST | `/engagements/{eid}/coverage/items/{cid}/audit` | `coverage.sample_audit`/`apply_audit_verdict` 相关（F3） |
| `get_report(eid)` | GET | `/engagements/{eid}/report` **路径假设** | §2.6 GET /engagements/{id}/report/{rpt_id}；客户端取 latest |
| `health()` | GET | `/health` | 连通性探活 |

**自查**：方法名与 skeleton §3 服务签名一一可映射（上表为自查清单）；无服务端显式端点的路径已标注假设。

## 4. stub server 测试方法

`test_protocol_client.py` 用 **FastAPI TestClient** 起 stub（`build_stub()`：`/health`、`/engagements/{eid}`、PUT `/status`→409、POST `/projects/{pid}/intents/{iid}/claim`→409 LEASE_CONFLICT、GET `/scope/check`→403 SCOPE_DENIED、`/traffic/{tid}` 与 `/projects/{pid}/export` 回显）。fixture 把 TestClient 注入 `CairnClient(..., client=tc)`。断言：401 抛 `AuthError`(error_code=AUTH_INVALID)；409 抛对应 error_code（ENGAGEMENT_INVALID_STATE/LEASE_CONFLICT）；403 抛 `ScopeDeniedError`；Bearer 头正确（stub 用 `Header()` 读）；`for_model`/`format=yaml` 参数透传。

**环境排雷**：starlette TestClient 与本仓库 httpx 0.28 组合可用，但必须用 `Header(default="")` 标注读请求头——写成 `authorization: str = ""` 会被 FastAPI 当 **query 参数**导致永远 401（初版测试踩坑）。另 starlette 对 `httpx2` 有 deprecation warning（无害）。

## 5. 留给 30（tasks）/40（loop）的注意事项

- **配置**：用 `dispatcher.config.load(path, *, env=None)`；`${ENV_VAR}` 未设会在启动报错（指明变量名），api_token 必须 `${}`。`replay` 不在 `workers[].task_types` 合法枚举内（引擎任务，`worker='replay-engine'`，不占 worker 并发）。scope_policy 同名键覆盖 tuning 默认由 per-engagement 数据决定，**本层不强制、仅透传**。
- **merge**：worker 有效环境用 `WorkerConfig.effective_env(cfg.common_env)`（per-worker 优先）；Agent 容器绝不注入 Cairn token（C5）。
- **客户端**：`CairnClient` 无缓存、每次走 HTTP；进度/心跳的进程内缓存由 40 自行实现。创建 finding 时 `detected_by` 传 worker 名、`actor='agent'`（仅 open）；`verify` 派发需排除 finding 创建者 + 跳过 `verify_eligible=false` worker。覆盖写回用 `write_coverage_result` + `Idempotency-Key` 头防重放。
- **端点假设需对齐**：§3 表中标注「路径假设」的端点（`scope/check`、`coverage/result`、`task_runs`、`/tasks/{id}/events`、`/tasks/{id}/finish`、`/report` latest）在阶段 2 联调时以服务端子域实际实现为准，改任一侧要同步本表。
- **resolve_traffic 默认值差异**：skeleton 服务签名默认 `for_model=False`（全量）；本客户端默认 `for_model=True`（digest）。40 派发 LLM 任务前用默认值即可拿到 digest。
- 本包不实现任何任务逻辑（30）与调度主循环（40）。

## 6. 自测结果

- `pytest cairn/tests/test_dispatcher_config.py cairn/tests/test_protocol_client.py` → **30 passed**（config 20 例 + client 10 例）。
- 全量 `uv run --project cairn pytest -q` → **85 passed**（含 10/13 并行产物，无冲突；agent 13 `dispatcher/cli.py` 已按本层 `config.load` API 接入）。
- CLAUDE.md §6 yaml 冒烟三份示例均 OK。

## 7. 未实现 / 待定

- 任务执行器（bootstrap/reason/explore/verify/audit/replay 逻辑）与调度主循环——属 30/40，本包不实现。
- `scope/check`、`coverage/result`、`task_runs`/events/finish、report latest 等端点的**服务端实现**尚未存在（阶段 2 联调）。
- `local` 顶层段在 `dispatch-config-spec.md` §0 顶层列表未列出，但 `dispatch.local.example.yaml` 用了它——本包已按示例支持；若 spec 属疏漏建议在 Phase 0 对齐检查中补入 spec。
