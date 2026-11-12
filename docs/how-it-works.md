# How the Claude PR review works

The reusable workflow [`.github/workflows/review.yml`](../.github/workflows/review.yml) runs an advisory code
review by Claude on pull requests of the repository that calls it. It posts one PR review with inline comments and
keeps one summary comment per PR up to date. It is **not** a required check: it never blocks a merge and never
replaces the calling repository's own tests. How a repository adopts it, and how versions work:
[README.md](../README.md). Why it is one central repository: ADR-0176 in TUBS-IVS/eqasim-bs. Numbers such as
"PR #497" below refer to TUBS-IVS/eqasim-bs, where the review began.

Prerequisites: the calling repository's secret `ANTHROPIC_API_KEY`, a key of a **workspace** of the Console
organization (an organization-level key fails with "not scoped to a workspace"), and the organization secret
`CLAUDE_REVIEW_BUDGETS` with each tier's hard budget per run ("The gate", below). Comments are posted with the
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
| `classify` | no | `check-config` (see below), `state` and `interdiff` (see "Rounds"), then `classify`: reads `git diff --numstat base...head` and `--name-status -M base...head`, the branch kind, labels, draft state, the state and the interdiff, and decides `none` / `light` / `full` / `deep` plus the outputs `mode`, `diff_source`, `round`, `effort` |
| `review` | yes, only if `classify` says so | installs the pinned Claude Code CLI, recomputes `state` and `interdiff` (no artifact travels between the jobs), `review` (runs `claude -p`), `outcome`, `post` |

**`check-config` comes first.** It validates the calling repository's config and the prompt it names
(`config_problems`) and fails the gate job, before anything is spent, with one `::error::` line per problem: a key the
code does not know (a misspelt key would otherwise silently disable its rule), a missing section or key, a tier of
`TIER_NAMES` that is missing or has no model, a turn limit below 1, a tier without a budget, a tool beyond
`READ_ONLY_TOOLS`, a `verify_tier` that is no tier, equal force and skip labels, or a prompt with an unknown
placeholder, without `{{DIFF_BLOCK}}` or `{{ROUND_BLOCK}}`, or with `{{PR_BODY}}` outside two `{{FENCE}}` lines. Fork
and Dependabot PRs are skipped before it.

