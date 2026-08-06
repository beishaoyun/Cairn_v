<!-- mock-phase: audit -->
# Mock Prompt: audit (structured JSON contract)

You are the coverage sampling reviewer. A high-priority coverage cell was
independently retested to confirm the recorded coverage verdict. The mock
driver emits JSON driven by `MOCK_AUDIT`.

# Output Requirements
Return only one raw JSON object:

{"accepted": true, "data": {
  "item_id": "c-013",
  "verdict": "covered",
  "reason": "independent retest agrees with recorded result",
  "depth_reached": "standard"
}}

# Rules
- `verdict` ∈ covered | discrepancy.
- `discrepancy` → the sampled cell falls back to untested and is re-prioritized.

# Context
## Coverage item
{item_id} — {target} × {test_type}
## Prior recorded result
{recorded}
## Authorized scope
{scope}
