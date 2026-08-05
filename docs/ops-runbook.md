# 运维手册（Deployment & Operations Runbook）

> 配套：`architecture-research-report-pentest-v2.md`、`database-ddl-draft.md`、`worker-sandbox-hardening.md`
> 用途：新平台（渗透测试版）从裸机到稳定运行的运维指南 —— 部署、密钥、迁移、CA 生命周期、备份恢复、容量规划、故障处理
> 本文档补足 v2 报告未含的运维章节（O1）。

---

## 1. 部署拓扑与前置

```
宿主（Linux，≥4C/8G，磁盘按 §5 规划）
├── docker compose：
│   ├── cairn-server        # 进程一：FastAPI + SQLite（唯一 DB 写者）
│   ├── cairn-dispatcher    # 进程二：调度执行器（不直挂 docker.sock，经 executor）
│   └── cairn-executor      # P1 侧车：唯一持 docker.sock 的进程（可选但推荐）
├── 证据/流量/工作区持久卷  # datas/{evidence,traffic,workspace}
└── cron：每日备份 + VACUUM + 证据 GC
```

- 单机部署即可（SQLite + 线程池，无横向扩展）；多实例 Dispatcher **不支持**（v2 §1.3）。
- 网络：Server/Dispatcher 间走内网；**捕获代理（mitmproxy）只在本机/内网监听**，不对外暴露。
- 时间：所有节点统一 NTP，杜绝时钟漂移影响租约/窗口判定（v2 §2.1 统一 UTC）。

## 2. 环境变量与密钥清单

| 变量 | 注入到 | 说明 |
|---|---|---|
| `CAIRN_API_TOKEN` | server、dispatcher、executor | 平台 Bearer Token（内部工具鉴权）。**仅这三个进程**；**绝不注入 Agent 容器**（§12 规则 37） |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 等 | 仅 Agent 容器（经 dispatcher 注入） | LLM CLI 凭证，环境变量，不落镜像 |
| `CAIRN_EXECUTOR_URL` | dispatcher | executor 侧车地址（P1） |
| `CAIRN_DB_PATH` | server | SQLite 路径（默认 `~/.local/share/cairn/cairn.db`） |
| `CAIRN_EVIDENCE_ROOT` | server、executor | 证据/流量根目录（静态加密或 0700，见 §5.3） |
| `${VAR}` 展开 | dispatch.yaml | 配置内密钥一律 `${ENV_VAR}` 引用，仓库禁明文 |

- 首次启动 server 会打印一次自动生成的 token（或显式设 `CAIRN_API_TOKEN`）；记录到密钥库。
- `.gitignore` 覆盖 `*.env` / `dispatch.yaml`；示例文件只放占位符。
- CI 卡点：`gitleaks` 全仓 secret 扫描（v2 §2.5）。

## 3. 部署步骤

```bash
# 1) 克隆 + 构建
git clone <repo> && cd cairn
cp dispatch.example.yaml dispatch.yaml          # 填入 ${ENV_VAR} 引用，不写明文

# 2) 配置环境
export CAIRN_API_TOKEN="$(openssl rand -hex 32)"
# 其余 LLM key 注入 docker-compose environment（或 .env，不入库）

# 3) 起服务（executor 侧车 P1，推荐）
docker compose up -d --build
docker compose ps                              # 三个服务均 healthy

# 4) 首次冒烟
curl -s -H "Authorization: Bearer $CAIRN_API_TOKEN" \
  http://127.0.0.1:8000/settings
```

## 4. 数据库迁移 SOP（v1 → v2）

