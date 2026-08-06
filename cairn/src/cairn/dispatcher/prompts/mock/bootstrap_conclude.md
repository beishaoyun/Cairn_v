<!-- mock-phase: bootstrap_conclude -->
# Mock Prompt: bootstrap_conclude (structured JSON contract)

Conclude phase for the mock initial sweep. STOP all work; summarize only
already-confirmed recon findings and partial discoveries.

# Output Requirements
Return only one raw JSON object:

{"accepted": true, "data": {
  "fact": {"description": "<confirmed recon summary>"},
  "discoveries": [...],
  "coverage": {"outcome": "no_issue"}
}}

# Rules
- No `complete` field. Findings only if already confirmed.
