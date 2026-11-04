"""Gate, review run, posting and cost record of the automatic Claude PR review
(.github/workflows/claude-review.yml).

    python scripts/claude_review.py classify --config .github/claude-review.yml --numstat diff.tsv \
        [--name-status status.tsv] --branch feature/i1-x [--labels a,b] [--draft] \
        [--event-action labeled --event-label x]
    python scripts/claude_review.py review --config .github/claude-review.yml --tier full \
        --repo OWNER/NAME --pr 12 --sha <head sha> --out result.json [--diff-out .claude/review-pr.diff] \
        [--state state.json --interdiff-dir DIR --diff-source pr|interdiff|none --mode M --round N]
    python scripts/claude_review.py post --result result.json --repo OWNER/NAME --pr 12 \
        --tier full --sha <head sha> [--diff-file pr.diff] [--dry-run]
    python scripts/claude_review.py cost --execution-file result.json [--tier full] [--model m] [--json out.json]

classify decides WITHOUT calling a model whether the review runs and at which tier
("light", "full" or "deep"), from the `git diff --numstat` and `--name-status` of the PR (how
much changed, and whether files of the structure paths were added, deleted or renamed), its
branch kind, labels and draft state, against the thresholds in the config. It prints the decision with its reason
and, under GitHub Actions, writes run/tier/model/max_turns/reason to $GITHUB_OUTPUT and a
line to $GITHUB_STEP_SUMMARY, so a skipped review is never silent.

review runs `claude -p` (Claude Code in print mode) with the API key in ANTHROPIC_API_KEY,
the tier's model, turn and budget limits, read-only tools and a JSON schema for the findings,
and writes the CLI's JSON result to --out. It hands the model the PR diff with the new-file
line number in front of every line, so the model can cite lines GitHub accepts. Why it runs
`claude -p` and not anthropics/claude-code-action: see the note below. The model only reads;
posting is done here.

post turns the findings into one PR review with inline comments on changed lines, and keeps
one summary comment per PR up to date (edited in place). A finding whose line is not part of
the diff cannot be an inline comment; it is listed in the summary instead, and the
inline/summary-only counts are logged. --dry-run prints the payloads and posts nothing.

cost reads the result record (the `claude -p` JSON result, or an Agent SDK message list) and
reports the API cost in USD, the token counts, turns and duration as the CLI recorded them.
A missing or unparsable record is an error (exit 1), not a silent zero.

See docs/codebase/notes/claude-pr-review.md.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

SUMMARY_MARKER = "<!-- claude-pr-review:summary -->"
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# The review history lives in a hidden block at the end of the bot's summary comment: one place
# that survives re-runs and needs no artifact or database (ADR-0171). The JSON never contains the
# comment end "-->" (encode_state escapes ">"), so the block cannot close the comment early.
STATE_MARKER = "<!-- claude-pr-review:state "
STATE_END = " -->"
# Outcome words of an author's reply, read the way the session retro reads them
# (~/.claude/retro/bin/retro_reviews.py): whitespace collapsed, casefolded, "*_ " stripped.
REJECTED_PREFIXES = ("rejected", "not changed")
# Our own verification replies; they must never be taken for the author's answer.
BOT_REPLY_PREFIX = "re-review at"
# The state is kept small: a finding that is no longer open keeps no text.
TITLE_CHARS = 200
BODY_CHARS = 600
NOTE_CHARS = 300

# Structured output the model must return (claude -p --json-schema).
FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "two or three sentences on the change and the review"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "path": {"type": "string", "description": "repository-relative path of the changed file"},
                    "line": {"type": "integer", "description": "line number in the NEW version of the file"},
                    "title": {"type": "string", "description": "the defect in one short sentence"},
                    "body": {"type": "string", "description": "concrete failure scenario and, if obvious, the fix"},
                    "scope": {"type": "string", "enum": ["introduced", "pre-existing"],
                              "description": "introduced by this PR, or a defect that was there before it"},
                },
                "required": ["severity", "path", "line", "title", "body", "scope"],
            },
        },
        "previous": {
            "type": "array",
            "description": "one entry per open finding of an earlier round (empty on a first round)",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "resolved": {"type": "boolean"},
                    "note": {"type": "string", "description": "one line of evidence"},
                },
                "required": ["id", "resolved", "note"],
            },
        },
        "coverage_complete": {"type": "boolean", "description": "true only if the whole diff handed in was read"},
    },
    "required": ["summary", "findings", "previous", "coverage_complete"],
}


def empty_state() -> dict:
    return {"schema": 1, "rounds": [], "findings": []}


def encode_state(state: dict) -> str:
    """The hidden block for the summary comment. ">" is written as the JSON escape \\u003e so the
    block never contains "-->" (a finding title may), which would end the HTML comment early."""
    return STATE_MARKER + json.dumps(state, separators=(",", ":")).replace(">", "\\u003e") + STATE_END


def _state_span(body: str):
    start = body.find(STATE_MARKER)
    if start < 0:
        return None
    end = body.find(STATE_END, start + len(STATE_MARKER))
    return None if end < 0 else (start, end + len(STATE_END))


def decode_state(body: str) -> dict | None:
    """The state of a summary comment, or None for no block or a block that is not valid state
    (hand-edited): the caller logs the fallback instead of crashing the gate."""
    span = _state_span(body)
    if span is None:
        return None
    try:
        state = json.loads(body[span[0] + len(STATE_MARKER):span[1] - len(STATE_END)])
    except json.JSONDecodeError:
        return None
    if (not isinstance(state, dict) or not isinstance(state.get("rounds"), list)
            or not isinstance(state.get("findings"), list)):
        return None
    return state


def strip_state(body: str) -> str:
    """The visible comment without the state block."""
    span = _state_span(body)
    return body if span is None else body[:span[0]] + body[span[1]:]


def last_complete_sha(state: dict) -> str:
    """Commit of the newest round that reviewed everything it set out to (else ""). A stale round
    (a slow run of a commit that was no longer the head when it posted) is skipped: it is newer
    in the list but older in the history, and the next interdiff would re-review files again."""
    for round_ in reversed(state["rounds"]):
        if round_.get("complete") and not round_.get("stale"):
            return str(round_.get("sha", ""))
    return ""


def automatic_rounds(state: dict) -> int:
    """Rounds that count against rereview.max_rounds: every automatic round that called the model
    (cost above zero), complete or not. A forced round and a round without a model call do not use
    one up. ``complete`` only decides whether the reviewed commit moves (last_complete_sha):
    counting complete rounds only would let a PR the model never reads in full be reviewed on
    every push without limit (Claude review finding on PR #521)."""
    return sum(1 for r in state["rounds"] if r.get("automatic") and float(r.get("cost_usd") or 0) > 0)


def open_findings(state: dict) -> list:
    return [f for f in state["findings"] if f.get("status") == "open"]


def _outcome_text(text: str) -> str:
    return " ".join(str(text).split()).casefold().lstrip("*_ ")


def refresh_state(state: dict, threads: dict) -> dict:
    """Bring the open findings up to date with what happened in their threads since the last round.

    Only findings that are still "open" change: a reply that starts with a rejection outcome word,
    or a thread resolved by a human, makes the finding "dismissed" (the author closed it, so it is
    not verified again); otherwise it stays open and the author's last reply is kept as a note for
    the model. A finding the bot itself resolved stays "resolved", although GitHub shows its
    thread as resolved too."""
    out = copy.deepcopy(state)
    for finding in out["findings"]:
        thread = threads.get(finding.get("comment_id"))
        if finding.get("status") != "open" or thread is None:
            continue
        author_replies = [r for r in thread.get("replies", []) if not _outcome_text(r).startswith(BOT_REPLY_PREFIX)]
        if any(_outcome_text(r).startswith(REJECTED_PREFIXES) for r in author_replies) or thread.get("resolved"):
            finding["status"] = "dismissed"
        elif author_replies:
            finding["author_note"] = " ".join(author_replies[-1].split())[:NOTE_CHARS]
    return out


def merge_state(existing: dict, delta: dict) -> dict:
    """Merge the state of one round into the stored one: rounds keyed by (commit, run id), findings
    by id, the delta wins. A retry of the same run therefore replaces its round instead of adding one.
    Findings that are not open keep no body or note, so the comment stays small."""
    merged = copy.deepcopy(existing)
    for key, identify in (("rounds", lambda r: (r.get("sha"), r.get("run_id"))),
                          ("findings", lambda f: f.get("id"))):
        position = {identify(item): index for index, item in enumerate(merged[key])}
        for item in delta[key]:
            if identify(item) in position:
                merged[key][position[identify(item)]] = copy.deepcopy(item)
            else:
                position[identify(item)] = len(merged[key])
                merged[key].append(copy.deepcopy(item))
    for finding in merged["findings"]:
        if finding.get("status") != "open":
            finding.pop("body", None)
            finding.pop("author_note", None)
    return merged


@dataclass
class Decision:
    run: bool
    tier: str  # "light", "full", "deep" or "none"
    model: str
    max_turns: int
    reason: str
    changed_lines: int
    changed_files: int
    critical_files: list
    mode: str = "first"  # "first", "rereview", "verify", "forced" or "none"
    diff_source: str = "pr"  # what the model reads: "pr" (whole PR), "interdiff" or "none"
    round: int = 1


@dataclass
class RoundContext:
    """What a further round is gated on (from `state` and `interdiff`)."""
    automatic_rounds: int
    open_findings: int
    last_sha: str  # last complete round; "" on a PR the review has not seen
    changed_entries: list | None  # numstat entries since last_sha; None without a usable interdiff
    fallback: str = ""  # why the interdiff could not be used (then the whole PR is reviewed)
    round: int = 1
    reverted: tuple = ()  # files the PR changed at last_sha and no longer changes (reverted to main)
    events: tuple = ()  # (status, path, ...) of files added, deleted or renamed since last_sha


def round_context(state: dict, meta: dict, changed_entries: list) -> RoundContext:
    usable = bool(meta.get("last_sha")) and not meta.get("fallback")
    return RoundContext(automatic_rounds(state), len(open_findings(state)), str(meta.get("last_sha", "")),
                        changed_entries if usable else None, str(meta.get("fallback", "")),
                        len(state["rounds"]) + 1, tuple(meta.get("reverted", [])),
                        tuple(tuple(event) for event in meta.get("events", [])))


def glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a path glob to a regex: "**" crosses directories, "*" and "?" do not."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path: str, patterns) -> bool:
    return any(glob_to_regex(pattern).match(path) for pattern in patterns)


def git_lines(text: str, keepends: bool = False) -> list:
    """Split git output on "\\n" only. str.splitlines() also splits on \\x0b, \\x0c, \\x1c-\\x1e,
    \\x85 and \\u2028/\\u2029, which can occur INSIDE a source line, and would put the hunk line
    counts out of step (Claude review finding on PR #497)."""
    lines = re.split(r"(?<=\n)", text) if keepends else text.split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def parse_numstat(text: str) -> list:
    """Parse `git diff --numstat` lines into (path, changed_lines); binary files count 0 lines."""
    entries = []
    for line in git_lines(text):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            raise ValueError(f"not a numstat line: {line!r}")
        added, deleted, path = parts[0], parts[1], parts[-1]
        # A rename is reported as "old => new" or "dir/{old => new}/file"; the new path counts.
        if " => " in path:
            path = re.sub(r"\{[^{}]* => ([^{}]*)\}", r"\1", path)
            path = path.split(" => ")[-1].replace("//", "/")
        lines = 0 if added == "-" else int(added) + int(deleted)
        entries.append((path, lines))
    return entries


def parse_name_status(text: str) -> list:
    """Parse `git diff --name-status` lines into (status, path, ...) for the files that were added,
    deleted, renamed or copied ("A", "D", "R", "C"; a rename or copy carries the old and the new
    path). Modified files and type changes are not structure changes and are left out."""
    events = []
    for line in git_lines(text):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0][:1].isalpha():
            raise ValueError(f"not a name-status line: {line!r}")
        status = parts[0][0]
        if status in "ADRC":
            events.append((status, *parts[1:]))
    return events


