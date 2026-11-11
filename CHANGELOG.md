# Changelog

Every release is a tag `vMAJOR.MINOR.PATCH`; the major tag (`v1`) moves to the newest release of its
major version, and callers name the major tag (README.md, "Versions"). The newest release comes first.

## Unreleased

- A tier's `model` may be an alias such as `fable` or `opus`; the pinned CLI resolves it to the newest version it
  knows. The run record (log line, job summary, artifact) names the version called next to the configured name
  (new field `models_used`).
- Claude Code CLI 2.1.322 (was 2.1.309); Fable 5.2 needs 2.1.321 or later.
- New workflow `bump-claude-code.yml`: every Monday it opens a pull request when npm has a newer Claude Code CLI.

## v1.3.0 (2026-11-05)

- No output of a review shows its API cost any more (the owner's decision of 2026-11-05). For a public repository the
  summary comment with its state block, the log and job summary of a run and its artifacts are all public.
  - The rounds table of the summary comment has no cost column; its footer gives the turns only.
  - The state block records per round whether it called the model (`model_call`) instead of the amount. A block
    written by an earlier version is read the same way (a cost above zero is a model call) and loses its amounts at
    the next write, so the round cap of an open PR still holds.
  - The run record (job summary, log line, artifact `claude-review-cost-*`) gives status, tier, model, turns and
    duration: no cost and no token counts, which give the cost at the public prices.
  - The findings record keeps the key `cost_usd`, always empty (schema 2 only adds fields).
- The budgets per run are not published either. They come from the new optional secret `CLAUDE_REVIEW_BUDGETS` (an
  organization secret, JSON with each tier's budget in USD), which the caller passes next to `ANTHROPIC_API_KEY`
  (`examples/claude-review.yml`). Only the gate and the review step get it, the model's own process does not, and no
  output names a budget. `check-config` validates it without quoting a value and refuses a `max_budget_usd` left in
  the config while the secret is set. A caller that does not pass the secret yet still runs on its config's
  `max_budget_usd`, with a warning on every run; the code holds no upper bound for that value any more. On GitHub
  Free a private repository does not get organization secrets and needs a repository secret of the same name.
- A money amount in a text of the CLI that the review publishes (the error of a failed run, the CLI's own warnings in
  the log) is replaced by `[amount]`: a run cut off by its budget may name it.
- The command `cost` is `outcome` now. File and artifact of the run record keep their v1 names.
- Minor release: the config keys (`max_budget_usd` stays valid without the secret), the caller's interface (the new
  secret is optional), the artifact names and the fields of the findings record stay, and a state block of an earlier
  version is read as before. Moving `v1` back below v1.3.0 would count the rounds that v1.3.0 recorded as free (they
  carry no `cost_usd`), and a caller that passes the budgets secret would fail on an older version; roll forward
  instead.
- Outputs written before this release keep their amounts (earlier comment revisions, and logs, job summaries and
  artifacts within their retention). The cost of the reviews is in the Anthropic Console, under the workspace of each
  repository's API key.

## v1.2.0 (2026-11-05)

- A fix pushed after the last automatic round is still checked. Past `rereview.max_rounds`, a push with open
  findings gets up to `rereview.verify_after_cap` more rounds (new optional key, default 2) on
  `rereview.verify_tier`; they check the fixes in the changes since the last complete round and never re-review the
  push. A merge of the base branch alone gets none, a revert gets one without a diff, a rebase none. Before, the cap
  stopped every run, so the fix of a last-round finding was never verified. `verify_after_cap: 0` restores that.
- A reply starting with "Fixed" keeps its finding open for that check also when its thread was resolved; before,
  any resolved thread dismissed the finding unverified. A resolved thread without a fix claim still dismisses it.
- The summary says what a push after the cap still gets.

## v1.1.0 (2026-11-04)

- New optional config key `tiers.<tier>.effort` (`low`, `medium`, `high`, `xhigh` or `max`): the review runs
  `claude -p --effort <level>` for that tier. Without the key nothing changes: the CLI applies the model's default
  (medium on Opus 5.5 and Sonnet 5.5, high on Fable 5.1; Claude Code docs, "Model configuration"). `check-config`
  rejects any other value, because the CLI would only warn and silently run at the default. The review step's log
  line names the effort of every run.

## v1.0.0 (2026-11-04)

First release as a central repository (ADR-0176 in TUBS-IVS/eqasim-bs).

- `claude_review.py` imported from TUBS-IVS/eqasim-bs `scripts/claude_review.py` at its `main` 5d08a5f7 (last
  change of the script: PR #536), plus the fix that matches posted inline comments to findings by path and body
  (`match_comment_ids`; GitHub's listing of a review's comments has no `line` field, so before the fix every
  findings record carried `comment_id` None).
- New command `check-config`, run first by the gate job: it validates the calling repository's gate config and
  prompt and fails the gate, before anything is spent, on a missing tier, a budget above the owner's cap per run, a tool
  beyond Read/Grep/Glob, a misspelt key or an unknown prompt placeholder.
- The reusable workflow `.github/workflows/review.yml` (the two jobs of the former per-repository workflow) fetches
  the script at its own commit (`job.workflow_sha`); callers keep triggers, queue and permissions
  (`examples/claude-review.yml`) and pass only `ANTHROPIC_API_KEY`.
- The tests moved with the script (`tests/test_claude_review.py`); they run against a fixture caller holding
  TUBS-IVS/eqasim-bs's gate config and prompt as of the import.
- Unchanged: gate rules, tiers, prompts, the findings record (`schema: 2`), the artifact names and the state block.
