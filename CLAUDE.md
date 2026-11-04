# CLAUDE.md

The shared Claude PR review of TUBS-IVS: `claude_review.py`, the reusable workflow `.github/workflows/review.yml` and
their tests. README.md says what lives here and how repositories adopt it; docs/how-it-works.md is the one home of
how the mechanism works, including the rules for maintainers at its end. Read both before changing anything.

- **Every change reaches every calling repository** once the major tag moves. Change with tests (`python -m pytest
  tests`), in a pull request with green CI; for a change of the workflow or of what the model sees, first run it from a
  test branch of a calling repository (README.md, "Test a change before release").
- **The contract with the maintainer's session retro is fixed** (README.md): caller file name, artifact names,
  findings record `schema: 2` (fields only added), state marker, reply words.
- **This repository is public.** Never name the private calling repositories in code, docs, commits, issues or PRs
  here; say "private repositories of the group".
- **Never push, tag or move a tag without the owner's explicit yes**, asked each time.
- All code, comments, docs and commit messages in English; chat answers to the owner in German.