STATUS_WORDS = {"A": "added", "D": "deleted", "R": "renamed", "C": "copied"}


def _measure(config: dict, entries: list) -> tuple:
    """(changed lines, changed files, critical paths, printable size) of numstat entries outside
    the ignored paths."""
    counted = [(path, lines) for path, lines in entries if not matches_any(path, config.get("ignored_paths", []))]
    changed_lines = sum(lines for _, lines in counted)
    # A structure path is always critical too (the structure paths are the backbone of the critical ones).
    critical_patterns = [*config.get("critical_paths", []), *config.get("structure_paths", [])]
    critical = sorted(path for path, _ in counted if matches_any(path, critical_patterns))
    size = f"{changed_lines} changed lines in {len(counted)} files"
    if critical:
        size += f", critical: {', '.join(critical[:5])}" + (f" (+{len(critical) - 5} more)" if len(critical) > 5 else "")
    return changed_lines, len(counted), critical, size


def _structure(config: dict, entries: list, events) -> tuple:
    """(changed structure files, their changed lines, structure changes) of numstat entries and
    name-status events outside the ignored paths. A structure change is a file of the structure
    paths that was added, deleted, renamed or copied; for a rename either path counts."""
    ignored, structure = config.get("ignored_paths", []), config.get("structure_paths", [])
    touched = [(path, lines) for path, lines in entries
               if matches_any(path, structure) and not matches_any(path, ignored)]
    changes = [f"{STATUS_WORDS[event[0]]} {event[-1]}" for event in events
               if any(matches_any(path, structure) and not matches_any(path, ignored) for path in event[1:])]
    return len(touched), sum(lines for _, lines in touched), changes


def _size_tier(config: dict, measured: tuple, structure: tuple, kind: str) -> tuple:
    """(tier, why) from the size and structure rules shared by a first and a further round."""
    changed_lines, changed_files, critical = measured[:3]
    structure_files, structure_lines, structure_changes = structure
    limits = config["thresholds"]
    if kind in config.get("skip_kinds", []) and not critical:
        return "none", f"skipped: branch kind '{kind}/' touches no critical path"
    if structure_changes:
        shown = ", ".join(structure_changes[:3]) + (f" (+{len(structure_changes) - 3} more)"
                                                     if len(structure_changes) > 3 else "")
        return "deep", f"deep: structure changed: {shown}"
    if structure_files >= limits["deep_min_structure_files"]:
        return "deep", f"deep: >= {limits['deep_min_structure_files']} structure files changed"
    if structure_lines >= limits["deep_min_structure_lines"]:
        return "deep", f"deep: >= {limits['deep_min_structure_lines']} changed lines in structure files"
    if changed_lines >= limits["deep_min_lines"]:
        return "deep", f"deep: >= {limits['deep_min_lines']} changed lines"
    if changed_files >= limits["deep_min_files"]:
        return "deep", f"deep: >= {limits['deep_min_files']} changed files"
    if critical:
        return "full", "full: critical path touched"
    if changed_lines >= limits["full_min_lines"]:
        return "full", f"full: >= {limits['full_min_lines']} changed lines"
    if changed_files >= limits["full_min_files"]:
        return "full", f"full: >= {limits['full_min_files']} changed files"
    if changed_files:
        return "light", "light: a small change"
    return "none", "skipped: nothing counted changed (only ignored files)"


def classify(config: dict, entries: list, branch: str, labels: list, draft: bool,
             event_action: str = "", event_label: str = "", context: RoundContext | None = None,
             events=()) -> Decision:
    """Decide whether and how the review runs. Without a usable ``context`` (no earlier complete
    round, or the interdiff fell back) the first-round rules apply to the whole PR (``entries``
    and its name-status ``events``). With one, a push is measured by ``context.changed_entries``
    and ``context.events`` only: the files whose change against main differs from the last
    complete round, and the files added, deleted or renamed since then."""
    label_cfg = config["labels"]
    tiers = config["tiers"]
    rereview = config["rereview"]
    kind = branch.split("/", 1)[0] if "/" in branch else ""
    round_number = context.round if context else 1
    further = (context is not None and bool(context.last_sha) and not context.fallback
               and context.changed_entries is not None)
    measured = _measure(config, context.changed_entries if further else entries)
    structure = _structure(config, context.changed_entries if further else entries,
                           context.events if further else events)
    prefix = f"fallback: {context.fallback}; " if context and context.fallback else ""
    if further:
        prefix += (f"round {round_number}, {context.automatic_rounds}/{rereview['max_rounds']} automatic rounds used, "
                   f"{context.open_findings} open finding(s), since {context.last_sha[:12]}: ")

    def decide(tier: str, why: str, mode: str, diff_source: str, size: str = measured[3]) -> Decision:
        run = tier != "none"
        return Decision(run, tier, tiers[tier]["model"] if run else "", int(tiers[tier]["max_turns"]) if run else 0,
                        f"{prefix}{why} ({size})", measured[0], measured[1], measured[2],
                        mode if run else "none", diff_source if run else "none", round_number)

    if label_cfg["skip"] in labels:
        return decide("none", f"skipped: label '{label_cfg['skip']}' is set", "none", "none")
    if event_action == "labeled":
        # Any label change re-triggers the workflow; only adding the force label starts a review,
        # once. Later pushes are gated normally, so a forgotten force label cannot bill every push.
        # A forced round reads the whole PR again, whatever the round cap and the interdiff say, on
        # the deep tier: whoever adds the label asks for the most thorough review.
        if event_label == label_cfg["force"]:
            return decide("deep", f"forced: label '{label_cfg['force']}' was added", "forced", "pr",
                          _measure(config, entries)[3])
        return decide("none", f"skipped: label event for '{event_label}', not '{label_cfg['force']}'", "none", "none")
    if draft and config.get("skip_drafts", True):
        return decide("none", "skipped: draft PR", "none", "none")
    # The cap holds for every push once rounds exist, also after a rebase (interdiff fallback):
    # otherwise a force-push after the last round would buy a full review of the whole PR.
    if context is not None and context.automatic_rounds >= int(rereview["max_rounds"]):
        return decide("none", f"round cap: {context.automatic_rounds} automatic rounds; "
                              f"add label '{label_cfg['force']}' for another", "none", "none")
    if not further:
        tier, why = _size_tier(config, measured, structure, kind)
        return decide(tier, why, "first", "pr")
    verify = rereview["verify_tier"]
    if not context.changed_entries:
        # Only a revert (a file back to main's state) can fix a finding without a changed file; a
        # merge of main alone cannot, so it gets no verify run (Claude review finding on PR #521).
        if context.open_findings and context.reverted:
            return decide(verify, f"verify: {len(context.reverted)} file(s) reverted to main since the last "
                                  "complete round, open findings are checked at head", "verify", "none")
        return decide("none", "skipped: nothing changed against main since the last complete round", "none", "none")
    tier, why = _size_tier(config, measured, structure, kind)
    if tier == "none" and context.open_findings:
        return decide(verify, f"verify: {context.open_findings} open finding(s), the push itself is not reviewed ({why})",
                      "verify", "interdiff")
    return decide(tier, why, "rereview", "interdiff")


