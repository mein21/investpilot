# InvestPilot Local — agent rules

## Mandatory context after every compaction

1. After any context compaction/summarization, STOP and read `PROMPT.md` in this
   directory from start to finish before doing anything else.
2. Then read this file and `README.md` (once it exists).
3. Continue from the last completed vertical slice; do not restart the project.
4. The customer's requirements in `PROMPT.md` are the source of truth. If code,
   docs and memory disagree, `PROMPT.md` wins.

## Non-negotiable engineering rules

- Runtime has NO AI/LLM/ML/text generation/external AI APIs. All investment
  decisions are deterministic rules, formulas and user settings.
- Money and quantities that affect money use `Decimal`; never `float`.
- All timestamps are timezone-aware UTC.
- T-Invest integration is isolated behind `TInvestClient`.
- External responses are validated with Pydantic.
- Retries only for safe GET/read calls. Never auto-retry order submission.
- Every order request/response gets an audit record with secrets masked.
- Real trading is disabled by default; Sandbox is the default.
- AUTO mode must remain a forbidden feature flag.
- Never commit `.env`, tokens, databases or backups.
- Do not invent T-Invest API fields. The verified contract reference lives in
  `docs/tinvest-contract-notes.md`; anything unverified must be marked TODO.

## Workflow

- One vertical slice at a time, tests after each slice.
- Run `ruff format`, `ruff check`, `mypy` (where configured) and `pytest`.
- Do not modify unrelated files.
- Each progress report: what was studied, files changed, why, verification
  commands, test results, remaining risks.
