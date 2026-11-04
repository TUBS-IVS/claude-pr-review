# Changelog

Every release is a tag `vMAJOR.MINOR.PATCH`; the major tag (`v1`) moves to the newest release of its
major version, and callers name the major tag (README.md, "Versions"). The newest release comes first.

## v1.0.0 (2026-11-04)

First release as a central repository (ADR-0176 in TUBS-IVS/eqasim-bs).

- `claude_review.py` imported from TUBS-IVS/eqasim-bs `scripts/claude_review.py` at its `main` 5d08a5f7 (last
  change of the script: PR #536), plus the fix that matches posted inline comments to findings by path and body
  (`match_comment_ids`; GitHub's listing of a review's comments has no `line` field, so before the fix every
  findings record carried `comment_id` None).
- New command `check-config`, run first by the gate job: it validates the calling repository's gate config and
  prompt and fails the gate, before anything is spent, on a missing tier, a budget above 50 USD per run, a tool
  beyond Read/Grep/Glob, a misspelt key or an unknown prompt placeholder.
- The reusable workflow `.github/workflows/review.yml` (the two jobs of the former per-repository workflow) fetches
  the script at its own commit (`job.workflow_sha`); callers keep triggers, queue and permissions
  (`examples/claude-review.yml`) and pass only `ANTHROPIC_API_KEY`.
- The tests moved with the script (`tests/test_claude_review.py`); they run against a fixture caller holding
  TUBS-IVS/eqasim-bs's gate config and prompt as of the import.
- Unchanged: gate rules, tiers, prompts, the findings record (`schema: 2`), the artifact names and the state block.
