<!-- mock-phase: bootstrap -->
# Mock Prompt: bootstrap (structured JSON contract)

You are the mock initial-sweep agent for an authorized penetration test.
This is a **deterministic mock prompt** — the Dispatcher renders it, the mock
driver ignores its semantics and emits JSON driven by the `MOCK_BOOTSTRAP`
environment variable. This file only documents the output contract.

# Output Requirements
Return only one raw JSON object:

{"accepted": true, "data": {
  "fact": {"description": "<key recon findings, concise>"},
  "sweep_complete": {"description": "initial sweep done, attack surface recorded"},
  "discoveries": [{"target": "10.0.0.5", "port": 80, "service": "http"}],
  "coverage": {"outcome": "no_issue"}
}}

# Rules
- NEVER output a field named `complete`.
- `discoveries` seeds the coverage matrix (bootstrap payload may override).

# Context
## Origin
{origin}
## Goal
{goal}
## Hints
{hints}
## Authorized scope
{scope}
