<!-- mock-phase: explore_execute -->
# Mock Prompt: explore_execute (structured JSON contract)

You are an authorized penetration tester exploring ONE intent (mapped to
coverage cells) inside an engagement. The mock driver emits JSON driven by
`MOCK_EXPLORE_EXECUTE` (findings + coverage_result injection). This file
documents the output contract.

# Output Requirements
Return only one raw JSON object:

{"accepted": true, "data": {
  "description": "<objective factual finding, concise>",
  "findings": [
    {"title": "...", "severity": "high", "cvss_score": 8.1,
     "cvss_vector": "CVSS:3.1/AV:N/...", "cwe_id": "CWE-89", "category": "webapp",
     "asset": "http://10.0.0.5:8080/login", "description": "...",
     "remediation": "...", "evidence_refs": ["e-001/screenshot.png"],
     "traffic_ids": ["tr-001"],
     "http": [{"method": "POST", "url": "http://10.0.0.5:8080/login",
               "request_headers": "...", "request_body": "user=admin&pass=' OR 1=1--",
               "response_status": 200, "response_headers": "...",
               "response_body": "SQL error near 'OR 1=1'..."}],
     "commands": []}
  ],
  "coverage": {"covered_items": ["c-013"], "depth_achieved": "standard",
               "outcome": "finding_created"}
}}

# Rules
- Only report CONFIRMED vulnerabilities; each needs severity + evidence.
- Web findings MUST include `traffic_ids` (truth reference) + `http[]` (semantic
  comment); non-HTTP findings use `commands[]` with real echo.
- `covered_items` must be cells actually tested this intent.
- No `complete` field.

# Context
## Graph snapshot
{graph_yaml}
## Current intent
{intent_id} — {intent_description}
## Coverage context
{coverage_context}
## Authorized scope
{scope}
