# 任务：初探收尾（总结已确认成果）

这是初探阶段的**收尾指令**，覆盖此前任何指令：**立即停止一切工作**，只总结本次初探已经确认的侦察结果与部分 discoveries。不要继续规划、等待或执行更多工具。

# 输出要求
**只返回一个原始 JSON 对象**（结构与 bootstrap 相同）：

```json
{"accepted": true, "data": {
  "fact": {"description": "<已确认的侦察总结>"},
  "discoveries": [
    {"target": "<主机/IP>", "port": 80, "service": "http"}
  ],
  "coverage": {"outcome": "no_issue"}
}}
```

字段说明：
- `fact.description`：必填，总结已确认的攻击面。
- `discoveries[]`：已确认的部分发现（可为空数组）。
- `coverage.outcome`：bootstrap 阶段固定为 `no_issue`。
- **禁止字段 `complete`**；用 `sweep_complete` 表示「初探完成」。

# 规则
- 只基于已确认的信息总结；不要补测、不要等待、不要运行更多命令。
- 只允许在授权范围内活动；禁止越界请求、DoS、破坏性操作（本阶段不应再发起任何请求/命令）。
- 诚实负面：没有任何已确认发现时，如实说明。
