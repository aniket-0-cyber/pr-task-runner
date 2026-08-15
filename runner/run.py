#!/usr/bin/env python3
"""
Batch-run codx over every repo checkout in this folder.

Flow (deliberately split in two, so nothing leaves your machine unreviewed):

    python3 run.py list          # what's here and where each repo stands
    python3 run.py fix           # every repo IN PARALLEL, then STOPS
    python3 run.py review        # show the diffs it produced
    python3 run.py push          # commit + push + post /bot2 qc-check
    python3 run.py rescore       # post /bot rescore on the PRs

Every command takes `--task 1392,1393` (or bare PR numbers) to act on a subset.
A repo that has already been pushed gets only the comments posted since that
push, so follow-up rounds fix what's still broken instead of starting over.

`fix` runs all repos at once by default (`-j N` to cap it). Each agent gets its
own codx process and its own repo directory, so they never touch each other's
files. Output lines are tagged with the PR number, and each repo also gets a
full log at .runner/<repo>/log.txt.

The agent runs under codx's `workspace-write` sandbox: it can only write inside
the repo it was pointed at, and its shell commands have no network access. The
commit, the push, and the bot comment all happen in this script, outside the
sandbox, only after you've looked at the diff.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

BASE = Path(__file__).resolve().parent        # runner/ — everything ships here
ROOT = BASE.parent                            # where the repo checkouts live
WORK = BASE / "work"                          # generated state and artefacts
STATE_FILE = WORK / "state.json"

PROMPTS = BASE / "prompts"
SCHEMAS = BASE / "schemas"
PROMPT_TEMPLATE = PROMPTS / "fix.md"
SCORE_TEMPLATE = PROMPTS / "score.md"
SCORE_SCHEMA = SCHEMAS / "score.json"
HARDEN_TEMPLATE = PROMPTS / "harden.md"
ANALYSIS_TEMPLATE = PROMPTS / "analysis.md"
ANALYSIS_SCHEMA = SCHEMAS / "analysis.json"
SYNTHESIS_TEMPLATE = PROMPTS / "analysis-synthesis.md"
SYNTHESIS_SCHEMA = SCHEMAS / "analysis-synthesis.json"
AUDIT_TEMPLATE = PROMPTS / "audit.md"
REPAIR_TEMPLATE = PROMPTS / "repair.md"
AUDIT_SCHEMA = SCHEMAS / "audit.json"
# the fairness reviewer's rubric standard, injected verbatim into the fix prompt
CALIBRATION_TEMPLATE = PROMPTS / "review-prompt-rubric-calibration.md"
# the five audit dimensions, condensed from the RL Data Quality Audit guides
QC_TEMPLATE = PROMPTS / "qc-guidelines.md"
ANALYSE_TEMPLATE = PROMPTS / "analyse.md"
ANALYSE_SCHEMA = BASE / "schemas" / "analyse.json"

CODX = shutil.which("codx") or str(Path.home() / ".local/bin/codx")
# The pipeline uses three bot commands. `qc-check` and `fairness-review` are two
# halves of one review and are always posted together — on a new task, and again
# after each fix round; `rescore` is posted once both come back clean. Anything
# else you want to run, pass by hand: `rescore --body "..."`.
QC_COMMAND = "/bot2 qc-check"
FAIRNESS_COMMAND = "/bot2 fairness-review"
RESCORE_COMMAND = "/bot2 rescore"
# A rescore only re-scores existing traces "after rubric/test-only changes".
# Once a solver-visible file changes the traces themselves must be re-run, which
# is a different and much heavier bot command.
TRACE_RUN_COMMAND = "/bot mm-trace-run"

# both reviews, always together: a task needs a current pass from each, so
# asking for one without the other only half-answers the question
REVIEW_COMMANDS = (QC_COMMAND, FAIRNESS_COMMAND)   # what `trigger` posts
PUSH_COMMANDS = REVIEW_COMMANDS                    # what `push` posts
BOT_COMMAND = QC_COMMAND                   # kept for anything still importing it
REVIEW_COMMAND = QC_COMMAND                # ditto

# identifies the bot's actual PASS/WARN/FAIL verdict among all the other
# chatter on a PR — see latest_fairness_review()
# The bot has posted three shapes over time and all are still on live PRs:
#   "Task Fairness Review" — gating, headline verdict PASS / WARN / FAIL
#   "Task Fairness Audit"  — non-gating, a Findings list with severities
#   "Task review"          — the combined `/bot2 review` reply: fairness and
#                            both QC phases in one comment, with an At-a-glance
#                            block and numbered items
# Match any; see fairness_verdict() for how each becomes a verdict.
COMBINED_MARKER = "Task review"
# `human-review` is the current family. It has three headings — "Task review —
# issues found" (fairness + both QC phases), "Human fairness review — issues
# found" (fairness alone, what /bot2 fairness-review returns), and "Task review
# — partially completed" (a stage crashed). The HTML marker covers all of them
# and any heading they add next.
HUMAN_REVIEW_MARKER = "github-review-bot:human-review"
# The bot stamps every comment it posts with an HTML marker, and those are the
# only reliable signal: the queue acknowledgement says "Human fairness review
# queued", so matching the phrase anywhere in the body picks up the ack — which
# is newer than the review it announces, and would shadow it.
REVIEW_HTML_MARKERS = ("github-review-bot:fairness-review", HUMAN_REVIEW_MARKER)
# fallback for comments carrying no HTML marker: the phrase must be the heading
FAIRNESS_MARKERS = ("Task Fairness Review", "Task Fairness Audit", COMBINED_MARKER,
                    "Human fairness review")
FAIRNESS_MARKER = FAIRNESS_MARKERS[0]      # kept for anything still importing it


def is_review_comment(body: str) -> bool:
    """Whether a comment is a review result rather than chatter about one."""
    if any(m in body for m in REVIEW_HTML_MARKERS):
        return True
    return any(line.lstrip().startswith("#")
               and any(h in line for h in FAIRNESS_MARKERS)
               for line in body.splitlines())
COMMIT_MESSAGE = "Address PR review feedback"

# Sandbox for the agent. workspace-write = can write inside the repo only, and
# no network for its shell commands. Override with --sandbox if a fix genuinely
# needs more (e.g. danger-full-access), but understand what you're giving up.
DEFAULT_SANDBOX = "workspace-write"

# Minutes an agent gets before it is killed. Tasks normally land well inside
# this; the limit exists so one runaway cannot hold a 17-repo batch open.
DEFAULT_TIMEOUT_MIN = 30

# folder names come in several shapes: `...-pr-1392-some-task-name`, plain
# `...-pr-1406`, and bare `pr-1709`, so `pr-` may start the name or follow a
# hyphen, and the digits may be followed by a hyphen or end the name
PR_NUM_RE = re.compile(r"(?:^|-)pr-(\d+)(?:-|$)")

C = {
    "dim": "\033[2m", "red": "\033[31m", "grn": "\033[32m",
    "yel": "\033[33m", "blu": "\033[34m", "bld": "\033[1m", "off": "\033[0m",
}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}


# repos run concurrently, so every write to stdout and to state goes through a
# lock — otherwise lines from different agents interleave mid-line
_print_lock = threading.Lock()
_state_lock = threading.Lock()


def say(msg: str, color: str = "") -> None:
    with _print_lock:
        print(f"{C.get(color, '')}{msg}{C['off']}", flush=True)


class Emitter:
    """Tags every line with its repo so parallel output stays readable."""

    def __init__(self, tag: str, logfile: Path | None = None):
        self.tag = tag
        self.log = logfile.open("w") if logfile else None

    def __call__(self, msg: str, color: str = "") -> None:
        with _print_lock:
            print(f"{C['dim']}{self.tag}{C['off']} "
                  f"{C.get(color, '')}{msg}{C['off']}", flush=True)
        if self.log:
            self.log.write(f"{msg}\n")
            self.log.flush()

    def close(self) -> None:
        if self.log:
            self.log.close()


# --------------------------------------------------------------------------
# repo discovery
# --------------------------------------------------------------------------

@dataclass
class Repo:
    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def workdir(self) -> Path:
        return WORK / self.name

    def git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", *args], cwd=self.path,
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed in {self.name}:\n{out.stderr.strip()}")
        return out.stdout.strip()

    @property
    def branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD")

    @property
    def slug(self) -> str:
        """owner/repo, parsed from origin."""
        url = self.git("remote", "get-url", "origin")
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        return m.group(1) if m else url

    @property
    def pr_number(self) -> str | None:
        # a folder named for nothing but the number is the plainest shape of all
        if self.name.isdigit():
            return self.name
        m = PR_NUM_RE.search(self.name)
        return m.group(1) if m else None

    def is_dirty(self) -> bool:
        return bool(self.git("status", "--porcelain"))

    def ensure_local_ignore(self) -> None:
        """Keep TASK.md out of git entirely — it's our input, not deliverable.

        Written to .git/info/exclude so it stays local and never appears in a
        commit or in the dirty check.
        """
        exclude = self.path / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text() if exclude.exists() else ""
        if "TASK.md" in current:
            return
        with exclude.open("a") as f:
            f.write("\n# added by run.py — local batch-runner input, never commit\nTASK.md\n")


def discover() -> list[Repo]:
    repos = [
        Repo(p) for p in sorted(ROOT.iterdir())
        if p.is_dir() and not p.name.startswith(".") and (p / ".git").exists()
    ]
    for r in repos:
        r.ensure_local_ignore()  # so TASK.md never counts as a dirty tree
    return repos


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    WORK.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def mark(repo: Repo, status: str, **extra) -> None:
    # read-modify-write, so it must be serialised across worker threads
    with _state_lock:
        state = load_state()
        entry = state.get(repo.name, {})
        entry.update(status=status, updated=time.strftime("%Y-%m-%d %H:%M:%S"), **extra)
        state[repo.name] = entry
        save_state(state)


def status_of(repo: Repo) -> str:
    return load_state().get(repo.name, {}).get("status", "new")


# Transient run artefacts: the raw event stream, the prompt we sent, the tagged
# log. Useful while a run is happening, landfill afterwards — and the event
# streams are what make .runner huge.
TRANSIENT = ("*events.jsonl", "*prompt.txt", "*log.txt")


def prune(*dirs: Path, keep: bool = False) -> int:
    """Delete transient artefacts. Returns bytes reclaimed."""
    if keep:
        return 0
    freed = 0
    for d in dirs:
        if not d.is_dir():
            continue
        for pattern in TRANSIENT:
            for f in d.glob(pattern):
                try:
                    freed += f.stat().st_size
                    f.unlink()
                except OSError:
                    pass
    return freed


# Results worth keeping across a normal `clean`: diffs, agent summaries, score
# and analysis output. `clean --results` is what removes these.
RESULTS = ("*result.json", "*.diff", "last-message.md", "synthesis.json", "report.md")


def dir_size(d: Path) -> int:
    if not d.is_dir():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def wipe(d: Path) -> int:
    """Delete a whole work directory. Returns bytes reclaimed."""
    size = dir_size(d)
    shutil.rmtree(d, ignore_errors=True)
    return 0 if d.exists() else size


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


# --------------------------------------------------------------------------
# PR context
# --------------------------------------------------------------------------

def have_gh() -> bool:
    return shutil.which("gh") is not None


def post_pr_comment(repo: Repo, body: str) -> tuple[bool, str]:
    """Post a comment on the repo's PR. Returns (ok, detail)."""
    if not have_gh():
        return False, "gh is not installed"
    if not repo.pr_number:
        return False, "no PR number in the folder name"
    proc = subprocess.run(
        ["gh", "pr", "comment", repo.pr_number, "--repo", repo.slug, "--body", body],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return True, proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "posted"
    err = proc.stderr.strip()
    return False, err.splitlines()[-1] if err else f"gh exited {proc.returncode}"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def last_push_time(repo: Repo) -> datetime | None:
    """When we last pushed this repo — the cutoff for 'new' comments.

    Prefers the timestamp recorded by `push`, whatever status label the run
    ended on: `no-changes` and `review-requested` runs push too, and reading
    only `pushed` rows silently skipped half the repos. Falls back to the last
    commit on the branch, which covers repos pushed before that was recorded
    (and repos you pushed by hand).
    """
    entry = load_state().get(repo.name, {})
    if recorded := parse_ts(entry.get("pushed_at")):
        return recorded
    try:
        return parse_ts(repo.git("log", "-1", "--format=%cI"))
    except RuntimeError:
        return None


def ago(when: datetime | None) -> str:
    """How long ago, in one short cell: `4m`, `2h`, `3d`."""
    if when is None:
        return "—"
    secs = (datetime.now(timezone.utc) - when).total_seconds()
    if secs < 0:
        return "0m"
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 90 * 60:
        return f"{secs / 60:.0f}m"
    if secs < 48 * 3600:
        return f"{secs / 3600:.0f}h"
    return f"{secs / 86400:.0f}d"


# Every bot result names the commit it ran against — "Pinned PR head:
# `475300a7ae46…`". That SHA is the only exact answer to "has this verdict seen
# my latest work?"; comparing timestamps cannot, because the bot's own commits
# move the head too.
PINNED_HEAD_RE = re.compile(r"Pinned PR head:\s*`?([0-9a-f]{7,40})`?", re.IGNORECASE)

# After a rescore the bot pushes its refreshed verifier artefacts onto the
# branch. That moves the PR head without invalidating anything, so those
# commits are not "work the bot has not seen" — only ours are.
BOT_COMMIT_RE = re.compile(r"^chore\((?:rescore|reverify|verifier)\)", re.IGNORECASE)


def pinned_head(body: str) -> str | None:
    """The commit a bot result was produced against, if it says."""
    m = PINNED_HEAD_RE.search(body or "")
    return m.group(1).lower() if m else None


def commit_history(data: dict) -> tuple[list[str], str | None, datetime | None]:
    """(every commit oldest-first, the newest one that is ours, when it landed)."""
    oids: list[str] = []
    own, when = None, None
    for c in data.get("commits") or []:
        oid = (c.get("oid") or "").lower()
        if not oid:
            continue
        oids.append(oid)
        if not BOT_COMMIT_RE.match(c.get("messageHeadline") or ""):
            own, when = oid, parse_ts(c.get("committedDate"))
    return oids, own, when


def edited_since_submission(data: dict) -> bool:
    """Whether anyone has changed the task since the contributor submitted it.

    The contributor's own commits arrive with no GitHub account linked to them,
    so a login on a commit means somebody worked on the PR afterwards — us, or
    a teammate on the same batch, which is just as much a reason not to treat
    the task as untouched. The bot's `chore(rescore)` commits are not edits.

    A contributor whose commits *were* account-linked would read as edited,
    which only costs a rescore; reading an edited task as untouched would
    straight-fail work somebody had already started.
    """
    for c in data.get("commits") or []:
        head = (c.get("messageHeadline") or "").strip()
        if BOT_COMMIT_RE.match(head):
            continue
        login = ((c.get("authors") or [{}])[0] or {}).get("login") or ""
        if login or head == COMMIT_MESSAGE:
            return True
    return False


def ran_before_our_work(pinned: str | None, oids: list[str],
                        own: str | None) -> bool | None:
    """Whether a verdict was produced before our newest commit.

    True means the bot has not seen the current task yet, so its result says
    nothing about what is on the branch now. None means unknowable — an older
    comment format that names no head, or a head that has since been
    force-pushed away — and an unknown is never reported as fresh.
    """
    if not pinned or not own or not oids:
        return None

    def index(sha: str) -> int | None:
        for i, oid in enumerate(oids):
            if oid.startswith(sha) or sha.startswith(oid):
                return i
        return None

    at, ours = index(pinned), index(own)
    if at is None or ours is None:
        return None
    return at < ours


ACK_MARKER = "github-review-bot:ack"
TRACE_RUN_STARTED = "Trace Run Started"
# a finished trace run replaces the start notice; until one of these lands the
# job is still queued or running
TRACE_RUN_DONE = ("trace run complete", "trace run finished", "trace run failed",
                  "trace run results", "trace run cancelled")
# which queued job an ack announces, and the result that would retire it
ACK_KINDS = (("rescore", "rescore"), ("qc phases", "qc"), ("qc check", "qc"),
             ("fairness", "fairness"))

# How long a queued job may plausibly take before silence means it was lost
# rather than slow. QC and rescore normally post within five minutes; a trace
# run is oracle + nop + five solver trials and queues behind everything else,
# so it gets most of a working day before we call it dead.
JOB_PATIENCE_MIN = {"qc": 30, "rescore": 30, "fairness": 30, "trace run": 6 * 60}


def job_overdue(kind: str, when: datetime) -> bool:
    """Whether a queued job has been silent long enough to count as lost.

    Waiting is only the right answer while the bot is actually coming back.
    A row parked on `wait` for a job that died is the same stale-data problem
    in a different costume.
    """
    limit = JOB_PATIENCE_MIN.get(kind, 30)
    return (datetime.now(timezone.utc) - when).total_seconds() > limit * 60


def running_jobs(data: dict, latest: dict[str, datetime | None]) -> list[tuple[str, datetime]]:
    """Bot jobs queued or running right now, newest first.

    An ack ("📊 Rescore queued …") means a result is on its way. Until it
    lands, the previous result is the *previous* one — reporting it as the
    current state, with no sign that it is about to be replaced, is how this
    table went stale without saying so.
    """
    jobs: list[tuple[str, datetime]] = []
    comments = data.get("comments") or []
    for c in comments:
        body, when = c.get("body") or "", parse_ts(c.get("createdAt"))
        if when is None:
            continue
        if ACK_MARKER in body:
            low = body.lower()
            kind = next((k for phrase, k in ACK_KINDS if phrase in low), None)
            if kind is None:
                continue
            result = latest.get(kind)
            if result is None or result < when:
                jobs.append((kind, when))
        elif TRACE_RUN_STARTED in body:
            later = [(c2.get("body") or "").lower() for c2 in comments
                     if (c2.get("createdAt") or "") > (c.get("createdAt") or "")]
            if not any(phrase in text for text in later for phrase in TRACE_RUN_DONE):
                jobs.append(("trace run", when))
    return sorted(jobs, key=lambda j: j[1], reverse=True)


def latest_fairness_review(data: dict) -> dict | None:
    """The newest legacy fairness verdict. Deprecated — QC is the standard now,
    but these comments are still on open PRs, so they stay readable.

    The thread is full of noise around it: the bot's command help, the
    `/bot ...` commands we post ourselves, "Fairness Review Started" progress
    pings, and infrastructure-failure notices that explicitly say they are not
    a quality verdict. A PR can also carry more than one verdict when the bot
    has re-run, so the newest one wins.
    """
    return hits[-1] if (hits := fairness_reviews(data)) else None


def fairness_reviews(data: dict) -> list[dict]:
    """Every fairness verdict on the PR, oldest first."""
    hits = [c for c in (data.get("comments") or [])
            if is_review_comment(c.get("body") or "")]
    return sorted(hits, key=lambda c: c.get("createdAt") or "")


def earlier_rounds(data: dict, kind: str, newest: dict) -> str:
    """Every earlier round of the same reviewer, each one whole.

    The agent is otherwise sent only the newest review, which is right for
    *what* to fix and wrong for *how*: it hides that the same complaint has
    already come back four or five times, so each round tries the previous
    round's answer again. Nothing is summarised — which round said what, and in
    what words, is exactly the detail that shows a remedy was already tried.
    """
    every = qc_checks(data) if kind == "QC check" else fairness_reviews(data)
    stamp = newest.get("createdAt") or ""
    prior = [c for c in every if (c.get("createdAt") or "") < stamp]
    if not prior:
        return ""

    blocks = []
    for i, c in enumerate(prior, 1):
        when = (c.get("createdAt") or "")[:16].replace("T", " ")
        blocks += ["", f"#### Round {i} of {len(prior)} — {when}", "",
                   (c.get("body") or "").strip()]

    return "\n".join([
        "",
        "",
        f"### Earlier {kind} rounds on this task ({len(prior)} before the one above)",
        "",
        "**These are history, not your task list.** The review above is what is",
        "outstanding; work that. These are here for one reason: a complaint that",
        "keeps coming back is one the earlier rounds did not actually answer, so",
        "whatever they tried is known not to work. Read them to avoid repeating a",
        "failed remedy, and do not go back and re-fix a finding that no longer",
        "appears in the current review — it has already been dealt with.",
        *blocks,
    ])


def fetch_pr_context(repo: Repo, emit, since: datetime | None = None,
                     fairness_only: bool = True) -> tuple[str, str]:
    """Return (context_text, source).

    By default only the newest review is sent. When `since` is set the
    verdict must also be newer than that — otherwise the bot has not re-reviewed
    the last push yet and there is nothing new to act on.

    Falls back to a local TASK.md when GitHub can't be read.
    """
    local = repo.path / "TASK.md"

    if have_gh() and repo.pr_number:
        proc = subprocess.run(
            ["gh", "pr", "view", repo.pr_number, "--repo", repo.slug,
             "--json", "title,body,url,comments,reviews"],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout)

            if fairness_only:
                # Both reviews gate, so the agent gets both — each whole, and
                # only the newest of each. An older one may have been answered
                # already, and re-sending it invites the agent to redo work or
                # chase findings that no longer hold.
                qc, fair = latest_qc_check(data), latest_fairness_review(data)
                # one combined comment can be both; do not send it twice
                if qc and fair and qc.get("body") == fair.get("body"):
                    fair = None

                parts, sources, withheld = [], [], []
                for review, kind in ((qc, "QC check"), (fair, "fairness review")):
                    if review is None:
                        withheld.append((kind, "has never run on this task"))
                        continue
                    when = parse_ts(review.get("createdAt"))
                    if since and when and when <= since:
                        # the bot has not re-run this one since our push, so it
                        # describes an earlier commit
                        withheld.append((kind, "has not re-run since your last "
                                               "push, so it describes an earlier "
                                               "commit"))
                        continue
                    text = review.get("body") or ""
                    if glance(text):
                        # the v3 shape: a fairness review, or one combined
                        # comment already carrying both halves
                        heading = next((l for l in text.splitlines()
                                        if l.startswith("## ")), "")
                        rendered = render_verdict(data, review)
                        sources.append(kind if STANDALONE_FAIRNESS in heading.lower()
                                       else "combined review")
                    else:
                        rendered = render_review(data, review, kind)
                        sources.append(kind)
                    parts.append(rendered + earlier_rounds(data, kind, review))

                if parts:
                    # both reviews gate, so a missing half has to be said out
                    # loud — silence reads as "that one passed"
                    if withheld:
                        parts.append("\n".join([
                            "### One half of the review is missing", "",
                            *(f"- The **{kind}** {why}." for kind, why in withheld),
                            "",
                            "Both reviews have to pass. Work what is above; do",
                            "not assume the missing half is clean, and do not",
                            "undo anything to satisfy a verdict you cannot see.",
                        ]))
                        sources.append(f"{withheld[0][0]} withheld")
                    return "\n\n---\n\n".join(parts), " + ".join(sources)
                if withheld:
                    return "", "stale-verdict"
                emit("no review on this PR yet — using the full thread", "yel")

            text, kept = render_pr(data, since)
            if kept == 0:
                # nothing but pipeline chatter; running an agent on a command
                # listing wastes a round and invites invented work
                return ("", "no-new-comments" if since else "none")
            return text, "github (new only)" if since else "github"

        err = proc.stderr.strip()
        emit(f"gh could not read PR #{repo.pr_number}: "
             f"{err.splitlines()[-1] if err else 'unknown error'}", "yel")

    if local.exists():
        return local.read_text().strip(), "TASK.md"

    return "", "none"


AUDIT_NOTE = """\
NOTE ON THIS FORMAT — the audit below calls itself "non-gating" and says it does
not accept, reject or fail the task. That describes their process, not yours.
Treat every finding marked `confirmed` as work for this round, and check the
`probable` ones. Each finding names the affected criteria, the weight at stake,
and usually a suggested repair — use them.
"""

COMBINED_NOTE = """\
NOTE ON THIS FORMAT — this is one review covering the fairness check and both QC
phases. It calls itself advisory and non-gating; that describes their process,
not yours. Work the numbered items under "What needs attention", use each
"Suggested fix", and start with the ones tagged `confirmed`.

One exception. Items reporting that the corpus archive is a Git LFS pointer
rather than the full WARC are NOT defects — that is the staging design, as the
corpus rule above says. Do not act on them, do not regenerate a corpus, and do
not edit the pointer. Say in your final message that you left them, and why.
"""

FAIRNESS_NOTE = """\
NOTE ON THIS FORMAT — this is the fairness half of the review, from `/bot2
fairness-review`. It asks a different question from the QC check: whether the
task can be solved as written from what the solver can actually see, rather
than how the bundle is put together. Both gate, so this one is work too.

It calls itself advisory and non-gating; that describes their process, not
yours. Work the numbered items under "What needs attention", use each
"Suggested fix", and start with the ones tagged `confirmed`. A heading of
"review advised" still means there is something here to answer.

One exception. Items reporting that the corpus archive is a Git LFS pointer
rather than the full WARC are NOT defects — that is the staging design, as the
corpus rule above says. Do not act on them, do not regenerate a corpus, and do
not edit the pointer. Say in your final message that you left them, and why.
"""


def render_review(data: dict, comment: dict, kind: str) -> str:
    """A standalone QC result, sent whole.

    Nothing is summarised, folded or capped. The per-criterion tables look
    repetitive — the same finding restated for every criterion it touches —
    but the rows are not interchangeable: on #2562 the ones that named the
    actual ID mapping (`fact-claim01` is staged as `fact-c001`) sat at the
    bottom of a 52-row table, so any cap on the first N rows sent ten copies
    of one finding and dropped the mapping the agent needed. There is no
    reliable way to tell in advance which row carries the detail that matters,
    so every row goes. The largest of these comments is about 25k characters,
    which is a small part of the prompt and a much smaller risk than guessing
    wrong about what to leave out.
    """
    who = (comment.get("author") or {}).get("login", "unknown")
    return "\n".join([
        f"# {data.get('title', '(no title)')}",
        "",
        f"## {kind} by @{who} — {comment.get('createdAt', '')}",
        "",
        "Its five dimensions are prompt quality, rubric accuracy, test",
        "correctness, coverage & balance, and task adequacy. Fix what it marks",
        "with a warning or a cross.",
        "",
        (comment.get("body") or "").strip(),
    ]).strip()


def render_verdict(data: dict, verdict: dict) -> str:
    who = (verdict.get("author") or {}).get("login", "unknown")
    body = (verdict.get("body") or "").strip()
    if glance(body):
        # any of the v3 headings — take the bot's own, so the agent sees whether
        # this covered fairness alone or fairness plus both QC phases
        heading = next((l.lstrip("# ").strip() for l in body.splitlines()
                        if l.startswith("## ")), "Task review")
        if STANDALONE_FAIRNESS in heading.lower():
            # fairness only. The combined note would tell the agent this
            # covers both QC phases, which it does not.
            kind, note = "fairness review", FAIRNESS_NOTE
        else:
            kind, note = oneline(heading), COMBINED_NOTE
    elif "Task Fairness Audit" in body:
        kind, note = "Task Fairness Audit", AUDIT_NOTE
    else:
        kind, note = "Task Fairness Review", ""
    return "\n".join([
        f"# {data.get('title', '(no title)')}",
        "",
        f"## {kind} by @{who} — {verdict.get('createdAt', '')}",
        "",
        note,
        body,
    ]).strip()


# Comments that are chatter about the pipeline rather than feedback on the task.
# The thread is mostly these: a 6KB command listing, queue acknowledgements,
# capacity notices, and infrastructure failures that say so themselves.
NOISE_MARKERS = (
    "github-review-bot:ack:",
    "github-review-bot:capacity-warning:",
    "auditrobot:offline-pr-audit",
    "## TerminalBench Bot Commands",
    "## Bot Commands",
    "Review Infrastructure Failure",
    "not found.\n\nIt may have completed or never existed",
)


def is_noise(body: str) -> bool:
    """Whether a comment carries no task feedback worth sending to an agent."""
    text = (body or "").strip()
    if not text:
        return True
    if any(m in text for m in NOISE_MARKERS):
        return True
    # our own bot commands, e.g. "/bot2 fairness-review"
    return bool(re.fullmatch(r"/\w[\w-]*(\s+[\w./-]+)*", text))


def render_pr(data: dict, since: datetime | None = None) -> tuple[str, int]:
    """Render PR discussion, dropping pipeline chatter.

    Returns (text, number_of_items_kept). `kept` counts only real feedback, so a
    thread of nothing but bot noise reports no context rather than starting an
    agent on a command listing.
    """
    parts = [f"# {data.get('title', '(no title)')}", ""]

    # on a follow-up round the original description is stale context, and
    # re-sending it invites the agent to redo work it already pushed
    if not since:
        if body := (data.get("body") or "").strip():
            parts += ["## Pull request description", "", body, ""]

    kept = 0

    for rev in data.get("reviews") or []:
        text = (rev.get("body") or "").strip()
        when = parse_ts(rev.get("submittedAt"))
        if not text or (since and (not when or when <= since)):
            continue
        who = (rev.get("author") or {}).get("login", "unknown")
        stamp = f" — {rev.get('submittedAt')}" if since else ""
        parts += [f"## Review by @{who} ({rev.get('state', '')}){stamp}", "", text, ""]
        kept += 1

    for com in data.get("comments") or []:
        text = (com.get("body") or "").strip()
        when = parse_ts(com.get("createdAt"))
        if not text or is_noise(text) or (since and (not when or when <= since)):
            continue
        who = (com.get("author") or {}).get("login", "unknown")
        stamp = f" — {com.get('createdAt')}" if since else ""
        parts += [f"## Comment by @{who}{stamp}", "", text, ""]
        kept += 1

    return "\n".join(parts).strip(), kept


FOLLOW_UP_NOTE = """\
IMPORTANT — this is a FOLLOW-UP round, not the first pass.

An earlier fix for this pull request was already committed and pushed, and the
reviewers have since re-checked it.

**What is outstanding is the newest review from each reviewer**, at the top of
each section below. That is your task list. Under it sits every earlier round
from the same reviewer, in full and clearly marked as history — those are there
so you can see what previous rounds already tried, because a complaint that
keeps coming back is one that whatever was tried did not answer.

The earlier work is already in git history — do not redo it, do not revert it,
and do not re-litigate a finding that no longer appears in the newest review.
"""


def format_rules(repo: Repo) -> str:
    """The rule block injected into the fix prompt."""
    results, fails = rules_summary(repo)
    lines = [("FAIL" if not r["ok"] else "ok  ") + f"  {r['rule']} — {r['detail']}"
             for r in results]
    block = "\n".join(lines)
    if fails:
        block += (f"\n\n{len(fails)} check(s) FAIL. Fixing them is part of this "
                  "task, whether or not the reviewer mentioned them.")
    else:
        block += "\n\nAll automated checks pass — do not regress them."
    return block


def guidance(path: Path, what: str) -> str:
    """A standalone guidance file, injected verbatim into the fix prompt.

    Passed as a format *value*, never spliced into the template, so any JSON
    braces inside it are never re-interpreted by str.format. A missing file
    degrades to a note rather than breaking the run.
    """
    if not path.exists():
        return f"(no {what} is installed — skip this section)"
    return path.read_text().strip()


def bundle_facts(repo: Repo) -> str:
    """Where everything is and what is in the rubric, computed up front.

    An agent that is not handed this spends its first dozen round trips finding
    the bundle, locating the tier and contract files, and paging through
    rubrics.json to learn what the criteria are. All of it is cheap to compute
    here and saves round trips that cost real time.
    """
    bundle = task_bundle(repo)
    if bundle is None:
        return "(no task bundle found under contributor_tasks/)"

    rel = bundle.relative_to(repo.path)
    lines = [f"Bundle: `{rel}`", "", "Files that exist (paths are repo-relative):"]

    for name in ("instruction.md", "tests/rubrics.json", "tests/instruction.md",
                 "tests/source_tiers.txt", "tests/source_tier.txt",
                 "tests/test_outputs.py", "tests/reference/ground_truth.json",
                 "tests/reference/pre-push-contract.json",
                 "tests/reference/claim_contract.json",
                 "solution/evidence_graph.json", "solution/report.md",
                 "solution/solve.sh", "environment/corpus/corpus_manifest.json"):
        path = bundle / name
        if not path.exists():
            continue
        try:
            n = len(path.read_bytes().splitlines())
            size = path.stat().st_size
            note = f"{n} lines, {human(size)}"
        except OSError:
            note = "unreadable"
        lines.append(f"  {rel / name}  ({note})")

    rubric_path = bundle / "tests" / "rubrics.json"
    if not rubric_path.exists():
        return "\n".join(lines)

    try:
        crit = (json.loads(rubric_path.read_text()).get("criteria")) or []
    except (OSError, json.JSONDecodeError, AttributeError):
        return "\n".join(lines + ["", "(rubrics.json could not be parsed)"])

    pos = [c for c in crit if (c.get("weight") or 0) > 0]
    neg = [c for c in crit if (c.get("weight") or 0) < 0]
    lines += [
        "",
        f"Rubric: {len(crit)} criteria — {len(pos)} positive totalling "
        f"{sum(c['weight'] for c in pos):g}, {len(neg)} negative totalling "
        f"{sum(c['weight'] for c in neg):g}.",
        "",
        "Criterion index — id, axis, weight, error category. Use it to open the "
        "one you need instead of reading the whole file:",
    ]
    for c in crit:
        cid = c.get("id", "?")
        axis = c.get("axis", "?")
        weight = c.get("weight", 0)
        cat = c.get("error_category") or ""
        lines.append(f"  {weight:>+5g}  {axis:<30} {cid}"
                     + (f"  [{cat}]" if cat else ""))

    return "\n".join(lines)


def build_prompt(repo: Repo, pr_context: str, follow_up: bool = False) -> str:
    template = PROMPT_TEMPLATE.read_text()
    pr_number = repo.pr_number or "?"
    url = f"https://github.com/{repo.slug}/pull/{pr_number}" if repo.pr_number else ""
    return template.format(
        repo_slug=repo.slug,
        branch=repo.branch,
        pr_number=pr_number,
        pr_url=url,
        round_note=FOLLOW_UP_NOTE if follow_up else "",
        rules=format_rules(repo),
        bundle_facts=bundle_facts(repo),
        qc_guidelines=guidance(QC_TEMPLATE, "QC guideline set"),
        rubric_calibration=guidance(CALIBRATION_TEMPLATE, "rubric-calibration standard"),
        pr_context=pr_context or "(no discussion could be retrieved)",
    )


# --------------------------------------------------------------------------
# running the agent
# --------------------------------------------------------------------------

TIMED_OUT = 124  # conventional exit code for "killed by a time limit"


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the agent's whole process group, escalating if it ignores TERM.

    codx is a bash wrapper that spawns a subshell and node, so signalling the
    pid we hold leaves the real agent running. The group is what has to go —
    which is why the process is started in a session of its own.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def run_agent(repo: Repo, prompt: str, sandbox: str, network: bool,
              emit, timeout: int = 0) -> tuple[int, str]:
    repo.workdir.mkdir(parents=True, exist_ok=True)
    (repo.workdir / "prompt.txt").write_text(prompt)
    events_path = repo.workdir / "events.jsonl"
    last_msg = repo.workdir / "last-message.md"

    cmd = [CODX, "exec", "-C", str(repo.path), "-s", sandbox, "--json",
           "-o", str(last_msg)]
    if network and sandbox == "workspace-write":
        cmd += ["-c", "sandbox_workspace_write.network_access=true"]
    cmd.append("-")  # prompt arrives on stdin

    emit(f"running: codx exec -s {sandbox}{' +network' if network else ''}"
         + (f", {timeout // 60}m limit" if timeout else ""), "dim")

    captured: list[str] = []
    timed_out = threading.Event()

    with events_path.open("w") as events:
        proc = subprocess.Popen(
            cmd, cwd=repo.path,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            start_new_session=True,   # its own process group, so kill_tree works
        )

        # stderr is a pipe like any other: a process that fills it while we are
        # busy reading stdout blocks forever, and so do we. Drain it in parallel.
        drain = threading.Thread(
            target=lambda: captured.append(proc.stderr.read() or ""), daemon=True)
        drain.start()

        def give_up() -> None:
            timed_out.set()
            emit(f"no result after {timeout // 60}m — killing the agent", "red")
            kill_tree(proc)

        alarm = threading.Timer(timeout, give_up) if timeout else None
        if alarm:
            alarm.start()

        try:
            proc.stdin.write(prompt)
            proc.stdin.close()

            for line in proc.stdout:
                events.write(line)
                summarise_event(line, emit)

            proc.wait()
        finally:
            if alarm:
                alarm.cancel()
            drain.join(timeout=5)

    if timed_out.is_set():
        return TIMED_OUT, f"no result after {timeout // 60}m — agent killed"

    stderr = ("".join(captured)).strip()
    # the codx wrapper always prints a keepalive teardown line; not an error
    stderr = "\n".join(
        l for l in stderr.splitlines() if "assignment_keepalive_loop" not in l
    ).strip()

    return proc.returncode, stderr


SHELL_WRAPPER_RE = re.compile(r"""^/bin/(?:ba|z)?sh\s+-l?c\s+(['"])(.*)\1$""", re.DOTALL)


def summarise_event(line: str, emit) -> None:
    """Condense codx's JSONL thread/item stream into something readable."""
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return

    kind = ev.get("type", "")

    if kind == "error":
        emit(f"error: {ev.get('message', '')}", "red")
        return

    if kind == "turn.failed":
        err = (ev.get("error") or {}).get("message", "turn failed")
        emit(f"turn failed: {err}", "red")
        return

    if kind == "turn.completed":
        usage = ev.get("usage") or {}
        emit(f"[tokens in={usage.get('input_tokens', 0)} "
             f"out={usage.get('output_tokens', 0)}]", "dim")
        return

    if not kind.startswith("item."):
        return

    item = ev.get("item") or {}
    itype = item.get("type", "")

    # commands are announced on item.started so you see them as they run;
    # everything else reads better once complete
    if itype == "command_execution":
        if kind == "item.started":
            cmd = item.get("command", "")
            if m := SHELL_WRAPPER_RE.match(cmd):
                cmd = m.group(2)
            emit(f"$ {oneline(cmd)[:120]}", "blu")
        elif kind == "item.completed" and item.get("exit_code") not in (0, None):
            emit(f"  exited {item.get('exit_code')}", "yel")
        return

    if kind != "item.completed":
        return

    if itype == "agent_message":
        if text := (item.get("text") or "").strip():
            emit(oneline(text)[:300], "dim")
    elif itype == "file_change":
        for change in item.get("changes") or []:
            path = change.get("path") if isinstance(change, dict) else change
            kindname = change.get("kind", "edit") if isinstance(change, dict) else "edit"
            emit(f"{kindname} {Path(str(path)).name}", "grn")
    elif itype == "error":
        emit(f"error: {item.get('message', '')}", "red")


def oneline(text: str) -> str:
    """Collapse to a single line — multi-line output wrecks tagged parallel logs."""
    return " ".join(text.split())


def agent_conclusion(repo: Repo, limit: int = 160) -> str:
    """First sentence of the agent's closing message — why it did what it did."""
    path = repo.workdir / "last-message.md"
    if not path.exists():
        return ""
    text = " ".join(path.read_text().split())
    return first_sentence(text, limit) if text else ""


def capture_diff(repo: Repo) -> str:
    """Diff including untracked files, without staging any content."""
    subprocess.run(["git", "add", "-N", "."], cwd=repo.path,
                   capture_output=True, text=True)
    out = subprocess.run(["git", "--no-pager", "diff"], cwd=repo.path,
                         capture_output=True, text=True)
    diff = out.stdout
    (repo.workdir / "changes.diff").write_text(diff)
    return diff


def backup_working_tree(repo: Repo) -> Path:
    """Save the current diff before discarding it, so a round is never lost."""
    repo.workdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "add", "-N", "."], cwd=repo.path,
                   capture_output=True, text=True)
    out = subprocess.run(["git", "--no-pager", "diff"], cwd=repo.path,
                         capture_output=True, text=True)
    path = repo.workdir / f"discarded-{time.strftime('%Y%m%d-%H%M%S')}.diff"
    path.write_text(out.stdout)
    return path