**The budgets** (each tier's hard cap per run, `claude -p --max-budget-usd`) come from the organization secret
`CLAUDE_REVIEW_BUDGETS` (`BUDGETS_ENV`), which the caller passes: a JSON object with a finite number above 0 for every
tier, `{"light": <usd>, "full": <usd>, "deep": <usd>}`. They are the owner's and are not published (the owner's
decision of 2026-11-05; ADR-0181 in TUBS-IVS/eqasim-bs), so no config holds them, no output names them, and the
gate and the review step are the only steps that get the secret; the model's own process does not. `check-config`
fails on a secret that is not such an object, lacks a tier or holds anything but a positive number for one, and on a
`max_budget_usd` left in the config while the secret is set; its messages never quote a value. A caller that does
not pass the secret yet still runs on the deprecated `max_budget_usd` of its config (above 0), with a
`::warning::` on every run; a tier with neither is a failure, so no run is unbounded. On GitHub Free an organization
secret does not reach a private repository, which then holds a repository secret of the same name (README.md, "Adopt
it in a repository").

The thresholds, paths, labels, models and turn limits live in the calling repository's `.github/claude-review.yml`,
not in code. Re-tune them from the run records below and the cost in the Anthropic Console ("Run record"). Decision
order (`classify`; the
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
   >= `deep_min_lines` changed lines or >= `deep_min_files` changed files outside `supporting_paths` (below).
7. Full tier: a critical path (every structure path is also critical), or >= `full_min_lines` lines, or
   >= `full_min_files` files.
8. Light tier: any other counted change.
9. Nothing counted changed (only ignored files) -> no review.

The three tiers and why the deep one exists: ADR-0175 in TUBS-IVS/eqasim-bs. `structure_paths` names the backbone of
a repository; keep it a short list, so that routine additions stay full-tier changes.

`supporting_paths` (optional) names what supports a change rather than makes it: documentation and tests. Their
changed lines and files do not count towards `deep_min_lines` and `deep_min_files`, so a long ADR or test file next to
a short code change is no deep review. They are still read, still count towards the full tier's thresholds, and the
structure rules count a path whatever else it matches. The gate's reason names their share (`... changed lines in N
files, X of them in Y supporting files`). Without the key every line counts alike, as before v1.5.0.

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
marked `stale`. A stale round still read its whole commit, so it can be the reviewed commit the next round starts from
(`last`, below). Threads of resolved findings are closed only after the state is written. `model_call` says whether
the round called the model (the API reported a cost above zero); the block is
part of a public comment, so it never holds the amount ("Run record"). A block written before v1.3.0 carries
`cost_usd` per round instead: `decode_state` turns it into `model_call` and drops the amount, so the round cap of an
open PR still holds and the next write of the comment no longer shows it.

**Commands, both model-free.** `state --repo --pr --out` reads the comment and the threads of open findings and writes
`state.json`. `interdiff --config --state --base-sha --head-sha --out-dir` compares `git diff base...last` and
`git diff base...head` per file section (`split_sections`; `index` lines and hunk numbers are normalised away, so a
merge of the base branch shifts nothing) and writes `interdiff.diff`, `.numstat` and `.json` (changed, unchanged and
reverted paths; a renamed file is changed; `delta`, the lines the push itself changed, see "The review run and the
posting"). `classify` takes `--state`, `--interdiff-json`, `--interdiff-numstat`.

**The reviewed commit.** `last` is, of the rounds with `complete: true`, the one whose commit is the newest ancestor
of head, by the number of commits between them (`reviewed_commit`, `git rev-list --count`). The history decides,
neither the order in which the rounds posted nor `stale`. A push that arrives while a review still runs makes that
round stale; before v1.5.0 a stale round was never `last`, so the next run found no reviewed commit and read the whole
PR again on the tier of the first round. A slow run of an older commit that posts after a newer round is further from
head and so never `last`.

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
of ignored files alone gets no run, like a merge of the base branch, since it cannot fix a finding. A push of at most
`rereview.small_push_lines` changed lines (optional; without the key this rule is off) and without a structure change
is only checked: it runs on `rereview.verify_tier` instead of the tier the thresholds and paths give it, and never on
a higher tier than they give. Nearly all code of a repository is a critical path, so without this rule a fix of a few
lines is a full review. A structure change
of a further round is a file added, deleted or renamed against the base at head that was not so at the last complete
round (`interdiff` writes these as `events` in `interdiff.json`), so a file added in round 1 does not make every later
round deep. The `skip_kinds` rule applies as in the first round.

**The force label** reads the whole PR again: a deep-tier round over the complete PR diff (`mode` `forced`,
`diff_source` `pr`), whatever the cap and the interdiff, recorded with `automatic: false` and not counted against the
cap. It still gets the open findings.

**The effort of a run** is the gate's decision too (output `effort`, empty for the model's default), handed to the
review job: the `effort` of the run's tier, or, for a check, `rereview.verify_effort` (optional). A check is every
verify round and every small push; a first round, a forced round and a larger push run at the effort of their tier.

**Fallbacks, all logged with `[claude-review]`, in the job summary and the summary comment.**
- Summary without a state block (or a corrupt one): `fallback: no state block in the existing summary; first-round
  review, earlier findings unknown`; the state starts empty.
- No complete round's commit is an ancestor of head (rebase, force-push): `fallback: reviewed commit <sha12> is not an
  ancestor of head (rebase or force-push?); the whole PR is reviewed`; the round counter and the open findings are
  kept, the cap still applies, and the prompt tells the model that the line numbers of the open findings may have
  moved.
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

**The run.** `review` also gets `--state`, `--interdiff-dir`, `--diff-source`, `--mode`, `--round`, `--effort` and
`GH_TOKEN`: it
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
  `model` (a full id, or an alias such as `fable` that the pinned CLI resolves to the newest version it knows,
  Claude Code docs "Model configuration"; the run record names the version called, "Run record"), the effort the
  gate decided (`--effort`: the tier's `effort` or, for a check, `rereview.verify_effort`, see "Rounds"; without any,
  the model's default, per the same docs page; the log line names it),
  `max_turns`, the tier's budget (`tier_budget`: from the secret, "The gate"; a hard cap per run, never printed: the
  log line names only its source), the JSON schema `FINDINGS_SCHEMA`, `--bare`
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
- On a further round (`diff_source` `interdiff`) the model reads the push, not the whole section of every file the
  push touched (a file that is new in the PR is one hunk, so a fix of a few lines meant reading, and reporting on,
  hundreds of reviewed lines). `interdiff` records the lines the push itself changed (`delta` in `interdiff.json`:
  `changed_since` of `git diff -U0 last head` for the changed files), and `number_delta` cuts the numbered diff down
  to them: a `>` in front marks every line that is new or changed since `last`, `DELTA_CONTEXT` lines of its hunk
  stay on each side, a hunk without such a line is left out, a longer unchanged run becomes one `... N lines left
  out ...` line, and a `~` line says where the push removed lines that leave no trace in the PR's diff. Only an added
  line of the PR's diff is marked, so a change of the base branch that a merge brought in is not. A file of which git
  names no changed line (binary, mode change) is shown whole. The prompt paragraph `{{DIFF_BLOCK}}` explains the
  marks. The raw diff that `post` places findings on stays the whole sections (`interdiff.diff`), so a finding on a
  context line is still an inline comment. With `diff_source` `none` there is no diff at all (only open findings to
  verify). The posting of a further round follows "Rounds".
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
status, tier, model, turns and duration **as the CLI reported them**, and the effort the gate decided for it. For
the model it gives the configured name
and, when that is an alias, the ids the CLI called (`models_used`, the keys of the result's `modelUsage`), e.g.
`fable (claude-fable-5-2)`. It writes a table to the job summary and uploads
`claude-review-cost.json` as the artifact `claude-review-cost-pr<NN>-run<id>-<attempt>` (90 days; file and artifact
keep their v1 names, README.md "Contract with the session retro"). A missing record fails the step instead of
reporting a zero. A run that ended early (`subtype` other than `success`, e.g. `error_max_turns`, or `is_error`) is
marked as such with a workflow warning, and the error text of a failed run is printed: its review is incomplete.

**No output of a run shows its API cost** (the owner's decision of 2026-11-05). For a public repository the comments
with their state block, the log and job summary of a run and its artifacts are all public, so none of them carries the
cost or the token counts, which give the cost at the public prices: the summary comment has no cost column and gives
the turns only, the state block keeps `model_call` instead of the amount ("Rounds"), the run record has no cost or
token fields, and the findings record keeps its key `cost_usd` (schema 2 only adds fields), always empty. A run reads
the cost of its own result only to decide `model_call`. The budgets per run are not published either (they come from
a secret, "The gate"), and a text of the CLI that the review publishes (the error of a failed run, the CLI's own
warnings in the log) has every money amount replaced by `[amount]` (`redact_amounts`): a run cut off by its budget
may name it. The cost of the reviews is in the Anthropic Console, under the workspace of the calling repository's
API key. Outputs written before v1.3.0 keep their amounts: earlier revisions of summary comments, and logs, job
summaries and artifacts within their retention.

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
- Thresholds, paths, labels, models, turn limits and tools belong in each calling repository's config, never in code
  or in the workflow; the label names must not appear in either workflow (a test checks this). The budgets belong in
  the organization secret only, never in a config, the code, the docs or an output.
- A new config key needs `CONFIG_KEYS` (or `check-config` rejects it as unknown), a default that keeps old configs
  valid, a test, and a line in CHANGELOG.md; a key that old configs must add is a new major version.
- Keep the review advisory until its findings have been checked against real PRs; making it a required check is a
  separate decision of each repository.
- The pinned CLI version (`CLAUDE_CODE_VERSION` in review.yml) changes only through a pull request. Every Monday
  `bump-claude-code.yml` compares it with the newest npm release and, when that is newer, runs the tests and opens
  one (`bump/claude-code-<version>`). A pull request of the GITHUB_TOKEN starts no other workflow: start Tests by
  hand on it, and test it from a calling repository like any change of the workflow, because the pin also decides
  what a model alias resolves to. The job needs the repository setting "Allow GitHub Actions to create and approve
  pull requests".
- Keep the model read-only: posting belongs in `post`, not in the model's tools.
- Never write the API cost, a token count or a budget into an output of a run (comment, state block, log, job
  summary, artifact; "Run record"): every one is public for a public repository. Tests feed a result with a cost and
  budgets through `check-config`, `review`, `outcome` and `post` and fail on any amount they find.
