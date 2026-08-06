<!-- mock-phase: verify mock-stage: blind -->
# Mock Prompt: verify · blind stage (structured JSON contract)

You are an INDEPENDENT analyst on an authorized penetration test. You have NOT
been told what vulnerability to look for. The mock driver emits JSON driven by
`MOCK_VERIFY` — the blind stage emits `observations` (from payload / rules that
match `prompt_has: observations`).

# Output Requirements
Return only one raw JSON object:

{"accepted": true, "data": {
  "observations": [
    {"vuln": "SQL injection in /login", "severity": "high",
     "traffic_id": "tr-001", "basis": "response echoes SQL error"}
  ],
  "traffic_note": "no issue demonstrable / limited coverage"
}}

# Rules
- Base observations ONLY on `{traffic_digest}`. Do NOT run new tools.
- Honest negatives are welcome: `observations: []` is valid.

# Context
## Traffic digest
{traffic_digest}
## Authorized scope
{scope}
