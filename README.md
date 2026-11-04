# claude-pr-review

An advisory code review by Claude on pull requests, shared by the repositories of TUBS-IVS. A model-free gate
decides from the size and kind of a change whether a review runs and with which model; the review reads the diff with
read-only tools, posts its findings as inline comments and one summary comment, works in rounds (re-reviews only what
changed, verifies fixes, stops after a cap) and records its API cost. It never blocks a merge.

One repository, so that a fix is made once: the review ran as a copied script in six repositories before
(ADR-0176 in [TUBS-IVS/eqasim-bs](https://github.com/TUBS-IVS/eqasim-bs)). Used by TUBS-IVS/eqasim-bs,
TUBS-IVS/eqasim-java-bs and four private repositories of the group.

How it works, in detail: [docs/how-it-works.md](docs/how-it-works.md). Changes per version: [CHANGELOG.md](CHANGELOG.md).

## What lives where

| Here (shared) | In each calling repository (its own) |
|---|---|
| `claude_review.py`: gate, review run, posting, cost and findings records | `.github/workflows/claude-review.yml`: the thin caller (triggers, queue, permissions) |
| `.github/workflows/review.yml`: the reusable workflow with the two jobs | `.github/claude-review.yml`: the gate config (paths, thresholds, tiers, labels) |
| `tests/`: the tests of the script and of both workflows | `.github/claude-review-prompt.md`: the prompt (what to look for, which rules to read) |
| `examples/claude-review.yml`: the caller template | `.claude/rules/`: project rules the prompt tells the review to read |

## Adopt it in a repository

1. Add the repository secret `ANTHROPIC_API_KEY`: a key of a **workspace** of the Console organization (an
   organization-level key fails). Only this secret is handed to the shared workflow.
2. Copy [`examples/claude-review.yml`](examples/claude-review.yml) to `.github/workflows/claude-review.yml`. Keep that
   file name (the maintainer's session retro lists review runs by it). Adjust only the triggers: the base branch
   (`branches: [main]`) and, if needed, `paths-ignore` for files no review should ever start for.
3. Add the gate config `.github/claude-review.yml`. Start from a sibling repository's or from
   [`tests/fixtures/caller/.github/claude-review.yml`](tests/fixtures/caller/.github/claude-review.yml) and set its
   paths for your repository. Keys: `labels` (`force`, `skip`), `skip_drafts`, `critical_paths`, `structure_paths`,
   `ignored_paths`, `skip_kinds`, `thresholds`, `rereview` (`max_rounds`, `verify_tier`), `tiers` (`light`, `full`,
   `deep`, each with `model`, `max_turns`, `max_budget_usd` of at most 50 USD and an optional `effort`: `low`,
   `medium`, `high`, `xhigh` or `max`; without it the model's default effort applies) and `review` (`prompt_file`,
   `allowed_tools` from Read, Grep, Glob). Make the caller, the config and the prompt critical paths.
4. Add the prompt named by `review.prompt_file`. Placeholders: `{{REPO}}`, `{{PR}}`, `{{TIER}}`, `{{SHA}}`,
   `{{PR_TITLE}}`, `{{DIFF_BLOCK}}` and `{{ROUND_BLOCK}}` (both required), and `{{PR_BODY}}` between two `{{FENCE}}`
   lines (so the author's text stays data).
5. Create the two labels named in the config (`claude-review` adds one deep review, `skip-claude-review` stops it).

The gate job checks the config and the prompt on every pull request (`check-config`) and fails, before anything is
spent, with one error line per problem. Locally: `python claude_review.py check-config --config
.github/claude-review.yml`, run from the root of the calling repository.

## Versions

Releases are tags `vMAJOR.MINOR.PATCH`; the major tag `v1` points to the newest `v1.x.y`. Callers name
`@v1`, so a fix reaches every calling repository at its next pull-request event without a change there. The
workflow fetches the script at its own commit, so a caller always runs workflow and script of one version; it can pin
an exact release (`@v1.0.0`) instead. A change that breaks the config keys, the caller's interface, the state block
that later rounds of an open PR read, the findings record or the artifact names is a new major version, which each
repository adopts deliberately; it never moves `v1`. Every run names the exact commit of the script it ran in its log
and job summary ("Review tool"), so the version behind any review stays traceable.

### Release (maintainers)

1. Change, with tests, in a pull request; CI (`.github/workflows/tests.yml`) must be green.
2. For a change of the workflow or of what the model sees, first [test it from a calling repository](#test-a-change-before-release).
3. After the merge (with its CHANGELOG.md entry): tag the merge commit (`git tag -a vX.Y.Z -m vX.Y.Z <commit>`),
   move the major tag to the same commit (`git tag -f v1 <commit>`) and push both (`git push origin vX.Y.Z` and
   `git push -f origin v1`). Only administrators should create, move or delete `v*` tags (a repository ruleset):
   whoever moves `v1` changes code that runs with the API key in every calling repository.

### Test a change before release

In a calling repository, on a test branch, point the caller's `uses:` at the branch of this repository
(`...review.yml@my-branch`). The workflow then fetches the script of that branch's commit, so the pull request of the
test branch runs the new version end to end. Close that pull request without merging.

## Contract with the session retro

The maintainer's session retro (outside this repository) learns from confirmed findings. It relies on: the caller's
file name `claude-review.yml`, the artifact names `claude-review-findings-pr<NN>-run<id>-<attempt>` and
`claude-review-cost-pr<NN>-run<id>-<attempt>`, the findings record (`schema: 2`, fields only added), the state block
`<!-- claude-pr-review:state ... -->` of the summary comment, and the reply words: answer each finding's thread with
`Fixed ...` or `Rejected: ...` (docs/how-it-works.md, "Learning from the findings").

## Development

```bash
pip install pytest pyyaml
python -m pytest tests
```

CI runs the tests on Linux and Windows with Python 3.10, the version the workflow runs the script with. The tests use
a fixture caller (`tests/fixtures/caller/`): the gate config and prompt of TUBS-IVS/eqasim-bs as of the import.

## History and license

The script and its tests were imported from TUBS-IVS/eqasim-bs (`scripts/claude_review.py`,
`tests/test_claude_review.py`) at its commit 5d08a5f7, followed by the comment-id fix; the first commits of this
repository show both steps, and the earlier history is in that repository. PR, issue and ADR numbers in comments refer
to TUBS-IVS/eqasim-bs. License: GNU GPL v2 ([LICENSE](LICENSE)), as for TUBS-IVS/eqasim-bs, where the script began.
