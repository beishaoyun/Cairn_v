# 任务：确定性重放（复测证据闭环）

`replay` 是**确定性引擎任务**（`worker='replay-engine'`，非 LLM 任务）。它不依赖模型，而是重放原始触发包 + payload 变体，经捕获代理发送，与原始响应签名比对，判定目标是否已修复。本模板记录该引擎的**结果契约**（注入结果，不经过模型生成）。

# 触发包
{trigger_traffic_id}

# Payload 变体
{variants}

# 授权边界
- 重放请求只发往 `{scope}` 声明的 **authorized** 目标；**prohibited 目标严禁**任何连接/扫描/探测。
- 禁止越界请求、DoS（重放变体不得放大为拒绝服务）、破坏性操作。

# 输出结果契约
引擎输出一个原始 JSON 对象：

```json
{"accepted": true, "data": {
  "result": "remediated",
  "matched_original": 0
}}
```

字段说明：
- `result` ∈ `remediated` | `unchanged` | `ambiguous` | `error`。
- `matched_original`：仍与原始响应签名匹配的条数（**非负整数**）。
- `remediated` 触发复测确认账本（`kind='replay'`，幂等，重复触发不 +1）。
- 复测流量由捕获写入器以 `role=replay` 落库为证据，形成闭环。

# 规则
- 本任务是确定性引擎，不涉及模型输出；此文档仅说明注入结果契约与边界约束。
