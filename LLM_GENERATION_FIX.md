# LLM generation failure repair

## Root cause

The rc4 research adapter sent the complete Pydantic `Generated` schema to Gemini's
structured decoder. Its bounded arrays contain nested hypothesis objects with further
bounded arrays, long strings and numeric bounds. Gemini rejected this request with HTTP
400: the schema constraint had too many states for serving. This happened before any
candidate generation or BRAIN simulation.

The private incident export retained only `LLMError`, so it could not distinguish this
from authentication, quota or transport errors. The application log did not retain the
HTTP details. Read-only reconstruction of historical SQLite journal snapshots recovered
the schema-complexity error alongside the two failure events. A later successful model
call had cleared the current key error field. No private export, journal, prompt,
expression, response, database or credential belongs in this repository.

Two independent application defects amplified the provider rejection:

1. Provider status and failure category were discarded when wrapping exceptions. Some
   controlled-call paths replaced the cause with a generic attempt-limit message.
2. An empty generation stage advanced through selection/evaluation to COMPLETE even when
   every generation had failed and no research result existed.

## Correction

- Send a compact shape schema to the provider: preserve objects, required fields,
  references and scalar types, while enforcing value and cardinality bounds with the
  unchanged Pydantic validator locally. The full contract is also included in the prompt.
  No fallback HTTP request or automatic format negotiation is added.
- Persist allowlisted diagnostic categories, HTTP status and recovery instructions.
  Provider bodies and response content are excluded from campaign failure events.
  HTTP rejection, model access, credentials, quota, rate limiting, unavailable service,
  timeout, connection, malformed JSON, local schema failure, refusal, empty output and
  truncation are separate outcomes.
- Retain durable request reservations after uncertain/failed attempts. Missing usage
  remains unknown. A successful HTTP response that fails parsing or validation retains
  its known usage. Every additional physical request still needs its own reservation.
- Stop all-failed generation as terminal FAILED before simulation. Successful candidates
  survive partial generation failures. Failed campaigns cannot be resumed or reset.
- Project legacy false-COMPLETE reports and history as FAILED while exposing storedStatus;
  the original record is unchanged. Legacy causes remain unknown unless independently
  recovered; the application does not invent diagnoses for old events.
- Show failure details, settings navigation and a recovery-draft action. Recovery clears
  previous approval text and draft overrides, makes no provider call, and requires a new
  preview, frozen caps and explicit live approval. Global charges are retained.

## Validation and limits

`backend/tests/test_generation_failures.py` uses the real OpenAI-compatible SDK with
MockTransport, a disposable SQLite database and synthetic credentials. It exercises the
schema-complexity incident through SDK, service, parser and controller, plus HTTP and
transport faults, malformed/schema-invalid responses, known/unknown usage, partial
success, restart and historical projection. Existing durable accounting, pause/resume,
budget, AST and research integration regressions remain part of the complete suite.

The native macOS smoke test runs these fault-injection tests against the installed
release wheel before testing launcher startup, frontend/API readiness, offline research,
pause/restart/resume, exports and preference/history persistence. Validation branch builds
cannot publish a release.

No live generation, paid API request, BRAIN simulation or alpha submission is needed for
these gates. Provider acceptance and output quality of a new live campaign remain an
explicitly approved canary step; offline tests cannot prove live provider availability.

Provider reference: [Gemini structured output constraints](https://ai.google.dev/gemini-api/docs/structured-output).
