# How the Claude PR review works

The reusable workflow [`.github/workflows/review.yml`](../.github/workflows/review.yml) runs an advisory code
review by Claude on pull requests of the repository that calls it. It posts one PR review with inline comments and
keeps one summary comment per PR up to date. It is **not** a required check: it never blocks a merge and never
replaces the calling repository's own tests. How a repository adopts it, and how versions work:
[README.md](../README.md). Why it is one central repository: ADR-0176 in TUBS-IVS/eqasim-bs. Numbers such as
"PR #497" below refer to TUBS-IVS/eqasim-bs, where the review began.

Prerequisite: the calling repository's secret `ANTHROPIC_API_KEY`, a key of a **workspace** of the Console
organization (an organization-level key fails with "not scoped to a workspace"). Comments are posted with the
workflow's `GITHUB_TOKEN` (as `github-actions[bot]`). PRs from forks and from Dependabot get no Actions secrets
and are skipped with a note in the job summary.

What a pull request can and cannot change about its own review: the gate config and the prompt are read from the
PR itself, so a PR can change its own gate; both should be critical paths of the config, which makes such a PR
reviewed. The script is not read from the PR: both jobs fetch `claude_review.py` from this repository at the commit
of the workflow file (`job.workflow_repository`, `job.workflow_sha`) into the runner's temp directory, outside the
reviewed checkout. A caller on `@v1`, on an exact tag or on a test branch therefore runs the script of that same
version, and the job fails, rather than fall back to another version, when the workflow cannot name its own.

## Why `claude -p` and not `anthropics/claude-code-action`