1. **备份先行**：`sqlite3 cairn.db "VACUUM INTO 'backup_$(date +%s).db'"`。
2. 启动新版本 server，`db.py` 迁移钩子自动执行（幂等 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE projects ADD COLUMN engagement_id` 等，见 DDL §10）。
3. 验证：`PRAGMA user_version` 与迁移日志；抽查 `projects.engagement_id` 回填为 NULL（历史数据兼容）正常。
4. 老库 `bootstrap_mode → bootstrap_enabled` 由迁移映射（沿用 v1 `_ensure_project_columns`）。
5. 失败回滚：停新版本 → 用备份库恢复 → 回到旧版本。

## 5. 存储与容量规划

### 5.1 数据分布

| 路径 | 内容 | 增长特性 |
|---|---|---|
| `datas/cairn/cairn.db` | 结构化库 + FTS5 | 慢（元数据） |
| `datas/evidence/{engagement_id}/` | 截图/命令日志/报告 | 中 |
| `datas/traffic/{engagement_id}/` | 全量请求/响应 + pcap | **最快**（见下） |
| `datas/workspace/{project_id}/` | 每项目持久工作区（B2） | 中 |

### 5.2 流量容量核算（D6）

目录爆破 10k-100k 请求 × 1-4KB ≈ 数十~几百 MB；大文件测试/长窗口可破 10GB/engagement。
→ 磁盘 = 并发 engagement 数 × `capture_quota` × 1.5（归档缓冲）。默认 quota 10GB，planning 时人工确认。

### 5.3 安全基线

- `evidence/`、`traffic/` 目录**静态加密或 0700 受限权限**（捕获流量含目标真实凭据，capture spec §9.9）；
- 归档（C4）强制加密后再 zstd；
- `VACUUM INTO` 备份文件同样落在受限目录。

## 6. 备份与恢复演练

| 项 | 策略 |
|---|---|
| 数据库 | 每日 `sqlite3 db "VACUUM INTO 'backup_<date>.db'"`（在线备份，WAL 下安全）+ 保留 30 天 |
| 证据/流量 | 每日增量 rsync 到异地/对象存储；归档块含 sha256 清单（C4/C5） |
| VACUUM | 每日对主库 `PRAGMA incremental_vacuum` 或定期全量 `VACUUM`（WAL 文件收缩） |
| 恢复演练 | 每季度：从备份库启动新实例 → 冒烟 `/settings` + 抽查最近 engagement → 对比 traffic 校验和 |

## 7. CA 生命周期（捕获代理）

| 阶段 | 操作 |
|---|---|
| 生成 | 每 Engagement 生成独立 CA（私钥仅 Dispatcher/executor 持有） |
| 注入 | 容器注入证书（`SSL_CERT_FILE` / `NODE_EXTRA_CA_CERTS` 等，见 sandbox §4.1）；Java 需 trustStore（C6 差异） |
| 轮换 | 定期或密钥疑似泄露时轮换：旧 CA 吊销 + 新 CA 重注入 + 容器重建 |
| 吊销 | Engagement 结束（finalize/archive）即吊销 CA；kill 即停捕获（C3） |
| 审计 | `openssl ca -status <serial>` 可查；吊销列表留档 |

## 8. 调度与捕获健康监控

- **Dispatcher 健康**：`/settings` + 日志"主循环 tick"间隔；`scheduler_state` 落库（重启恢复 checkpoint/冷却）。
- **捕获代理**：每 Engagement 检查 mitmproxy 存活 + 白名单刷新（C11，≤1 interval）；`capture_gap` 计数（C2 对账）出现即告警。
- **容量告警**：`capture_quota` 用量 >80% → 告警 + 滚动归档；磁盘 <20% → 停新 engagement。
- **熔断演练**：每月在 staging 触发 global kill switch，验证 1 秒内全部 Agent 进程被 SIGKILL（C1）。

## 9. 常见故障处理

| 症状 | 排查 | 处置 |
|---|---|---|
| Dispatcher 重启丢状态 | 检查 `scheduler_state` 落库是否开启 | 启动即回载，无需人工 |
| 写库 `database is locked` | WAL + busy_timeout 已设；多为 task_events 高频写 | 降级：事件摘要批量化 / 原始流文件即真相（capture spec §7.2） |
| 捕获缺失（`capture_gap`） | 代理是否存活 / 白名单是否刷新 / NO_PROXY 泄漏 | 补测 + 降级命令证据（C2/F10） |
| Agent 绕过代理 | TLS 指纹/NO_PROXY 忽略 | fail-closed 白名单兜底（F5），报告标注 unverified |
| 磁盘告警 | 归档未触发 | 手动触发滚动归档；临时调 quota |
| token 泄露 | 检查 Agent 容器是否误注入 `CAIRN_API_TOKEN` | 立即轮换 token + 重建容器（§12 规则 37） |

## 10. 验收清单

1. 三容器健康；`CAIRN_API_TOKEN` 仅在 server/dispatcher/executor（`docker exec` 检查 Agent 容器无该变量）。
2. 备份/恢复演练通过；VACUUM 计划在 cron 中。
3. 流量/证据目录权限 0700 或静态加密；归档可校验恢复。
4. kill switch 演练：SIGKILL < 1s；捕获同步停止。
5. CA 吊销列表有效；新 Engagement 的 CA 与旧隔离。