def read_result_record(execution_file: Path) -> dict:
    """Return the final "result" message of a Claude Code execution file (JSON array or JSON lines)."""
    text = execution_file.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        messages = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        messages = [json.loads(line) for line in git_lines(text) if line.strip()]
    results = [m for m in messages if isinstance(m, dict) and m.get("type") == "result"]
    if not results:
        raise ValueError(f"no 'result' message in {execution_file}")
    return results[-1]


def cost_summary(result: dict) -> dict:
    if "total_cost_usd" not in result:
        raise ValueError("result message carries no total_cost_usd")
    usage = result.get("usage") or {}
    return {
        "total_cost_usd": float(result["total_cost_usd"]),
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0)),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
        "num_turns": int(result.get("num_turns", 0)),
        "duration_s": round(float(result.get("duration_ms", 0)) / 1000.0, 1),
        "is_error": bool(result.get("is_error", False)),
        "subtype": str(result.get("subtype", "unknown")),
        # On a failed run the result text carries the error (e.g. the API's message), which the
        # action itself hides in its log; keep it so the failure is diagnosable.
        "error_text": str(result.get("result", ""))[:1000] if result.get("is_error") else "",
        "model_usage": result.get("modelUsage", {}),
    }


def build_prompt(template: str, values: dict, verbatim: dict | None = None) -> str:
    """Fill the {{KEY}} placeholders of the prompt file. ``values`` are filled first and a
    placeholder left over is an error; ``verbatim`` values (author-written text such as the PR
    body, or finding titles) are filled last, so a "{{X}}" inside them is neither filled nor an
    error."""
    prompt = template
    for key, value in values.items():
        prompt = prompt.replace("{{" + key + "}}", value)
    left = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", prompt)) - {"{{" + key + "}}" for key in (verbatim or {})})
    if left:
        raise ValueError(f"unknown placeholders in the prompt file: {left}")
    if verbatim:
        # One pass, so a "{{ROUND_BLOCK}}" written inside the PR body is not filled in turn.
        prompt = re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: verbatim.get(m.group(1), m.group(0)), prompt)
    return prompt


def diff_block(diff_source: str, diff_file: str, changed: list, reverted: list, ignored=()) -> str:
    """The prompt paragraph that says what the model reads: the whole PR diff, the files changed
    since the last complete round, or no diff at all (only open findings to verify). ``ignored``
    names the changed files left out of the diff as ignored paths, so an undeclared change to one
    of them (a regenerated input, say) can still be held against the PR description."""
    reading = ("Read ALL of it with Read, page by page (offset/limit) when it is long, before you conclude. "
               "Left of the `|` every added (`+`) and context (` `) line carries its line number in the NEW "
               "version of the file; deleted (`-`) lines and headers carry none.")
    gone = (f" These files were in the PR's change at the last reviewed commit and are not any more "
            f"(reverted to main or renamed): {', '.join(reverted)}." if reverted else "")
    left_out = (f" These changed files are left out of the diff as ignored paths (generated, binary or "
                f"machine-written): {', '.join(ignored)}. Check against the PR description that their change "
                f"is declared." if ignored else "")
    if diff_source == "pr":
        return f"The complete diff of the PR is in the file {diff_file}. {reading}{left_out}"
    if diff_source == "interdiff":
        return (f"This is a further round. The file {diff_file} holds the PR's diff against main for only "
                f"the files whose change differs from the last reviewed commit: {', '.join(changed)}. "
                f"{reading} The other files of the PR were reviewed before and did not change.{gone}{left_out}")
    return ("This is a further round and there is no diff file: nothing in the PR's change against main "
            f"differs from the last reviewed commit.{gone}{left_out} Verify the open findings below by reading the "
            "files at head with Read/Grep/Glob.")


def round_block(state: dict, mode: str, round_number: int, moved: bool = False) -> str:
    """The prompt section with the open findings of earlier rounds ("" when there are none).
    ``moved``: the whole PR is read again (forced round, or the interdiff fell back after a
    rebase), so the cited line numbers may no longer match."""
    findings = open_findings(state)
    if not findings:
        return ""
    lines = [f"RE-REVIEW, ROUND {round_number}. These findings of earlier rounds are already posted on the PR "
             "and still open:", ""]
    for f in findings:
        lines.append(f"- id `{f['id']}`, `{f['path']}:{f['line']}`, {f['severity']}, {f.get('scope', 'introduced')}: "
                     f"{f['title']}")
        if f.get("body"):
            lines.append(f"  Failure scenario: {' '.join(str(f['body']).split())}")
        if f.get("author_note"):
            lines.append(f"  Author's reply: {f['author_note']}")
    lines += ["", "Do not report these again. For each one, return an entry in `previous` with its `id`, "
                  "`resolved` (true only if the defect is gone at this commit) and a one-line `note` with the "
                  "evidence. A finding whose code only moved is still open. Report only NEW defects in "
                  "`findings`."]
    if moved or mode == "forced":
        lines.append("The line numbers above are from an earlier commit and may have moved.")
    return "\n".join(lines)


def fence_for(text: str) -> str:
    """A backtick fence longer than the longest backtick run in ``text`` (at least four), so the
    text cannot close it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(4, longest + 1)


def review_prompt(template: str, repo: str, pr: int, tier: str, sha: str, diff_source: str, diff_file: str,
                  meta: dict, state: dict, mode: str, round_number: int, title: str, body: str,
                  ignored=()) -> str:
    """The complete prompt of one review run. The PR body is the author's text: it goes in verbatim
    inside a backtick fence longer than any backtick run in it (so it cannot close the fence) and
    fills no placeholder."""
    body = body.strip() or "(empty)"
    values = {"REPO": repo, "PR": str(pr), "TIER": tier, "SHA": sha, "FENCE": fence_for(body),
              "DIFF_BLOCK": diff_block(diff_source, diff_file, meta.get("changed", []), meta.get("reverted", []),
                                       ignored)}
    verbatim = {"PR_TITLE": " ".join(str(title).split()), "PR_BODY": body,
                "ROUND_BLOCK": round_block(state, mode, round_number, moved=bool(meta.get("fallback")))}
    return build_prompt(template, values, verbatim)


def claude_command(claude_bin: str, tier_cfg: dict, allowed_tools: list) -> list:
    return [
        claude_bin, "-p",
        # --bare: authenticate strictly with ANTHROPIC_API_KEY (never a Claude plan login, so the
        # run bills the API key), and load no user hooks, plugins, memory or CLAUDE.md on its own.
        "--bare",
        "--model", str(tier_cfg["model"]),
        "--max-turns", str(int(tier_cfg["max_turns"])),
        "--max-budget-usd", str(float(tier_cfg["max_budget_usd"])),
        "--output-format", "json",
        "--json-schema", json.dumps(FINDINGS_SCHEMA),
        # dontAsk denies every tool that is not pre-allowed; there is nobody to ask in CI.
        "--permission-mode", "dontAsk",
        # Last, because the option takes a variadic list.
        "--allowedTools", ",".join(allowed_tools),
    ]


def walk_diff(diff_text: str):
    """Yield ``(raw line, path, new-side line number or None)`` for every line of a unified diff.

    The number is set for added and context lines inside a hunk (the lines GitHub accepts
    inline comments on) and None for headers, deleted lines and "\\ No newline" markers. A hunk
    ends when the line counts of its header are used up; only then are header lines read, so an
    added line "++ x" (shown as "+++ x") is content, not a "+++ b/file" header (Claude review
    finding on PR #497)."""
    path = None
    new_line = old_left = new_left = 0
    for raw in git_lines(diff_text):
        if old_left > 0 or new_left > 0:
            # A context line of an empty source line may arrive as "" when trailing blanks are stripped.
            if raw.startswith("+"):
                yield raw, path, new_line
                new_line, new_left = new_line + 1, new_left - 1
            elif raw.startswith(" ") or raw == "":
                yield raw, path, new_line
                new_line, new_left, old_left = new_line + 1, new_left - 1, old_left - 1
            else:
                if raw.startswith("-"):
                    old_left -= 1
                yield raw, path, None
            continue
        if raw.startswith("diff --git "):
            path = None
        elif raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
        elif raw.startswith("@@"):
            match = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
            if not match:
                raise ValueError(f"unparsable hunk header: {raw!r}")
            old_left = int(match.group(1)) if match.group(1) is not None else 1
            new_line = int(match.group(2))
            new_left = int(match.group(3)) if match.group(3) is not None else 1
        yield raw, path, None


def commentable_lines(diff_text: str) -> dict:
    """Map each file of a unified diff to the NEW-side line numbers GitHub accepts inline comments on
    (added and context lines inside hunks)."""
    lines_by_path: dict = {}
    for _, path, number in walk_diff(diff_text):
        if path is not None and number is not None:
            lines_by_path.setdefault(path, set()).add(number)
    return lines_by_path


def number_diff(diff_text: str) -> str:
    """The diff as the model reads it: every added or context line carries its new-file line
    number left of a "|", other lines a blank column. The first review runs cited line numbers
    of the diff file itself (e.g. 723 in a 650-line script), so every finding landed off the diff."""
    out = []
    for raw, _, number in walk_diff(diff_text):
        out.append(f"{number:>6} |{raw}" if number is not None else f"{'':>6} |{raw}")
    return "\n".join(out) + ("\n" if out else "")


def read_findings(result: dict) -> dict:
    """Return the structured findings of a `claude -p --json-schema` result."""
    if result.get("is_error"):
        raise ValueError(f"the review run failed: {' '.join(str(result.get('result', '')).split())[:500]}")
    structured = result.get("structured_output")
    if structured is None:
        # Older CLI versions return the JSON only as the result text.
        try:
            structured = json.loads(result.get("result", ""))
        except (TypeError, json.JSONDecodeError):
            raise ValueError("the result carries no structured_output and no JSON result text") from None
    if not isinstance(structured, dict) or not isinstance(structured.get("findings"), list):
        raise ValueError("the structured output has no 'findings' list")
    return structured


def _scope(finding: dict) -> str:
    # A result written before the scope field existed reads as "introduced", as every finding was then.
    return finding.get("scope") or "introduced"


def comment_body(finding: dict) -> str:
    """The inline comment text; a pre-existing defect is tagged so it does not read as the author's."""
    tag = " (pre-existing)" if _scope(finding) == "pre-existing" else ""
    return f"**{finding['severity'].upper()}{tag}: {finding['title']}**\n\n{finding['body']}"


def _by_severity(findings: list) -> list:
    return sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9))