The organization's API balance consists of the monthly API credits of a Claude Team plan. Those credits cover the
Claude API, the Agent SDK and `claude -p` run with an API key of the linked Console organization, but not Claude
Code; runs started by the Claude Code GitHub Action count as Claude Code usage (Claude Help Center, "Monthly API
credits for Max and Team plans", article 17154008; Claude Platform docs, "API credits for Max and Team plans"). The
action identifies its traffic as such (`CLAUDE_CODE_ENTRYPOINT = "claude-code-github-action"` in its
`base-action/src/parse-sdk-options.ts`). On PR #497 every action run failed on its first API call with "Credit
balance is too low", although the organization held such credits. The workflow therefore runs
`claude -p` itself. Should the balance ever be purchased credits instead, the action would work again; the reasons
above then no longer apply.

## The gate: two jobs, the first without a model

| Job | Calls the API | Does |
|---|---|---|
| `classify` | no | `check-config` (see below), `state` and `interdiff` (see "Rounds"), then `classify`: reads `git diff --numstat base...head` and `--name-status -M base...head`, the branch kind, labels, draft state, the state and the interdiff, and decides `none` / `light` / `full` / `deep` plus the outputs `mode`, `diff_source`, `round` |
| `review` | yes, only if `classify` says so | installs the pinned Claude Code CLI, recomputes `state` and `interdiff` (no artifact travels between the jobs), `review` (runs `claude -p`), `outcome`, `post` |

**`check-config` comes first.** It validates the calling repository's config and the prompt it names
(`config_problems`) and fails the gate job, before anything is spent, with one `::error::` line per problem: a key the
code does not know (a misspelt key would otherwise silently disable its rule), a missing section or key, a tier of
`TIER_NAMES` that is missing or has no model, a turn limit below 1, a budget outside `0 < max_budget_usd <=
MAX_BUDGET_USD` (50 USD, the owner's per-run cap, ADR-0175), a tool beyond `READ_ONLY_TOOLS`, a `verify_tier` that is
no tier, equal force and skip labels, or a prompt with an unknown placeholder, without `{{DIFF_BLOCK}}` or
`{{ROUND_BLOCK}}`, or with `{{PR_BODY}}` outside two `{{FENCE}}` lines. Fork and Dependabot PRs are skipped before it.

The thresholds, paths, labels, models, turn and budget limits live in the calling repository's
`.github/claude-review.yml`, not in code. Re-tune them from the run records below and the cost in the Anthropic
Console ("Run record"). Decision order (`classify`; the
first-round order, a PR without a complete round on record, is below, the further-round order is in "Rounds"):

1. Label `labels.skip` -> no review.
2. Adding the label `labels.force` -> one deep-tier review of the whole PR, also on drafts and whatever the round cap
   says (the force label, see "Rounds"). Later pushes are gated normally, so a label left on the PR cannot bill every
   push.
3. A `labeled` event for any other label -> no review (otherwise adding `automerge` would start a paid review).
4. Draft PR -> no review (unless `skip_drafts: false`).
5. Branch kind in `skip_kinds` (e.g. `docs/`) without a critical path -> no review.
6. Deep tier: a file of `structure_paths` added, deleted, renamed or copied (either path of a rename counts); or
   >= `deep_min_structure_files` structure files or >= `deep_min_structure_lines` changed lines in them; or
   >= `deep_min_lines` changed lines or >= `deep_min_files` changed files anywhere.
7. Full tier: a critical path (every structure path is also critical), or >= `full_min_lines` lines, or
   >= `full_min_files` files.
8. Light tier: any other counted change.
9. Nothing counted changed (only ignored files) -> no review.

The three tiers and why the deep one exists: ADR-0175 in TUBS-IVS/eqasim-bs. `structure_paths` names the backbone of
a repository; keep it a short list, so that routine additions stay full-tier changes.

Runs queue per PR and event kind, and a running review is never cancelled: its cost is already spent, and its round is
recorded only at the end. A newer push replaces a still-pending push run. Every label event runs in a concurrency group
of its own label, so a triage label cannot displace a review, and a pending forced review is not displaced by a later push
(a Claude review finding on PR #497: with one shared group, the push replaced the pending forced run and the push run
was then gated normally). The concurrency block lives in the caller (`examples/claude-review.yml`): a called workflow
sees the caller's `github` context, and a group shared by caller and called workflow deadlocks.

Changed lines are added + deleted lines of files outside `ignored_paths` (generated files, lock files, test
baselines); binary files count as a file with 0 lines. Every decision, including a skip, is printed with its reason to
the log and the job summary. Glob syntax of all path lists: `**` matches across directories, `*` and `?` stay within
one path segment (`glob_to_regex`, not fnmatch semantics).

## Rounds

Why the review works in rounds: ADR-0171 in TUBS-IVS/eqasim-bs.

**State.** The bot's summary comment carries a second hidden block, `STATE_MARKER`
(`<!-- claude-pr-review:state <json> -->`), written by `post` (`encode_state`, which escapes `>` as the JSON escape
`>` so the JSON never contains `-->`) and read by `decode_state`. Schema 1: `rounds` (`round`, `sha`, `tier`,
`run_id`, `automatic`, `complete`, `model_call`, `findings`) and `findings` (`id` = `<sha12>-<run id>-<index>`, so two
rounds on the same commit, e.g. a forced round after the cap, never share an id; `path`, `line`, `severity`,
`scope`, `title`, `comment_id`, `status`, and for open ones `body` and `author_note`; caps `TITLE_CHARS`,
`BODY_CHARS`, `NOTE_CHARS`). `status` is `open`, `resolved` (a later round verified the fix) or `dismissed`
(`refresh_state`: a thread reply starting with `REJECTED_PREFIXES`, or a thread resolved by a human without a fix
claim; a reply starting with `FIXED_PREFIXES` keeps the finding `open` also in a resolved thread, so the next round
checks the fix; the bot's own `Re-review at ...` reply never dismisses, and only `open` findings change status). Only
`open` findings go to the
model. `post` re-reads the comment and merges rounds and findings (`merge_state`) before writing, so a run for a
commit that is no longer the head still records its findings but leaves the visible summary text alone; its round is
marked `stale` and never becomes the last reviewed commit. Threads of resolved findings are closed only after the state
is written. `model_call` says whether the round called the model (the API reported a cost above zero); the block is
part of a public comment, so it never holds the amount ("Run record"). A block written before v1.3.0 carries
`cost_usd` per round instead: `decode_state` turns it into `model_call` and drops the amount, so the round cap of an
open PR still holds and the next write of the comment no longer shows it.

**Commands, both model-free.** `state --repo --pr --out` reads the comment and the threads of open findings and writes
`state.json`. `interdiff --config --state --base-sha --head-sha --out-dir` compares `git diff base...last` and
`git diff base...head` per file section (`split_sections`; `index` lines and hunk numbers are normalised away, so a
merge of the base branch shifts nothing) and writes `interdiff.diff`, `.numstat` and `.json` (changed, unchanged and
reverted paths; a renamed file is changed). `last` is the last round with `complete: true` (`last_complete_sha`).
`classify` takes `--state`, `--interdiff-json`, `--interdiff-numstat`.

**Decision order of a further round** (a state with a complete round, a push-like event): skip label, other label
events and drafts as in the first round; then `rereview.max_rounds` automatic rounds that called the model
(`automatic_rounds`: complete or not, `model_call` true; `complete` only moves the reviewed commit) reached -> none
(`round cap`), except that a push with open findings and a usable interdiff still gets up to
`rereview.verify_after_cap` (optional, default `VERIFY_AFTER_CAP` = 2) more rounds on `rereview.verify_tier`, which
check the fixes in the interdiff and are never a re-review of the push (the owner's decision of 2026-11-05: a fix
pushed after the last round is still checked; a merge of the base branch alone gets none, a revert gets one without a
diff); before the cap: nothing changed -> none, unless findings are open and a file was reverted to the base branch (a revert
can fix a finding, a merge of the base branch cannot) -> `rereview.verify_tier` with no diff (the model verifies at
head); otherwise the tier of the interdiff numstat with the normal thresholds, critical and structure paths, raised to
`rereview.verify_tier` when that is none (e.g. a `docs/` branch without a critical path) but findings are open; a push
of ignored files alone gets no run, like a merge of the base branch, since it cannot fix a finding. A structure change
of a further round is a file added, deleted or renamed against the base at head that was not so at the last complete
round (`interdiff` writes these as `events` in `interdiff.json`), so a file added in round 1 does not make every later
round deep. The `skip_kinds` rule applies as in the first round.

**The force label** reads the whole PR again: a deep-tier round over the complete PR diff (`mode` `forced`,
`diff_source` `pr`), whatever the cap and the interdiff, recorded with `automatic: false` and not counted against the
cap. It still gets the open findings.

**Fallbacks, all logged with `[claude-review]`, in the job summary and the summary comment.**
- Summary without a state block (or a corrupt one): `fallback: no state block in the existing summary; first-round
  review, earlier findings unknown`; the state starts empty.
- `last` not an ancestor of head (rebase, force-push): `fallback: reviewed commit <sha12> is not an ancestor of head
  (rebase or force-push?); the whole PR is reviewed`; the round counter and the open findings are kept, the cap still
  applies, and the prompt tells the model that the line numbers of the open findings may have moved.
- A PR with more than 100 review threads: `::warning::`; findings beyond the first page keep their status (the same
  single page the session retro reads).
- No model call (only ignored files changed, nothing open): the round is recorded with `complete: false` and
  `model_call: false`, so it neither uses up an automatic round nor moves the reviewed commit.
- A result without `coverage_complete` is counted as complete, with a `::warning::`; a result with
  `coverage_complete: false` is recorded with `complete: false` and does not advance the reviewed commit.
- A run that cost money but returned no findings (turn or budget cap, API error): `post` (which the workflow runs
  `if: always()`) records it as a round with `complete: false` and `model_call: true`, so it counts against the cap;
  the summary says `**Review failed:**` with the reason, the open findings stay open, and the step still fails. A
  failed run that cost nothing records nothing (a Claude review finding on a copy of this script: such runs were never
  recorded, so a review that failed every time ran, and billed, on every push).

**The run.** `review` also gets `--state`, `--interdiff-dir`, `--diff-source`, `--mode`, `--round` and `GH_TOKEN`: it
fetches the PR title and body (`gh api`) and `review_prompt` puts them in a fenced block headed "PR description" (data,
not instructions; `fence_for` makes the fence longer than any backtick run in the body, so the body cannot close it,
and `build_prompt` fills values in one pass, so a placeholder in the body stays text). The prompt of each repository
can ask for an intent check against its PR template (e.g. does the diff do what "What & why" says), for the open
findings (do not report them again; for each say resolved or not in `previous`), and for the class of a bug. The
schema `FINDINGS_SCHEMA` adds `scope` per finding (`introduced` / `pre-existing`), `previous` and
`coverage_complete`.

**Posting.** New findings are posted with the prefix `**HIGH: title**` or `**HIGH (pre-existing): title**`; a new
finding is dropped as `duplicate of <id>` when an open finding has the same path and the same normalised title or the
same line and severity. A previous finding reported resolved gets a reply `Re-review at <sha12>: no longer present.
<note>` and its thread is resolved (GraphQL `resolveReviewThread`); one reported still open is only listed in the
summary under "Still open from earlier rounds"; the summary shows a rounds table and, at the cap, names the force
label. A bot reply never starts with "Fixed", "Rejected" or "Not changed": those are the retro's outcome words.

**Trigger hygiene.** A caller can keep runs away from files it never wants reviewed with `on.pull_request.paths-ignore`
(TUBS-IVS/eqasim-bs ignores its generated views, rebuilt by bot PRs that merge within seconds). Label events still run
the free `classify` job; suppressing them in the caller would copy the label names into it.

## The review run and the posting

- `review` fills the placeholders of the prompt file (`review.prompt_file`) and runs `claude -p` with the tier's
  `model`, its `effort` when the config sets one (`--effort`; otherwise the model's default: medium on Opus 5.5 and
  Sonnet 5.5, high on Fable 5.1, per the Claude Code docs "Model configuration"; the log line of the run names it),
  `max_turns` and `max_budget_usd` (a hard cap per run), the JSON schema `FINDINGS_SCHEMA`, `--bare`
  (authentication strictly by `ANTHROPIC_API_KEY`, never a Claude plan login; no user hooks, plugins, memory or
  automatic CLAUDE.md loading; the prompt names the rule files to read), `--permission-mode dontAsk` and only the read
  tools of `review.allowed_tools` (Read, Grep, Glob). The model has no shell, cannot write files and cannot post
  anything. The script builds the PR diff itself (`git diff base...head` in CI, which has no API size limit;
  `gh pr diff` without `--base-sha`), drops the file sections that match `ignored_paths` (logged by count and path; a
  PR with only ignored files makes no model call), and writes it to `.claude/review-pr.diff` inside the checkout. The
  model pages through a numbered copy (`.claude/review-pr.diff.numbered`, `number_diff`): every added and context line
  carries its new-file line number, which is the `line` the model must cite. A local test run (2026-10-23) showed that
  with `gh pr diff` as a tool the model piped it into grep/awk, which the read-only tool list denied, and reviewed only
  part of the diff; the first CI runs on the plain diff file cited line numbers of the diff file itself, so every
  finding landed off the diff. Git output is split on `\n` only (`git_lines`): `str.splitlines()` also splits on
  characters such as U+2028 that can occur inside a source line. The prompt goes in on stdin; the JSON result goes to
  a file.
- `post` turns the findings into one PR review (`event: COMMENT`) whose inline comments sit on new-side lines of the
  diff (`commentable_lines`, which follows the line counts of each hunk header, so an added line starting with `++ ` is
  content, not a file header), ordered by severity. GitHub rejects inline comments outside the diff, so such findings
  go into the summary under "Not on a changed line"; the inline/summary-only counts are logged, and a warning fires
  when every finding lies outside the diff. The summary comment carries a hidden marker; only a comment by
  `github-actions[bot]` with that marker is edited (`upsert_summary`). A forced and a push review can post at the same
  time, so after writing, the oldest summary keeps the current body and any duplicate is deleted. A run whose commit
  is no longer the PR head leaves an existing summary alone, so a slower review of an older commit cannot replace the
  newer one; its inline comments are still posted. A failed run (`is_error`, or no structured output) posts no findings
  and fails the step with the error text; when it cost money its round is still recorded (see the fallbacks above).
  The diff is read with `errors="replace"`: a non-UTF-8 byte (e.g. in a Latin-1 data file) must not crash the review.
  On a further round the model reads the numbered interdiff (`diff_source` `interdiff`, whole file sections at head
  line numbers) or no diff at all (`none`: only open findings to verify); the posting then follows "Rounds".
- `post --dry-run` prints both payloads and posts nothing, for local tests.

## Learning from the findings (session retro)

`post --record` writes `claude-review-findings.json`, uploaded per run as the artifact
`claude-review-findings-pr<NN>-run<id>-<attempt>` (90 days): every finding with severity, path, line, title, body,
placement and, for an inline comment, its GitHub comment id (`match_comment_ids`, keyed on path and body: the listing
of a review's comments has no `line` field, so the former key with the line left every record without an id until
2026-11-04). The maintainer's personal session retro (`~/.claude/retro`, `retro_reviews.py`, outside this
repository) lists the runs of each calling repository's `claude-review.yml` (the caller's file name, which is why it
must stay), downloads these records, reads each inline finding's thread and feeds the findings to its judge, which
matches them against the lessons it already knows. Only a finding the author **confirmed** counts towards a lesson;
two confirmed sightings on different PRs make the retro propose a rule (`.claude/rules/<topic>.md`) in that
repository. Since a prompt can make the review read every file in `.claude/rules/`, an accepted rule then guides both
the next author and the next review.

The outcome is read from the replies in the finding's review thread, so answer each one:

- **`Fixed ...`** (e.g. `Fixed in <sha>: <what changed>`) when the finding was right and is fixed: a confirmed
  defect, it counts;
- **`Rejected: <reason>`** (or `Not changed: ...`) when it is not a defect: it never counts;
- no such reply: `resolved` / `open`, recorded as tentative evidence only. A summary-only finding has no thread and
  stays `unknown`.

## Run record

After every review, `claude_review.py outcome` reads the `claude -p` JSON result and reports how the run ended: its
status, tier, model, turns and duration **as the CLI reported them**. It writes a table to the job summary and uploads
`claude-review-cost.json` as the artifact `claude-review-cost-pr<NN>-run<id>-<attempt>` (90 days; file and artifact
keep their v1 names, README.md "Contract with the session retro"). A missing record fails the step instead of
reporting a zero. A run that ended early (`subtype` other than `success`, e.g. `error_max_turns`, or `is_error`) is
marked as such with a workflow warning, and the error text of a failed run is printed: its review is incomplete.

**No output of a run shows its API cost** (the owner's decision of 2026-11-05). For a public repository the comments
with their state block, the log and job summary of a run and its artifacts are all public, so none of them carries the
cost or the token counts, which give the cost at the public prices: the summary comment has no cost column and gives
the turns only, the state block keeps `model_call` instead of the amount ("Rounds"), the run record has no cost or
token fields, and the findings record keeps its key `cost_usd` (schema 2 only adds fields), always empty. A run reads
the cost of its own result only to decide `model_call`. The cost of the reviews is in the Anthropic Console, under the
workspace of the calling repository's API key. The budget caps (`max_budget_usd`) stay in each caller's config: they
are limits, not spending. Outputs written before v1.3.0 keep their amounts: earlier revisions of summary comments, and
logs, job summaries and artifacts within their retention.

To compare tiers, collect the run records of several runs, e.g.
`gh run download <run-id> --repo <caller> -n claude-review-cost-pr<NN>-run<run-id>-1`, and set them against the cost
in the Console.

## Rules for maintainers

- A change here reaches every calling repository when the major tag moves (README.md, "Release"). Run the tests, and
  for a change of the workflow or of what the model sees, one review run from a test branch of a calling repository
  (README.md, "Test a change before release") before moving the tag.
- The contract with the session retro is fixed: the artifact names, the findings record (`schema` 2, fields only ever
  added, never renamed or removed), the state marker, and the outcome words. A bot reply never starts with "Fixed",
  "Rejected" or "Not changed"; the verification reply starts with `Re-review at <sha12>:`. The state block never
  contains `-->`.
- Thresholds, paths, labels, models, limits and tools belong in each calling repository's config, never in code or in
  the workflow; the label names must not appear in either workflow (a test checks this).
- A new config key needs `CONFIG_KEYS` (or `check-config` rejects it as unknown), a default that keeps old configs
  valid, a test, and a line in CHANGELOG.md; a key that old configs must add is a new major version.
- Keep the review advisory until its findings have been checked against real PRs; making it a required check is a
  separate decision of each repository.
- Bump the pinned CLI version (`CLAUDE_CODE_VERSION` in the workflow) deliberately.
- Keep the model read-only: posting belongs in `post`, not in the model's tools.
- Never write the API cost or a token count into an output of a run (comment, state block, log, job summary,
  artifact; "Run record"): every one is public for a public repository. A test feeds a result with a cost through
  `outcome` and `post` and fails on any amount it finds.