def restore_repo(repo: Repo, backup: bool = True) -> str:
    """Put a checkout back to HEAD: discard edits and untracked files.

    The diff is saved first, so a round is recoverable even when the tree it
    came from is gone. Ignored files (TASK.md via .git/info/exclude) survive —
    `git clean` is deliberately run without -x.
    """
    saved = backup_working_tree(repo) if backup else None
    subprocess.run(["git", "reset", "--hard"], cwd=repo.path,
                   capture_output=True, text=True)
    subprocess.run(["git", "clean", "-fd"], cwd=repo.path,
                   capture_output=True, text=True)
    return f"restored to HEAD (diff saved to {saved.name})" if saved else "restored to HEAD"


def touched_traces(repo: Repo) -> list[str]:
    """Recorded-run files the agent modified — it should never touch these."""
    out = subprocess.run(["git", "diff", "--name-only"], cwd=repo.path,
                         capture_output=True, text=True)
    return [f for f in out.stdout.splitlines()
            if f.startswith("trace/") or f.startswith("traces/")]


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def select(repos: list[Repo], args) -> list[Repo]:
    """Repos named by positional args and/or --task. Empty selection = all."""
    # both forms accept commas, so `fix 1392,1393` and `fix --task 1392,1393`
    # behave the same — a comma is never part of a repo folder name
    patterns: list[str] = []
    for chunk in (list(getattr(args, "only", None) or [])
                  + list(getattr(args, "task", None) or [])):
        patterns += [p.strip() for p in chunk.split(",") if p.strip()]

    if not patterns:
        return repos

    chosen: dict[str, Repo] = {}
    for pattern in patterns:
        # a bare number means the PR number; anything else is a name substring
        key = pattern.lstrip("#").removeprefix("pr").lstrip("-")
        if key.isdigit():
            hits = [r for r in repos if r.pr_number == key]
        else:
            hits = [r for r in repos if pattern in r.name]
        if not hits:
            say(f"no repo matches {pattern!r}", "red")
            sys.exit(1)
        chosen.update({r.name: r for r in hits})
    return list(chosen.values())


def cmd_list(args) -> None:
    repos = discover()
    if not repos:
        say("no git checkouts found here", "yel")
        return

    say(f"{len(repos)} repo(s) in {ROOT}\n", "bld")
    colors = {"new": "", "fixed": "yel", "pushed": "grn", "failed": "red", "no-context": "dim"}
    for r in repos:
        st = status_of(r)
        dirty = " (working tree dirty)" if r.is_dirty() else ""
        say(f"  {st:<11} PR #{r.pr_number or '?':<6} {r.name}{dirty}", colors.get(st, ""))

    if not have_gh():
        say("\ngh is not installed — PR comments can't be fetched and the bot", "yel")
        say("comment can't be posted. Fix with:  brew install gh && gh auth login", "yel")
        say("Until then, drop a TASK.md in a repo folder with the comment pasted in.", "yel")


def fix_one(repo: Repo, args, width: int) -> tuple[str, str]:
    """Process a single repo. Returns (status, detail). Runs in a worker thread."""
    repo.workdir.mkdir(parents=True, exist_ok=True)
    tag = f"#{repo.pr_number or '?':<{width}} |"
    emit = Emitter(tag, repo.workdir / "log.txt")

    try:
        repo.ensure_local_ignore()

        if repo.is_dirty():
            n = len(repo.git("status", "--porcelain").splitlines())
            # an explicitly named repo runs regardless; the diff is saved first
            # so a discarded round is always recoverable
            if args.dry_run:
                emit(f"{n} uncommitted file(s) would be discarded (dry run: "
                     "leaving them alone)", "yel")
            elif args.discard_dirty or getattr(args, "explicit", False):
                backup = backup_working_tree(repo)
                emit(f"discarding {n} uncommitted file(s); saved to {backup.name}", "yel")
                repo.git("reset", "--hard", "HEAD")
            else:
                # usually left behind by a run that died partway (a proxy drop
                # mid-stream will do it), and it blocks every later attempt
                emit(f"working tree has {n} uncommitted file(s) — skipping", "yel")
                emit("  keep them:    git -C <repo> stash", "dim")
                emit("  discard them: python3 run.py fix --task "
                     f"{repo.pr_number} --discard-dirty", "dim")
                return "skipped", f"{n} uncommitted file(s) block this repo"

        # a repo we've already pushed gets only the comments posted since,
        # unless --all-comments asks for the whole thread again
        since = None if args.all_comments else last_push_time(repo)
        if since:
            emit(f"follow-up round — comments since {since:%Y-%m-%d %H:%M} UTC", "blu")

        pr_context, source = fetch_pr_context(repo, emit, since,
                                              fairness_only=not args.all_comments)
        if source == "stale-verdict":
            emit("the review predates the last push — bot hasn't "
                 "re-reviewed yet", "yel")
            return "waiting", "no new review since last push"
        if source == "no-new-comments":
            emit("no new comments since the last push — nothing to do", "grn")
            return "up-to-date", "no new comments since last push"
        if not pr_context:
            emit("no PR comments and no TASK.md — skipping", "yel")
            mark(repo, "no-context")
            return "no-context", "no PR comments or TASK.md"
        emit(f"context: {source} ({len(pr_context)} chars)", "dim")

        prompt = build_prompt(repo, pr_context, follow_up=bool(since))

        if args.dry_run:
            repo.workdir.mkdir(parents=True, exist_ok=True)
            (repo.workdir / "prompt.txt").write_text(prompt)
            emit(f"dry run — prompt written to {repo.workdir / 'prompt.txt'}", "blu")
            return "dry-run", f"{len(prompt)} char prompt, agent not started"

        code, stderr = run_agent(repo, prompt, args.sandbox, args.network, emit,
                                 getattr(args, "timeout", 0) * 60)

        if code == TIMED_OUT:
            # a killed agent usually leaves real edits behind; keep them and say
            # so, rather than reporting failure and hiding a half-done bundle
            partial = capture_diff(repo)
            files = partial.count("\ndiff --git") + partial.startswith("diff --git")
            detail = stderr + (f", {files} file(s) already changed" if files else
                               ", working tree untouched")
            mark(repo, "timed-out", pr=repo.pr_number, files=files, note=stderr)
            return "timed-out", detail

        if code != 0:
            emit(f"codx exited {code}", "red")
            detail = stderr.splitlines()[-1] if stderr else f"exit {code}"
            if stderr:
                emit(detail, "red")
            mark(repo, "failed", exit_code=code, note=detail)
            return "failed", detail

        diff = capture_diff(repo)
        if not diff.strip():
            # the agent finishing cleanly with nothing to change is a valid
            # outcome, not a failure — it usually means the feedback was
            # informational, or the issue was already fixed in an earlier round
            why = agent_conclusion(repo)
            emit("no changes needed", "yel")
            if why:
                emit(f"  {why}", "dim")
            mark(repo, "no-changes", note=why or "agent reported nothing to fix")
            return "no-changes", why or "agent found nothing to fix"

        files = diff.count("\ndiff --git") + diff.startswith("diff --git")

        # editing recorded runs falsifies the evidence the scoring reads
        if traces := touched_traces(repo):
            emit(f"WARNING: modified {len(traces)} recorded-run file(s) — "
                 "these should not be touched", "red")
            for t in traces[:5]:
                emit(f"  {t}", "red")
            emit(f"  revert with: git -C {repo.name} checkout -- "
                 + " ".join(traces[:3]) + (" ..." if len(traces) > 3 else ""), "yel")

        emit(f"done — {files} file(s) changed", "grn")
        mark(repo, "fixed", pr=repo.pr_number, files=files,
             **({"touched_traces": traces} if traces else {}))
        return "fixed", (f"{files} file(s) changed"
                         + (f" — {len(traces)} RECORDED-RUN file(s) touched" if traces else ""))

    except Exception as e:  # one repo blowing up must not kill the batch
        emit(f"unexpected error: {e}", "red")
        mark(repo, "failed", note=str(e))
        return "failed", str(e)
    finally:
        emit.close()


