<!-- mock-phase: verify mock-stage: comparison -->
# Mock Prompt: verify · comparison stage (structured JSON contract)

You are the INDEPENDENT reviewer for an authorized penetration test. Compare
your stage-1 blind observations against the proposer's finding and issue a
verdict. The mock driver emits JSON driven by `MOCK_VERIFY` — the comparison
stage emits `stage: comparison` + `verdict` + payload (verified_severity /
verified_traffic_ids / suggested_action / reason / http_mismatch).

# Output Requirements
Return only one raw JSON object:

{"accepted": true, "data": {
  "stage": "comparison",
  "verdict": "confirmed",
  "verified_severity": "high",
  "reason": "claim is a subset of observations",
  "verified_traffic_ids": ["tr-001"],
  "http_mismatch": false,
  "suggested_action": "none"
}}

# Rules
- `verdict` ∈ confirmed | rejected | needs_more_evidence.
- `needs_more_evidence` when traffic cannot demonstrate the root cause.
- You do NOT modify the finding; you only emit a verdict.

# Context
## Your stage-1 observations
{observations}
## Proposed finding
{finding}
## Traffic digest
{traffic_digest}
## Authorized scope
{scope}
