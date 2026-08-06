<!-- mock-phase: explore_conclude -->
# Mock Prompt: explore_conclude (structured JSON contract)

Conclude phase for the SAME intent. STOP exploring; summarize only what was
already confirmed before this prompt. The mock driver emits JSON driven by
`MOCK_EXPLORE_CONCLUDE`.

# Output Requirements
Return only one raw JSON object, same shape as explore:

{"accepted": true, "data": {"description": "<confirmed key findings>",
  "findings": [...], "coverage": {"covered_items": [...], "depth_achieved": "...",
  "outcome": "..."}}}

# Rules
- Base output ONLY on already-confirmed information.
- No `complete`.