def cmd_fix(args) -> None:
    all_repos = discover()
    repos = select(all_repos, args)
    explicit = bool(getattr(args, "only", None) or getattr(args, "task", None))
    args.explicit = explicit

    # naming repos explicitly means you want them run, including ones already
    # pushed — that's the whole point of re-running after the bot re-reviews
    if not args.redo and not explicit:
        # "pushed" belongs here: the normal loop is push -> bot re-reviews ->
        # fix again. Those repos cost nothing when there is no new verdict —
        # they short-circuit to "waiting" before any agent starts.
        repos = [r for r in repos
                 if status_of(r) in ("new", "failed", "no-context", "pushed")]

    if not repos:
        say("nothing to do — everything is already fixed or pushed "
            "(name specific PRs with --task, or use --redo)", "grn")
        return

    # naming a task explicitly means run it — the un-pushed-changes guard only
    # protects you from a bare `fix` clobbering work you forgot about
    waiting = [r for r in repos if status_of(r) == "fixed"]
    if waiting and not args.redo and not explicit:
        for r in waiting:
            say(f"skipping #{r.pr_number}: un-pushed changes from a previous run "
                "(name it with --task to run anyway, or pass --redo)", "yel")
        repos = [r for r in repos if status_of(r) != "fixed"]
        if not repos:
            say("nothing left to run", "yel")
            return

    jobs = args.jobs if args.jobs and args.jobs > 0 else len(repos)
    jobs = min(jobs, len(repos))
    width = max(len(r.pr_number or "?") for r in repos)

    say(f"running {', '.join('#' + (r.pr_number or '?') for r in repos)} "
        f"— {jobs} at a time", "bld")
    say("lines are tagged with the PR number; full per-repo logs in "
        f"{WORK}/<repo>/log.txt\n", "dim")

    started = time.time()
    results: dict[str, tuple[str, str]] = {}

    done_colors = {"fixed": "grn", "up-to-date": "grn", "no-changes": "grn",
                   "dry-run": "blu", "failed": "red", "timed-out": "red",
                   "no-context": "yel", "skipped": "yel", "waiting": "yel"}

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(fix_one, r, args, width): r for r in repos}
        for n, fut in enumerate(as_completed(futures), 1):
            repo = futures[fut]
            status, detail = results[repo.name] = fut.result()
            # report the moment each repo lands, so a slow straggler no longer
            # hides the fact that everything else is finished and reviewable
            mins, secs = divmod(int(time.time() - started), 60)
            say(f"[{n}/{len(futures)} done, {mins}m{secs:02d}s] {status:<11} "
                f"#{repo.pr_number or '?':<{width}}  {detail}",
                done_colors.get(status, ""))
            if n < len(futures):
                pending = [futures[f].pr_number or "?" for f in futures if not f.done()]
                if len(pending) <= 3:
                    say(f"    still running: {', '.join('#' + p for p in pending)}", "dim")

    elapsed = int(time.time() - started)
    say(f"\n{'=' * 70}", "dim")
    say(f"Finished in {elapsed // 60}m {elapsed % 60}s", "bld")

    for repo in repos:
        status, detail = results.get(repo.name, ("unknown", ""))
        say(f"  {status:<11} #{repo.pr_number or '?':<{width}}  {detail}",
            done_colors.get(status, ""))

    # a dry run exists to produce the prompt for inspection — pruning it away
    # would defeat the point
    if not args.dry_run and (freed := prune(*(r.workdir for r in repos))):
        say(f"\ncleaned up {human(freed)} of intermediates", "dim")

    if any(s == "fixed" for s, _ in results.values()):
        say("\nNothing has been pushed.", "bld")
        say("  python3 run.py review      # read the diffs")
        say("  python3 run.py push        # commit, push, post the bot comment")


def unpushed_commits(repo: Repo) -> int:
    """Commits on the branch that the remote does not have yet."""
    try:
        out = repo.git("log", f"origin/{repo.branch}..HEAD", "--oneline")
    except RuntimeError:
        return 0
    return len(out.splitlines()) if out else 0


def cmd_review(args) -> None:
    # no status gating — show whatever the selected repos actually have
    repos = select(discover(), args)
    if not repos:
        say("no repos selected", "yel")
        return

    for repo in repos:
        say(f"\n{'=' * 78}", "dim")
        say(f"{repo.name}  (PR #{repo.pr_number})", "bld")
        say(f"{'=' * 78}", "dim")

        summary = repo.workdir / "last-message.md"
        if summary.exists():
            say("\n-- agent summary " + "-" * 60, "blu")
            print(summary.read_text().strip())

        # the live working tree is the truth; the saved diff is only a fallback
        # for a repo whose changes were already committed
        live = capture_diff(repo) if repo.is_dirty() else ""
        saved = repo.workdir / "changes.diff"
        if live:
            say("\n-- diff " + "-" * 69, "blu")
            print(live)
        elif saved.exists():
            say("\n-- diff (from last run; working tree is clean) " + "-" * 30, "blu")
            print(saved.read_text())
        else:
            say("\nno changes", "dim")


def sync_with_remote(repo: Repo) -> str:
    """Rebase onto the remote branch so the push fast-forwards. Returns a note.

    A rejected push here is routine, not exceptional: `/bot2 rescore` pushes
    refreshed judge artefacts to the same branch between our rounds. Those live
    under traces/, which the agent is forbidden to touch, so our commit rebases
    cleanly on top. A genuine conflict aborts and leaves the commit alone.
    """
    branch = repo.branch
    fetch = subprocess.run(["git", "fetch", "origin", branch], cwd=repo.path,
                           capture_output=True, text=True)
    if fetch.returncode != 0:
        return f"could not fetch origin/{branch} — pushing anyway"

    ref = f"origin/{branch}"
    exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                            cwd=repo.path, capture_output=True, text=True)
    if exists.returncode != 0:
        return ""  # branch is new upstream; nothing to rebase onto

    behind = int(repo.git("rev-list", "--count", f"HEAD..{ref}") or 0)
    if not behind:
        return ""

    reb = subprocess.run(["git", "rebase", ref], cwd=repo.path,
                         capture_output=True, text=True)
    note = f"rebased onto {ref} (+{behind} remote commit(s))"
    if reb.returncode == 0:
        return note

    # Classify what actually clashed. `DU` = upstream deleted a file our commit
    # edited — the layout migrations do this, dropping tests/instruction.json in
    # favour of tests/instruction.md. Resurrecting a file upstream deleted is
    # never right, so take the deletion; anything else is a real merge.
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo.path,
                            capture_output=True, text=True).stdout
    deleted_upstream, blocking = [], []
    for line in status.splitlines():
        code, path = line[:2], line[3:].strip()
        if code == "DU":
            deleted_upstream.append(path)
        elif code in ("DD", "AU", "UD", "UA", "AA", "UU"):
            blocking.append(path)

    if blocking or not deleted_upstream:
        subprocess.run(["git", "rebase", "--abort"], cwd=repo.path,
                       capture_output=True, text=True)
        listed = "\n".join(f"      {p}" for p in (blocking or ["(unknown)"])[:8])
        raise RuntimeError(
            f"rebase onto {ref} conflicts on content — your commit is intact, "
            f"resolve by hand:\n"
            f"    cd {repo.name} && git pull --rebase origin {branch}\n"
            f"    both sides edited:\n{listed}")

    # keep a copy of what we drop, so a round's work is never silently lost
    repo.workdir.mkdir(parents=True, exist_ok=True)
    saved = repo.workdir / "dropped-by-rebase"
    for path in deleted_upstream:
        source = repo.path / path
        if source.exists():
            target = saved / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        subprocess.run(["git", "rm", "-q", "--", path], cwd=repo.path,
                       capture_output=True, text=True)

    cont = subprocess.run(["git", "rebase", "--continue"], cwd=repo.path,
                          capture_output=True, text=True,
                          env={**os.environ, "GIT_EDITOR": "true"})
    if cont.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], cwd=repo.path,
                       capture_output=True, text=True)
        raise RuntimeError(
            f"rebase onto {ref} could not continue — your commit is intact:\n"
            f"    cd {repo.name} && git pull --rebase origin {branch}\n"
            + (cont.stdout or cont.stderr).strip()[:400])

    return (f"{note}; upstream deleted {len(deleted_upstream)} file(s) we edited "
            f"— took the deletion, copies in {saved}")


def cmd_push(args) -> None:
    # no status gating — push whatever the selected repos actually have
    repos = select(discover(), args)
    ready, skipped = [], []
    for r in repos:
        if r.is_dirty() or unpushed_commits(r):
            ready.append(r)
        else:
            skipped.append(r)

    for r in skipped:
        say(f"  #{r.pr_number}: nothing to push — working tree clean, "
            "no unpushed commits", "dim")
    if not ready:
        say("nothing to push", "yel")
        return

    commands = PUSH_COMMANDS
    say(f"\nAbout to push {len(ready)} repo(s) and post "
        f"{' + '.join(repr(c) for c in commands)} on each:\n", "bld")
    for r in ready:
        n = len(r.git("status", "--porcelain").splitlines())
        ahead = unpushed_commits(r)
        what = ", ".join(filter(None, [
            f"{n} changed file(s)" if n else "",
            f"{ahead} unpushed commit(s)" if ahead else ""]))
        say(f"  PR #{r.pr_number}  {what}")
    repos = ready

    if not args.yes:
        if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
            say("aborted", "yel")
            return

    for repo in repos:
        say(f"\n{repo.name}", "bld")
        try:
            if repo.is_dirty():
                repo.git("add", "-A")
                repo.git("commit", "-m", args.message)
                say("  committed", "grn")
            else:
                say("  nothing to commit; pushing existing commits", "dim")

            for attempt in (1, 2):
                if note := sync_with_remote(repo):
                    say(f"  {note}", "dim")
                try:
                    repo.git("push", "origin", "HEAD")
                    break
                except RuntimeError:
                    # the bot can land a rescore between our fetch and our push
                    if attempt == 2:
                        raise
                    say("  push rejected — remote moved again, re-syncing", "yel")
            say(f"  pushed to {repo.branch}", "grn")
        except RuntimeError as e:
            say(f"  {e}", "red")
            mark(repo, "failed", note="push failed")
            continue

        for command in commands:
            ok, detail = post_pr_comment(repo, command)
            if ok:
                say(f"  posted {command}", "grn")
            else:
                say(f"  could not post {command}: {detail}", "red")
                say(f"  post it yourself on PR #{repo.pr_number}: {command}", "yel")

        # stamped AFTER the bot comment, so the bot's own reply and everything
        # the reviewer says next counts as "new" on the following round
        mark(repo, "pushed", pr=repo.pr_number,
             pushed_at=datetime.now().astimezone().isoformat())

    say("\nAll done.", "bld")
    if pushed := [r.pr_number for r in repos if status_of(r) == "pushed" and r.pr_number]:
        say("When the bot has re-reviewed, fix only what's still broken with:", "dim")
        say(f"  python3 run.py fix --task {','.join(pushed)}", "dim")


VERDICT_RE = re.compile(
    rf"{FAIRNESS_MARKER}\s*[-–—]\s*\*{{0,2}}(PASS|WARN|FAIL)", re.IGNORECASE)

# new-format headline, e.g. "3 confirmed issue(s), 0 probable warning(s), ..."
AUDIT_COUNTS_RE = re.compile(
    r"(\d+)\s+confirmed issue|(\d+)\s+probable warning", re.IGNORECASE)
# "### 🔴 `FR-INS-25B47D461E` — Temporal and conflict precedence is ..."
FINDING_RE = re.compile(r"^###\s*\S*\s*`([A-Z0-9-]+)`\s*[-–—]\s*(.+?)\s*$", re.MULTILINE)
# "- **Classification:** `instruction-ambiguity` · `high` · `confirmed`"
CLASSIFICATION_RE = re.compile(
    r"\*\*Classification:\*\*\s*`([^`]+)`\s*·\s*`([^`]+)`\s*·\s*`([^`]+)`")


# combined format's At-a-glance block:
#   "- **Fairness:** ⚠️ 2 confirmed issue(s)"
#   "- **QC Phase 1:** ✅ passed (0 flag(s))"
# The row is "- **Fairness:** <mark> <text>". The mark is an emoji and the set
# keeps growing — ✅ ⚠️ ❌ ⚪ 🟡 so far — so capture the rest of the line whole and
# decide from the words, which have stayed stable. The QC row is "QC Phase 1" /
# "QC Phase 2" on a full run, or plain "QC" when the stage never reached them.
GLANCE_RE = re.compile(
    r"^-\s*\*\*(Fairness|QC(?: Phase [12])?):?\*\*\s*(.+?)\s*$", re.MULTILINE)
INCOMPLETE = "could not complete"
COUNT_RE = re.compile(r"(\d+)\s+(confirmed|probable)", re.IGNORECASE)
# leading emoji and variation selectors, stripped for display
MARK_RE = re.compile(r"^[^\w\d]+")


def glance_state(text: str) -> tuple[str, int, int]:
    """(state, confirmed, probable) for one At-a-glance row.

    `state` is "incomplete" when the stage crashed, "clean" when it explicitly
    reports nothing, else "" — meaning judge by the counts.
    """
    low = text.lower()
    if INCOMPLETE in low:
        return "incomplete", 0, 0
    counts = {kind.lower(): int(n) for n, kind in COUNT_RE.findall(text)}
    confirmed, probable = counts.get("confirmed", 0), counts.get("probable", 0)
    if confirmed or probable:
        return "", confirmed, probable
    if any(w in low for w in ("no issues", "passed", "no flag", "clean")):
        return "clean", 0, 0
    return "", 0, 0
# "1. **Title** _(Fairness · confirmed · medium · instruction-ambiguity)_"
ITEM_RE = re.compile(r"^\d+\.\s*\*\*(.+?)\*\*\s*_\((.+?)\)_", re.MULTILINE)


def glance(body: str) -> dict[str, str]:
    """The At-a-glance rows: name -> text, with the leading mark stripped.

    Scoped to the At-a-glance section. A "Could not complete" section repeats
    `- **Fairness:** [timestamp] checking …` with engine logs, and an unscoped
    match would take those instead — they come later in the body.
    """
    start = body.find("### At a glance")
    if start == -1:
        return {}
    rest = body[start + len("### At a glance"):]
    end = rest.find("\n### ")
    block = rest if end == -1 else rest[:end]
    return {name: MARK_RE.sub("", text).strip()
            for name, text in GLANCE_RE.findall(block)}


CONFIDENCES = ("confirmed", "probable", "unconfirmed")


def review_items(body: str, source: str | None) -> list[str]:
    """Numbered findings for one source, each led by its confidence.

    The tag reads `Fairness · probable · high · trace-task-drift`. Confidence is
    the part that decides what to do — confirmed is established, probable is a
    judgement call — so it goes first, where a narrow column will still show it.

    `source=None` takes every item whatever it is tagged. A fairness comment can
    raise its only finding under `Provenance warning`, and filtering on
    `Fairness` alone leaves a flagged review with nothing to show for it.
    """
    out = []
    for title, tag in ITEM_RE.findall(body):
        if source and source.lower() not in tag.lower():
            continue
        parts = [p.strip().lower() for p in tag.split("·")]
        conf = next((p for p in parts if p in CONFIDENCES), "")
        out.append(oneline(f"{conf}: {title}" if conf else title))
    return out


def confidence_counts(body: str, source: str = "Fairness") -> dict[str, int]:
    """How many findings of each confidence, for the summary line."""
    counts: dict[str, int] = {}
    for _, tag in ITEM_RE.findall(body):
        if source.lower() not in tag.lower():
            continue
        for part in (p.strip().lower() for p in tag.split("·")):
            if part in CONFIDENCES:
                counts[part] = counts.get(part, 0) + 1
    return counts


RESCORE_MARKER = "github-review-bot:rescore"
# "**Result (Task compatibility):** Rescore incomplete: 0 of 3 traces were ..."
RESULT_RE = re.compile(r"^\*\*Result \(([^)]*)\):\*\*\s*(.+?)\s*$", re.MULTILINE)
# the verifier's own message, which is the part worth acting on
INFRA_RE = re.compile(r"INFRA_ERROR:\s*(.+?)(?:;|$)", re.MULTILINE)


def latest_rescore(data: dict) -> dict | None:
    """The newest `/bot2 rescore` result, or None if it has never run."""
    hits = [c for c in (data.get("comments") or [])
            if RESCORE_MARKER in (c.get("body") or "")]
    if not hits:
        return None
    return max(hits, key=lambda c: c.get("createdAt") or "")


FRESH_MEAN_RE = re.compile(r"\*\*Fresh mean reward:\*\*\s*([\d.]+)")
# "| `solver-01-Z2RM6Up` | 0.36194 | 0.384328 | 53 | ✅ Reverified |"
RESCORE_TRIAL_RE = re.compile(
    r"^\|\s*`([^`]+)`\s*\|\s*([\d.]+|—)\s*\|\s*([\d.]+|—)\s*\|", re.MULTILINE)


# "| **Mean** | **0.520** | **234,989** |" in a submitter's trial table
BOLD_MEAN_RE = re.compile(r"\|\s*\*\*Mean\*\*\s*\|\s*\*\*([\d.]+)\*\*")


def submitted_mean(data: dict) -> float | None:
    """The mean the task was submitted with, from the PR body or its comments.

    Always recorded somewhere: usually `Mean SOTA score` in the body, sometimes
    only as the bold Mean row of the opening trial table. This is the baseline
    to show when a rescore failed before producing its own committed column.
    """
    texts = [data.get("body") or ""]
    texts += [(c.get("body") or "") for c in (data.get("comments") or [])]
    for text in texts:
        if m := MEAN_SOTA_RE.search(text):
            return float(m.group(1))
        if m := BOLD_MEAN_RE.search(text):
            return float(m.group(1))
    return None


def rescore_scores(body: str) -> tuple[float | None, float | None, list[tuple]]:
    """(committed mean, fresh mean, per-trial rows) from a rescore comment.

    The bot compares each trial's reward at the pinned head against the reward
    its fresh run produced, so both numbers come from the same comment and need
    no local files.
    """
    rows = []
    for name, old, new in RESCORE_TRIAL_RE.findall(body):
        rows.append((name,
                     float(old) if old != "—" else None,
                     float(new) if new != "—" else None))
    olds = [o for _, o, _ in rows if o is not None]
    news = [n for _, _, n in rows if n is not None]
    committed = sum(olds) / len(olds) if olds else None
    if m := FRESH_MEAN_RE.search(body):
        fresh = float(m.group(1))
    else:
        fresh = sum(news) / len(news) if news else None
    return committed, fresh, rows


def rescore_stale_traces(body: str) -> bool:
    """Whether the rescore failed because the committed solver runs are stale.

    Editing instruction.md after the traces were recorded leaves the two out of
    step, and the bot says so outright: "Fresh solver traces are required;
    verifier-only rescoring cannot repair changed solver inputs." Rescoring
    again produces the identical failure, so it is not the next step.
    """
    low = body.lower()
    return ("stale-solver-inputs" in low
            or "fresh solver traces are required" in low)


def rescore_blames_task(body: str) -> bool:
    """Whether a failed rescore is the task's fault rather than the harness's.

    The bot names a failure category — "task compatibility" means the verifier
    refused because the bundle breaks a contract it enforces, which is ours to
    repair. Anything else (verifier crash, infrastructure, timeout) is theirs,
    and rescoring again is the answer rather than editing the task.
    """
    low = body.lower()
    # a stale-trace preflight needs fresh solver runs, which no edit provides —
    # the report says so itself, so do not send it to `repair`
    if "fresh solver traces are required" in low or "stale-solver-inputs" in low:
        return False
    if "infra_error" in low:
        return True
    if m := re.search(r"failure categories:\s*(.+)", low):
        return "task" in m.group(1)
    if m := RESULT_RE.search(body):
        return "task" in m.group(1).lower()
    return False


def rescore_status(body: str) -> tuple[str, str]:
    """(status, one-line reason) from a rescore comment.

    status is "ok" when every trace reverified, "failed" when none did, and
    "partial" in between. The reason prefers the verifier's own INFRA_ERROR
    text — that is the sentence naming what the task must change.
    """
    heading = next((l for l in body.splitlines() if l.startswith("## ")), "")
    if m := re.search(r"Traces:\s*(\d+)\s+successfully reverified,\s*(\d+)\s+failed", body):
        good, bad = int(m.group(1)), int(m.group(2))
        if not good:
            # 0 and 0 means a preflight failure — it never got as far as a
            # trial, so "0 failed" is not success
            status = "failed"
        elif bad:
            status = "partial"
        else:
            status = "failed" if "❌" in heading else "ok"
    else:
        status = "failed" if ("❌" in heading) else ("partial" if "⚠️" in heading else "ok")

    if infra := INFRA_RE.search(body):
        return status, oneline(infra.group(1))
    if res := RESULT_RE.search(body):
        category, text = res.groups()
        return status, oneline(f"{category.lower()}: {text}")
    return status, ""


QC_MARKER = "Task QC check"
# standalone QC comment: "| Prompt quality | ⚠️ 3/5 · agentic yes | ..."
QC_DIMENSION_RE = re.compile(r"^\|\s*([A-Z][^|]{3,30}?)\s*\|\s*([✅⚠❌][^|]*?)\s*\|",
                             re.MULTILINE)
# "### Phase 2 — post-rollout statistics and RCA ⚠️", but also the shape that
# carries no mark at all: "### Phase 2 — incomplete". Matching only on a mark
# reads the second one as fine, because there is no ❌ to find.
QC_PHASE_RE = re.compile(r"^###\s*(Phase [12])\b\s*(?:—|–|-)?\s*([^\n]*)$",
                         re.MULTILINE)
# "Actionable Phase 2 flags: `negative-dual-lane:RUBRIC_DESIGN`, ..."
QC_FLAGS_RE = re.compile(r"Actionable Phase \d flags:\s*(.+)", re.MULTILINE)
# The bot's own verdict on its own run, e.g. "## ✅ Task QC check — requested
# phases passed" or "## ❌ Task QC check infrastructure failure".
QC_HEADING_RE = re.compile(r"^##\s*(.+?)\s*$", re.MULTILINE)
# a crashed runner reports what killed it in a fenced line under "**Failure**"
QC_FAILURE_RE = re.compile(r"\*\*Failure\*\*\s*\n+\s*`([^`]+)`")
# ... and the useful part of that is usually the engine's own ERROR line
QC_ERROR_RE = re.compile(r"ERROR:?\s*([^|\\]+)")
# What each heading means, in the bot's own words, matched in order. The
# emoji is deliberately not the test: the At-a-glance mark has already been
# ✅, ⚠️, ❌, ⚪ and 🟡, and reading the words survives the next one. The bot
# draws the important line itself — a phase that could not run because the
# *task package* is broken is a finding, while one killed by auth, sandbox,
# runner or model trouble is not a verdict about the task at all.
QC_HEADINGS = (("infrastructure failure", "incomplete"),
               ("issues found", "issues"),
               ("incomplete", "issues"),
               ("passed", "ok"))


def qc_checks(data: dict) -> list[dict]:
    """Every QC result on the PR, oldest first, standalone or combined."""
    hits = [c for c in (data.get("comments") or [])
            if QC_MARKER in (c.get("body") or "")
            or "github-review-bot:qc-check" in (c.get("body") or "")
            or (glance(c.get("body") or "")
                and any(k.startswith("QC") for k in glance(c.get("body") or "")))]
    return sorted(hits, key=lambda c: c.get("createdAt") or "")


def latest_qc_check(data: dict) -> dict | None:
    """The newest QC result, standalone or inside a combined review."""
    return hits[-1] if (hits := qc_checks(data)) else None


def qc_crash_reason(body: str) -> str:
    """One line saying what killed a QC run, for an infrastructure failure."""
    if not (m := QC_FAILURE_RE.search(body)):
        return "the QC runner stopped before producing a result"
    text = m.group(1)
    # the engine's last ERROR names the actual cause ("Selected model is at
    # capacity"); the rest is the runner narrating its own progress
    errors = [oneline(e).strip(" .|") for e in QC_ERROR_RE.findall(text)]
    return (errors[-1] if errors else oneline(text))[:120]


