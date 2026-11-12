# Changelog

Every release is a tag `vMAJOR.MINOR.PATCH`; the major tag (`v1`) moves to the newest release of its
major version, and callers name the major tag (README.md, "Versions"). The newest release comes first.

## v1.5.0 (2026-11-12)

Further rounds cost less: they start where the last review ended, read what the push changed, and a small push is
only checked. Measured on the run records of TUBS-IVS/eqasim-bs (2026-11-04 to 2026-11-12) and replayed there without
a model call (rounds 2 and 3 of its PR #571 read 13 and 39 lines instead of 1461 and 1486, on the light tier instead
of the deep one).

- A push that arrives while a review still runs no longer makes the next run review the whole PR again. That round is
  stale, and a stale round never counted as the reviewed commit, so the next run started from nothing, on the tier of
  the first round (TUBS-IVS/eqasim-bs PR #571, #582, #607: three whole deep reviews each, for pushes of 2 to 91 lines).
  The reviewed commit is now the complete round whose commit is the newest ancestor of head, stale or not.
- A further round reads the push, not the whole section of every file the push touched: its diff is cut down to the
  lines changed since the last reviewed commit, marked `>`, with 30 lines of context; unchanged hunks are left out.
  No change of the push is hidden by the cut: what leaves no line to mark (removed lines, a part of the PR's change
  that was taken back) is named in a `~` line, and a hunk that removes lines is left out only when an earlier round
  read exactly that hunk, so a merge of the base branch that changes what the PR replaces is shown. The prompt
  paragraph `{{DIFF_BLOCK}}` explains the marks, so no prompt file needs a change. Findings are still placed on the
  whole sections, and the review step's log line says how much was cut away.
- New optional config key `supporting_paths` (documentation, tests): changed lines and files in these paths do not
  count towards the deep tier's size thresholds (`deep_min_lines`, `deep_min_files`). They still count for the full
  tier, the structure rules are unchanged, and the model still reads them. TUBS-IVS/eqasim-bs PR #553, 25 lines of
  code and 1119 lines of documentation, was a deep review.
- New optional key `rereview.small_push_lines`: a push of at most that many changed lines, without a structure
  change, is only checked. It runs on `rereview.verify_tier` instead of the tier the first-round rules give it (nearly
  all code is a critical path, so every fix was a full review), and never on a higher tier than they give. A push
  that takes a file out of the PR or changes a binary file is never small.
- New optional key `rereview.verify_effort`: the effort of every check, that is of the verify rounds and of small
  pushes. Without it a check runs at the effort of its tier, as every run did.
- The gate decides the effort of a run (new output `effort` of the gate job, passed to `review` and `outcome`); the
  run record (log line, job summary, artifact) names it (new field `effort`).
- Minor release: without the new keys the gate picks every tier as before. The caller's interface, the artifact
  names, the findings record and the state block are unchanged (`stale` is still written, for an earlier version to
  read after a roll-back), and the run record only gains `effort`. Two things change without a config change: the
  round after a stale round, and the diff a further round reads.

## v1.4.0 (2026-11-11)

- A tier's `model` may be an alias such as `fable` or `opus`; the pinned CLI resolves it to the newest version it
  knows. The run record (log line, job summary, artifact) names the version called next to the configured name
  (new field `models_used`).
- Claude Code CLI 2.1.322 (was 2.1.309); Fable 5.2 needs 2.1.321 or later.
- New workflow `bump-claude-code.yml`: every Monday it opens a pull request when npm has a newer Claude Code CLI.
- Minor release: full model ids stay valid, and the config keys, the caller's interface, the artifact names and the
  findings record are unchanged; the run record only gains `models_used`.

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