def plan_comments(findings: list, commentable: dict) -> tuple:
    """Split findings into inline review comments and summary-only ones (line not in the diff)."""
    inline, summary_only = [], []
    for finding in _by_severity(findings):
        path, line = finding.get("path"), finding.get("line")
        if isinstance(line, int) and line in commentable.get(path, set()):
            inline.append({"path": path, "line": line, "side": "RIGHT", "body": comment_body(finding)})
        else:
            summary_only.append(finding)
    return inline, summary_only


def normalise_title(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(title).casefold()).split())


def drop_duplicates(findings: list, open_previous: list) -> tuple:
    """Drop new findings that repeat an open finding of an earlier round: same path and either the
    same normalised title or the same line and severity. The prompt already forbids repeats; this
    is the safety net, so a reworded repeat is not posted twice. The price: a genuinely new finding
    on the same line with the same severity is dropped too. Returns (kept, [(dropped, previous id)])."""
    kept, dropped = [], []
    for finding in findings:
        match = next((p for p in open_previous if p.get("path") == finding.get("path") and (
            normalise_title(p.get("title", "")) == normalise_title(finding.get("title", ""))
            or (p.get("line") == finding.get("line") and p.get("severity") == finding.get("severity")))), None)
        (dropped.append((finding, match["id"])) if match else kept.append(finding))
    return kept, dropped


def previous_outcomes(structured: dict, open_previous: list) -> tuple:
    """(resolved, still open, unknown ids) of the open findings the model was asked to verify. An
    open finding the model did not answer for stays open."""
    by_id = {f["id"]: f for f in open_previous}
    answers = {}
    unknown = []
    for entry in structured.get("previous") or []:
        if entry.get("id") in by_id:
            answers[entry["id"]] = entry
        else:
            unknown.append(str(entry.get("id")))
    resolved, still_open = [], []
    for finding in open_previous:
        answer = answers.get(finding["id"])
        note = " ".join(str((answer or {}).get("note", "")).split())
        (resolved if answer and answer.get("resolved") else still_open).append(dict(finding, verify_note=note))
    return resolved, still_open, unknown


def verification_reply(sha: str, note: str) -> str:
    """The bot's reply on a finding the re-review found fixed. It never starts with "Fixed",
    "Rejected" or "Not changed": the session retro reads those words as the AUTHOR's outcome."""
    return f"Re-review at {sha[:12]}: no longer present. {note}".strip()


def match_comment_ids(inline: list, posted: list) -> list:
    """Return the GitHub id of each planned inline comment (None where no posted comment matches).

    The key is (path, body) without the line: the listing of one review's comments
    (``pulls/<pr>/reviews/<id>/comments``) returns ``position`` but no ``line`` field, so a key
    with the line matched no posted comment at all (every findings record carried comment_id None
    until 2026-11-04). Comments with the same path and body pair up in posting order, which is
    the order GitHub lists them in."""
    by_key: dict = {}
    for comment in posted:
        key = (comment.get("path"), (comment.get("body") or "").strip())
        by_key.setdefault(key, []).append(comment.get("id"))
    ids = []
    for comment in inline:
        candidates = by_key.get((comment["path"], comment["body"].strip()), [])
        ids.append(candidates.pop(0) if candidates else None)
    return ids


def placed_findings(findings: list, inline: list, comment_ids: list) -> list:
    """The findings in severity order, each with its placement and, when inline, its comment id."""
    placed = {(c["path"], c["line"], c["body"]): cid for c, cid in zip(inline, comment_ids)}
    out = []
    for finding in _by_severity(findings):
        key = (finding.get("path"), finding.get("line"), comment_body(finding))
        out.append(dict(finding, placement="inline" if key in placed else "summary", comment_id=placed.get(key)))
    return out


def findings_record(placed: list, args, result: dict, summary: str, previous: list = (),
                    dropped_duplicates: int = 0) -> dict:
    """The machine-readable record of one review run that the session retro learns from
    (~/.claude/retro, retro_reviews.py): every finding with its placement and, when inline,
    its comment id, so the retro can read whether the finding was fixed or rejected. Schema 2
    adds round, mode, scope, the verification of earlier findings and the dropped repeats; no
    field of schema 1 changed."""
    findings = [{"index": index, "severity": f.get("severity"), "path": f.get("path"), "line": f.get("line"),
                 "title": f.get("title"), "body": str(f.get("body", ""))[:1500], "scope": _scope(f),
                 "placement": f["placement"], "comment_id": f["comment_id"]} for index, f in enumerate(placed)]
    return {"schema": 2, "repo": args.repo, "pr": args.pr, "head_sha": args.sha, "tier": args.tier,
            "round": args.round, "mode": args.mode,
            "run_id": os.environ.get("GITHUB_RUN_ID"), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "created": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "cost_usd": result.get("total_cost_usd"), "num_turns": result.get("num_turns"),
            "summary": summary, "findings": findings, "previous": list(previous),
            "dropped_duplicates": dropped_duplicates}


def round_delta(args, placed: list, resolved: list, result: dict, complete: bool) -> dict:
    """The state of this round, to be merged into the stored state by upsert_summary."""
    round_entry = {"round": args.round, "sha": args.sha, "tier": args.tier,
                   "run_id": os.environ.get("GITHUB_RUN_ID", ""), "automatic": args.mode != "forced",
                   "complete": complete, "cost_usd": result.get("total_cost_usd"), "findings": len(placed)}
    # The run id is part of the finding id: two rounds on the same commit (a forced round after the
    # cap) must not give the same ids, or the newer finding replaces the older one in the state.
    run = os.environ.get("GITHUB_RUN_ID", "") or "local"
    findings = [{"id": f"{args.sha[:12]}-{run}-{index}", "path": f.get("path"), "line": f.get("line"),
                 "severity": f.get("severity"), "scope": _scope(f), "title": str(f.get("title", ""))[:TITLE_CHARS],
                 "body": " ".join(str(f.get("body", "")).split())[:BODY_CHARS], "author_note": "",
                 "comment_id": f["comment_id"], "status": "open"} for index, f in enumerate(placed)]
    findings += [{k: v for k, v in dict(f, status="resolved").items() if k != "verify_note"} for f in resolved]
    return {"schema": 1, "rounds": [round_entry], "findings": findings}