def qc_summary(body: str) -> tuple[str, list[str]]:
    """(status, items) from a QC result in either shape.

    Status is "ok" when the bot passed the task, "issues" when it found
    something to repair, "incomplete" when its own run died without judging
    anything, and "none" when this is not a QC comment.

    For a pass, the items are advisory notes rather than work: the bot applies
    its own threshold ("Problematic weighted share: 0.6% … threshold is 10%")
    before it says passed, so a ⚠️ dimension under that bar is something to
    read, not a fix round to spend.
    """
    rows = {k: v for k, v in glance(body).items() if k.startswith("QC")}
    if rows:
        if all(INCOMPLETE in t.lower() for t in rows.values()):
            return "incomplete", [oneline(f"{n}: {t}") for n, t in rows.items()]
        bad = [oneline(f"{n}: {t}") for n, t in sorted(rows.items())
               if glance_state(t)[0] != "clean"]
        return ("issues" if bad else "ok"), bad

    if QC_MARKER not in body:
        return "none", []

    # The heading is the bot's verdict on its own run; everything below it is
    # supporting detail. Judging by the detail alone is how a crash that judged
    # nothing — "## ❌ Task QC check infrastructure failure" — came out as a
    # pass, because a body with no findings in it has no findings to count.
    heading = (m.group(1) if (m := QC_HEADING_RE.search(body)) else "")
    verdict = next((v for phrase, v in QC_HEADINGS if phrase in heading.lower()), None)
    if verdict == "incomplete":
        return "incomplete", [oneline(f"QC runner failed: {qc_crash_reason(body)}")]

    # A standalone QC comment reports in three places, and Phase 1 can be
    # entirely clean while Phase 2 raises the flags — reading only the
    # five-dimension table gives "issues" with nothing to show for it.
    items = [oneline(f"{name}: {result}")
             for name, result in QC_DIMENSION_RE.findall(body)
             if result.startswith(("⚠", "❌"))]
    for phase, rest in QC_PHASE_RE.findall(body):
        rest = rest.strip()
        if "✅" in rest:
            continue
        # a marked phase says how it went in the mark; an unmarked one
        # ("### Phase 2 — incomplete") says it in words
        items.append(f"{phase} raised issues" if ("⚠" in rest or "❌" in rest)
                     else oneline(f"{phase} {rest}".strip()))
    if m := QC_FLAGS_RE.search(body):
        flags = m.group(1).strip().rstrip(".")
        if flags and "none" not in flags.lower():
            items.append(oneline(f"Phase 2 flags: {flags}"))

    if verdict == "ok":
        return "ok", items
    if verdict == "issues":
        # never report issues with an empty list; the count column would read 0
        return "issues", items or ["issues found — see the QC comment"]

    # a heading we do not recognise, or none at all: fall back to the detail,
    # and let the emoji break the tie rather than passing the task by default
    if "❌" in heading or "⚠" in heading:
        return "issues", items or ["issues found — see the QC comment"]
    if "issues found" not in body.lower() and not items:
        return "ok", []
    return "issues", items or ["issues found — see the QC comment"]


def latest_review(data: dict) -> dict | None:
    """The newest legacy fairness comment. Only the newest one is current."""
    return latest_fairness_review(data)


# What `/bot2 fairness-review` says about its own run, in its own words. Read
# in order, and matched on the words rather than the mark for the same reason
# the At-a-glance rows are: the marks so far are ✅ ⚠️ 🟡 ⚪ and the next one
# would otherwise parse as a pass.
FAIRNESS_HEADINGS = (("partially completed", "incomplete"),
                     ("issues found", "FAIL"),
                     ("review advised", "WARN"),
                     ("no confirmed issue", "PASS"))
STANDALONE_FAIRNESS = "human fairness review"


def fairness_verdict(body: str) -> tuple[str, list[str]]:
    """(verdict, failing items) from either shape of fairness comment.

    The old format states PASS / WARN / FAIL outright. The new "Audit" format is
    explicitly non-gating — it "does not accept, reject, approve, fail, or
    disqualify the task" — so there is no verdict to read: one is derived from
    the findings, because the pipeline still has to decide what to send to `fix`.
    Confirmed issues are treated as FAIL, probable-only as WARN.

    What `/bot2 fairness-review` posts today states its verdict in the heading,
    and that beats anything derived from the body: a "🟡 review advised" whose
    only finding is tagged `Provenance warning` counts nothing under `Fairness`
    and used to come out a clean PASS.
    """
    heading = (m.group(1) if (m := QC_HEADING_RE.search(body)) else "")
    if STANDALONE_FAIRNESS in heading.lower():
        low = heading.lower()
        stated = next((v for phrase, v in FAIRNESS_HEADINGS if phrase in low), None)
        if stated == "incomplete":
            return "incomplete", [oneline(f"Fairness: {heading}")]
        if stated == "PASS":
            return "PASS", []
        if stated:
            # its own findings first; fall back to every numbered item, since a
            # flagged review has to show what it flagged
            items = review_items(body, "Fairness") or review_items(body, None)
            return stated, items or [oneline(heading)]

    if m := VERDICT_RE.search(body):
        # old shape: the bot marks each failing section with a cross
        failing = [oneline(l.lstrip("#").replace("❌", "").strip())
                   for l in body.splitlines()
                   if l.startswith("##") and "❌" in l]
        return m.group(1).upper(), failing

    if rows := glance(body):
        text = rows.get("Fairness")
        if text is None:
            return "?", []
        state, confirmed, probable = glance_state(text)
        # the stage crashed rather than judging the task — not a verdict at all,
        # and certainly not a failure to send to `fix`
        if state == "incomplete":
            return "incomplete", [oneline(f"Fairness: {text}")]
        items = review_items(body, "Fairness")
        if state == "clean" and not items:
            return "PASS", []
        if not confirmed and not probable:
            # a mark we do not recognise, but findings are listed: treat them as
            # real rather than silently passing the task
            by_conf = confidence_counts(body)
            confirmed = by_conf.get("confirmed", len(items))
            probable = by_conf.get("probable", 0)
        verdict = "FAIL" if confirmed else ("WARN" if probable else "PASS")
        if verdict == "PASS":
            # nothing to report; a wording we did not match as "clean" is still
            # a pass when no finding is listed under it
            return "PASS", []
        # lead with the tally when there is more than one, so the column says
        # how much is established versus judgement before it runs out of room
        if len(items) > 1:
            tally = ", ".join(f"{n} {c}" for c, n in
                              sorted(confidence_counts(body).items()) if n)
            if tally:
                items = [f"{tally} — {items[0]}"] + items[1:]
        return verdict, items or [oneline(f"Fairness: {text}")]

    if "Task Fairness Audit" not in body:
        return "?", []

    findings = FINDING_RE.findall(body)
    classes = CLASSIFICATION_RE.findall(body)
    confirmed = sum(1 for _, _, conf in classes if conf.lower() == "confirmed")
    probable = sum(1 for _, _, conf in classes if conf.lower() == "probable")

    # fall back to the headline counts when no finding block parses
    if not classes:
        for a, b in AUDIT_COUNTS_RE.findall(body):
            if a:
                confirmed = int(a)
            if b:
                probable = int(b)

    verdict = "FAIL" if confirmed else ("WARN" if probable else "PASS")
    failing = [f"{fid}: {title}" for fid, title in findings] or (
        [f"{confirmed} confirmed, {probable} probable"] if confirmed or probable else [])
    return verdict, failing


