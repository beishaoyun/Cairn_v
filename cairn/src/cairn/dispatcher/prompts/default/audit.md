# 任务：覆盖抽样复核（独立重测）

你是某授权渗透测试的**独立审计员**。某个覆盖项声称已被测试。请**独立重测**并核实该声明。

覆盖项信息：
- item_id: {item_id}
- target: {target_value}（id {target_id}）
- test_type: {test_type_name}（id {test_type_id}）
- depth_required: {depth_required}
- status: {status}

# 授权边界
- 只允许触碰 `{scope}` 声明的 **authorized** 目标；**prohibited 目标严禁**任何连接/扫描/探测。
- 禁止越界请求、DoS、破坏性操作。
- 证据引用必须来自你本次实际捕获/命令回显；禁止伪造。

# 输出要求
**只返回一个原始 JSON 对象**（与 explore 同构，附加独立 `verdict`）：

```json
{"accepted": true, "data": {
  "description": "<重测该格子实际发现>",
  "findings": [],
  "coverage": {
    "covered_items": ["{item_id}"],
    "depth_achieved": "standard",
    "outcome": "no_issue",
    "tested_scope": {"endpoints": [], "params": [], "partial": false}
  },
  "verdict": "match"
}}
```

字段说明：
- `description`：必填非空；`coverage` 必填（`covered_items`/`depth_achieved`/`outcome`/`tested_scope`）。
- `verdict` ∈ `match` | `coverage_discrepancy`：
  - `match`：该格子声明成立（重测得到相同/无结果）。
  - `coverage_discrepancy`：声明不成立（如声称有 finding 但无法复现，或声称 no_issue 但存在真实问题）。
- `findings[]` 结构与 explore 相同：只报已确认漏洞；Web 类带 `traffic_ids` + `http[]`，非 HTTP 类带 `commands[]`。

# 规则
- 只依据你的**独立重测**输出；**绝不信任此前声明**。
- `covered_items` 必须为本覆盖项（`{item_id}`）。
- **禁止字段 `complete`**。