def summary_markdown(structured: dict, inline: list, summary_only: list, tier: str, sha: str,
                     cost_line: str, state: dict | None = None, round_number: int = 1, still_open: list = (),
                     resolved: list = (), cap_reached: bool = False, dropped: int = 0,
                     force_label: str = "claude-review", fallback: str = "", failure: str = "") -> str:
    findings = _by_severity(structured["findings"])
    parts = [SUMMARY_MARKER, f"## Claude review (round {round_number}, {tier} tier, commit {sha[:12]})", "",
             f"**Review failed:** {failure}" if failure else structured["summary"], ""]
    if fallback:
        parts += [f"**Fallback:** {fallback}.", ""]
    if findings:
        parts += ["| Severity | Scope | Where | Finding |", "|---|---|---|---|"]
        parts += [f"| {f['severity']} | {_scope(f)} | `{f['path']}:{f['line']}` | {f['title']} |" for f in findings]
    elif not failure:
        parts.append("No new findings." if round_number > 1 else "No findings.")
    if summary_only:
        parts += ["", "**Not on a changed line** (could not be posted inline):", ""]
        parts += [f"- **{f['severity']}** `{f['path']}:{f['line']}`: {f['title']}. {f['body']}" for f in summary_only]
    if resolved:
        parts += ["", "**Resolved in this round:**", ""]
        parts += [f"- `{f['path']}:{f['line']}`: {f['title']}. {f.get('verify_note', '')}".rstrip() for f in resolved]
    if still_open:
        parts += ["", "**Still open from earlier rounds:**", ""]
        parts += [f"- **{f['severity']}** `{f['path']}:{f['line']}`: {f['title']}" for f in still_open]
    if state and state.get("rounds"):
        parts += ["", "| Round | Commit | Tier | Cost (USD) | New findings | Complete |", "|---|---|---|---|---|---|"]
        parts += [f"| {r['round']} | `{str(r['sha'])[:12]}` | {r['tier']}{'' if r.get('automatic') else ' (forced)'} "
                  f"| {float(r.get('cost_usd') or 0):.4f} | {r.get('findings', 0)} | {'yes' if r.get('complete') else 'no'} |"
                  for r in state["rounds"]]
    if cap_reached:
        parts += ["", f"Automatic rounds used up: later pushes are not reviewed. Add the label `{force_label}` "
                      "for another review of the whole PR."]
    parts += ["", f"<sub>{len(inline)} inline, {len(summary_only)} summary-only"
                  + (f", {dropped} repeat(s) of open findings dropped" if dropped else "")
                  + f". {cost_line} Advisory review, not a required check.</sub>"]
    return "\n".join(parts) + "\n"


def split_sections(diff_text: str) -> list:
    """Split a unified diff into ``(path, text)`` per file section; the path is the NEW path of the
    ``diff --git a/x b/y`` header (``y``), None for text before the first header or an unparsable one.
    Joining the texts gives the diff back."""
    sections: list = []
    for line in git_lines(diff_text, keepends=True):
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*) b/(.*)$", line.rstrip("\r\n"))
            sections.append([match.group(2) if match else None, [line]])
        elif sections:
            sections[-1][1].append(line)
        else:
            sections.append([None, [line]])
    return [(path, "".join(lines)) for path, lines in sections]


def filter_diff(diff_text: str, ignored_patterns) -> tuple:
    """Drop the file sections of a unified diff whose path matches the gate's ignored_paths.

    Returns (kept diff text, dropped paths). The gate already leaves these files out of its
    line counts; handing them to the model as well would spend turns and budget on generated
    views and lock files (Claude review finding on PR #497)."""
    kept, dropped = [], []
    for path, text in split_sections(diff_text):
        if path and matches_any(path, ignored_patterns):
            dropped.append(path)
        else:
            kept.append(text)
    return "".join(kept), dropped


def normalise_section(text: str) -> str:
    """A file section with what a merge of main changes but the PR's own change does not: the blob
    ids of the ``index`` line and the line numbers of the hunk headers (the hunk's code is kept)."""
    out = []
    for line in git_lines(text, keepends=True):
        if line.startswith("index "):
            continue
        out.append(re.sub(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", "@@", line) if line.startswith("@@") else line)
    return "".join(out)


@dataclass
class Interdiff:
    diff_text: str  # the head sections of the changed files, in head order
    changed: list  # paths new in the PR diff since the last round or different there
    unchanged: list  # paths whose section is the same, ignoring shifted hunks and blob ids
    reverted: list  # paths the last round's PR diff had and the head's has not


def interdiff(then_diff: str, now_diff: str) -> Interdiff:
    """Compare the PR diff (base...commit) at the last reviewed commit with the one at head, per file.

    A file-level comparison is enough here and needs no patch algebra: a merge of main shifts
    hunks and changes blob ids, which normalise_section removes, so what is left as "changed" is
    what the author's own commits changed. A renamed file shows as a new path (changed) and a
    vanished old one (reverted)."""
    then = {path: normalise_section(text) for path, text in split_sections(then_diff) if path}
    changed, unchanged, kept = [], [], []
    seen = set()
    for path, text in split_sections(now_diff):
        if not path:
            continue
        seen.add(path)
        if then.get(path) == normalise_section(text):
            unchanged.append(path)
        else:
            changed.append(path)
            kept.append(text)
    return Interdiff("".join(kept), changed, unchanged, [path for path in then if path not in seen])


def _pr_diff(repo: str, pr: int, base_sha: str = "", head_sha: str = "") -> str:
    # errors="replace": a diff can carry bytes that are not UTF-8 (a Latin-1 data file); a strict
    # decode would crash the whole review on such a PR. Only the replaced characters are lost.
    if base_sha:
        # Local git, as the gate does: no API diff size limit (gh pr diff fails on very large diffs).
        return subprocess.run(["git", "-c", "core.quotepath=false", "diff", f"{base_sha}...{head_sha}"],
                              check=True, capture_output=True, text=True, encoding="utf-8",
                              errors="replace").stdout
    return subprocess.run(["gh", "pr", "diff", str(pr), "--repo", repo],
                          check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def _pr_description(repo: str, pr: int) -> tuple:
    """(title, body) of the PR, so the review can check the diff against its stated intent. A
    failed read is logged and the prompt says the description is unavailable; the review still
    runs (the description is an extra check, not the review's input)."""
    try:
        data = json.loads(_gh_api([f"repos/{repo}/pulls/{pr}"]))
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as error:
        print(f"::warning::PR description unavailable ({error}); the intent check is skipped.")
        return "", "PR description unavailable."
    return str(data.get("title") or ""), str(data.get("body") or "")


def _git(args: list) -> str:
    # errors="replace" and unquoted paths, as in _pr_diff: a non-UTF-8 byte must not crash the review.
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def _is_ancestor(older: str, newer: str) -> bool:
    """True when ``older`` is an ancestor of ``newer`` (False after a rebase or force-push)."""
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], capture_output=True).returncode == 0


def _gh_api(args: list, payload: dict | None = None) -> str:
    command = ["gh", "api", *args]
    if payload is None:
        return subprocess.run(command, check=True, capture_output=True, text=True).stdout
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        name = handle.name
    try:
        return subprocess.run([*command, "--input", name], check=True, capture_output=True, text=True).stdout
    finally:
        os.unlink(name)


def _gh_graphql(query: str, variables: dict) -> dict:
    """One GraphQL call through `gh api graphql` (_gh_api has no -F for typed variables)."""
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    for name, value in variables.items():
        command += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
    return json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)


_THREADS_QUERY = """query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $pr) {
    reviewThreads(first: 100) { pageInfo { hasNextPage }
      nodes { id isResolved comments(first: 30) { nodes { databaseId body } } } } } } }"""


def _fetch_threads(repo: str, pr: int) -> dict:
    """The review threads of a PR keyed by the databaseId of their first comment (the id the state
    stores): ``{comment_id: {"thread_id", "resolved", "replies": [text of every later comment]}}``."""
    owner, name = repo.split("/", 1)
    data = _gh_graphql(_THREADS_QUERY, {"owner": owner, "name": name, "pr": pr})
    page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
    if (page.get("pageInfo") or {}).get("hasNextPage"):
        # One page of 100, as the session retro reads it; findings in later threads keep their status.
        print("::warning::This PR has more than 100 review threads; findings beyond the first 100 are "
              "neither dismissed nor resolved from their threads.")
    threads = {}
    for node in page["nodes"]:
        comments = node["comments"]["nodes"]
        if comments:
            threads[comments[0]["databaseId"]] = {"thread_id": node["id"], "resolved": bool(node["isResolved"]),
                                                  "replies": [c.get("body") or "" for c in comments[1:]]}
    return threads


def _append(env_var: str, text: str) -> None:
    target = os.environ.get(env_var)
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


def _cmd_classify(args) -> int:
    import yaml  # only classify needs it; the cost step runs on a bare runner Python

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    entries = parse_numstat(Path(args.numstat).read_text(encoding="utf-8"))
    events = parse_name_status(Path(args.name_status).read_text(encoding="utf-8")) if args.name_status else []
    labels = [label.strip() for label in (args.labels or "").split(",") if label.strip()]
    context = None
    if args.state:
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
        meta = json.loads(Path(args.interdiff_json).read_text(encoding="utf-8")) if args.interdiff_json else {}
        changed = (parse_numstat(Path(args.interdiff_numstat).read_text(encoding="utf-8"))
                   if args.interdiff_numstat else [])
        context = round_context(state, meta, changed)
    decision = classify(config, entries, args.branch, labels, args.draft,
                        args.event_action or "", args.event_label or "", context, events)
    print(f"[claude-review] {decision.reason}")
    print(f"[claude-review] run={str(decision.run).lower()} tier={decision.tier} model={decision.model or '-'} "
          f"mode={decision.mode} diff={decision.diff_source} round={decision.round}")
    _append("GITHUB_OUTPUT", f"run={str(decision.run).lower()}\ntier={decision.tier}\nmodel={decision.model}\n"
                             f"max_turns={decision.max_turns}\nmode={decision.mode}\n"
                             f"diff_source={decision.diff_source}\nround={decision.round}\nreason={decision.reason}\n")
    _append("GITHUB_STEP_SUMMARY", f"### Claude review gate\n\n- Decision: **{decision.tier}** "
                                   f"(round {decision.round}, mode {decision.mode})\n"
                                   f"- Reason: {decision.reason}\n")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(asdict(decision), indent=2), encoding="utf-8")
    return 0