def read_verdict(repo: Repo) -> dict:
    """Fetch one PR's current QC, rescore and legacy fairness state.

    Every signal comes back with two pieces of provenance: when it was posted,
    and which commit it ran against. Without those a result that predates the
    current task looks exactly like one posted a minute ago.
    """
    out = {"repo": repo, "pr": repo.pr_number, "verdict": "?", "when": None,
           "failing": [], "error": None, "local": None, "remote": None,
           "fresh": None, "reference": MIN_FRESH_CONTEXT,
           "rescore": "none", "rescore_why": "",
           "qc": "none", "qc_failing": [], "rescore_blames_task": False,
           "rescore_stale_traces": False,
           "qc_at": None, "rescore_at": None, "fetch_error": None,
           "qc_stale": None, "rescore_stale": None, "verdict_stale": None,
           "head": None, "own_head": None, "own_at": None, "running": [],
           "fresh_source": "none", "truncated": False, "untouched": False}
    out["fresh"], out["reference"] = read_fresh_context(repo)
    if out["fresh"] is not None:
        out["fresh_source"] = "bundle trace summary"

    # the bot commits refreshed verifier files when it rescores, so the current
    # score lives on the remote branch until you pull; read it from there
    out["local"] = read_trials(repo)["baseline"]
    branch = repo.branch
    fetched = subprocess.run(["git", "fetch", "origin", branch], cwd=repo.path,
                             capture_output=True, text=True)
    if fetched.returncode == 0:
        out["remote"] = read_trials(repo, f"origin/{branch}")["baseline"]
    else:
        # falling back to the on-disk score without saying so is how a stale
        # number gets shown as the current one
        err = fetched.stderr.strip()
        out["fetch_error"] = err.splitlines()[-1] if err else "git fetch failed"

    if not repo.pr_number:
        out["error"] = "no PR number in folder name"
        return out

    proc = subprocess.run(
        ["gh", "pr", "view", repo.pr_number, "--repo", repo.slug,
         "--json", "title,body,url,comments,reviews,commits,headRefOid"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip()
        out["error"] = err.splitlines()[-1] if err else f"gh exited {proc.returncode}"
        return out

    data = json.loads(proc.stdout)
    out["head"] = (data.get("headRefOid") or "").lower() or None
    oids, own, own_at = commit_history(data)
    out["own_head"], out["own_at"] = own, own_at
    out["untouched"] = not edited_since_submission(data)
    # gh returns one page of comments; on a thread that long the newest result
    # may not be in hand, and "latest" would be a guess
    out["truncated"] = len(data.get("comments") or []) >= 100

    # the payload is already here, so these cost nothing extra
    if out["fresh"] is None:
        out["fresh"] = fresh_from_pr_data(data)
        if out["fresh"] is not None:
            # what the task was submitted with, not a current measurement
            out["fresh_source"] = "PR body (as submitted)"
    if qc := latest_qc_check(data):
        body = qc.get("body") or ""
        out["qc"], out["qc_failing"] = qc_summary(body)
        out["qc_at"] = parse_ts(qc.get("createdAt"))
        out["qc_stale"] = ran_before_our_work(pinned_head(body), oids, own)
    if rs := latest_rescore(data):
        body = rs.get("body") or ""
        out["rescore"], out["rescore_why"] = rescore_status(body)
        out["rescore_blames_task"] = rescore_blames_task(body)
        out["rescore_stale_traces"] = rescore_stale_traces(body)
        out["rescore_at"] = parse_ts(rs.get("createdAt"))
        out["rescore_stale"] = ran_before_our_work(pinned_head(body), oids, own)

    verdict = latest_review(data)
    if verdict is not None:
        body = verdict.get("body") or ""
        out["verdict"], out["failing"] = fairness_verdict(body)
        out["when"] = parse_ts(verdict.get("createdAt"))
        out["verdict_stale"] = ran_before_our_work(pinned_head(body), oids, own)
    else:
        out["verdict"] = "none"

    out["running"] = running_jobs(data, {"qc": out["qc_at"],
                                         "rescore": out["rescore_at"],
                                         "fairness": out["when"]})
    return out


# what each bucket means in one word, printed per row so a task never needs
# cross-referencing against the summary to know its next step
ACTIONS = {
    "push": "push", "token": "drop", "reject": "drop", "fix": "fix",
    "repair": "repair", "review": "trigger", "waiting": "trigger",
    "running": "wait", "harden": "harden", "rescore": "rescore",
    "retrace": "escalate", "done": "done", "none": "—",
}


# Red means something failed. Yellow means something wants attention or the
# pipeline is mid-flight. Green means nothing to do.
BUCKET_COLOURS = {"token": "red", "reject": "red", "repair": "red",
                  "harden": "yel", "review": "yel", "waiting": "yel",
                  "running": "yel", "push": "yel",
                  "done": "grn", "rescore": "grn"}


def row_colour(r: dict, bucket: str) -> str:
    """What colour a row is, by severity rather than by verb.

    A `fix` is not automatically a failure. QC findings and a fairness FAIL
    are; a fairness "review advised" is a question about the task, and
    painting it the same red as a rejected bundle makes the loudest signal in
    the table mean nothing.
    """
    if bucket == "fix":
        failed = r["qc"] == "issues" or r["verdict"] == "FAIL"
        return "red" if failed else "yel"
    return BUCKET_COLOURS.get(bucket, "")


def row_age(r: dict, bucket: str) -> datetime | None:
    """When the result this row rests on was posted.

    Not simply the newest comment on the PR: what matters is the age of the
    thing that decided the row, which is the QC verdict for a review outcome
    and the rescore for a score outcome.
    """
    if bucket == "running" and r.get("running"):
        return max(w for _, w in r["running"])
    if bucket in ("push", "token", "none"):
        return None
    score_led = bucket in ("repair", "retrace", "rescore", "harden", "done")
    order = (["rescore_at", "qc_at", "when"] if score_led
             else ["qc_at", "when", "rescore_at"])
    return next((r[k] for k in order if r.get(k)), None)


def decide(r: dict, pending: str, starved: bool, threshold: float,
           fail_only: bool = False) -> tuple[str, str]:
    """Where a PR stands and why. Returns (bucket, reason).

    The pipeline's actual order:

      1. Un-pushed work first — a verdict that has not seen it is stale.
      2. Under the fresh-context bar is a straight fail; nothing else matters.
      2b. A result that is in flight, or that pinned a commit older than our
         newest one, is not a verdict about this branch and settles nothing.
      3. **QC and fairness both gate.** They answer different questions — QC
         whether the bundle is built right, fairness whether the task can be
         solved as written — so a task needs a current pass from each, and
         either one's findings are work.
      4. Rescore is a score, not a review. It only routes when it failed *and*
         blamed the task; otherwise the number decides — at or under the cap the
         task is done, above it the task is too easy.
    """
    if pending:
        return "push", pending
    if starved:
        return "token", (f"{r['fresh'] / 1000:.0f}k < {r['reference'] / 1000:.0f}k"
                         " — straight fail")
    if r["error"]:
        return "none", r["error"]

    # kind -> when it was queued, newest of each kind
    live = {k: w for k, w in sorted(r.get("running") or [], key=lambda j: j[1])}

    # The two reviews, in one vocabulary. "advised" is work like "issues" is,
    # but it is not a failure — a fairness "review advised" asks a question
    # rather than rejecting the task. `--fail-only` drops it back to a pass.
    fair = {"PASS": "ok", "FAIL": "issues",
            "WARN": "ok" if fail_only else "advised",
            "incomplete": "incomplete"}.get(r["verdict"], "none")
    reviews = (("QC", "qc", r["qc"], r["qc_stale"], r["qc_failing"]),
               ("fairness", "fairness", fair, r["verdict_stale"], r["failing"]))

    # A result is only about the branch as it stands now, and each review is
    # asked separately. Two things break that, and both used to pass silently
    # as a current verdict: the bot is still working (so what is posted is the
    # *previous* answer), and the posted answer pinned a commit older than our
    # newest one (so it judged a task that no longer exists).
    for label, key, status, stale, _ in reviews:
        if key in live:
            if job_overdue(key, live[key]):
                return "waiting", (f"{label} queued {ago(live[key])} ago and "
                                   "never landed — ask again")
            return "running", f"{label} re-running — queued {ago(live[key])} ago"
    for label, key, status, stale, _ in reviews:
        if status != "none" and stale:
            return "waiting", f"{label} ran against an older commit — ask for a fresh one"
    # a review whose own runner died judged nothing. It is not a pass, and it
    # is not a set of findings to fix — it is a missing review.
    for label, key, status, _, items in reviews:
        if status == "incomplete":
            why = "; ".join(items) or "the runner stopped"
            return "review", f"{label} never completed ({why}) — ask for another"
    if missing := [label for label, _, status, *_ in reviews if status == "none"]:
        # ask for it before fixing anything: the other reviewer's findings are
        # worth one fix round together, not one round each
        pending_work = [f"{label} already found {len(items)}"
                        for label, _, status, _, items in reviews
                        if status in ("issues", "advised") and items]
        also = f" ({'; '.join(pending_work)})" if pending_work else ""
        return "review", f"no {' or '.join(missing)} result yet{also}"

    # Both have answered about the current branch. Either one's findings are
    # work, and both sets go in the reason so the row says which reviewer
    # wanted what.
    if problems := [f"{label} {'advises' if status == 'advised' else 'found'} "
                    + ("; ".join(items) or "attention")
                    for label, _, status, _, items in reviews
                    if status in ("issues", "advised")]:
        return "fix", "; ".join(problems)

    cleared = "QC and fairness both passed"
    notes = len(r["qc_failing"]) + (len(r["failing"]) if fail_only and
                                    r["verdict"] == "WARN" else 0)
    if notes:
        # passed, but something was noted under the reviewer's own bar — worth
        # saying so rather than printing an unqualified pass
        cleared += f", {notes} sub-threshold note{'s' if notes > 1 else ''}"

    # Review is settled either way; from here it is only about the score. A
    # rescore that blamed the task is the one rescore result worth routing on —
    # any other failure is the harness's, and rescoring again is the answer.
    if "rescore" in live and not job_overdue("rescore", live["rescore"]):
        return "running", f"{cleared}; rescore queued {ago(live['rescore'])} ago"
    if r["rescore"] == "failed" and r["rescore_stale_traces"]:
        # the committed solver runs no longer match the current instruction, so
        # verifier-only rescoring will fail the same way every time — and a push
        # since then does not un-stale them. Asking for a second trace run while
        # the first is still queued only adds a job.
        if "trace run" in live and not job_overdue("trace run", live["trace run"]):
            return "running", (f"trace run started {ago(live['trace run'])} ago — "
                               "no result posted yet")
        return "retrace", "solver traces are stale — rescoring cannot fix it"
    if r["rescore"] == "failed" and r["rescore_blames_task"]:
        # a content complaint about a commit we have since replaced may already
        # be answered; re-measure before spending a repair round on it
        if r["rescore_stale"]:
            return "rescore", ("verifier rejected an older commit — re-score "
                               "before acting on it")
        return "repair", r["rescore_why"] or "verifier rejected the task"

    base = r["remote"] if r["remote"] is not None else r["local"]
    if base is None:
        return "rescore", f"{cleared} — needs a score"

    # A task that passed review exactly as submitted is judged on the score it
    # was submitted with. Nothing was edited, so there is nothing to re-measure
    # and nothing to harden — hardening would mean rewriting a task the review
    # already accepted. It passes or it fails, and that is the whole answer.
    if r.get("untouched"):
        if base > threshold:
            return "reject", (f"passed review unchanged and scores {base:.3f} > "
                              f"{threshold} — a straight fail")
        return "done", f"passed review unchanged, scores {base:.3f} — done"

    if base > threshold:
        return "harden", f"{cleared} but scores {base:.3f} > {threshold} — too easy"
    if r["rescore"] in ("none", "failed"):
        return "rescore", f"{cleared}, last measured {base:.3f} — needs a rescore"
    if r["rescore_stale"]:
        # the number is real, but it measured a commit we have since replaced
        return "rescore", (f"{cleared}, but {base:.3f} predates your latest "
                           "commit — needs a rescore")
    return "done", f"{cleared}, scores {base:.3f} — done"


def cmd_check(args) -> None:
    """Fetch every PR's current verdict and print the command to fix the bad ones."""
    if not have_gh():
        say("gh is not installed — can't read PR verdicts", "red")
        say("  brew install gh && gh auth login", "yel")
        sys.exit(1)

    repos = select(discover(), args)
    with ThreadPoolExecutor(max_workers=max(1, len(repos))) as pool:
        results = list(pool.map(read_verdict, repos))
    results.sort(key=lambda r: r["pr"] or "")

    width = max((len(r["pr"] or "?") for r in results), default=4)
    colors = {"PASS": "grn", "WARN": "yel", "FAIL": "red",
              "incomplete": "yel", "none": "dim", "?": "dim"}

    buckets: dict[str, list[tuple[Repo, str]]] = {}

    if not args.quiet:
        say(f"as of {datetime.now().strftime('%H:%M:%S')} — AGE is how long ago the "
            "result this row rests on was posted; * marks one that ran against an "
            "older commit than yours", "dim")
        say(f"{'PR':<{width + 1}}  {'DO THIS':<9} {'QC':<8} {'FAIR':<8} "
            f"{'RESCORE':<8} {'SCORE':<14} {'TOKENS':<7} {'AGE':<5} WHY", "bld")

    for r in results:
        repo = r["repo"]
        local = status_of(repo)
        # un-pushed work always comes first — no point re-fixing against a
        # verdict that hasn't seen your existing changes yet
        # ask the working tree, not the status label — harden, no-changes and
        # hand edits all leave real work that a status check would miss
        # a task under the token bar needs corpus work, and no fix round can
        # supply that — sending it to `fix` burns a round for nothing
        starved = r["fresh"] is not None and r["fresh"] < r["reference"]

        pending = ""
        if repo.is_dirty() or unpushed_commits(repo):
            n = len(repo.git("status", "--porcelain").splitlines())
            pending = (f"{n} un-pushed file(s)" if n else
                       f"{unpushed_commits(repo)} un-pushed commit(s)")

        # staleness is decided inside decide(), against the commit each result
        # pinned — the old check here compared the *legacy fairness* timestamp
        # to the last push even when QC was the signal that routed the row, so
        # a QC posted after the push still read as "bot re-running"
        bucket, detail = decide(r, pending, starved, args.threshold,
                                getattr(args, "fail_only", False))
        buckets.setdefault(bucket, []).append((repo, detail))

        # the remote value is the one the bot most recently measured
        old, new = r["local"], r["remote"]
        base = new if new is not None else old

        if not args.quiet:
            if base is None:
                shown = "—"
            elif old is not None and new is not None and abs(new - old) > 5e-4:
                shown = f"{old:.3f}→{new:.3f}"      # rescored since you pulled
            else:
                shown = f"{base:.3f}"
            if base is not None and base > args.threshold:
                shown += "!"        # at or above the bar: task is too easy
            if r["fetch_error"]:
                # could not reach origin, so this is whatever is on disk
                shown = f"~{shown}"
            if r["fresh"] is None:
                tokens = "—"
            else:
                tokens = f"{r['fresh'] / 1000:.0f}k"
                if r["fresh"] < r["reference"]:
                    tokens += "!"   # under the bar: needs corpus work, not a fix
            # a failure is a failure whichever check caught it: a task under
            # the token bar is red even when its verdict is still unknown
            rescore = {"ok": "ok", "failed": "FAILED", "partial": "partial",
                       "none": "—"}.get(r["rescore"], r["rescore"])
            qc = {"ok": "ok", "issues": f"{len(r['qc_failing'])} bad",
                  "incomplete": "crashed", "none": "—"}.get(r["qc"], r["qc"])
            # a pass the bot qualified is not the same as a clean one
            if r["qc"] == "ok" and r["qc_failing"]:
                qc = f"ok·{len(r['qc_failing'])}"
            fair = {"PASS": "ok", "WARN": "advised", "FAIL": f"{len(r['failing'])} bad",
                    "incomplete": "crashed", "none": "—",
                    "?": "—"}.get(r["verdict"], r["verdict"])
            if r["verdict_stale"] and r["verdict"] not in ("none", "?"):
                fair += "*"
            # a result that pinned an older commit is not current, and the
            # column has to say so rather than reading like a fresh pass
            if r["qc_stale"]:
                qc += "*"
            if r["rescore_stale"] and r["rescore"] != "none":
                rescore += "*"
            colour = row_colour(r, bucket)
            say(f"#{r['pr'] or '?':<{width}}  {ACTIONS.get(bucket, bucket):<9} "
                f"{qc:<8} {fair:<8} {rescore:<8} {shown:<14} {tokens:<7} "
                f"{ago(row_age(r, bucket)):<5} {detail[:52]}", colour)
            # one line per PR by default; -v adds what each bot actually said
            if args.verbose:
                # a done row still shows its QC notes — they are the reason the
                # column reads `ok·2` instead of `ok`, and hiding them is what
                # makes a qualified pass indistinguishable from a clean one
                quiet_row = bucket in ("done",)
                stamps = [f"qc {ago(r['qc_at'])} ago" if r["qc_at"] else "qc never",
                          f"fairness {ago(r['when'])} ago" if r["when"]
                          else "fairness never",
                          f"rescore {ago(r['rescore_at'])} ago" if r["rescore_at"]
                          else "rescore never",
                          f"tokens from {r['fresh_source']}"]
                if r["running"]:
                    stamps += [f"{kind} queued {ago(when)} ago"
                               for kind, when in r["running"]]
                if r["fetch_error"]:
                    stamps.append(f"origin unreachable: {r['fetch_error']}")
                if r["truncated"]:
                    stamps.append("comment thread hit the page limit")
                say(f"{'':<{width + 3}}{'posted':<9} {', '.join(stamps)[:96]}", "dim")
                for label, items in (("qc note" if r["qc"] == "ok" else "qc",
                                      r["qc_failing"]),
                                     ("fairness", [] if quiet_row else r["failing"]),
                                     ("rescore", [r["rescore_why"]]
                                      if r["rescore"] == "failed" and r["rescore_why"]
                                      else [])):
                    for item in items[:2]:
                        say(f"{'':<{width + 3}}{label:<9} {item[:96]}", "dim")

    def prs(items) -> str:
        return ",".join(repo.pr_number for repo, _ in items if repo.pr_number)

    if args.quiet:
        # meant for $(...) — emit the bare command, or nothing at all
        if fix := buckets.get("fix"):
            print(f"python3 run.py fix --task {prs(fix)}")
        return

    # ordered the way the pipeline is actually worked, most blocking first.
    # every group prints the same three things: what it is, which PRs, what to run
    plan = [
        ("push",    "UN-PUSHED WORK — review and push first", "yel",
         lambda b: [f"python3 run.py review --task {prs(b)}",
                    f"python3 run.py push   --task {prs(b)}"]),
        ("token",   f"BELOW THE {MIN_FRESH_CONTEXT / 1000:.0f}k TOKEN BAR — "
                    "a straight fail, no review or fix will help", "red",
         lambda b: ["needs corpus depth; run `audit` for the numbers"]),
        ("reject",  f"TOO EASY AS SUBMITTED — passed QC with no changes and scores "
                    f"above {args.threshold}, so it is a straight fail", "red",
         lambda b: ["nothing to run — the score it was submitted with is the answer",
                    "hardening it would rewrite a task the review already passed"]),
        ("fix",     "NEED FIXING — a review raised something to answer", "bld",
         lambda b: [f"python3 run.py fix --task {prs(b)}"]),
        ("repair",  "NEED REPAIR — the verifier rejected the bundle", "bld",
         lambda b: [f"python3 run.py repair --task {prs(b)}"]),
        ("retrace", "CANNOT BE FIXED HERE — a solver-visible file changed after "
                    "their traces were recorded", "red",
         lambda b: ["rescore cannot help; the solver must be re-run",
                    f'python3 run.py comment --task {prs(b)} -b "{TRACE_RUN_COMMAND}"',
                    "escalate if you do not have solver access"]),
        ("review",  "NEED A REVIEW — QC or fairness has not usably answered yet", "yel",
         lambda b: [f"python3 run.py trigger --task {prs(b)}"]),
        ("waiting", "OUT OF DATE — the newest result pinned an older commit "
                    "than yours", "yel",
         lambda b: [f"python3 run.py trigger --task {prs(b)}"]),
        ("running", "BOT IS STILL WORKING — a result is on its way; "
                    "re-run check rather than queueing another", "yel",
         lambda b: ["nothing to run"]),
        ("harden",  f"TOO EASY — score above {args.threshold}", "red",
         lambda b: [f"python3 run.py harden --task {prs(b)}"]),
        ("rescore", "NEED A SCORE — review is clean", "grn",
         lambda b: [f"python3 run.py rescore --task {prs(b)}"]),
        ("done",    "DONE — review clean, score under the cap", "grn",
         lambda b: []),
    ]
    for name, label, colour, commands in plan:
        if not (b := buckets.get(name)):
            continue
        say(f"\n{label}  ({len(b)})", colour)
        say(f"  {prs(b)}", "dim")
        for line in commands(b):
            say(f"  {line}", "grn" if line.startswith("python3") else "dim")
        if name == "fix" and (blocked := [r for r, _ in b if r.is_dirty()]):
            say(f"  #{','.join(x.pr_number for x in blocked)} has uncommitted "
                "changes that will block it — add --discard-dirty", "yel")

    if not buckets or set(buckets) <= {"done"}:
        say("Nothing to do.", "grn")


def task_bundle(repo: Repo) -> Path | None:
    """The single task directory under contributor_tasks/."""
    root = repo.path / "contributor_tasks"
    if not root.is_dir():
        return None
    bundles = [p for p in sorted(root.iterdir())
               if p.is_dir() and (p / "instruction.md").exists()]
    return bundles[0] if len(bundles) == 1 else (bundles[0] if bundles else None)


MEAN_SOTA_RE = re.compile(r"Mean\s+SOTA\s+score[^`\n]*`([0-9.]+)`", re.IGNORECASE)
# a trial row like: | 1 | `openai-codex/gpt-5.6-sol` | 0.2395 |
TRIAL_ROW_RE = re.compile(r"^\|\s*\d+\s*\|.*\|\s*([0-9]*\.[0-9]+)\s*\|\s*$", re.MULTILINE)


def pr_reported_score(repo: Repo) -> float | None:
    """The score stated in the PR's opening comment, i.e. what was submitted.

    Prefers an explicit `Mean SOTA score` line; older PR bodies only carry the
    per-trial table, so fall back to averaging its reward column.
    """
    if not (have_gh() and repo.pr_number):
        return None
    proc = subprocess.run(
        ["gh", "pr", "view", repo.pr_number, "--repo", repo.slug, "--json", "body"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        body = json.loads(proc.stdout).get("body") or ""
    except json.JSONDecodeError:
        return None

    if m := MEAN_SOTA_RE.search(body):
        return float(m.group(1))
    if rows := TRIAL_ROW_RE.findall(body):
        return sum(float(v) for v in rows) / len(rows)
    return None


def parse_reward(text: str, path: str) -> tuple[float | None, str | None]:
    """(reward, role) from either a bare reward.txt number or a result.json."""
    if path.endswith("reward.txt"):
        try:
            return float(text.strip()), None
        except ValueError:
            return None, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, None
    # newer bundles nest the reward under verifier_result; older ones put it
    # at the top level alongside an explicit evaluation_role
    reward = (((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")
              if data.get("verifier_result") else data.get("reward"))
    if not isinstance(reward, (int, float)):
        return None, None
    return float(reward), data.get("evaluation_role")


def reward_files(repo: Repo, ref: str | None = None) -> dict[str, str]:
    """path -> contents for every reward file, from a git ref or the worktree."""
    def wanted(rel: str) -> bool:
        return rel.endswith("result.json") or rel.endswith("verifier/reward.txt")

    if ref is None:
        out = {}
        for name in ("result.json", "reward.txt"):
            for p in repo.path.rglob(name):
                rel = p.relative_to(repo.path).as_posix()
                if ".git/" in rel or not wanted(rel):
                    continue
                try:
                    out[rel] = p.read_text()
                except OSError:
                    pass
        return out

    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref],
                            cwd=repo.path, capture_output=True, text=True)
    if listed.returncode != 0:
        return {}
    out = {}
    for rel in listed.stdout.splitlines():
        if not wanted(rel):
            continue
        blob = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=repo.path,
                              capture_output=True, text=True)
        if blob.returncode == 0:
            out[rel] = blob.stdout
    return out


def read_trials(repo: Repo, ref: str | None = None) -> dict:
    """Recover measured rewards from recorded trial directories.

    Two layouts exist in the wild: `<bundle>/traces/solver-*` and
    `trace/<task>/codex-trial-*`. Both carry the same result.json, so match on
    the file rather than the directory name, and classify by path.

    Within a trial, `verifier/reward.txt` wins over `result.json`: a `/bot2
    rescore` rewrites the verifier files and leaves result.json at its original
    value, so result.json alone reports a score that can be several rounds old.

    Pass `ref` (e.g. `origin/<branch>`) to read the values recorded there
    instead of the ones on disk, without touching the working tree.
    """
    trials = {"solver": [], "oracle": [], "nop": [], "files": []}
    best: dict[str, tuple[float, str | None, bool]] = {}

    for path, text in sorted(reward_files(repo, ref).items()):
        reward, role = parse_reward(text, path)
        if reward is None:
            continue
        parent = PurePosixPath(path).parent
        trial = str(parent.parent if parent.name == "verifier" else parent)
        fresher = path.endswith("reward.txt")
        if trial not in best or (fresher and not best[trial][2]):
            # keep the role from result.json even when reward.txt supplies the number
            keep_role = role or (best.get(trial) or (None, None, None))[1]
            best[trial] = (reward, keep_role, fresher)

    for trial, (reward, role, _) in sorted(best.items()):
        label = (role or trial).lower()
        kind = ("oracle" if "oracle" in label
                else "nop" if "nop" in label
                else "solver")
        trials[kind].append(reward)
        trials["files"].append(trial)

    trials["baseline"] = (sum(trials["solver"]) / len(trials["solver"])
                          if trials["solver"] else None)
    return trials


# Task-quality rules every bundle must satisfy. Rule 4 (binary / atomic /
# independent criteria) is a judgement call and is left to the agent; the rest
# are computed exactly from the files.
NEG_WEIGHT_MIN, NEG_WEIGHT_MAX = -100, -1
NEG_WEIGHT_SPECIAL = -500
MIN_POSITIVE_POINTS = 300
# negative criteria may number up to this share of the positive ones, inclusive
MAX_NEG_RATIO = 0.60
# a task should force the solver to pull real context; used only when the
# bundle's own trace summary does not declare a fresh_context_reference
MIN_FRESH_CONTEXT = 200_000
# a mean solver score above this means the task is too easy and needs hardening
MAX_MEAN_SCORE = 0.6
# an estimate this far from the measured baseline needs justifying, not trusting
BIG_DRIFT = 0.15
TIER_MINIMUMS = {"0": 3, "1": 25, "2": 5}
TIER_LABELS = {"0": "Tier 0 decoys", "1": "Tier 1 sources", "2": "Tier 2 sources"}


def read_fresh_context(repo: Repo) -> tuple[float | None, int]:
    """Mean solver fresh input tokens, and the bar the bundle sets for itself.

    Recorded in `trace/<task>/trace-summary.json`, so this costs a file read
    rather than a GitHub round trip — which matters because `rules` runs inside
    every fix and audit prompt. Returns (mean or None, reference).
    """
    means: list[float] = []
    reference = MIN_FRESH_CONTEXT
    for path in sorted(repo.path.rglob("trace-summary.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        value = data.get("mean_solver_fresh_input_tokens")
        if isinstance(value, (int, float)):
            means.append(float(value))
        # the bundle declares its own bar; prefer it over our default
        declared = data.get("fresh_context_reference")
        if isinstance(declared, (int, float)) and declared > 0:
            reference = int(declared)
    return (sum(means) / len(means) if means else None), reference


MEAN_FRESH_RE = re.compile(
    r"mean fresh (?:context|input tokens)\D{0,12}([\d,]+(?:\.\d+)?)", re.IGNORECASE)


def fresh_from_table(text: str) -> float | None:
    """Mean fresh-context value out of a markdown trial table."""
    lines = text.splitlines()
    for i, header in enumerate(lines):
        if "|" not in header:
            continue
        cells = [c.strip().lower() for c in header.strip("|").split("|")]
        col = next((j for j, c in enumerate(cells) if "fresh" in c), None)
        if col is None:
            continue
        values, mean = [], None
        for row in lines[i + 1:]:
            if "|" not in row:
                break
            cs = [c.strip() for c in row.strip("|").split("|")]
            if len(cs) <= col:
                continue
            raw = cs[col].replace("*", "").replace(",", "").strip()
            if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
                continue          # separator rows and stray cells
            if "mean" in cs[0].lower():
                mean = float(raw)
            else:
                values.append(float(raw))
        if mean is not None:
            return mean
        if values:
            return sum(values) / len(values)
    return None


def fresh_from_pr_data(data: dict) -> float | None:
    """Fresh context out of an already-fetched PR payload.

    The submitter always records it — as `Mean fresh context:` in the PR body,
    or as a `Fresh input tokens` column in the opening trial table.
    """
    texts = [data.get("body") or ""]
    texts += [(c.get("body") or "") for c in (data.get("comments") or [])]
    for text in texts:
        if m := MEAN_FRESH_RE.search(text):
            return float(m.group(1).replace(",", ""))
        if (value := fresh_from_table(text)) is not None:
            return value
    return None


def fresh_from_github(repo: Repo) -> float | None:
    """Fresh context from the PR, for bundles that ship no trace summary."""
    if not repo.pr_number or not have_gh():
        return None
    proc = subprocess.run(
        ["gh", "pr", "view", repo.pr_number, "--repo", repo.slug,
         "--json", "body,comments"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return fresh_from_pr_data(json.loads(proc.stdout))
    except json.JSONDecodeError:
        return None


def read_tiers(bundle: Path) -> dict[str, int]:
    """Count sources per tier from tests/source_tier(s).txt."""
    counts: dict[str, int] = {}
    for name in ("source_tiers.txt", "source_tier.txt"):
        path = bundle / "tests" / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[-1].isdigit():
                counts[parts[-1]] = counts.get(parts[-1], 0) + 1
        break
    return counts


def rubric_formula(rub: dict) -> str | None:
    """`aggregation` is a dict in some bundles and a bare string in others."""
    agg = rub.get("aggregation")
    if isinstance(agg, str):
        return agg
    if isinstance(agg, dict):
        return agg.get("formula_id")
    return None


def check_rules(repo: Repo) -> list[dict]:
    """Evaluate the task-quality rules. Each result: rule, ok, detail."""
    out: list[dict] = []
    bundle = task_bundle(repo)
    if bundle is None:
        return [{"rule": "bundle", "ok": False, "detail": "no task bundle found"}]

    rubric_path = bundle / "tests" / "rubrics.json"
    if not rubric_path.exists():
        out.append({"rule": "rubric", "ok": False, "detail": "tests/rubrics.json missing"})
        return out

    try:
        crit = (json.loads(rubric_path.read_text()).get("criteria") or [])
    except json.JSONDecodeError as e:
        return [{"rule": "rubric", "ok": False, "detail": f"rubrics.json unreadable: {e}"}]

    pos = [c for c in crit if (c.get("weight") or 0) > 0]
    neg = [c for c in crit if (c.get("weight") or 0) < 0]
    pos_pts = sum(c["weight"] for c in pos)
    neg_pts = sum(c["weight"] for c in neg)

    out.append({
        "rule": "negative weight < positive weight",
        "ok": abs(neg_pts) < pos_pts,
        "detail": f"negative {abs(neg_pts):g} vs positive {pos_pts:g}",
        "short": f"neg weight {abs(neg_pts):g}/{pos_pts:g}",
    })
    pct = (len(neg) / len(pos) * 100) if pos else 0
    out.append({
        "rule": f"negative count <= {MAX_NEG_RATIO:.0%} of positive count",
        "ok": len(neg) <= MAX_NEG_RATIO * len(pos),
        "detail": f"{len(neg)} negative vs {len(pos)} positive ({pct:.0f}%)",
        "short": f"neg count {pct:.0f}%/{MAX_NEG_RATIO:.0%}",
    })
    out.append({
        "rule": f"positive criteria > {MIN_POSITIVE_POINTS} points",
        "ok": pos_pts > MIN_POSITIVE_POINTS,
        "detail": f"{pos_pts:g} points",
        "short": f"points {pos_pts:g}/{MIN_POSITIVE_POINTS}",
    })

    bad = [c for c in neg
           if not (NEG_WEIGHT_MIN <= c["weight"] <= NEG_WEIGHT_MAX
                   or c["weight"] == NEG_WEIGHT_SPECIAL)]
    out.append({
        "rule": f"negative weights within {NEG_WEIGHT_MIN}..{NEG_WEIGHT_MAX} "
                f"(or {NEG_WEIGHT_SPECIAL} special case)",
        "ok": not bad,
        # every offender, not a sample: the agent fixes what it is shown
        "detail": "all in range" if not bad else
                  ", ".join(f"{c.get('id','?')}={c['weight']}" for c in bad),
        "short": f"{len(bad)} weight(s) out of range",
    })

    fresh, reference = read_fresh_context(repo)
    out.append({
        "rule": f"fresh input context >= {reference / 1000:.0f}k tokens",
        "ok": fresh is not None and fresh >= reference,
        "detail": (f"{fresh:,.0f} tokens" if fresh is not None
                   else "no trace summary — cannot tell"),
        "short": (f"fresh ctx {fresh / 1000:.0f}k/{reference / 1000:.0f}k"
                  if fresh is not None else "fresh ctx unknown"),
    })

    tiers = read_tiers(bundle)
    for tier, minimum in TIER_MINIMUMS.items():
        have = tiers.get(tier, 0)
        out.append({
            "rule": f"{TIER_LABELS[tier]} >= {minimum}",
            "ok": have >= minimum,
            "detail": f"{have} found" if tiers else "no source tier file",
            "short": (f"Tier {tier} {have}/{minimum}" if tiers
                      else "no source tier file"),
        })

    return out


def rules_summary(repo: Repo) -> tuple[list[dict], list[dict]]:
    results = check_rules(repo)
    return results, [r for r in results if not r["ok"]]


def task_metrics(repo: Repo) -> dict:
    """Quantitative fact sheet for one task, computed straight from the files.

    Everything here is measured, not inferred, so the analysis agent can reason
    about causes instead of spending its budget rediscovering numbers.
    """
    m: dict = {"pr": repo.pr_number, "name": repo.name}
    bundle = task_bundle(repo)
    if bundle is None:
        m["error"] = "no task bundle"
        return m

    m["task"] = bundle.name
    trials = read_trials(repo)
    m["baseline"] = trials["baseline"]
    m["solver_trials"] = trials["solver"]
    m["oracle"] = trials["oracle"][0] if trials["oracle"] else None
    m["nop"] = trials["nop"][0] if trials["nop"] else None
    m["reported"] = pr_reported_score(repo)

    # current rubric composition
    rubric_path = bundle / "tests" / "rubrics.json"
    if rubric_path.exists():
        try:
            rub = json.loads(rubric_path.read_text())
            crit = rub.get("criteria") or []
            pos = [c for c in crit if (c.get("weight") or 0) > 0]
            neg = [c for c in crit if (c.get("weight") or 0) < 0]
            axes: dict[str, int] = {}
            for c in crit:
                axes[c.get("axis") or "?"] = axes.get(c.get("axis") or "?", 0) + 1
            m["rubric"] = {
                "criteria": len(crit),
                "positive": len(pos),
                "positive_weight": sum(c["weight"] for c in pos),
                "negative": len(neg),
                "negative_weight": sum(c["weight"] for c in neg),
                "axes": axes,
                "formula": rubric_formula(rub),
            }
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
            # bundles vary in shape between task generations; a surprise field
            # must not take down the whole estimate
            m["rubric"] = None
            m["rubric_error"] = f"{type(e).__name__}: {e}"

    # MET rate per criterion family, averaged over the recorded solver runs.
    # ids are prefixed by family (fact-, schedule-, negative-, ...), which is
    # what actually separates a hard task from an easy one.
    tally: dict[str, list[int]] = {}
    judgments = 0
    for jpath in sorted(repo.path.rglob("judgment.json")):
        if "solver" not in str(jpath) and "trial" not in str(jpath):
            continue
        try:
            j = json.loads(jpath.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        judgments += 1
        for c in j.get("criteria") or []:
            fam = str(c.get("id", "?")).split("-")[0]
            tally.setdefault(fam, []).append(1 if c.get("verdict") == "MET" else 0)
        m.setdefault("denominator", j.get("positive_denominator"))

    m["judgments_read"] = judgments
    m["met_by_family"] = {
        fam: {"met": sum(v), "total": len(v), "rate": round(sum(v) / len(v), 3)}
        for fam, v in sorted(tally.items(), key=lambda kv: -len(kv[1]))
    }

    # instruction size — how much is handed to the solver vs left to derive
    instr = bundle / "instruction.md"
    if instr.exists():
        text = instr.read_text()
        m["instruction"] = {
            "lines": len(text.splitlines()),
            "words": len(text.split()),
            "table_rows": sum(1 for l in text.splitlines() if l.startswith("|")),
        }

    # deliberately no corpus size: the payload lives in S3 and the checked-in
    # WARC is an LFS pointer, so any local byte count is meaningless and was
    # being read as evidence that the task was broken
    m["source_tiers"] = read_tiers(bundle)
    return m


def score_one(repo: Repo, args, width: int) -> dict:
    """Estimate the reward for one task. Runs in a worker thread."""
    repo.workdir.mkdir(parents=True, exist_ok=True)
    tag = f"#{repo.pr_number or '?':<{width}} |"
    emit = Emitter(tag, repo.workdir / "score-log.txt")
    out = {"repo": repo, "pr": repo.pr_number, "score": None, "error": None, "data": {}}

    try:
        bundle = task_bundle(repo)
        if bundle is None:
            out["error"] = "no task bundle under contributor_tasks/"
            emit(out["error"], "red")
            return out

        # read the measured rewards here rather than making the agent hunt for
        # them — it's exact, free, and removes the biggest source of error
        trials = read_trials(repo)
        out["baseline"] = trials["baseline"]
        out["reported"] = pr_reported_score(repo)
        if out["reported"] is not None:
            emit(f"PR reports {out['reported']:.4f}", "dim")
        if trials["baseline"] is None:
            measured = ("No recorded trials were found in this repository. You have no\n"
                        "measured baseline — reason from the rubric alone and set confidence "
                        "to `low`.")
            emit("no recorded trials — falling back to blind estimation", "yel")
        else:
            # the per-family hit rates are the prior for any criterion the
            # traces never judged — without them the agent invents optimistic
            # numbers for rewritten rubrics
            fam = task_metrics(repo).get("met_by_family") or {}
            fam_lines = [f"  {k:<14} {v['met']}/{v['total']} met ({v['rate']:.0%})"
                         for k, v in fam.items()] or ["  (none recorded)"]
            measured = "\n".join([
                f"Solver trials: {', '.join(f'{r:.4f}' for r in trials['solver'])}",
                f"MEASURED BASELINE (mean solver reward): {trials['baseline']:.6f}",
                f"Oracle: {', '.join(f'{r:.4f}' for r in trials['oracle']) or 'n/a'}",
                f"NOP: {', '.join(f'{r:.4f}' for r in trials['nop']) or 'n/a'}",
                "",
                "Historical hit rate per criterion family — this is your prior "
                "for any criterion the traces did not judge:",
                *fam_lines,
                "",
                "Per-criterion verdicts for these runs are in the sibling "
                "`verifier/judgment.json` of each trial directory:",
                *(f"  {f}" for f in trials["files"]),
            ])
            emit(f"measured baseline {trials['baseline']:.4f} "
                 f"from {len(trials['solver'])} trial(s)", "dim")

        prompt = SCORE_TEMPLATE.read_text().format(
            bundle=bundle.relative_to(repo.path), task_name=bundle.name,
            measured=measured)
        (repo.workdir / "score-prompt.txt").write_text(prompt)

        if args.dry_run:
            emit(f"dry run — prompt at {repo.workdir / 'score-prompt.txt'}", "blu")
            out["dry_run"] = True
            return out

        result_file = repo.workdir / "score-result.json"
        cmd = [CODX, "exec", "-C", str(repo.path),
               "-s", "read-only",            # estimating must never edit the task
               "--json", "-o", str(result_file),
               "--output-schema", str(SCORE_SCHEMA), "-"]

        emit(f"estimating score for {bundle.name}", "dim")
        with (repo.workdir / "score-events.jsonl").open("w") as events:
            proc = subprocess.Popen(cmd, cwd=repo.path, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                events.write(line)
                summarise_event(line, emit)
            proc.wait()

        if proc.returncode != 0:
            err = (proc.stderr.read() or "").strip()
            err = "\n".join(l for l in err.splitlines()
                            if "assignment_keepalive_loop" not in l).strip()
            out["error"] = err.splitlines()[-1] if err else f"codx exited {proc.returncode}"
            emit(out["error"], "red")
            return out

        try:
            data = json.loads(result_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            out["error"] = f"could not parse estimate: {e}"
            emit(out["error"], "red")
            return out

        out["data"] = data
        out["score"] = float(data.get("estimated_score"))
        emit(f"estimate {out['score']:.3f} ({data.get('confidence', '?')} confidence)",
             "grn" if out["score"] <= args.threshold else "yel")

        # a big move away from measurement is usually a rubric rewrite the
        # estimator has guessed optimistically about — surface it, don't hide it
        base = out.get("baseline")
        if isinstance(base, (int, float)):
            drift = out["score"] - base
            out["drift"] = drift
            if abs(drift) > BIG_DRIFT:
                emit(f"NOTE: {drift:+.3f} from the measured {base:.3f} — "
                     "check the reasoning before trusting this", "yel")
        return out

    except Exception as e:
        out["error"] = str(e)
        emit(f"unexpected error: {e}", "red")
        return out
    finally:
        emit.close()


def cmd_score(args) -> None:
    """Estimate each task's reward locally, since /bot rescore is unavailable."""
    repos = select(discover(), args)
    jobs = args.jobs if args.jobs and args.jobs > 0 else len(repos)
    jobs = min(max(jobs, 1), len(repos))
    width = max((len(r.pr_number or "?") for r in repos), default=4)

    say(f"Estimating scores for {len(repos)} task(s), {jobs} at a time", "bld")
    say(f"threshold: {args.threshold}  (at or below = good, the task is hard enough)\n", "dim")

    started = time.time()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(lambda r: score_one(r, args, width), repos))
    results.sort(key=lambda r: r["pr"] or "")

    # the bot's PASS/WARN/FAIL is cheap to fetch and belongs next to the score:
    # a task can pass review and still be too easy, which is the case worth seeing
    reviews: dict[str, str] = {}
    if have_gh():
        with ThreadPoolExecutor(max_workers=max(1, len(repos))) as pool:
            for v in pool.map(read_verdict, repos):
                reviews[v["repo"].name] = "—" if v["error"] else v["verdict"]

    elapsed = int(time.time() - started)

    if args.dry_run:
        say(f"\ndry run — {len(results)} prompt(s) written, no agent started", "blu")
        for r in results:
            say(f"  {r['repo'].workdir / 'score-prompt.txt'}", "dim")
        return

    say(f"\n{'=' * 78}", "dim")
    say(f"Finished in {elapsed // 60}m {elapsed % 60}s\n", "bld")
    say(f"{'PR':<{width + 2}} {'OLD':<8} {'MEASURED':<9} {'NEW':<8} {'CHANGE':<9} "
        f"{'REVIEW':<7} {'SCORE':<7} CONF", "bld")
    say(f"{'':<{width + 2}} {'(PR)':<8} {'(traces)':<9} {'(est.)':<8} {'':<9} {'(bot)':<7}", "dim")

    over = []
    for r in results:
        review = reviews.get(r["repo"].name, "—")

        if r["error"]:
            say(f"#{r['pr']:<{width}}  {'—':<8} {'—':<9} {'—':<8} {'—':<9} "
                f"{review:<7} {'error':<7} {r['error'][:28]}", "red")
            continue

        below = r["score"] <= args.threshold
        if not below:
            over.append(r["repo"])

        old = r.get("reported")
        base = r.get("baseline")
        fmt = lambda v: f"{v:.3f}" if isinstance(v, (int, float)) else "—"

        # movement against whichever prior number we have, PR-stated first
        prior = old if isinstance(old, (int, float)) else base
        if isinstance(prior, (int, float)):
            d = r["score"] - prior
            change = f"{d:+.3f}"
        else:
            change = "—"

        conf = str(r["data"].get("confidence", "?"))
        if abs(r.get("drift") or 0) > BIG_DRIFT:
            conf += "  <-- big jump, verify"
        say(f"#{r['pr']:<{width}}  {fmt(old):<8} {fmt(base):<9} {r['score']:<8.3f} "
            f"{change:<9} {review:<7} {'below' if below else 'ABOVE':<7} {conf}",
            "grn" if below else "red")

    say("")
    if over:
        say(f"{len(over)} task(s) estimated above {args.threshold} — "
            "too easy, likely to be rejected:", "red")
        say(f"  python3 run.py fix --task {','.join(r.pr_number for r in over)}")
        # the dangerous combination: the bot is happy but the task is too easy,
        # so nothing else in the pipeline will catch it
        if sneaky := [r for r in over if reviews.get(r.name) == "PASS"]:
            say(f"  note: #{','.join(r.pr_number for r in sneaky)} "
                f"{'is' if len(sneaky) == 1 else 'are'} passing fairness review "
                "despite this — the bot will not flag it", "yel")
    else:
        say(f"All estimates are at or below {args.threshold}.", "grn")
    if not args.dry_run and (freed := prune(*(r["repo"].workdir for r in results))):
        say(f"cleaned up {human(freed)} of intermediates", "dim")
    say(f"\nFull reasoning: {WORK}/<repo>/score-result.json", "dim")


def run_codx_json(repo_path: Path, prompt: str, schema: Path, out_file: Path,
                  events_file: Path, emit) -> tuple[dict | None, str]:
    """Run codx read-only with a JSON output schema. Returns (parsed, error)."""
    # the synthesis pass runs from the container folder, which is not itself a
    # git repo; codx refuses that without an explicit opt-out
    cmd = [CODX, "exec", "-C", str(repo_path), "-s", "read-only", "--json",
           "--skip-git-repo-check",
           "-o", str(out_file), "--output-schema", str(schema), "-"]
    with events_file.open("w") as events:
        proc = subprocess.Popen(cmd, cwd=repo_path, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            events.write(line)
            summarise_event(line, emit)
        proc.wait()

    if proc.returncode != 0:
        err = (proc.stderr.read() or "").strip()
        err = "\n".join(l for l in err.splitlines()
                        if "assignment_keepalive_loop" not in l).strip()
        return None, err.splitlines()[-1] if err else f"codx exited {proc.returncode}"
    try:
        return json.loads(out_file.read_text()), ""
    except (OSError, json.JSONDecodeError) as e:
        return None, f"could not parse output: {e}"


def cell(text: str) -> str:
    """Flatten text so it cannot break the markdown table it lands in."""
    return " ".join((text or "").split()).replace("|", "/")


def audit_one(repo: Repo, args, width: int) -> dict:
    """Round-two read-only review of one finished task. Runs in a worker thread."""
    repo.workdir.mkdir(parents=True, exist_ok=True)
    emit = Emitter(f"#{repo.pr_number or '?':<{width}} |", repo.workdir / "audit-log.txt")
    out = {"repo": repo, "pr": repo.pr_number, "data": None, "error": None}

    try:
        if task_bundle(repo) is None:
            out["error"] = "no task bundle"
            emit(out["error"], "red")
            return out

        # the discussion is context for what round one was asked to fix; an
        # audit still stands on its own if GitHub cannot be reached
        pr_context, _ = fetch_pr_context(repo, emit, None, fairness_only=True)

        prompt = AUDIT_TEMPLATE.read_text().format(
            repo_slug=repo.slug,
            pr_number=repo.pr_number or "?",
            pr_url=(f"https://github.com/{repo.slug}/pull/{repo.pr_number}"
                    if repo.pr_number else ""),
            bundle_facts=bundle_facts(repo),
            rules=format_rules(repo),
            qc_guidelines=guidance(QC_TEMPLATE, "QC guideline set"),
            pr_context=pr_context or "(no discussion could be retrieved)",
        )
        (repo.workdir / "audit-prompt.txt").write_text(prompt)

        if args.dry_run:
            emit(f"dry run — prompt at {repo.workdir / 'audit-prompt.txt'}", "blu")
            out["error"] = f"dry run ({len(prompt)} chars)"
            return out

        emit("auditing (read-only)", "dim")
        data, err = run_codx_json(
            repo.path, prompt, AUDIT_SCHEMA,
            repo.workdir / "audit-result.json",
            repo.workdir / "audit-events.jsonl", emit)

        if err:
            out["error"] = err
            emit(err, "red")
            return out

        out["data"] = data
        verdict = (data or {}).get("verdict", "?")
        emit(f"{verdict}: {(data or {}).get('headline', '')}",
             {"ok": "grn", "minor": "yel", "major": "red"}.get(verdict, ""))
        return out

    except Exception as e:
        out["error"] = str(e)
        emit(f"unexpected error: {e}", "red")
        return out
    finally:
        emit.close()


def cmd_audit(args) -> None:
    """Round two: read-only review of finished tasks. Never edits anything."""
    repos = select(discover(), args)
    if not repos:
        say("no repos selected", "yel")
        return

    if not args.deep:
        cmd_audit_fast(args, repos)
        return

    jobs = args.jobs if args.jobs and args.jobs > 0 else len(repos)
    jobs = min(max(jobs, 1), len(repos))
    width = max(len(r.pr_number or "?") for r in repos)

    say(f"Auditing {len(repos)} task(s) with an agent, {jobs} at a time — "
        "read-only, nothing will be changed\n", "bld")

    results = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(audit_one, r, args, width): r for r in repos}
        for fut in as_completed(futures):
            results.append(fut.result())

    if args.dry_run:
        say(f"\ndry run — {len(results)} prompt(s) written, no agent started", "blu")
        return

    results.sort(key=lambda r: (
        {"major": 0, "minor": 1, "ok": 2}.get((r["data"] or {}).get("verdict"), 3),
        r["pr"] or ""))

    # a markdown table: one row per task, so the whole result pastes into a doc
    # or a PR comment without reformatting
    rows, counts = [], {}
    for r in results:
        pr = r["pr"] or "?"
        data = r["data"]
        if not data:
            counts["error"] = counts.get("error", 0) + 1
            rows.append((pr, "error", "—", "—", cell(r["error"] or "")))
            continue

        verdict = data.get("verdict", "?")
        counts[verdict] = counts.get(verdict, 0) + 1

        # audit reports one numeric rule only: fresh input context. The rest
        # (points, ratios, tiers) are a fix-round concern and belong to `rules`
        # and `check`, not to a round-two review.
        _, failing = rules_summary(r["repo"])
        broke = "; ".join(f.get("short") or f["rule"] for f in failing
                          if "fresh input context" in f["rule"]) or "ok"

        shown = [i for i in (data.get("issues") or [])
                 if args.verbose or i.get("severity") != "low"]
        def phrase(i: dict) -> str:
            detail = (i.get("detail") or "").rstrip(".")
            cid = i.get("criterion_id") or ""
            # the agent is told to name the criterion, and usually does — only
            # prefix it when the sentence does not already carry it
            lead = f"{cid}: " if cid and cid.lower() not in detail.lower() else ""
            return f"{lead}{detail}."

        prose = " ".join(phrase(i) for i in shown) or "No defects found."

        rows.append((pr, verdict, str(data.get("criteria_checked", 0)),
                     cell(broke), cell(prose)))

    head = ("PR", "Verdict", "Criteria", "Fresh context", "Issues")
    last = len(head) - 1
    # every column is padded to line up except Issues, which is free prose —
    # padding it would trail hundreds of spaces onto each row
    w = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
         for i, h in enumerate(head)]

    def line(cells, colour="") -> None:
        say("| " + " | ".join(c if i == last else c.ljust(w[i])
                              for i, c in enumerate(cells)) + " |", colour)

    say("")
    line(head)
    line(tuple("-" * w[i] for i in range(len(head))))
    for row in rows:
        line(row, {"ok": "grn", "minor": "yel", "major": "red"}.get(row[1], ""))

    say("")
    parts = [f"{n} {k}" for k, n in sorted(counts.items()) if n]
    say(f"{len(results)} audited — {', '.join(parts)}", "bld")
    if bad := [r["pr"] for r in results
               if (r["data"] or {}).get("verdict") == "major" and r["pr"]]:
        say(f"  python3 run.py fix --task {','.join(bad)} --redo   "
            "# the majors need another round", "yel")
    if not args.verbose and any((r["data"] or {}).get("issues") for r in results):
        say("  -v shows low-severity findings too", "dim")
    say(f"  full findings: {WORK}/<repo>/audit-result.json", "dim")

    prune(*(r["repo"].workdir for r in results))


def cmd_audit_fast(args, repos: list[Repo]) -> None:
    """The default audit: fresh input context, and nothing else.

    Read straight out of each bundle's trace summary — no GitHub call, no agent.
    Rubric quality is the bot's fairness review and the `rules` command; this
    answers one question only.
    """
    rows, failed, unknown = [], [], []
    reference = MIN_FRESH_CONTEXT

    def measure(repo: Repo) -> tuple[float | None, int]:
        """(tokens, bar). Falls back to the PR when the bundle has no summary."""
        fresh, ref = read_fresh_context(repo)
        return (fresh if fresh is not None else fresh_from_github(repo)), ref

    with ThreadPoolExecutor(max_workers=max(1, len(repos))) as pool:
        measured = list(pool.map(measure, sorted(repos, key=lambda r: r.pr_number or "")))

    for repo, (fresh, reference) in zip(
            sorted(repos, key=lambda r: r.pr_number or ""), measured):
        bar = f"{reference / 1000:.0f}k"
        if fresh is None:
            result, tokens = "UNKNOWN", "—"
            reason = f"UNKNOWN, mean token size not recorded in the bundle or on the PR"
            unknown.append(repo.pr_number or repo.name)
        else:
            ok = fresh >= reference
            result = "PASS" if ok else "FAIL"
            tokens = f"{fresh:,.0f}"
            # the reason carries its own verdict so it still reads correctly
            # once pasted into a sheet, away from the Result column
            reason = (f"{result}, mean token size {fresh:,.0f} "
                      f"{'>=' if ok else '<'} {bar}")
            if not ok:
                failed.append(repo.pr_number or repo.name)
        rows.append((repo.pr_number or "?", result, tokens, reason))

    head = ("PR", "Result", "Mean tokens", "Reason")
    w = [max(len(h), *(len(row[i]) for row in rows)) if rows else len(h)
         for i, h in enumerate(head)]

    last = len(head) - 1

    def line(cells, colour="") -> None:
        # Reason is free prose and sets its own width; padding it would trail
        # spaces across every row
        say("| " + " | ".join(c if i == last else c.ljust(w[i])
                              for i, c in enumerate(cells)) + " |", colour)

    say(f"Fresh input context — {len(repos)} task(s), need >= {reference:,}\n", "bld")
    line(head)
    line(tuple("-" * w[i] for i in range(len(head))))
    for row in rows:
        line(row, {"PASS": "grn", "FAIL": "red"}.get(row[1], "yel"))

    say("")
    say(f"{len(rows) - len(failed) - len(unknown)} pass, {len(failed)} fail"
        + (f", {len(unknown)} unknown" if unknown else ""), "bld")
    if failed:
        say(f"  FAIL: {', '.join(failed)}", "red")

    # tab-separated so a straight copy lands in two Excel columns; no colour,
    # nothing to strip out afterwards
    say("\nReasons — copy from here into Excel:\n", "bld")
    for pr, _, _, reason in rows:
        say(f"{pr}\t{reason}")


def analyse_one(repo: Repo, args, width: int) -> dict:
    """Diagnose why one task scores what it scores. Runs in a worker thread."""
    repo.workdir.mkdir(parents=True, exist_ok=True)
    emit = Emitter(f"#{repo.pr_number or '?':<{width}} |", repo.workdir / "analysis-log.txt")
    out = {"repo": repo, "pr": repo.pr_number, "data": None, "error": None}

    try:
        bundle = task_bundle(repo)
        if bundle is None:
            out["error"] = "no task bundle"
            emit(out["error"], "red")
            return out

        metrics = task_metrics(repo)
        out["metrics"] = metrics
        base = metrics.get("baseline")
        prompt = ANALYSIS_TEMPLATE.read_text().format(
            bundle=bundle.relative_to(repo.path), task_name=bundle.name,
            pr_number=repo.pr_number or "?",
            score=f"{base:.3f}" if isinstance(base, (int, float)) else "unknown",
            rules=format_rules(repo),
            metrics=json.dumps(metrics, indent=2, default=str))
        (repo.workdir / "analysis-prompt.txt").write_text(prompt)

        if args.dry_run:
            emit(f"dry run — prompt at {repo.workdir / 'analysis-prompt.txt'}", "blu")
            out["error"] = "dry run"
            return out

        base = metrics.get("baseline")
        emit(f"analysing {bundle.name}" + (f" (measured {base:.3f})" if base else ""), "dim")
        data, err = run_codx_json(
            repo.path, prompt, ANALYSIS_SCHEMA,
            repo.workdir / "analysis-result.json",
            repo.workdir / "analysis-events.jsonl", emit)

        if err:
            out["error"] = err
            emit(err, "red")
            return out

        out["data"] = data
        emit(f"done — {data.get('verdict')}, {data.get('difficulty_type')}", "grn")
        return out

    except Exception as e:
        out["error"] = str(e)
        emit(f"unexpected error: {e}", "red")
        return out
    finally:
        emit.close()


def estimate_score(repo: Repo, args, width: int) -> tuple[float | None, str]:
    """Run the score estimator once. Returns (score, error)."""
    est_args = argparse.Namespace(threshold=args.threshold, dry_run=False,
                                  all_comments=False)
    r = score_one(repo, est_args, width)
    return r.get("score"), r.get("error") or ""


def harden_one(repo: Repo, args, width: int) -> dict:
    """Make one task harder, re-estimating after each attempt."""
    repo.workdir.mkdir(parents=True, exist_ok=True)
    emit = Emitter(f"#{repo.pr_number or '?':<{width}} |", repo.workdir / "harden-log.txt")
    out = {"repo": repo, "pr": repo.pr_number, "start": None,
           "attempts": [], "final": None, "error": None}

    try:
        bundle = task_bundle(repo)
        if bundle is None:
            out["error"] = "no task bundle"
            return out
        if repo.is_dirty():
            out["error"] = "working tree dirty — commit, stash or --discard-dirty"
            emit(out["error"], "yel")
            return out

        start = read_trials(repo)["baseline"]
        out["start"] = start
        if start is None:
            out["error"] = "no measured score to work from"
            emit(out["error"], "yel")
            return out

        emit(f"starting at {start:.3f}, target at or below {args.threshold}", "bld")
        if start < args.threshold:
            emit("already below threshold — nothing to do", "grn")
            out["final"] = start
            return out

        current = start
        for attempt in range(1, args.attempts + 1):
            emit(f"attempt {attempt}/{args.attempts}", "bld")

            prompt = HARDEN_TEMPLATE.read_text().format(
                bundle=bundle.relative_to(repo.path), task_name=bundle.name,
                score=f"{current:.3f}", threshold=args.threshold)
            (repo.workdir / "harden-prompt.txt").write_text(prompt)

            code, stderr = run_agent(repo, prompt, "workspace-write", args.network, emit)
            if code != 0:
                out["error"] = stderr.splitlines()[-1] if stderr else f"codx exited {code}"
                emit(out["error"], "red")
                break

            diff = capture_diff(repo)
            if not diff.strip():
                emit("no changes made — stopping", "yel")
                out["attempts"].append({"n": attempt, "changed": 0, "score": current,
                                        "note": agent_conclusion(repo)})
                break

            files = diff.count("\ndiff --git") + diff.startswith("diff --git")
            emit(f"changed {files} file(s), re-estimating...", "dim")

            new, err = estimate_score(repo, args, width)
            if err or new is None:
                emit(f"could not re-estimate: {err}", "yel")
                out["attempts"].append({"n": attempt, "changed": files,
                                        "score": None, "note": err})
                break

            moved = new - current
            emit(f"estimate {current:.3f} -> {new:.3f} ({moved:+.3f})",
                 "grn" if new < args.threshold else "yel")
            out["attempts"].append({"n": attempt, "changed": files, "score": new,
                                    "note": agent_conclusion(repo)})
            current = new

            if new < args.threshold:
                emit("below threshold — stopping", "grn")
                break

        out["final"] = current
        mark(repo, "hardened" if current < args.threshold else "still-too-easy",
             start_score=start, final_estimate=current)
        return out

    except Exception as e:
        out["error"] = str(e)
        emit(f"unexpected error: {e}", "red")
        return out
    finally:
        emit.close()


def cmd_harden(args) -> None:
    """Make too-easy tasks harder, checking after each attempt whether it worked."""
    repos = select(discover(), args)

    if not args.force:
        eligible = []
        for r in repos:
            base = read_trials(r)["baseline"]
            if base is not None and base > args.threshold:
                eligible.append(r)
            else:
                shown = f"{base:.3f}" if base is not None else "no measured score"
                say(f"skipping #{r.pr_number}: {shown} — not too easy (--force to override)", "dim")
        repos = eligible

    if not repos:
        say("no tasks need hardening", "grn")
        return

    width = max(len(r.pr_number or "?") for r in repos)
    jobs = min(max(args.jobs or len(repos), 1), len(repos))
    say(f"Hardening {len(repos)} task(s), up to {args.attempts} attempt(s) each", "bld")
    say(f"target: below {args.threshold}\n", "dim")

    started = time.time()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(lambda r: harden_one(r, args, width), repos))
    results.sort(key=lambda r: r["pr"] or "")

    elapsed = int(time.time() - started)
    say(f"\n{'=' * 70}", "dim")
    say(f"Finished in {elapsed // 60}m {elapsed % 60}s\n", "bld")
    say(f"{'PR':<{width + 2}} {'BEFORE':<8} {'AFTER':<8} {'CHANGE':<9} RESULT", "bld")

    for r in results:
        if r["error"] and not r["attempts"]:
            say(f"#{r['pr']:<{width}}  {'—':<8} {'—':<8} {'—':<9} {r['error'][:40]}", "red")
            continue
        s, f = r["start"], r["final"]
        ok = f is not None and f < args.threshold
        say(f"#{r['pr']:<{width}}  {s:<8.3f} "
            f"{(f'{f:.3f}' if f is not None else '—'):<8} "
            f"{(f'{f - s:+.3f}' if f is not None else '—'):<9} "
            f"{'below threshold' if ok else 'STILL TOO EASY'} "
            f"({len(r['attempts'])} attempt(s))", "grn" if ok else "red")

    prune(*(r["repo"].workdir for r in results))
    say("\nEstimates only — the real score needs a solver run.", "yel")
    say("Nothing pushed. Review the changes first:", "bld")
    say(f"  python3 run.py review --task {','.join(r['pr'] for r in results if r['attempts'])}")


def cmd_analysis_score(args) -> None:
    """Per-task diagnosis plus a cross-task synthesis of what drives the scores."""
    repos = select(discover(), args)
    jobs = args.jobs if args.jobs and args.jobs > 0 else len(repos)
    jobs = min(max(jobs, 1), len(repos))
    width = max((len(r.pr_number or "?") for r in repos), default=4)
    outdir = WORK / "analysis"
    outdir.mkdir(parents=True, exist_ok=True)

    started = time.time()

    if args.synthesis_only:
        # stage 1 is expensive; reuse what's already on disk
        say("reusing existing per-task analyses", "bld")
        results = []
        for repo in repos:
            path = repo.workdir / "analysis-result.json"
            if not path.exists():
                say(f"  #{repo.pr_number}: no cached analysis — run without "
                    "--synthesis-only first", "yel")
                continue
            try:
                results.append({"repo": repo, "pr": repo.pr_number,
                                "data": json.loads(path.read_text()),
                                "metrics": task_metrics(repo), "error": None})
                say(f"  #{repo.pr_number}: loaded", "dim")
            except json.JSONDecodeError as e:
                say(f"  #{repo.pr_number}: cached analysis unreadable ({e})", "red")
        results.sort(key=lambda r: r["pr"] or "")
    else:
        say(f"Analysing {len(repos)} task(s), {jobs} at a time", "bld")
        say("stage 1: per-task diagnosis   stage 2: cross-task synthesis\n", "dim")
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(lambda r: analyse_one(r, args, width), repos))
        results.sort(key=lambda r: r["pr"] or "")

    ok = [r for r in results if r["data"]]
    for r in results:
        if r["error"] and r["error"] != "dry run":
            say(f"  #{r['pr']}: {r['error']}", "red")

    if args.dry_run:
        say("\ndry run — no synthesis", "blu")
        return
    if not ok:
        say("no task analyses succeeded; nothing to synthesise", "red")
        return

    # stage 2 — one pass over all the per-task findings at once
    say(f"\nSynthesising trends across {len(ok)} task(s)...", "bld")
    emit = Emitter("synth |", outdir / "synthesis-log.txt")
    synth, err = None, ""
    try:
        scores = {r["pr"]: (r.get("metrics") or {}).get("baseline") for r in ok}
        prompt = SYNTHESIS_TEMPLATE.read_text().format(
            scores=json.dumps(scores, indent=2, default=str),
            analyses=json.dumps([r["data"] for r in ok], indent=2, default=str))
        (outdir / "synthesis-prompt.txt").write_text(prompt)
        synth, err = run_codx_json(
            ROOT, prompt, SYNTHESIS_SCHEMA, outdir / "synthesis.json",
            outdir / "synthesis-events.jsonl", emit)
    finally:
        emit.close()

    if err:
        say(f"synthesis failed: {err}", "red")

    report = write_analysis_report(ok, synth, outdir)
    freed = prune(outdir, *(r["repo"].workdir for r in results), keep=args.keep)
    elapsed = int(time.time() - started)

    say(f"\n{'=' * 78}", "dim")
    say(f"Finished in {elapsed // 60}m {elapsed % 60}s\n", "bld")

    if synth:
        say(f"{'PR':<{width + 2}} {'SCORE':<7} {'STATUS':<11} DIFFICULTY", "bld")
        for r in sorted(ok, key=lambda r: (r.get("metrics") or {}).get("baseline") or 0):
            d = r["data"]
            s = (r.get("metrics") or {}).get("baseline")
            health = str(d.get("score_health", ""))
            say(f"#{str(d.get('pr', r['pr'])):<{width}}  "
                f"{(f'{s:.3f}' if isinstance(s,(int,float)) else '—'):<7} "
                f"{health:<11} {str(d.get('difficulty_source',''))}",
                "grn" if health == "healthy" else "yel" if health == "borderline" else "red")
        say(f"\n{synth.get('headline', '')}", "bld")

    if freed:
        say(f"\ncleaned up {human(freed)} of intermediates (--keep to retain)", "dim")
    say(f"\nReport: {report}", "grn")



def first_sentence(text: str, limit: int = 200) -> str:
    """Keep reports skimmable — agents write two sentences where one will do."""
    for end in (". ", "; "):
        if (i := text.find(end)) != -1 and i < limit:
            return text[:i + 1].strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def write_analysis_report(results: list[dict], synth: dict | None, outdir: Path) -> Path:
    """A short briefing: what about how these tasks are built drives the score."""
    L: list[str] = ["# Task difficulty review", "",
                    "*Score is what a strong solver achieves. Below 0.5 is good "
                    "— the task is hard enough. 0.5 or above is too easy.*", ""]

    if synth:
        L += [f"**{synth.get('headline', '')}**", ""]

        def points(key: str, title: str) -> list[str]:
            rows = synth.get(key) or []
            if not rows:
                return []
            out = [f"## {title}", ""]
            for f in rows:
                # the agent sometimes names tasks and sometimes gives PR numbers
                tasks = ", ".join(
                    f"#{t}" if (t := str(x).lstrip("#")).isdigit() else t
                    for x in f.get("tasks") or [])
                out.append(f"- {f.get('point','')}" + (f" — {tasks}" if tasks else ""))
            return out + [""]

        L += points("keeps_difficulty_real", "What keeps these tasks hard")
        L += points("makes_tasks_too_easy", "What makes them too easy")
        L += points("false_difficulty", "Hard for the wrong reasons")

        if rec := synth.get("recommendations"):
            L += ["## Do this", ""]
            L += [f"{i}. {x}" for i, x in enumerate(rec, 1)] + [""]

    L += ["## Tasks", "",
          "| PR | Score | Status | Difficulty | Weak parts | Fix |",
          "|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: (r.get("metrics") or {}).get("baseline") or 0):
        d = r["data"]
        # the measured baseline is authoritative; the agent's own score field
        # has come back wrong before
        s = (r.get("metrics") or {}).get("baseline")
        weak = ", ".join(c.get("part", "") for c in (d.get("components") or [])
                         if c.get("quality") == "poor") or "—"
        L.append("| #{} | {} | {} | {} | {} | {} |".format(
            d.get("pr", r["pr"]),
            f"{s:.3f}" if isinstance(s, (int, float)) else "—",
            d.get("score_health", ""), d.get("difficulty_source", ""),
            weak, first_sentence(" ".join((d.get("top_fix") or "").split()), 110)))

    L += ["", "## Per task", ""]
    for r in sorted(results, key=lambda r: (r.get("metrics") or {}).get("baseline") or 0):
        d = r["data"]
        s = (r.get("metrics") or {}).get("baseline")
        head = f"**#{d.get('pr', r['pr'])} {d.get('task_name','')}**"
        if isinstance(s, (int, float)):
            head += f" — {s:.3f}, {d.get('score_health','')}, " \
                    f"{d.get('difficulty_source','')} difficulty"
        L += [head, "", d.get("why_this_score", ""), ""]

        # only the parts actually rated poor; the rest is in the JSON
        for c in d.get("components") or []:
            if c.get("quality") != "poor":
                continue
            issue = " ".join((c.get("issue") or "").split())
            L.append(f"- **{c.get('part','')}** — {first_sentence(issue)}")
        L += [""]

    L += ["---", f"*{len(results)} tasks · {time.strftime('%Y-%m-%d %H:%M')}*"]

    path = outdir / "report.md"
    path.write_text("\n".join(L))
    return path



def cmd_trigger(args) -> None:
    """Ask the bot to review freshly cloned tasks, before any fixing starts.

    `push` posts the same command after a fix round; this covers the other end,
    where a task has just landed and nothing has reviewed it yet.
    """
    repos = select(discover(), args)
    explicit = bool(getattr(args, "only", None) or getattr(args, "task", None))
    if not explicit:
        # a repo the pipeline has never acted on is the one that needs asking
        repos = [r for r in repos if status_of(r) == "new"]
    repos = [r for r in repos if r.pr_number]

    if not repos:
        say("nothing to trigger — no new repos with a PR number", "yel")
        return
    if not have_gh():
        say("gh is not installed — can't post", "red")
        return

    # a task under the token bar fails on that alone; asking the bot to review
    # it spends a run on a task that cannot pass
    if not args.starved:
        def measure(repo: Repo) -> tuple[Repo, float | None, int]:
            fresh, ref = read_fresh_context(repo)
            return repo, (fresh if fresh is not None else fresh_from_github(repo)), ref

        with ThreadPoolExecutor(max_workers=max(1, len(repos))) as pool:
            measured = list(pool.map(measure, repos))
        skipped = [(r, f, ref) for r, f, ref in measured if f is not None and f < ref]
        repos = [r for r, f, ref in measured if not (f is not None and f < ref)]
        if skipped:
            say(f"skipping {len(skipped)} PR(s) below the token bar "
                "(--starved to include them):", "yel")
            for r, f, ref in skipped:
                say(f"  #{r.pr_number}  {f / 1000:.0f}k < {ref / 1000:.0f}k", "dim")
            say("")
        if not repos:
            say("nothing left to trigger", "yel")
            return

    # both reviews unless -b named one explicitly
    bodies = [args.body] if args.body else list(REVIEW_COMMANDS)
    say(f"About to post {' + '.join(repr(b) for b in bodies)} on "
        f"{len(repos)} PR(s):\n", "bld")
    for r in repos:
        say(f"  #{r.pr_number}  {r.name}", "dim")
    if not args.yes:
        if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
            say("aborted", "yel")
            return

    posted = 0
    for repo in repos:
        results = [post_pr_comment(repo, body) for body in bodies]
        ok = all(good for good, _ in results)
        detail = "; ".join(d for good, d in results if not good)
        if ok:
            posted += 1
            say(f"  #{repo.pr_number}: posted {' + '.join(bodies)}", "grn")
            mark(repo, "review-requested", pr=repo.pr_number,
                 requested_at=datetime.now().astimezone().isoformat())
        else:
            say(f"  #{repo.pr_number}: {detail}", "red")

    say(f"\nRequested on {posted}/{len(repos)} PR(s).", "bld")
    if posted:
        say("  python3 run.py check      # once the bot has replied", "dim")


def repair_one(repo: Repo, args, width: int) -> tuple[str, str]:
    """Check a failing rescore against the bundle and fix it if it is real."""
    repo.workdir.mkdir(parents=True, exist_ok=True)
    tag = f"#{repo.pr_number or '?':<{width}} |"
    emit = Emitter(tag, repo.workdir / "repair-log.txt")

    try:
        if task_bundle(repo) is None:
            return "failed", "no task bundle"
        if repo.is_dirty() and not args.discard_dirty:
            n = len(repo.git("status", "--porcelain").splitlines())
            emit(f"working tree has {n} uncommitted file(s) — skipping", "yel")
            return "skipped", f"{n} uncommitted file(s)"
        if repo.is_dirty():
            backup_working_tree(repo)
            repo.git("reset", "--hard")

        report = fetch_rescore_report(repo, emit)
        if not report:
            emit("no rescore result on this PR", "yel")
            return "no-context", "no rescore result to act on"

        prompt = REPAIR_TEMPLATE.read_text().format(
            repo_slug=repo.slug,
            branch=repo.branch,
            pr_number=repo.pr_number or "?",
            pr_url=(f"https://github.com/{repo.slug}/pull/{repo.pr_number}"
                    if repo.pr_number else ""),
            bundle_facts=bundle_facts(repo),
            rules=format_rules(repo),
            qc_guidelines=guidance(QC_TEMPLATE, "QC guideline set"),
            rescore_report=report,
        )
        (repo.workdir / "repair-prompt.txt").write_text(prompt)
        if args.dry_run:
            emit(f"dry run — prompt at {repo.workdir / 'repair-prompt.txt'}", "blu")
            return "dry-run", f"{len(prompt)} char prompt, agent not started"

        code, stderr = run_agent(repo, prompt, DEFAULT_SANDBOX, args.network, emit,
                                 getattr(args, "timeout", 0) * 60)
        if code == TIMED_OUT:
            capture_diff(repo)
            mark(repo, "timed-out", pr=repo.pr_number, note=stderr)
            return "timed-out", stderr
        if code != 0:
            detail = stderr.splitlines()[-1] if stderr else f"exit {code}"
            mark(repo, "failed", exit_code=code, note=detail)
            return "failed", detail

        diff = capture_diff(repo)
        why = agent_conclusion(repo)
        if not diff.strip():
            # the agent judging the complaint bogus is a real outcome, not a miss
            mark(repo, "no-changes", note=why or "verifier complaint not a task defect")
            return "no-changes", why or "nothing to change"

        files = diff.count("\ndiff --git") + diff.startswith("diff --git")
        if traces := touched_traces(repo):
            emit(f"WARNING: modified {len(traces)} recorded-run file(s)", "red")
        mark(repo, "fixed", pr=repo.pr_number, files=files)
        return "repaired", f"{files} file(s) changed"

    except Exception as e:
        emit(f"unexpected error: {e}", "red")
        return "failed", str(e)
    finally:
        emit.close()


def fetch_rescore_report(repo: Repo, emit) -> str:
    """The newest rescore comment, verbatim."""
    if not repo.pr_number or not have_gh():
        return ""
    proc = subprocess.run(
        ["gh", "pr", "view", repo.pr_number, "--repo", repo.slug, "--json", "comments"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        emit("could not read the PR", "yel")
        return ""
    try:
        rs = latest_rescore(json.loads(proc.stdout))
    except json.JSONDecodeError:
        return ""
    return (rs.get("body") or "").strip() if rs else ""


def cmd_repair(args) -> None:
    """Act on rescores the verifier could not complete."""
    repos = select(discover(), args)
    if not repos:
        say("no repos selected", "yel")
        return

    jobs = args.jobs if args.jobs and args.jobs > 0 else len(repos)
    jobs = min(max(jobs, 1), len(repos))
    width = max(len(r.pr_number or "?") for r in repos)

    say(f"Repairing {len(repos)} task(s) from their rescore reports, "
        f"{jobs} at a time\n", "bld")

    results: dict[str, tuple[str, str]] = {}
    colors = {"repaired": "grn", "no-changes": "yel", "dry-run": "blu",
              "failed": "red", "timed-out": "red", "skipped": "yel",
              "no-context": "yel"}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(repair_one, r, args, width): r for r in repos}
        for n, fut in enumerate(as_completed(futures), 1):
            repo = futures[fut]
            status, detail = results[repo.name] = fut.result()
            say(f"[{n}/{len(futures)}] {status:<11} #{repo.pr_number or '?':<{width}}  "
                f"{detail}", colors.get(status, ""))

    say("")
    if any(s == "repaired" for s, _ in results.values()):
        say("Nothing has been pushed.", "bld")
        say("  python3 run.py review      # read the diffs")
        say("  python3 run.py push        # commit and push")
    if not args.dry_run:
        prune(*(r.workdir for r in repos))


def cmd_comment(args) -> None:
    """Post any comment on the selected PRs."""
    repos = select(discover(), args)

    if args.file:
        path = Path(args.file)
        if not path.exists():
            say(f"no such file: {path}", "red")
            sys.exit(1)
        body = path.read_text().strip()
    else:
        body = "\n".join(args.message or []).strip()
    if not body:
        say("nothing to post — give a message with -m, or a file with -F", "yel")
        sys.exit(1)

    if not have_gh():
        say("gh is not installed — can't post comments", "red")
        say("  brew install gh && gh auth login", "yel")
        sys.exit(1)

    if missing := [r for r in repos if not r.pr_number]:
        for r in missing:
            say(f"skipping {r.name} — no PR number in the folder name", "yel")
        repos = [r for r in repos if r.pr_number]
    if not repos:
        say("no PRs selected", "yel")
        return

    width = max(len(r.pr_number) for r in repos)
    preview = body if len(body) <= 500 else body[:500] + f"\n… (+{len(body) - 500} chars)"
    say(f"About to post this on {len(repos)} pull request(s):\n", "bld")
    say("-" * 66, "dim")
    say(preview)
    say("-" * 66, "dim")
    for r in repos:
        say(f"  #{r.pr_number:<{width}}  {r.name}", "dim")

    # posting is public and outward-facing, so it always confirms unless you
    # explicitly opt out with -y
    if not args.yes:
        if input("\nPost? [y/N] ").strip().lower() not in ("y", "yes"):
            say("aborted — nothing posted", "yel")
            return

    posted = 0
    for repo in repos:
        ok, detail = post_pr_comment(repo, body)
        if ok:
            posted += 1
            say(f"  #{repo.pr_number}: posted", "grn")
        else:
            say(f"  #{repo.pr_number}: {detail}", "red")
    say(f"\nPosted on {posted}/{len(repos)} PR(s).", "bld")


def read_rescore(repo: Repo) -> dict:
    """One PR's latest rescore result. Read-only, network-bound."""
    out = {"repo": repo, "pr": repo.pr_number, "old": None, "new": None,
           "status": "none", "why": "", "trials": [], "when": None,
           "old_from": ""}
    if not (repo.pr_number and have_gh()):
        out["why"] = "no PR number" if not repo.pr_number else "gh not installed"
        return out
    proc = subprocess.run(
        ["gh", "pr", "view", repo.pr_number, "--repo", repo.slug,
         "--json", "body,comments"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        out["why"] = (proc.stderr.strip().splitlines() or ["gh failed"])[-1]
        return out
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out["why"] = "could not parse the PR"
        return out

    rs = latest_rescore(data)
    if rs:
        body = rs.get("body") or ""
        out["when"] = parse_ts(rs.get("createdAt"))
        out["status"], out["why"] = rescore_status(body)
        out["old"], out["new"], out["trials"] = rescore_scores(body)
        out["old_from"] = "pinned head" if out["old"] is not None else ""
    else:
        out["why"] = "never rescored"

    # a failed rescore produces no committed column, but the submitted mean is
    # always on the PR — showing it beats showing a dash
    if out["old"] is None:
        out["old"] = submitted_mean(data)
        out["old_from"] = "submitted" if out["old"] is not None else ""
    return out


def cmd_rescore_check(args, repos: list[Repo]) -> None:
    """Show what the last rescore measured. Posts nothing."""
    with ThreadPoolExecutor(max_workers=max(1, len(repos))) as pool:
        results = list(pool.map(read_rescore, repos))
    results.sort(key=lambda r: r["pr"] or "")

    width = max((len(r["pr"] or "?") for r in results), default=4)
    num = lambda v: f"{v:.3f}" if v is not None else "—"
    say(f"{'PR':<{width + 1}}  {'OLD':>7} {'NEW':>7} {'DELTA':>8}  {'STATUS':<8} "
        f"{'WHEN':<11} DETAIL", "bld")

    for r in results:
        old, new = r["old"], r["new"]
        delta = f"{new - old:+.3f}" if (old is not None and new is not None) else "—"
        status = {"ok": "ok", "failed": "FAILED", "partial": "partial",
                  "none": "—"}.get(r["status"], r["status"])
        when = f"{r['when']:%m-%d %H:%M}" if r["when"] else "—"
        colour = ("red" if r["status"] == "failed" else
                  "yel" if r["status"] in ("partial", "none") else "grn")
        # mark where OLD came from: a failed rescore has no committed column,
        # so the number shown is the mean the task was submitted with
        tag = "*" if r["old_from"] == "submitted" else " "
        say(f"#{r['pr'] or '?':<{width}}  {num(old):>7}{tag} {num(new):>7} {delta:>8}  "
            f"{status:<8} {when:<11} {r['why'][:52]}", colour)
        if args.verbose:
            for name, o, n in r["trials"]:
                shift = f"{n - o:+.3f}" if (o is not None and n is not None) else "—"
                say(f"{'':<{width + 3}}{name:<22} {num(o):>7} → {num(n):>7}  {shift:>8}",
                    "dim")

    say("")
    moved = [r for r in results if r["old"] is not None and r["new"] is not None
             and abs(r["new"] - r["old"]) > 5e-4]
    over = [r for r in results if r["new"] is not None and r["new"] > MAX_MEAN_SCORE]
    broken = [r for r in results if r["status"] == "failed"]
    say(f"{len(results)} PR(s): {len(moved)} moved, {len(over)} above "
        f"{MAX_MEAN_SCORE}, {len(broken)} failed", "bld")
    if over:
        say(f"  too easy: {','.join(r['pr'] for r in over if r['pr'])}", "red")
    if broken:
        say(f"  failed:   {','.join(r['pr'] for r in broken if r['pr'])}", "red")
    if any(r["old_from"] == "submitted" for r in results):
        say("  * OLD is the submitted mean — that rescore never produced its own "
            "baseline", "dim")
    if not args.verbose and any(r["trials"] for r in results):
        say("  -v shows the per-trial numbers", "dim")


def cmd_rescore(args) -> None:
    """Post a bot command (default /bot rescore) on the selected PRs."""
    repos = select(discover(), args)
    if args.check:
        cmd_rescore_check(args, repos)
        return
    body = args.body

    if not have_gh():
        say("gh is not installed — can't post comments", "red")
        say("  brew install gh && gh auth login", "yel")
        sys.exit(1)

    if missing := [r for r in repos if not r.pr_number]:
        for r in missing:
            say(f"skipping {r.name} — no PR number in the folder name", "yel")
        repos = [r for r in repos if r.pr_number]

    if not repos:
        say("no PRs selected", "yel")
        return

    width = max(len(r.pr_number) for r in repos)

    say(f"About to post {body!r} on {len(repos)} pull request(s):\n", "bld")
    for r in repos:
        last = load_state().get(r.name, {}).get("rescored_at")
        note = f"  (last rescored {parse_ts(last):%Y-%m-%d %H:%M})" if parse_ts(last) else ""
        say(f"  #{r.pr_number:<{width}}  {r.name}{note}")

    # posting is public and outward-facing, so it always confirms unless you
    # explicitly opt out with -y
    if not args.yes:
        if input("\nPost? [y/N] ").strip().lower() not in ("y", "yes"):
            say("aborted — nothing posted", "yel")
            return

    posted = 0
    for repo in repos:
        ok, detail = post_pr_comment(repo, body)
        if ok:
            say(f"  #{repo.pr_number:<{width}}  posted", "grn")
            mark(repo, status_of(repo),
                 rescored_at=datetime.now().astimezone().isoformat())
            posted += 1
        else:
            say(f"  #{repo.pr_number:<{width}}  failed: {detail}", "red")

    say(f"\nPosted on {posted}/{len(repos)} PR(s).", "bld")
    if posted:
        say("The bot's reply counts as a new comment, so once it lands:", "dim")
        say(f"  python3 run.py fix --task {','.join(r.pr_number for r in repos)}", "dim")


# --------------------------------------------------------------------------
# readability — is the bundle written for a person to read?
# --------------------------------------------------------------------------

# The prose files a human actually reads. `instruction.md` is solver-visible,
# so anything that later acts on a finding about it pays for a solver re-run —
# which is why `analyse` only ever reports.
INSTRUCTION_FILE = "instruction.md"
REPORT_FILE = "solution/report.md"

# Files and directories every bundle is expected to carry.
REQUIRED_PATHS = (
    "instruction.md", "task.toml", "tests/rubrics.json", "tests/test.sh",
    "solution/report.md", "solution/solve.sh", "environment", "traces",
)

# A line fragment repeated this many times is boilerplate, not writing.
REPEAT_MIN = 3
# ...and this long. Low, deliberately: the most repeated clause on a real
# report was "Supporting captured passages: ⟨link⟩." — 37 characters once the
# link is reduced, and it appeared eleven times.
FRAGMENT_MIN = 30
# One unwrapped line longer than this is a paragraph pretending to be a line.
LONG_LINE = 400
# A record written as a run of bold labels — "**Status:** established" — is a
# table with the columns turned sideways.
RECORD_LABEL_RE = re.compile(r"^\*\*([^*]{2,40}?):\*\*\s*(.*)$")
RECORD_RUN_MIN = 3          # bold labels in a row before it is a record block
# "Categories: [drama, comedy, documentary, short]" — an enumeration in prose.
# A markdown link is `[text](url)`, so anything followed by `(` is excluded.
INLINE_ENUM_RE = re.compile(r"([A-Za-z][\w /'-]{0,40}):\s*\[([^\]]{6,})\](?!\()")
# six or more comma-separated items inside one sentence
COMMA_RUN_RE = re.compile(r"(?:[^,.;:!?\n]{2,60},\s*){5,}[^,.;:!?\n]{2,60}")
FENCE_RE = re.compile(r"^\s*```")
# `[Some Long Page Title | Site](https://…)`. Citation titles carry commas and
# clauses of their own, so they have to come out before anything counts commas
# or measures a sentence — otherwise every citation reads as a list.
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")


def strip_links(text: str) -> str:
    """Markdown links reduced to a single token, so prose can be measured."""
    return MD_LINK_RE.sub("⟨link⟩", text)


def md_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every line outside a fenced code block."""
    out, fenced = [], False
    for i, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append((i, line))
    return out


def repeated_fragments(lines: list[tuple[int, str]]) -> list[tuple[str, int]]:
    """Sentence-length fragments that recur verbatim, most repeated first.

    Reports run on templates, so the same "Supporting captured passages: …"
    clause can appear on every paragraph. Comparing whole lines misses it —
    the surrounding sentence differs every time — so this compares the
    sentences inside each line.
    """
    seen: dict[str, int] = {}
    for _, line in lines:
        for part in re.split(r"(?<=[.;])\s+", strip_links(line).strip()):
            part = part.strip()
            if len(part) >= FRAGMENT_MIN:
                seen[part] = seen.get(part, 0) + 1
    return sorted(((f, n) for f, n in seen.items() if n >= REPEAT_MIN),
                  key=lambda p: -p[1])


def record_blocks(lines: list[tuple[int, str]]) -> list[tuple[int, list[str]]]:
    """Runs of `**Label:** value` lines — records written the long way round."""
    blocks, run, start = [], [], 0
    for num, line in lines:
        if m := RECORD_LABEL_RE.match(line.strip()):
            if not run:
                start = num
            run.append(m.group(1))
        else:
            if len(run) >= RECORD_RUN_MIN:
                blocks.append((start, run))
            run = []
    if len(run) >= RECORD_RUN_MIN:
        blocks.append((start, run))
    return blocks


def prose_findings(text: str, where: str) -> list[dict]:
    """Every readability finding in one markdown file."""
    lines = md_lines(text)
    body = "\n".join(l for _, l in lines)
    out: list[dict] = []

    def add(check, detail, examples, count):
        out.append({"check": check, "file": where, "count": count,
                    "detail": detail, "examples": examples[:4]})

    if repeats := repeated_fragments(lines):
        total = sum(n for _, n in repeats)
        add("repeated-lines",
            f"{len(repeats)} fragment(s) repeat verbatim, {total} occurrence(s) "
            "— boilerplate a table would state once",
            [f"×{n}  {oneline(f)[:100]}" for f, n in repeats], len(repeats))

    if blocks := record_blocks(lines):
        # every block sharing one set of labels is a table's worth of rows
        shapes = {" | ".join(ls) for _, ls in blocks}
        under_log = "decision log" in body.lower()
        name = "decision-log" if under_log and len(shapes) <= 2 else "record-not-table"
        add(name,
            f"{len(blocks)} record(s) written as runs of `**Label:** value`"
            + (f", all sharing the columns `{next(iter(shapes))}`"
               if len(shapes) == 1 else "")
            + " — that is a table with its columns turned sideways, and it is "
              "what makes a decision log unreadable in bulk",
            [f"line {n}: {' | '.join(ls[:6])}" for n, ls in blocks], len(blocks))

    enums = [(n, m.group(1), m.group(2)) for n, line in lines
             for m in [INLINE_ENUM_RE.search(line)] if m and m.group(2).count(",") >= 2]
    if enums:
        add("inline-enumeration",
            f"{len(enums)} bracketed list(s) in prose — parallel enumerations "
            "belong in a table, one item per row",
            [f"line {n}: {label}: [{oneline(items)[:60]}]" for n, label, items in enums],
            len(enums))

    runs = [(n, m.group(0)) for n, line in lines
            for m in [COMMA_RUN_RE.search(strip_links(line))] if m]
    if runs:
        add("comma-run",
            f"{len(runs)} sentence(s) list six or more items inline — a bullet "
            "list or table reads them in one pass",
            [f"line {n}: {oneline(t)[:100]}" for n, t in runs], len(runs))

    long_lines = [(n, len(l)) for n, l in lines if len(l) > LONG_LINE]
    if long_lines:
        add("wall-of-text",
            f"{len(long_lines)} line(s) over {LONG_LINE} characters with no "
            "structure — a reader cannot scan for the part they need",
            [f"line {n}: {c} chars" for n, c in long_lines], len(long_lines))

    if body.strip() and not any(l.lstrip().startswith("|") for _, l in lines):
        headings = sum(1 for _, l in lines if l.lstrip().startswith("#"))
        add("no-tables",
            f"no markdown table anywhere in {len(lines)} line(s) "
            f"({headings} heading(s)) — every fact is prose",
            [], 1)

    return out


def humanize_findings(repo: Repo) -> list[dict]:
    """Readability and structure findings for one bundle. Read-only."""
    bundle = task_bundle(repo)
    if bundle is None:
        return [{"check": "structure", "file": "contributor_tasks/",
                 "count": 1, "detail": "no task bundle found", "examples": []}]

    missing = [p for p in REQUIRED_PATHS if not (bundle / p).exists()]
    out = []
    if missing:
        out.append({"check": "structure", "file": bundle.name, "count": len(missing),
                    "detail": f"{len(missing)} expected path(s) missing",
                    "examples": missing})

    # every bundle carries the instruction twice, once for the solver and once
    # staged under tests/. They are byte-identical everywhere they both exist,
    # so a difference is drift nobody meant — and it means the solver and the
    # grader were working from different papers.
    solver, staged = bundle / INSTRUCTION_FILE, bundle / "tests" / INSTRUCTION_FILE
    if solver.exists() and staged.exists():
        try:
            if solver.read_bytes() != staged.read_bytes():
                out.append({
                    "check": "instruction-drift", "file": INSTRUCTION_FILE, "count": 1,
                    "detail": "instruction.md and tests/instruction.md differ — the "
                              "solver and the grader are reading different papers",
                    "examples": [f"{solver.stat().st_size}B vs "
                                 f"{staged.stat().st_size}B"]})
        except OSError:
            pass

    for rel in (INSTRUCTION_FILE, REPORT_FILE):
        path = bundle / rel
        if not path.exists():
            continue
        try:
            out += prose_findings(path.read_text(), rel)
        except OSError as e:
            out.append({"check": "unreadable", "file": rel, "count": 1,
                        "detail": str(e), "examples": []})
    return out


def cmd_analyse(args) -> None:
    """Judge whether each bundle is written for a person. Read-only throughout.

    Two stages. The deterministic checks are free and always run — they count
    what is countable: stacked records, repeated clauses, enumerations buried
    in sentences, missing files. `--deep` then puts an agent on the part a
    regex cannot see: whether the instruction is answerable as written, whether
    the report answers it, and whether either reads as a person's writing.

    Nothing here edits anything. Rewriting is a separate decision, and for
    `instruction.md` an expensive one.
    """
    repos = select(discover(), args)
    if not repos:
        say("no repos selected", "yel")
        return

    width = max((len(r.pr_number or r.name) for r in repos), default=4)
    with ThreadPoolExecutor(max_workers=max(1, len(repos))) as pool:
        found = dict(zip((r.name for r in repos),
                         pool.map(humanize_findings, repos)))

    say(f"{'PR':<{width + 1}}  {'FILE':<20} {'CHECK':<19} {'N':>4}  WHAT", "bld")
    for repo in repos:
        findings = found[repo.name]
        if not findings:
            say(f"#{repo.pr_number or repo.name:<{width}}  {'—':<20} "
                f"{'clean':<19} {'':>4}  nothing the counter can fault", "grn")
            continue
        for f in findings:
            colour = "red" if f["check"] in ("structure", "instruction-drift",
                                             "unreadable") else "yel"
            say(f"#{repo.pr_number or repo.name:<{width}}  {f['file']:<20} "
                f"{f['check']:<19} {f['count']:>4}  {f['detail'][:60]}", colour)
            if args.verbose:
                for ex in f["examples"]:
                    say(f"{'':<{width + 3}}    {ex[:120]}", "dim")

    total = sum(len(v) for v in found.values())
    say(f"\n{total} counted finding(s) across {len(found)} bundle(s)", "bld")

    if not args.deep:
        say("  -v shows the offending lines", "dim")
        say(f"\n  python3 run.py analyse --task "
            f"{','.join(r.pr_number or r.name for r in repos)} --deep", "grn")
        say("  --deep reads the prose and judges whether it is answerable, "
            "not just countable", "dim")
        return

    started = time.time()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(analyse_prose_one, r, found[r.name], args, width): r
                   for r in repos}
        for fut in as_completed(futures):
            results.append(fut.result())
    results.sort(key=lambda r: r["pr"] or "")

    say(f"\n{'=' * 78}", "dim")
    say(f"Read in {int(time.time() - started) // 60}m "
        f"{int(time.time() - started) % 60}s\n", "bld")
    say(f"{'PR':<{width + 2}} {'VERDICT':<9} {'ANSWERABLE':<11} {'ANSWERED':<9} "
        f"HEADLINE", "bld")
    for r in results:
        if r.get("error"):
            say(f"#{r['pr']:<{width + 1}} {'error':<9} {'':<11} {'':<9} "
                f"{r['error'][:52]}", "red")
            continue
        colour = {"good": "grn", "adequate": "yel", "poor": "red"}.get(r["verdict"], "")
        say(f"#{r['pr']:<{width + 1}} {r['verdict']:<9} "
            f"{('yes' if r['answerable'] else 'NO'):<11} "
            f"{('yes' if r['answered'] else 'NO'):<9} {r['headline'][:52]}", colour)

    for r in results:
        if r.get("error") or not r.get("areas"):
            continue
        say(f"\n#{r['pr']}  {r['headline']}", "bld")
        for a in r["areas"]:
            mark = "" if a["reads_as_human"] else "   (reads generated)"
            colour = {"good": "grn", "adequate": "yel", "poor": "red"}.get(a["verdict"], "")
            say(f"  {a['area']:<14} {a['verdict']:<9} {a['detail'][:70]}{mark}", colour)
        for f in r["findings"]:
            # the bundle prefix is the same on every line and eats the column
            where = re.sub(r"^contributor_tasks/[^/]+/", "", f["where"])
            say(f"  {f['severity']:<6} {where}", "dim")
            say(f"  {'':<6} {oneline(f['what'])}", "dim")
            say(f"  {'':<6} wants: {oneline(f['suggested_shape'])}", "dim")


def analyse_prose_one(repo: Repo, findings: list[dict], args, width: int) -> dict:
    """One read-only agent pass over one bundle's prose."""
    repo.workdir.mkdir(parents=True, exist_ok=True)
    tag = f"#{repo.pr_number or repo.name:<{width}} |"
    emit = Emitter(tag, repo.workdir / "analyse-log.txt")
    out = {"pr": repo.pr_number or repo.name, "error": "", "areas": [], "findings": []}
    try:
        counted = "\n".join(
            f"- **{f['check']}** in `{f['file']}` ({f['count']}): {f['detail']}"
            + "".join(f"\n    - {ex}" for ex in f["examples"])
            for f in findings) or "(nothing — the counters found no fault)"

        prompt = ANALYSE_TEMPLATE.read_text().format(
            repo_slug=repo.slug, branch=repo.branch,
            pr_number=repo.pr_number or "?",
            bundle_facts=bundle_facts(repo),
            required_paths="\n".join(REQUIRED_PATHS),
            findings=counted,
        )
        if args.dry_run:
            (repo.workdir / "analyse-prompt.txt").write_text(prompt)
            emit(f"dry run — prompt written ({len(prompt)} chars)", "blu")
            out["error"] = f"dry run, {len(prompt)} char prompt"
            return out

        parsed, err = run_codx_json(
            repo.path, prompt, ANALYSE_SCHEMA,
            repo.workdir / "analyse-result.json",
            repo.workdir / "analyse-events.jsonl", emit)
        if parsed is None:
            out["error"] = err or "no result"
            return out

        out.update(verdict=parsed.get("verdict", "?"),
                   headline=parsed.get("headline", ""),
                   areas=parsed.get("areas", []),
                   findings=parsed.get("findings", []),
                   answerable=bool(parsed.get("instruction_is_answerable")),
                   answered=bool(parsed.get("report_answers_instruction")))
        emit(f"{out['verdict']} — {out['headline'][:60]}", "grn")
        return out
    except (RuntimeError, KeyError, ValueError) as e:
        out["error"] = str(e)
        return out
    finally:
        emit.close()


def cmd_rules(args) -> None:
    """Check the task-quality rules. Read-only and free."""
    repos = select(discover(), args)
    if not repos:
        say("no repos selected", "yel")
        return

    width = max(len(r.pr_number or "?") for r in repos)
    failing = []

    for repo in repos:
        results, fails = rules_summary(repo)
        head = f"#{repo.pr_number or '?':<{width}}  {len(results) - len(fails)}/{len(results)} pass"
        say(head, "grn" if not fails else "red")
        for r in results:
            if fails and not r["ok"]:
                say(f"    FAIL  {r['rule']} — {r['detail']}", "red")
            elif args.verbose:
                say(f"    ok    {r['rule']} — {r['detail']}", "dim")
        if fails:
            failing.append(repo)

    say("")
    if failing:
        say(f"{len(failing)} task(s) violate the rules:", "red")
        say(f"  python3 run.py fix --task {','.join(r.pr_number for r in failing)}")
        say("  (fix applies these rules automatically, alongside review comments)", "dim")
    else:
        say("All tasks satisfy the automated rules.", "grn")
    say("Rule 4 (binary / atomic / independent criteria) is not machine-checked "
        "— the agent reviews it during fix.", "dim")


def cmd_clean(args) -> None:
    """Delete run artefacts, and optionally put the checkouts back to HEAD.

    Layers, least to most destructive:

        (default)   transient artefacts — event streams, prompts, logs
        --results   the results too: diffs, summaries, score/analysis output
        --work      the whole work/<repo> directory for the selected repos
        --state     forget the recorded status in state.json
        --purge     work/ and everything in it, state.json included
        --repos     git reset --hard + git clean -fd in each checkout
        --all       every layer above
    """
    all_repos = discover()
    repos = select(all_repos, args)
    # work/analysis is shared across tasks, so it is only touched when the run
    # covers everything — a `clean 1709` must not delete another task's analysis
    whole = len(repos) == len(all_repos)

    purge = args.purge or args.all
    results = args.results or args.all
    work = args.work or purge
    reset_repos = args.repos or args.all
    forget = args.state or purge

    dirs = [r.workdir for r in repos]
    orphans: list[Path] = []
    if whole:
        # work dirs whose checkout is gone: the batch moved on, nothing else
        # walks them, and they are the bulk of what work/ accumulates
        known = {r.name for r in all_repos} | {"analysis"}
        orphans = sorted(d for d in WORK.iterdir()
                         if d.is_dir() and d.name not in known) if WORK.is_dir() else []
        dirs += [WORK / "analysis", *orphans]

    present = [d for d in dirs if d.is_dir()]
    if orphans:
        say(f"{len(orphans)} work dir(s) have no checkout left "
            f"({human(sum(dir_size(d) for d in orphans))}) — "
            f"{'wiping' if work else 'use --work to remove them entirely'}", "dim")

    # state entries whose checkout is gone are unreachable from any repo-driven
    # clean, so they accumulate forever — sweep them whenever the run is whole
    state = load_state()
    doomed = {r.name for r in repos}
    if whole:
        doomed |= set(state) - {r.name for r in all_repos}
    doomed &= set(state)
    total = dir_size(WORK)

    if args.dry_run:
        if purge and whole:
            say(f"would delete {WORK} entirely ({human(total)}), "
                f"state.json and all {len(state)} entries included", "bld")
        else:
            say(f"would clean {len(present)} work dir(s) for {len(repos)} repo(s):", "bld")
            for d in present:
                say(f"  {d}  ({human(dir_size(d))})", "dim")
            if work:
                say("  ...removed entirely", "yel")
            elif results:
                say("  ...transient artefacts and results removed (--results)", "yel")
            if forget:
                say(f"would forget {len(doomed)} state entr(y/ies)"
                    + (f", {len(doomed) - len(repos)} of them orphaned" if len(doomed) > len(repos) else "")
                    + ("; state.json removed once empty" if doomed >= set(state) else ""), "yel")
        if reset_repos:
            dirty = [r for r in repos if r.is_dirty()]
            say(f"would restore {len(dirty)} dirty checkout(s) to HEAD "
                f"(diffs saved first):", "yel")
            for r in dirty:
                say(f"  {r.name}", "dim")
        return

    if not args.yes:
        prompts = []
        if reset_repos and (dirty := [r for r in repos if r.is_dirty()]):
            prompts.append(f"discard uncommitted changes in {len(dirty)} checkout(s) "
                           f"({', '.join(r.name for r in dirty)}) — each diff is saved first")
        if forget and doomed:
            # the only thing here that no later run can rebuild: state is what
            # stops `fix` re-running a task that was already pushed
            prompts.append(f"forget {len(doomed)} state entr(y/ies) — the record of "
                           f"what has already been pushed, and not regenerable")
        if prompts:
            say("about to:", "yel")
            for line in prompts:
                say(f"  - {line}", "dim")
            if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                say("aborted", "yel")
                return

    if purge and whole and not reset_repos:
        # the whole point of `clean --purge`: work/ goes, nothing is left behind
        say(f"removed {WORK} entirely, {human(wipe(WORK))}", "grn")
        return

    freed = 0
    if work:
        # state.json lives at the top of work/, not in a repo dir, so wiping
        # per-repo dirs never loses the record of what has been pushed
        for d in present:
            freed += wipe(d)
        say(f"removed {len(present)} work dir(s), {human(freed)}", "grn")
    else:
        freed = prune(*dirs)
        say(f"reclaimed {human(freed)}" if freed else "nothing transient to clean", "grn")

        if results:
            extra = 0
            for d in present:
                for pattern in RESULTS:
                    for f in d.glob(pattern):
                        try:
                            extra += f.stat().st_size
                            f.unlink()
                        except OSError:
                            pass
            say(f"also removed {human(extra)} of results", "yel")

    if reset_repos:
        for r in repos:
            if not r.is_dirty():
                continue
            # backup only survives if the work dir does — after --work it would
            # just be recreated for one file, which is what we want anyway
            say(f"  {r.name}: {restore_repo(r)}", "dim")
        say("checkouts restored to HEAD", "grn")

    if forget and doomed:
        for name in doomed:
            state.pop(name, None)
        if state:
            save_state(state)
        elif STATE_FILE.exists():
            STATE_FILE.unlink()  # nothing left to remember; don't leave a stub
        orphaned = len(doomed) - len({r.name for r in repos} & doomed)
        say(f"forgot {len(doomed)} state entr(y/ies)"
            + (f" ({orphaned} orphaned)" if orphaned else "")
            + ("" if state else " — state.json removed"), "yel")

    if not args.dry_run and WORK.is_dir() and not any(WORK.iterdir()):
        WORK.rmdir()
        say(f"{WORK} is empty and has been removed", "dim")


def cmd_reset(args) -> None:
    repos = select(discover(), args)
    state = load_state()
    for r in repos:
        state.pop(r.name, None)
        say(f"reset {r.name}", "dim")
    save_state(state)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_only(sp):
        sp.add_argument("only", nargs="*",
                        help="PR number(s) or substring(s) of repo folder names")
        sp.add_argument("-t", "--task", action="append", metavar="PRS",
                        help="comma-separated PRs to act on, e.g. --task 1392,1393 "
                             "(repeatable; same thing as the positional form)")

    sp = sub.add_parser("list", help="show repos and their status")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("fix", help="run the agent on all repos in parallel, stopping before push")
    add_only(sp)
    sp.add_argument("--redo", action="store_true", help="re-run repos already fixed/pushed")
    sp.add_argument("-j", "--jobs", type=int, default=0,
                    help="repos to run at once (default: all of them; use 1 for sequential)")
    sp.add_argument("--sandbox", default=DEFAULT_SANDBOX,
                    choices=["read-only", "workspace-write", "danger-full-access"])
    sp.add_argument("--network", action="store_true",
                    help="let the agent's shell commands reach the network")
    sp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MIN, metavar="MIN",
                    help=f"minutes before an agent is killed so it stops holding "
                         f"up the batch (default: {DEFAULT_TIMEOUT_MIN}; 0 = no limit). "
                         "Edits it already made are kept and reported")
    sp.add_argument("--discard-dirty", action="store_true",
                    help="git reset --hard before running, throwing away "
                         "uncommitted changes left by an earlier failed run")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="build each prompt and stop, without starting any agent")
    sp.add_argument("--all-comments", action="store_true",
                    help="send the whole PR thread instead of just the latest "
                         "fairness review")
    sp.set_defaults(func=cmd_fix)

    sp = sub.add_parser("review", help="print the agent summary and diff for each repo")
    add_only(sp)
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("audit",
                        help="round two: read-only review of finished tasks (no edits)")
    add_only(sp)
    sp.add_argument("--deep", action="store_true",
                    help="run an agent for an independent read instead of using "
                         "the bot's own fairness review. Slow and costs tokens; "
                         "its opinion can contradict the verdict that gates the task")
    sp.add_argument("--threshold", type=float, default=MAX_MEAN_SCORE,
                    help=f"means above this are flagged as too easy "
                         f"(default: {MAX_MEAN_SCORE})")
    sp.add_argument("-j", "--jobs", type=int, default=0,
                    help="tasks to audit at once (default: all, --deep only)")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="show low-severity findings too")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="build the prompts and stop")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("push", help="commit, push, and post the bot comment")
    add_only(sp)
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    sp.add_argument("-m", "--message", default=COMMIT_MESSAGE)
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("score",
                        help="estimate each task's reward with codx (stand-in for /bot rescore)")
    add_only(sp)
    sp.add_argument("--threshold", type=float, default=MAX_MEAN_SCORE,
                    help=f"scores above this are flagged as too easy "
                         f"(default: {MAX_MEAN_SCORE})")
    sp.add_argument("-j", "--jobs", type=int, default=0,
                    help="tasks to estimate at once (default: all)")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="build the prompts and stop")
    sp.set_defaults(func=cmd_score)

    sp = sub.add_parser("harden",
                        help="make too-easy tasks harder, re-estimating after each attempt")
    add_only(sp)
    sp.add_argument("--threshold", type=float, default=MAX_MEAN_SCORE,
                    help=f"target: get the score to this or below "
                         f"(default: {MAX_MEAN_SCORE})")
    sp.add_argument("--attempts", type=int, default=1,
                    help="how many times to try per task (default: 1)")
    sp.add_argument("-j", "--jobs", type=int, default=0,
                    help="tasks to harden at once (default: all)")
    sp.add_argument("--network", action="store_true",
                    help="let the agent's shell commands reach the network")
    sp.add_argument("--force", action="store_true",
                    help="harden even tasks already below the threshold")
    sp.set_defaults(func=cmd_harden)

    sp = sub.add_parser("analysis-score",
                        help="diagnose why each task scores what it does, plus cross-task trends")
    add_only(sp)
    sp.add_argument("-j", "--jobs", type=int, default=0,
                    help="tasks to analyse at once (default: all)")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="build the prompts and stop")
    sp.add_argument("--synthesis-only", action="store_true",
                    help="reuse cached per-task analyses and redo only the "
                         "cross-task synthesis")
    sp.add_argument("--keep", action="store_true",
                    help="keep event streams, prompts and logs instead of "
                         "pruning them when the run finishes")
    sp.set_defaults(func=cmd_analysis_score)

    sp = sub.add_parser("rules", help="check the task-quality rules (free, read-only)")
    add_only(sp)
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="show passing checks too")
    sp.set_defaults(func=cmd_rules)

    sp = sub.add_parser("analyse",
                        help="judge whether the bundle is written for a person: "
                             "structure, instruction, report (read-only)")
    add_only(sp)
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="show the offending lines under every finding")
    sp.add_argument("--deep", action="store_true",
                    help="also run an agent to judge what the counters cannot: "
                         "whether the instruction is answerable and the report "
                         "answers it")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="write the prompt, start no agent")
    sp.add_argument("-j", "--jobs", type=int, default=4,
                    help="agents to run in parallel with --deep (default: 4)")
    sp.set_defaults(func=cmd_analyse)

    sp = sub.add_parser("clean",
                        help="delete run artefacts and optionally restore the checkouts")
    add_only(sp)
    sp.add_argument("--results", action="store_true",
                    help="also delete results (diffs, summaries, estimates, analyses)")
    sp.add_argument("--work", action="store_true",
                    help="delete the whole work dir for each selected repo "
                         "(implies --results); state.json is kept")
    sp.add_argument("--state", action="store_true",
                    help="forget the recorded status for the selected repos "
                         "(plus any entry whose checkout is gone), and remove "
                         "state.json once it is empty")
    sp.add_argument("--purge", action="store_true",
                    help="empty work/ completely — every work dir, the analysis "
                         "output and state.json. Use when you are done with "
                         "these repos. Implies --work --state")
    sp.add_argument("--repos", action="store_true",
                    help="restore each checkout to HEAD — git reset --hard plus "
                         "git clean -fd. Destructive: it discards uncommitted "
                         "work, though the diff is saved first")
    sp.add_argument("--all", action="store_true",
                    help="everything above: purge work/ and restore the checkouts")
    sp.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmations (discarding work, forgetting state)")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="list what would be removed and stop")
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("check",
                        help="fetch each PR's fairness verdict and print the command to fix the bad ones")
    add_only(sp)
    sp.add_argument("--fail-only", action="store_true",
                    help="treat only FAIL as needing work (default: FAIL and WARN)")
    sp.add_argument("--threshold", type=float, default=MAX_MEAN_SCORE,
                    help=f"measured scores above this are flagged "
                         f"(default: {MAX_MEAN_SCORE})")
    sp.add_argument("-q", "--quiet", action="store_true",
                    help="print just the fix command, for $(...) use")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="show what each bot actually said, under every row")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("trigger",
                        help=f"post {' + '.join(REVIEW_COMMANDS)} on freshly "
                             "cloned tasks")
    add_only(sp)
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    sp.add_argument("-b", "--body", default=None,
                    help="post this one command instead of the usual pair "
                         f"({' + '.join(REVIEW_COMMANDS)})")
    sp.add_argument("--starved", action="store_true",
                    help="include tasks below the fresh-context bar, which are "
                         "skipped by default because they fail on that alone")
    sp.set_defaults(func=cmd_trigger)

    sp = sub.add_parser("repair",
                        help="act on a rescore the verifier could not complete")
    add_only(sp)
    sp.add_argument("-j", "--jobs", type=int, default=0,
                    help="tasks to repair at once (default: all)")
    sp.add_argument("--network", action="store_true",
                    help="let the agent's shell commands reach the network")
    sp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MIN, metavar="MIN",
                    help=f"minutes before an agent is killed (default: "
                         f"{DEFAULT_TIMEOUT_MIN}; 0 = no limit)")
    sp.add_argument("--discard-dirty", action="store_true",
                    help="git reset --hard before running")
    sp.add_argument("-n", "--dry-run", action="store_true",
                    help="build the prompts and stop")
    sp.set_defaults(func=cmd_repair)

    sp = sub.add_parser("comment", help="post any comment on the selected PRs")
    add_only(sp)
    # -b/--body matches `rescore` and `trigger`; -m is kept as an alias
    sp.add_argument("-b", "--body", "-m", "--message", action="append",
                    metavar="TEXT", dest="message",
                    help="comment body; repeat for extra paragraphs")
    sp.add_argument("-F", "--file", metavar="PATH",
                    help="read the body from a file instead (markdown is fine)")
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    sp.set_defaults(func=cmd_comment)

    sp = sub.add_parser("rescore", help=f"post {RESCORE_COMMAND!r} on the selected PRs")
    add_only(sp)
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    sp.add_argument("-b", "--body", default=RESCORE_COMMAND,
                    help=f"comment to post (default: {RESCORE_COMMAND!r}) — use this "
                         "for any other bot command too")
    sp.add_argument("--check", action="store_true",
                    help="show what the last rescore measured instead of posting")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="with --check, show the per-trial numbers")
    sp.set_defaults(func=cmd_rescore)

    sp = sub.add_parser("reset", help="forget recorded status for repos")
    add_only(sp)
    sp.set_defaults(func=cmd_reset)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
