# 任务：探索收尾（同一条 intent 的总结）

这是对**同一条 intent** 的收尾阶段，覆盖此前任何指令：**停止探索、停止等待、停止执行命令**。只总结本阶段已经确认的内容。不要规划、不要补测、不要运行更多工具。

# 输出要求
**只返回一个原始 JSON 对象**（结构与 explore 相同，`findings` 可选，`coverage` 必填）：

```json
{"accepted": true, "data": {
  "description": "<已确认的关键发现>",
  "findings": [],
  "coverage": {
    "covered_items": ["c-013"],
    "depth_achieved": "standard",
    "outcome": "no_issue",
    "tested_scope": {"endpoints": [], "params": [], "partial": false}
  }
}}
```

字段说明：
- `description`：必填非空，总结已确认结论。
- `findings[]`：只携带已有证据的已确认漏洞（结构与 explore 相同：severity/cvss/cwe/asset/evidence_refs/traffic_ids/http/commands）。
- `coverage`：必填；`covered_items` 必须 ⊆ `{coverage_context}`，`depth_achieved`/`outcome` 取枚举值。
- **禁止字段 `complete`**。

# 规则
- 只基于已确认信息输出；不要规划、不要等待、不要执行更多命令。
- 只允许在授权范围内活动；禁止越界请求、DoS、破坏性操作（本阶段不应再执行任何命令）。
- 诚实负面：没有已确认发现时，如实报告 `outcome: "no_issue"` 并在 `description` 说明。

# 上下文
## 当前 intent
{intent_id} — {intent_description}
## 覆盖上下文
{coverage_context}