def _cmd_state(args) -> int:
    """Read the review history (summary comment and finding threads); no model, no cost."""
    comment_id, body = _summary_comment(args.repo, args.pr, args.bot_login)
    state = decode_state(body) if comment_id else None
    if state is None:
        state = empty_state()
        if comment_id:
            note = ("fallback: no state block in the existing summary; first-round review, "
                    "earlier findings unknown")
            print(f"[claude-review] {note}")
            _append("GITHUB_STEP_SUMMARY", f"- Review state: {note}\n")
    elif any(f.get("status") == "open" and f.get("comment_id") for f in state["findings"]):
        state = refresh_state(state, _fetch_threads(args.repo, args.pr))
    counts = {status: sum(1 for f in state["findings"] if f.get("status") == status)
              for status in ("open", "resolved", "dismissed")}
    print(f"[claude-review] state: {len(state['rounds'])} rounds ({automatic_rounds(state)} automatic complete), "
          f"open {counts['open']}, resolved {counts['resolved']}, dismissed {counts['dismissed']}, "
          f"last complete {last_complete_sha(state)[:12] or '-'}")
    Path(args.out).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return 0


def _cmd_interdiff(args) -> int:
    """What changed in the PR since the last complete round; no model, no cost."""
    import yaml

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    ignored = config.get("ignored_paths", [])
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    last = last_complete_sha(state)
    base, head = args.base_sha, args.head_sha
    full_now = _git(["diff", f"{base}...{head}"])
    now, ignored_changed = filter_diff(full_now, ignored)
    fallback = ""
    result = None
    if last and not _is_ancestor(last, head):
        fallback = (f"reviewed commit {last[:12]} is not an ancestor of head (rebase or force-push?); "
                    "the whole PR is reviewed")
        print(f"[claude-review] fallback: {fallback}")
        _append("GITHUB_STEP_SUMMARY", f"- Interdiff: fallback, {fallback}\n")
    elif last:
        full_then = _git(["diff", f"{base}...{last}"])
        then, _ = filter_diff(full_then, ignored)
        result = interdiff(then, now)
        # The ignored files whose change differs from the last round, named to the model like on round 1.
        ignored_then = {path: normalise_section(text) for path, text in split_sections(full_then)
                        if path and matches_any(path, ignored)}
        ignored_changed = [path for path, text in split_sections(full_now) if path in ignored_changed
                           and ignored_then.get(path) != normalise_section(text)]
    if result is None:
        paths = [path for path, _ in split_sections(now) if path]
        result = Interdiff(now, paths, [], [])
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "interdiff.diff").write_text(result.diff_text, encoding="utf-8")
    # Structure changes of this round: files added, deleted or renamed against main at head whose paths
    # were not in such a change at the last complete round (a file added before that round was already
    # reviewed as such). Paths, not whole events, are compared: git's rename pairing (-M, similarity
    # >= 50 %) can turn a rename into a delete plus an add, or back, when the file is edited later.
    events_now = parse_name_status(_git(["diff", "--name-status", "-M", f"{base}...{head}"]))
    if last and not fallback:
        numstat = _git(["diff", "--numstat", last, head, "--", *result.changed]) if result.changed else ""
        paths_then = {path for event in parse_name_status(_git(["diff", "--name-status", "-M", f"{base}...{last}"]))
                      for path in event[1:]}
        events = [event for event in events_now if not set(event[1:]) <= paths_then]
    else:
        numstat = _git(["diff", "--numstat", f"{base}...{head}"])
        events = events_now
    (out_dir / "interdiff.numstat").write_text(numstat, encoding="utf-8")
    (out_dir / "interdiff.json").write_text(json.dumps(
        {"last_sha": last, "fallback": fallback, "changed": result.changed, "unchanged": result.unchanged,
         "reverted": result.reverted, "events": [list(event) for event in events],
         "ignored_changed": ignored_changed}, indent=2), encoding="utf-8")
    print(f"[claude-review] interdiff: last complete round {last[:12] or '-'}, changed {len(result.changed)}, "
          f"unchanged {len(result.unchanged)}, reverted {len(result.reverted)}, "
          f"added/deleted/renamed {len(events)}")
    return 0


def _cmd_cost(args) -> int:
    try:
        if not args.execution_file:
            raise ValueError("empty execution file path (the action set no output)")
        summary = cost_summary(read_result_record(Path(args.execution_file)))
    except (OSError, ValueError) as error:
        print(f"error: no cost record: {error}", file=sys.stderr)
        _append("GITHUB_STEP_SUMMARY", f"### Claude review cost\n\n**No cost record:** {error}\n")
        return 1
    summary.update({"tier": args.tier, "model": args.model})
    # A run cut off by --max-turns or an API error still costs money but is not a complete review.
    # The SDK can report subtype "success" together with is_error (an API error on the first call).
    if summary["is_error"]:
        status = "error" if summary["subtype"] == "success" else summary["subtype"]
    else:
        status = "complete" if summary["subtype"] == "success" else summary["subtype"]
    line = (f"[claude-review] {status}: cost ${summary['total_cost_usd']:.4f} USD, tier {args.tier or '-'}, "
            f"model {args.model or '-'}, {summary['num_turns']} turns, {summary['duration_s']} s, "
            f"tokens in {summary['input_tokens']} / out {summary['output_tokens']} / "
            f"cache write {summary['cache_creation_input_tokens']} / cache read {summary['cache_read_input_tokens']}")
    print(line)
    if status != "complete":
        print(f"::warning::Claude review did not complete ({status}); the cost is real, the review is partial.")
    if summary["error_text"]:
        # One line, so the workflow command is not cut at the first newline.
        print(f"::error::Claude run error: {' '.join(summary['error_text'].split())}")
    _append("GITHUB_STEP_SUMMARY",
            "### Claude review cost\n\n| Status | Cost (USD) | Tier | Model | Turns | Duration (s) | Input | Output "
            "| Cache write | Cache read |\n|---|---|---|---|---|---|---|---|---|---|\n"
            f"| {status} | {summary['total_cost_usd']:.4f} | {args.tier or '-'} | {args.model or '-'} | {summary['num_turns']} "
            f"| {summary['duration_s']} | {summary['input_tokens']} | {summary['output_tokens']} "
            f"| {summary['cache_creation_input_tokens']} | {summary['cache_read_input_tokens']} |\n\n"
            "Source: the `result` record of the Claude Code execution file (as reported by the API).\n"
            + (f"\n**Run error:** `{' '.join(summary['error_text'].split())}`\n" if summary["error_text"] else ""))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


def _cmd_review(args) -> int:
    import yaml

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    tier_cfg = config["tiers"][args.tier]
    review_cfg = config["review"]
    state = json.loads(Path(args.state).read_text(encoding="utf-8")) if args.state else empty_state()
    interdiff_dir = Path(args.interdiff_dir) if args.interdiff_dir else None
    meta = (json.loads((interdiff_dir / "interdiff.json").read_text(encoding="utf-8"))
            if interdiff_dir and (interdiff_dir / "interdiff.json").exists() else {})
    # The model gets the diff as a file it pages through with Read, so it needs no shell at all
    # (a local run showed the model piping `gh pr diff` into grep/awk, which the read-only tool
    # list rightly denies, and the review then covered only part of the diff). A further round
    # reads only the files whose change differs from the last complete round (`interdiff`), a
    # verify round without such files reads none.
    dropped: list = []
    if args.diff_source == "interdiff":
        if interdiff_dir is None:
            print("::error::--diff-source interdiff needs --interdiff-dir")
            return 1
        diff_text = (interdiff_dir / "interdiff.diff").read_text(encoding="utf-8")
        dropped = list(meta.get("ignored_changed", []))
    elif args.diff_source == "none":
        diff_text = ""
        dropped = list(meta.get("ignored_changed", []))
    else:
        diff_text, dropped = filter_diff(_pr_diff(args.repo, args.pr, args.base_sha, args.sha),
                                         config.get("ignored_paths", []))
    diff_path = Path(args.diff_out)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    # Raw diff for `post` (it maps findings onto diff lines); numbered copy for the model.
    diff_path.write_text(diff_text, encoding="utf-8")
    numbered_path = diff_path.with_name(diff_path.name + ".numbered")
    numbered_path.write_text(number_diff(diff_text), encoding="utf-8")
    print(f"[claude-review] round {args.round} ({args.mode}), diff source {args.diff_source}: "
          f"{len(git_lines(diff_text))} lines -> {diff_path.as_posix()} (model reads {numbered_path.as_posix()}); "
          f"{len(dropped)} ignored file(s) left out" + (f": {', '.join(dropped[:10])}" if dropped else "")
          + f"; {len(open_findings(state))} open finding(s) of earlier rounds")
    if not diff_text.strip() and not open_findings(state):
        print("::warning::Nothing left to review after dropping ignored paths and no open finding; no model call.")
        Path(args.out).write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                              "total_cost_usd": 0.0, "num_turns": 0, "structured_output": {
                                                  "summary": "Only ignored (generated or lock) files changed.",
                                                  # Not complete: no review happened, so this round
                                                  # neither uses up an automatic round nor moves
                                                  # the reviewed commit.
                                                  "findings": [], "previous": [], "coverage_complete": False}}),
                                  encoding="utf-8")
        return 0
    title, body = _pr_description(args.repo, args.pr)
    prompt = review_prompt(Path(review_cfg["prompt_file"]).read_text(encoding="utf-8"), args.repo, args.pr,
                           args.tier, args.sha, args.diff_source if diff_text.strip() else "none",
                           numbered_path.as_posix(), meta, state, args.mode, args.round, title, body,
                           ignored=dropped)
    claude_bin = shutil.which(args.claude_bin) or args.claude_bin
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # claude --bare authenticates only with this key; fail here with a clear message instead.
        print("::error::ANTHROPIC_API_KEY is not set; the review needs a workspace API key.")
        return 1
    command = claude_command(claude_bin, tier_cfg, review_cfg["allowed_tools"])
    print(f"[claude-review] running claude -p: tier {args.tier}, model {tier_cfg['model']}, "
          f"max {tier_cfg['max_turns']} turns, budget {tier_cfg['max_budget_usd']} USD")
    # The prompt goes in on stdin: no argv length limit, and it is not echoed in process listings.
    completed = subprocess.run(command, input=prompt, capture_output=True, text=True, encoding="utf-8")
    Path(args.out).write_text(completed.stdout, encoding="utf-8")
    if completed.stderr.strip():
        print(completed.stderr.strip()[-2000:], file=sys.stderr)
    print(f"[claude-review] claude exited with {completed.returncode}; result written to {args.out}")
    return completed.returncode


