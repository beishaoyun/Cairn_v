<!-- mock-phase: replay -->
# Mock Prompt: replay (deterministic engine result injection)

`replay` is a **deterministic engine task** (worker='replay-engine', not an LLM
task). The mock driver injects the engine's *result* (response-signature
comparison product) via `MOCK_REPLAY`, closing the full-chain retest loop.
This file documents the injected result contract.

# Injected Result (mock driver stdout)
Return only one raw JSON object:

{"accepted": true, "data": {
  "result": "remediated",
  "matched_original": 0
}}

# Rules
- `result` ∈ remediated | unchanged | ambiguous | error.
- `matched_original` = number of original response signatures that still match.
- Replay evidence (the replayed exchange) is written by the capture writer as a
  traffic entry with `role=replay` (fixture seeds tr-101).

# Context
## Trigger package
{trigger_traffic_id}
## Payload variants
{variants}
## Authorized scope
{scope}