def _cmd_post(args) -> int:
    try:
        result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        # The workflow runs `post` after a failed review step too, which may have written no result.
        print(f"::error::Nothing to post: no review result ({error})")
        return 1
    failure = ""
    try:
        structured = read_findings(result)
    except ValueError as error:
        print(f"::error::Nothing to post: {error}")
        if float(result.get("total_cost_usd") or 0) <= 0:
            return 1
        # The run cost money but returned no findings (turn or budget cap, API error). It is still
        # recorded, as an incomplete round with its cost, so it counts against rereview.max_rounds:
        # otherwise a PR whose review fails every time is reviewed, and billed, again on every push
        # (Claude review finding on a copy of this script). The step still fails.
        subtype = str(result.get("subtype") or "unknown")
        failure = (f"the run ended without a review ({'error' if subtype == 'success' else subtype}): {error}. "
                   "Its cost counts against the automatic rounds; earlier findings stay open.")
        structured = {"summary": "", "findings": [], "previous": [], "coverage_complete": False}
    config = {}
    if args.config:
        import yaml
        config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    state = json.loads(Path(args.state).read_text(encoding="utf-8")) if args.state else empty_state()
    open_previous = open_findings(state)
    if "coverage_complete" not in structured:
        print("::warning::The result does not say whether the whole diff was read; the round counts as complete.")
    complete = bool(structured.get("coverage_complete", True))
    if not complete and not failure:
        print("::warning::The model did not read the whole diff; this round does not move the reviewed commit, "
              "so the next round reviews the same files again.")
    if open_previous and "previous" not in structured:
        print(f"::warning::The result has no 'previous' list; the {len(open_previous)} open finding(s) stay open.")
    resolved, still_open, unknown = previous_outcomes(structured, open_previous)
    for finding_id in unknown:
        print(f"::warning::The model reported unknown previous id {finding_id}; ignored.")
    findings, dropped = drop_duplicates(structured["findings"], open_previous)
    for finding, previous_id in dropped:
        print(f"[claude-review] dropped `{finding.get('path')}:{finding.get('line')}` {finding.get('title')!r}: "
              f"duplicate of {previous_id}")
    if open_previous:
        print(f"[claude-review] earlier findings: {len(open_previous)} open -> {len(resolved)} resolved, "
              f"{len(still_open)} still open")
    diff_text = Path(args.diff_file).read_text(encoding="utf-8") if args.diff_file else _pr_diff(args.repo, args.pr)
    inline, summary_only = plan_comments(findings, commentable_lines(diff_text))
    total = len(inline) + len(summary_only)
    print(f"[claude-review] findings {total}: inline {len(inline)}, summary-only {len(summary_only)} "
          "(line not in the diff)")
    if total and len(summary_only) == total:
        # Every finding off the diff usually means wrong paths or old-side line numbers, not a real pattern.
        print(f"::warning::All {total} findings point outside the diff; check path/line in the result.")
    cost_line = (f"Cost {float(result.get('total_cost_usd', 0)):.4f} USD, {result.get('num_turns', '?')} turns."
                 if "total_cost_usd" in result else "")
    review = {"commit_id": args.sha, "event": "COMMENT",
              "body": "Claude review: inline findings below; the summary is in the Claude review comment.",
              "comments": inline}
    comment_ids = [None] * len(inline)
    if args.dry_run:
        print("[claude-review] dry run, nothing posted. Review payload:")
        print(json.dumps(review, indent=2))
    elif inline:
        posted = json.loads(_gh_api([f"repos/{args.repo}/pulls/{args.pr}/reviews", "--method", "POST"], review))
        # One page of 100 (not --paginate, which would concatenate JSON arrays); a review posts far fewer.
        comments = json.loads(_gh_api([f"repos/{args.repo}/pulls/{args.pr}/reviews/{posted['id']}/comments"
                                       "?per_page=100"]) or "[]")
        comment_ids = match_comment_ids(inline, comments)
        matched = sum(1 for cid in comment_ids if cid is not None)
        print(f"[claude-review] inline comment ids matched {matched}/{len(inline)}")
        if matched < len(inline):
            # Without the id the retro cannot read the thread's outcome; the finding then stays "unknown".
            print(f"::warning::{len(inline) - matched} posted inline comment(s) could not be matched to a finding.")
    placed = placed_findings(findings, inline, comment_ids)
    delta = round_delta(args, placed, resolved, result, complete)
    preview = merge_state(state, delta)
    max_rounds = int(config.get("rereview", {}).get("max_rounds", 0) or 0)
    cap_reached = bool(max_rounds) and args.mode != "forced" and automatic_rounds(preview) >= max_rounds
    fallback = ""
    if args.interdiff_dir and (Path(args.interdiff_dir) / "interdiff.json").exists():
        fallback = json.loads((Path(args.interdiff_dir) / "interdiff.json").read_text(encoding="utf-8")).get("fallback", "")
    body = summary_markdown(dict(structured, findings=findings), inline, summary_only, args.tier, args.sha,
                            cost_line, preview, args.round, still_open, resolved, cap_reached, len(dropped),
                            config.get("labels", {}).get("force", "claude-review"), fallback, failure)
    if args.dry_run:
        print("[claude-review] Summary comment:")
        print(body)
        _close_resolved(args, resolved)
    if args.record:
        previous = [{"id": f["id"], "comment_id": f.get("comment_id"), "resolved": f in resolved,
                     "note": f.get("verify_note", "")} for f in resolved + still_open]
        previous.sort(key=lambda p: p["id"])
        record = findings_record(placed, args, result, structured.get("summary", ""), previous, len(dropped))
        Path(args.record).write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(f"[claude-review] findings record written to {args.record}")
    if args.dry_run:
        return 1 if failure else 0
    head = _gh_api([f"repos/{args.repo}/pulls/{args.pr}", "--jq", ".head.sha"]).strip()
    upsert_summary(args.repo, args.pr, args.bot_login, body, stale=bool(head) and head != args.sha, state_delta=delta)
    # Only after the state is written: a thread resolved before a failed state write would read as
    # resolved by a human next time, i.e. "dismissed" instead of "resolved".
    _close_resolved(args, resolved)
    return 1 if failure else 0


def _close_resolved(args, resolved: list) -> None:
    """Reply on the thread of every finding the re-review found fixed and resolve the thread. A
    failure here is logged, never fatal: the state still records the finding as resolved."""
    with_thread = [f for f in resolved if f.get("comment_id")]
    if not with_thread:
        return
    threads = _fetch_threads(args.repo, args.pr)
    closed = 0
    for finding in with_thread:
        reply = verification_reply(args.sha, finding.get("verify_note", ""))
        thread_id = (threads.get(finding["comment_id"]) or {}).get("thread_id")
        if not thread_id:
            print(f"::warning::No review thread found for finding {finding['id']} (comment {finding['comment_id']}); "
                  "it is replied to but its thread stays open.")
        if args.dry_run:
            print(f"[claude-review] would reply on {finding['comment_id']}: {reply}")
            if thread_id:
                print(f"[claude-review] would resolve thread {thread_id}")
                closed += 1
            continue
        try:
            _gh_api([f"repos/{args.repo}/pulls/{args.pr}/comments/{finding['comment_id']}/replies", "--method", "POST"],
                    {"body": reply})
            if thread_id:
                _gh_graphql(_RESOLVE_MUTATION, {"thread": thread_id})
                closed += 1
        except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"::warning::Could not reply on or resolve finding {finding['id']}: {error}")
    print(f"[claude-review] earlier findings resolved: {closed}/{len(with_thread)} threads closed on the PR")


_RESOLVE_MUTATION = """mutation($thread: ID!) { resolveReviewThread(input: {threadId: $thread}) { thread { id } } }"""


def _summary_ids(repo: str, pr: int, bot_login: str) -> list:
    # Only a comment the workflow itself wrote counts as the summary, so nobody else's comment is edited.
    select = (f'.[] | select(.user.login == "{bot_login}" and ((.body // "") | contains("{SUMMARY_MARKER}")))'
              " | .id")
    return _gh_api([f"repos/{repo}/issues/{pr}/comments", "--paginate", "--jq", select]).split()


def _summary_comment(repo: str, pr: int, bot_login: str) -> tuple:
    """(id, body) of the oldest summary comment of the bot, or (None, "")."""
    ids = _summary_ids(repo, pr, bot_login)
    if not ids:
        return None, ""
    return ids[0], json.loads(_gh_api([f"repos/{repo}/issues/comments/{ids[0]}"])).get("body") or ""


def upsert_summary(repo: str, pr: int, bot_login: str, body: str, stale: bool = False,
                   state_delta: dict | None = None) -> None:
    """Keep exactly one summary comment: edit the oldest, or create one.

    A forced review (label event) and a push review run in different concurrency groups and can
    post at the same time; both may then find no summary and create one each (Claude review
    finding on PR #497). After writing, the oldest summary is kept with this body and any later
    duplicate is deleted. ``stale``: this run reviewed a commit that is no longer the PR head; it
    then leaves an existing summary alone, so a slower review of an older commit cannot replace
    the summary of the newer one (Claude review finding on PR #497). Its inline comments stay.

    ``state_delta``: the state of this round. It is merged into the state block that is in the
    comment NOW (read here, not at the start of the run), so a parallel run's round is not lost,
    and the rounds are numbered in order. A stale run keeps the visible text but still merges its
    round and findings: they are on the PR and must be verified, never repeated."""
    existing = _summary_ids(repo, pr, bot_login)
    if state_delta is not None:
        current = (json.loads(_gh_api([f"repos/{repo}/issues/comments/{existing[0]}"])).get("body") or ""
                   if existing else "")
        if stale:
            state_delta = copy.deepcopy(state_delta)
            for round_ in state_delta["rounds"]:
                round_["stale"] = True
        merged = merge_state(decode_state(current) or empty_state(), state_delta)
        for number, round_ in enumerate(merged["rounds"], start=1):
            round_["round"] = number
        visible = strip_state(current if stale and existing else body).rstrip("\n") + "\n"
        body = visible + "\n" + encode_state(merged) + "\n"
        if stale and existing:
            print("::notice::This review is of an older commit than the PR head; the visible summary is left as it "
                  "is, its round and findings are added to the state.")
    elif stale and existing:
        print("::notice::This review is of an older commit than the PR head; the summary is left as it is.")
        return
    if existing:
        _gh_api([f"repos/{repo}/issues/comments/{existing[0]}", "--method", "PATCH"], {"body": body})
        written = existing[0]
    else:
        written = str(json.loads(_gh_api([f"repos/{repo}/issues/{pr}/comments", "--method", "POST"],
                                         {"body": body}))["id"])
    now = _summary_ids(repo, pr, bot_login)
    if now and now[0] != written:
        _gh_api([f"repos/{repo}/issues/comments/{now[0]}", "--method", "PATCH"], {"body": body})
    for duplicate in now[1:]:
        try:
            _gh_api([f"repos/{repo}/issues/comments/{duplicate}", "--method", "DELETE"])
        except subprocess.CalledProcessError:
            pass  # the parallel run deleted it first
    print(f"[claude-review] summary comment {now[0] if now else written} up to date"
          + (f"; removed {len(now) - 1} duplicate(s) from a parallel run" if len(now) > 1 else ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser("classify", help="decide whether and how the review runs")
    p_classify.add_argument("--config", required=True)
    p_classify.add_argument("--numstat", required=True, help="file holding `git diff --numstat base...head`")
    p_classify.add_argument("--name-status", default="",
                            help="file holding `git diff --name-status -M base...head` (structure changes)")
    p_classify.add_argument("--branch", required=True, help="PR head branch, e.g. feature/i1-slug")
    p_classify.add_argument("--labels", default="", help="comma-separated PR labels")
    p_classify.add_argument("--draft", action="store_true")
    p_classify.add_argument("--event-action", default="")
    p_classify.add_argument("--event-label", default="")
    p_classify.add_argument("--state", default="", help="state.json of the `state` command (further rounds)")
    p_classify.add_argument("--interdiff-json", default="", help="interdiff.json of the `interdiff` command")
    p_classify.add_argument("--interdiff-numstat", default="", help="interdiff.numstat of the `interdiff` command")
    p_classify.add_argument("--json", dest="json_out")
    p_classify.set_defaults(func=_cmd_classify)

    p_state = sub.add_parser("state", help="read the review history of the PR (summary comment and threads)")
    p_state.add_argument("--repo", required=True, help="OWNER/NAME")
    p_state.add_argument("--pr", required=True, type=int)
    p_state.add_argument("--out", required=True, help="state JSON for classify, interdiff and review")
    p_state.add_argument("--bot-login", default="github-actions[bot]",
                         help="author of the summary comment (the workflow token's identity)")
    p_state.set_defaults(func=_cmd_state)

    p_interdiff = sub.add_parser("interdiff", help="compare the PR diff at the last reviewed commit and at head")
    p_interdiff.add_argument("--config", required=True)
    p_interdiff.add_argument("--state", required=True, help="state JSON written by the state command")
    p_interdiff.add_argument("--base-sha", required=True, help="PR base commit")
    p_interdiff.add_argument("--head-sha", required=True, help="PR head commit")
    p_interdiff.add_argument("--out-dir", required=True, help="receives interdiff.diff, .numstat and .json")
    p_interdiff.set_defaults(func=_cmd_interdiff)

    p_review = sub.add_parser("review", help="run claude -p and write its JSON result")
    p_review.add_argument("--config", required=True)
    p_review.add_argument("--tier", required=True, choices=["light", "full", "deep"])
    p_review.add_argument("--repo", required=True, help="OWNER/NAME")
    p_review.add_argument("--pr", required=True, type=int)
    p_review.add_argument("--sha", required=True, help="PR head commit")
    p_review.add_argument("--out", required=True)
    p_review.add_argument("--base-sha", default="",
                          help="PR base commit: diff with local git (needs full history) instead of gh pr diff")
    p_review.add_argument("--diff-out", default=".claude/review-pr.diff",
                          help="where the PR diff is written for the model (inside the checkout, so Read may open "
                               "it); pass the same file to post --diff-file")
    p_review.add_argument("--state", default="", help="state.json of the `state` command")
    p_review.add_argument("--interdiff-dir", default="", help="output directory of the `interdiff` command")
    p_review.add_argument("--diff-source", default="pr", choices=["pr", "interdiff", "none"],
                          help="what the model reads (the gate's diff_source output)")
    p_review.add_argument("--mode", default="first", help="the gate's mode output")
    p_review.add_argument("--round", default=1, type=int, help="the gate's round output")
    p_review.add_argument("--claude-bin", default="claude")
    p_review.set_defaults(func=_cmd_review)

    p_post = sub.add_parser("post", help="post the findings of a review result to the PR")
    p_post.add_argument("--result", required=True)
    p_post.add_argument("--repo", required=True)
    p_post.add_argument("--pr", required=True, type=int)
    p_post.add_argument("--tier", required=True)
    p_post.add_argument("--sha", required=True)
    p_post.add_argument("--diff-file", help="unified diff of the PR; default: `gh pr diff`")
    p_post.add_argument("--dry-run", action="store_true", help="print the payloads, post nothing")
    p_post.add_argument("--record", help="write the machine-readable findings record (for the session retro) here")
    p_post.add_argument("--config", default="", help="gate config (round cap and force label for the summary)")
    p_post.add_argument("--state", default="", help="state.json of the `state` command (open earlier findings)")
    p_post.add_argument("--interdiff-dir", default="", help="output of `interdiff` (its fallback reaches the summary)")
    p_post.add_argument("--mode", default="first", help="the gate's mode output")
    p_post.add_argument("--round", default=1, type=int, help="the gate's round output")
    p_post.add_argument("--bot-login", default="github-actions[bot]",
                        help="author of the summary comment to update (the workflow token's identity)")
    p_post.set_defaults(func=_cmd_post)

    p_cost = sub.add_parser("cost", help="report the API cost of a finished review")
    p_cost.add_argument("--execution-file", required=True)
    p_cost.add_argument("--tier", default="")
    p_cost.add_argument("--model", default="")
    p_cost.add_argument("--json", dest="json_out")
    p_cost.set_defaults(func=_cmd_cost)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
