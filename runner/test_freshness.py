"""Exercise the freshness and QC-reading helpers against real comment shapes.

Run it from anywhere: `python3 runner/test_freshness.py`.
"""
import runpy, sys
from pathlib import Path
m = runpy.run_path(str(Path(__file__).resolve().parent / "run.py"), run_name="notmain")
commit_history, ran, pinned = m["commit_history"], m["ran_before_our_work"], m["pinned_head"]
running_jobs, decide, parse_ts = m["running_jobs"], m["decide"], m["parse_ts"]
qc_summary = m["qc_summary"]

A, B, BOT = "a"*40, "b"*40, "c"*40
DATA = {"commits": [
    {"oid": A, "committedDate": "2026-08-12T09:00:00Z", "messageHeadline": "Add task foo"},
    {"oid": B, "committedDate": "2026-08-12T10:17:00Z", "messageHeadline": "Address PR review feedback"},
    {"oid": BOT, "committedDate": "2026-08-12T11:00:00Z",
     "messageHeadline": "chore(rescore): refresh verifier and aggregate artifacts for foo"},
]}
oids, own, own_at = commit_history(DATA)
ok = lambda name, got, want: print(("PASS" if got == want else f"FAIL  got={got!r} want={want!r}"), name)

ok("bot chore commit is not 'our' newest", own, B)
ok("pinned head parsed", pinned("Pinned PR head: `bbbbbbbbbbbb`"), "bbbbbbbbbbbb")
ok("verdict from our newest commit is current", ran(B, oids, own), False)
ok("verdict from an older commit is stale",     ran(A, oids, own), True)
ok("verdict from the bot's own commit is current", ran(BOT, oids, own), False)
ok("short pinned sha still matches",           ran("bbbbbbb", oids, own), False)
ok("unknown sha is unknowable, not fresh",     ran("d"*40, oids, own), None)
ok("no pinned head is unknowable",             ran(None, oids, own), None)

# in-flight detection
def c(when, body): return {"createdAt": when, "body": body}
ACK = "<!-- github-review-bot:ack:1 -->\n🧪 QC Phases 1 → 2 queued for task `foo` (job-1)."
RESULT = "<!-- github-review-bot:qc-check:2 -->\n## ✅ Task QC check"
qc_at = parse_ts("2026-08-12T11:19:00Z")
ok("ack after the result means a job is in flight",
   [k for k, _ in running_jobs({"comments": [c("2026-08-12T11:19:00Z", RESULT),
                                             c("2026-08-12T11:30:00Z", ACK)]}, {"qc": qc_at})], ["qc"])
ok("ack before the result is retired",
   running_jobs({"comments": [c("2026-08-12T11:00:00Z", ACK),
                              c("2026-08-12T11:19:00Z", RESULT)]}, {"qc": qc_at}), [])
TRACE = "🚀 **Trace Run Started** (oracle + nop + Codex GPT-5.5 x5)"
ok("a trace run with no result is in flight",
   [k for k, _ in running_jobs({"comments": [c("2026-08-12T12:00:00Z", TRACE)]}, {})], ["trace run"])
ok("a finished trace run is not in flight",
   running_jobs({"comments": [c("2026-08-12T12:00:00Z", TRACE),
                              c("2026-08-12T12:40:00Z", "## Trace Run Complete — 5/5")]}, {}), [])

# routing. Queue times must be relative to now — a fixture pinned to a fixed
# date silently ages past the patience window and the test starts failing on
# the calendar rather than on the code.
from datetime import datetime, timedelta, timezone
def queued(minutes): return datetime.now(timezone.utc) - timedelta(minutes=minutes)

base = {"error": None, "verdict": "PASS", "qc": "ok", "qc_failing": [], "failing": [],
        "rescore": "ok", "rescore_why": "", "rescore_blames_task": False,
        "rescore_stale_traces": False, "qc_stale": False, "rescore_stale": False,
        "verdict_stale": None, "running": [], "local": 0.3, "remote": 0.3,
        "fresh": 500_000, "reference": 200_000}
ok("clean and current is done", decide(base, "", False, 0.6)[0], "done")
ok("stale QC never reads as done",
   decide({**base, "qc_stale": True}, "", False, 0.6)[0], "waiting")
ok("QC re-running never reads as done",
   decide({**base, "running": [("qc", queued(5))]}, "", False, 0.6)[0], "running")
ok("score older than our newest commit needs a rescore",
   decide({**base, "rescore_stale": True}, "", False, 0.6)[0], "rescore")
ok("stale traces with a trace run already queued is a wait, not another job",
   decide({**base, "rescore": "failed", "rescore_stale_traces": True,
           "running": [("trace run", queued(30))]}, "", False, 0.6)[0], "running")
ok("stale traces with nothing queued still escalates",
   decide({**base, "rescore": "failed", "rescore_stale_traces": True}, "", False, 0.6)[0], "retrace")
ok("QC findings against the current commit go to fix",
   decide({**base, "qc": "issues", "qc_failing": ["x"]}, "", False, 0.6)[0], "fix")
ok("un-pushed work still wins over everything",
   decide({**base, "qc_stale": True}, "2 files", False, 0.6)[0], "push")

failed = {**base, "rescore": "failed", "rescore_blames_task": True,
          "rescore_why": "ground truth maps an unknown criterion"}
ok("a current verifier rejection goes to repair",
   decide(failed, "", False, 0.6)[0], "repair")
ok("a rejection of a commit we replaced re-scores first",
   decide({**failed, "rescore_stale": True}, "", False, 0.6)[0], "rescore")
ok("stale traces still escalate even when the rescore predates our commit",
   decide({**failed, "rescore_stale": True, "rescore_blames_task": False,
           "rescore_stale_traces": True}, "", False, 0.6)[0], "retrace")

# a task whose own name ends in `-fairness` puts that word into every ack for
# it, including the QC ones — so the job kind must come from the job phrase,
# not from whatever the task happens to be called
QC_ACK = ("<!-- github-review-bot:ack:9 -->\n🧪 QC Phases 1 → 2 queued for task "
          "`some-claim-fairness` (job-9).")
RS_ACK = ("<!-- github-review-bot:ack:9 -->\n📊 Rescore queued for task "
          "`some-claim-fairness` (job-9).")
ok("a QC ack for a task named …-fairness is a QC job",
   [k for k, _ in running_jobs({"comments": [c("2026-08-12T11:30:00Z", QC_ACK)]}, {})], ["qc"])
ok("a rescore ack for the same task is a rescore job",
   [k for k, _ in running_jobs({"comments": [c("2026-08-12T11:30:00Z", RS_ACK)]}, {})], ["rescore"])

# waiting is only right while the bot is actually coming back
ok("a QC queued 5m ago is worth waiting for",
   decide({**base, "running": [("qc", queued(5))]}, "", False, 0.6)[0], "running")
ok("a QC queued 3h ago was lost — ask again",
   decide({**base, "running": [("qc", queued(180))]}, "", False, 0.6)[0], "waiting")
ok("a trace run queued 2h ago is still plausible",
   decide({**base, "rescore": "failed", "rescore_stale_traces": True,
           "running": [("trace run", queued(120))]}, "", False, 0.6)[0], "running")
ok("a trace run queued 9h ago is not — escalate",
   decide({**base, "rescore": "failed", "rescore_stale_traces": True,
           "running": [("trace run", queued(540))]}, "", False, 0.6)[0], "retrace")

# --------------------------------------------------------------------------
# reading a QC result. The heading is the bot's own verdict; the body is
# detail. Every shape below is trimmed from a real comment on this repo.
# --------------------------------------------------------------------------

# #2480, #2481, #2431: the runner died on model capacity. It judged nothing —
# reading only the body finds no findings and calls that a pass.
INFRA = """<!-- github-review-bot:qc-check:20dd30cd -->
## ❌ Task QC check infrastructure failure

The QC runner stopped before producing a structured result for the requested QC phase(s). This is a runner/auth/sandbox/model/output failure, not a finding about the task.

**Failure**

`QC check runner exited with code 1: [qc-check] starting QC phase 2: post-rollout statistics and RCA [qc-check] ERROR QC Phase 2 RCA batch 1/1 engine exited 1: "p18", \\| ERROR: Selected model is at capacity. Please try a different model.`
"""
status, items = qc_summary(INFRA)
ok("a crashed QC runner is not a pass", status, "incomplete")
ok("and it names what killed it",
   "at capacity" in (items[0] if items else ""), True)

# #2482: Phase 2 could not run because of the task package. The phase heading
# carries no ✅/⚠️/❌ mark at all, so a mark-based read sees nothing wrong.
PHASE_INCOMPLETE = """<!-- github-review-bot:qc-check:9b03932c -->
## ⚠️ Task QC check — one or more phases incomplete

### Phase 1 — pre-rollout static audit ✅

The task is well-specified and adequately provisioned.

### Phase 2 — incomplete

The post-rollout reward/zero-pass RCA could not be performed safely. This is a task package issue or committed-rollout compatibility issue, not an infrastructure failure.
"""
status, items = qc_summary(PHASE_INCOMPLETE)
ok("a phase that could not run is a finding", status, "issues")
ok("and the phase is named", any("Phase 2" in i for i in items), True)

# #2429, #2440, #2481: the bot passed the task with a sub-threshold warning
# ("Problematic weighted share: 0.6% … threshold is 10%"). Treating that as
# work sends a passed task to `fix`.
PASSED_WITH_NOTE = """<!-- github-review-bot:qc-check:aaaa -->
## ✅ Task QC check — requested phases passed

### Phase 1 — pre-rollout static audit ✅

| Dimension | Result | Evidence-backed rationale |
|---|---|---|
| Rubric accuracy | ✅ 0/67 problematic | All 67 criteria were inspected. |
| Test correctness | ⚠️ 1/67 problematic | The principal defect is the report-global negative decoy check. |

Flag rules triggered: _none_.
Problematic weighted share: **0.6%** (3.00 / 491.00 absolute weight).

### Phase 2 — post-rollout statistics and RCA ✅

Actionable Phase 2 flags: _none_.
"""
status, items = qc_summary(PASSED_WITH_NOTE)
ok("a pass with a sub-threshold warning is still a pass", status, "ok")
ok("but the warning is kept as a note", len(items), 1)

FOUND_ISSUES = """<!-- github-review-bot:qc-check:bbbb -->
## ⚠️ Task QC check — issues found

### Phase 1 — pre-rollout static audit ✅
### Phase 2 — post-rollout statistics and RCA ⚠️

Actionable Phase 2 flags: `negative-dual-lane:RUBRIC_DESIGN`.
"""
ok("issues found is issues", qc_summary(FOUND_ISSUES)[0], "issues")
ok("a clean pass has no notes", qc_summary(
    "## ✅ Task QC check — requested phases passed\n\n"
    "### Phase 1 — pre-rollout static audit ✅\n"
    "### Phase 2 — post-rollout statistics and RCA ✅\n"), ("ok", []))
ok("a non-QC comment is still none", qc_summary("## Something else entirely")[0], "none")

# routing: a crash is a missing review, not a pass and not a fix round
crashed = {**base, "qc": "incomplete", "qc_failing": ["QC runner failed: at capacity"]}
ok("a crashed QC asks for another review", decide(crashed, "", False, 0.6)[0], "review")
ok("a crashed QC never reads as done", decide(crashed, "", False, 0.6)[0] != "done", True)
ok("a passed-with-notes task is still done",
   decide({**base, "qc_failing": ["Test correctness: ⚠️ 1/67"]}, "", False, 0.6)[0], "done")

# the heading is read by its words, so an unfamiliar mark does not change the
# verdict — and an unfamiliar *heading* fails towards issues, never towards a
# silent pass
ok("a new emoji on a passing heading is still a pass",
   qc_summary("## 🟢 Task QC check — requested phases passed\n")[0], "ok")
ok("a new emoji on a crash is still a crash",
   qc_summary("## 🛑 Task QC check infrastructure failure\n")[0], "incomplete")
ok("an unrecognised heading with a warning mark is not a pass",
   qc_summary("## ⚠️ Task QC check — some phrasing we have never seen\n")[0], "issues")

# --------------------------------------------------------------------------
# a task that passed QC exactly as submitted. Nothing was edited, so there is
# nothing to re-measure and nothing to harden: the score it arrived with is
# the whole answer, either way.
# --------------------------------------------------------------------------
edited = m["edited_since_submission"]
ORIG = {"messageHeadline": "Add task foo", "authors": [{"login": None}]}
MINE = {"messageHeadline": "Address PR review feedback", "authors": [{"login": "me"}]}
HAND = {"messageHeadline": "tweak the rubric", "authors": [{"login": "me"}]}
MATE = {"messageHeadline": "Remove validation cache",
        "authors": [{"login": "teammate"}]}
CHORE = {"messageHeadline": "chore(rescore): refresh verifier artifacts",
         "authors": [{"login": "review-bot"}]}

ok("a PR with only the contributor's commit is untouched",
   edited({"commits": [ORIG]}), False)
ok("the bot's rescore commit is not an edit",
   edited({"commits": [ORIG, CHORE]}), False)
ok("our runner's own push counts",
   edited({"commits": [ORIG, MINE]}), True)
ok("a hand edit under our account counts too",
   edited({"commits": [ORIG, HAND]}), True)
ok("a teammate's push counts — it is not as submitted either",
   edited({"commits": [ORIG, MATE]}), True)
ok("and the runner's message counts even with no login on the commit",
   edited({"commits": [ORIG, {"messageHeadline": "Address PR review feedback",
                              "authors": [{}]}]}), True)

clean = {**base, "untouched": True, "rescore": "none"}
ok("passed QC unchanged and too easy is a straight fail",
   decide({**clean, "local": 0.957, "remote": 0.957}, "", False, 0.6)[0], "reject")
ok("passed QC unchanged and under the cap is done, with no rescore asked for",
   decide({**clean, "local": 0.486, "remote": 0.486}, "", False, 0.6)[0], "done")
ok("the same task once we have edited it is hardened, not failed",
   decide({**clean, "untouched": False, "local": 0.957, "remote": 0.957},
          "", False, 0.6)[0], "harden")
ok("and once edited, a low score still wants a rescore",
   decide({**clean, "untouched": False, "local": 0.486, "remote": 0.486},
          "", False, 0.6)[0], "rescore")
ok("QC findings on an untouched task are still findings",
   decide({**clean, "qc": "issues", "qc_failing": ["x"], "local": 0.4, "remote": 0.4},
          "", False, 0.6)[0], "fix")
ok("the token bar still fails first",
   decide({**clean, "local": 0.4, "remote": 0.4}, "", True, 0.6)[0], "token")
ok("an untouched task still missing a review is not straight-failed",
   decide({**clean, "qc": "none", "local": 0.957, "remote": 0.957},
          "", False, 0.6)[0], "review")
ok("an untouched task with no score at all still needs one",
   decide({**clean, "local": None, "remote": None}, "", False, 0.6)[0], "rescore")

# --------------------------------------------------------------------------
# what reaches the fix agent: the review comment, entire. A per-criterion
# table looks like pure repetition, but on #2562 the rows naming the actual ID
# mapping sat at the bottom of a 52-row table — any cap on the first N rows
# dropped exactly the detail the agent needed.
# --------------------------------------------------------------------------
render_review = m["render_review"]

BIG_TABLE = "\n".join(
    ["## \u26a0\ufe0f Task QC check \u2014 issues found", "",
     "### Phase 2 \u2014 post-rollout statistics and RCA \u26a0\ufe0f", "",
     "| Criterion | Classification | Evidence |", "|---|---|---|"]
    + [f"| `breadth-{i:02d}` | `DATA_ISSUE` | No matching criterion ID. |"
       for i in range(40)]
    + ["| `fact-claim01` | `DATA_ISSUE` | The staged factual criterion is named "
       "fact-c001, not fact-claim01. |",
       "| `fact-scenario-01` | `DATA_ISSUE` | The staged scenario criterion is "
       "named fact-s001, not fact-scenario-01. |"])
sent = render_review({"title": "t"}, {"body": BIG_TABLE, "createdAt": "now",
                                      "author": {"login": "review-bot"}}, "QC check")

ok("every row of a long table is sent",
   sum(1 for l in sent.splitlines() if l.startswith("| `")), 42)
ok("including the mapping rows at the very bottom",
   "fact-c001" in sent and "fact-s001" in sent, True)
ok("nothing is marked as omitted",
   "omitted" in sent or "more row(s)" in sent, False)
ok("the body arrives byte for byte", BIG_TABLE in sent, True)
ok("with the header naming who posted it and when",
   "QC check by @review-bot \u2014 now" in sent, True)

# --------------------------------------------------------------------------
# QC and fairness are two halves of one review. They answer different
# questions, so a task needs a current pass from each and either one's
# findings are work.
# --------------------------------------------------------------------------
fair_verdict = m["fairness_verdict"]

HEAD = "<!-- github-review-bot:human-review:1 -->\n## {} Human fairness review — {}\n"
ok("a clean fairness review passes",
   fair_verdict(HEAD.format("✅", "no confirmed issue found"))[0], "PASS")
ok("issues found is a fail",
   fair_verdict(HEAD.format("⚠️", "issues found"))[0], "FAIL")
ok("review advised is a warning, not a pass",
   fair_verdict(HEAD.format("\U0001f7e1", "review advised"))[0], "WARN")
ok("a partially completed review judged nothing",
   fair_verdict(HEAD.format("⚪", "partially completed"))[0], "incomplete")

# the real #2530 shape: advised, and its only finding is tagged Provenance
# warning, so filtering on `Fairness` alone left nothing and read as a pass
ADVISED = (HEAD.format("\U0001f7e1", "review advised") + """
### At a glance

- **Fairness:** \U0001f7e1 1 provenance warning(s); no confirmed fairness issue

### What needs attention

1. **Trace provenance drift could not be fully classified** _(Provenance warning · probable · low · trace-task-drift)_
   - **Concern:** The solver-visible index reports a different row count.
""")
verdict, items = fair_verdict(ADVISED)
ok("an advised review with only a provenance finding is not a pass", verdict, "WARN")
ok("and it still has something to show", len(items), 1)

both = {**base, "verdict": "PASS", "qc": "ok"}
ok("both clean is done", decide(both, "", False, 0.6)[0], "done")
ok("fairness findings are work even when QC passed",
   decide({**both, "verdict": "FAIL", "failing": ["hidden quota"]}, "", False, 0.6)[0], "fix")
ok("and the reason names which reviewer wanted what",
   "fairness found" in decide({**both, "verdict": "FAIL",
                               "failing": ["hidden quota"]}, "", False, 0.6)[1], True)
ok("QC findings are work even when fairness passed",
   decide({**both, "qc": "issues", "qc_failing": ["bad rubric"]}, "", False, 0.6)[0], "fix")
ok("both flagged reports both",
   decide({**both, "qc": "issues", "qc_failing": ["a"], "verdict": "FAIL",
           "failing": ["b"]}, "", False, 0.6)[1].count("found"), 2)
ok("a missing fairness review is asked for, not assumed",
   decide({**both, "verdict": "none"}, "", False, 0.6)[0], "review")
ok("a missing QC is asked for too",
   decide({**both, "qc": "none"}, "", False, 0.6)[0], "review")
ok("a crashed fairness review is a missing review, not a pass",
   decide({**both, "verdict": "incomplete", "failing": ["could not complete"]},
          "", False, 0.6)[0], "review")
ok("a stale fairness review settles nothing",
   decide({**both, "verdict_stale": True}, "", False, 0.6)[0], "waiting")
ok("a fairness review in flight is worth waiting for",
   decide({**both, "running": [("fairness", queued(5))]}, "", False, 0.6)[0], "running")
ok("review advised is work by default",
   decide({**both, "verdict": "WARN", "failing": ["advised"]}, "", False, 0.6)[0], "fix")
ok("and --fail-only lets it through",
   decide({**both, "verdict": "WARN", "failing": ["advised"]},
          "", False, 0.6, True)[0], "done")

ok("push and trigger both post the pair",
   (m["PUSH_COMMANDS"], m["REVIEW_COMMANDS"]),
   (("/bot2 qc-check", "/bot2 fairness-review"),) * 2)
ok("a missing review says what the other one already found",
   decide({**both, "verdict": "none", "qc": "issues", "qc_failing": ["a", "b"]},
          "", False, 0.6)[1], "no fairness result yet (QC already found 2)")

# --------------------------------------------------------------------------
# red means something failed, not merely that there is work
# --------------------------------------------------------------------------
row_colour = m["row_colour"]
advised = {**base, "qc": "ok", "verdict": "WARN", "failing": ["provenance drift"]}
ok("an advisory-only fix is yellow", row_colour(advised, "fix"), "yel")
ok("QC findings are red", row_colour({**base, "qc": "issues"}, "fix"), "red")
ok("a fairness FAIL is red", row_colour({**base, "verdict": "FAIL"}, "fix"), "red")
ok("both flagged is red",
   row_colour({**base, "qc": "issues", "verdict": "FAIL"}, "fix"), "red")
ok("a straight fail is red", row_colour(base, "reject"), "red")
ok("under the token bar is red", row_colour(base, "token"), "red")
ok("waiting on the bot is yellow", row_colour(base, "running"), "yel")
ok("done is green", row_colour(base, "done"), "grn")
ok("an advisory row still says fix", decide(advised, "", False, 0.6)[0], "fix")
ok("and the reason says advises, not found",
   decide(advised, "", False, 0.6)[1], "fairness advises provenance drift")

# --------------------------------------------------------------------------
# earlier rounds. The agent sees only the newest review, which hides that the
# same complaint has already come back several times.
# --------------------------------------------------------------------------
earlier_rounds = m["earlier_rounds"]
FAIR_AT = ("<!-- github-review-bot:human-review:{n} -->\n"
           "## ⚠️ Human fairness review — issues found\n\n"
           "### At a glance\n\n- **Fairness:** ⚠️ 1 confirmed issue(s)\n\n"
           "### What needs attention\n\n"
           "1. **{title}** _(Fairness · confirmed · high · verifier-mismatch)_\n")
def fair(n, title): return {"createdAt": f"2026-08-14T1{n}:00:00Z",
                            "body": FAIR_AT.format(n=n, title=title)}

rounds = [fair(1, "Hidden exact section-heading requirement"),
          fair(2, "Citation link-text exactness is hidden from solvers"),
          fair(3, "Unstated corpus-visit requirement")]
data = {"comments": rounds}
out = earlier_rounds(data, "fairness review", rounds[-1])
ok("earlier rounds are listed", "2 before the one above" in out, True)
ok("each one whole, not summarised",
   all(r["body"].strip() in out for r in rounds[:-1]), True)
ok("the newest is not repeated as history",
   out.count("Unstated corpus-visit requirement"), 0)
ok("they are labelled as history, not as work",
   "history, not your task list" in out, True)
ok("a first round has no history",
   earlier_rounds({"comments": [rounds[0]]}, "fairness review", rounds[0]), "")

many = [fair(i, f"finding {i}") for i in range(1, 8)]
out = earlier_rounds({"comments": many}, "fairness review", many[-1])
ok("nothing is capped — every earlier round goes",
   sum(1 for l in out.splitlines() if l.startswith("#### Round ")), 6)
ok("and every one of their bodies is whole",
   all(r["body"].strip() in out for r in many[:-1]), True)
ok("the count is stated", "6 before the one above" in out, True)

# --------------------------------------------------------------------------
# readability. Every fixture below is the shape of a real bundle file on this
# repo — walls of prose with the structure implied rather than shown.
# --------------------------------------------------------------------------
prose_findings, strip_links = m["prose_findings"], m["strip_links"]
import json as _json
def checks(text, where="solution/report.md"):
    return {f["check"]: f for f in prose_findings(text, where)}

# #2600: the same citation clause on eleven paragraphs
REPEATED = "\n\n".join(
    f"**row {i}:** finding number {i} with its own wording here. "
    "Supporting captured passages: [A Very Long Captured Page Heading | Site](https://x/y)."
    for i in range(6))
c = checks(REPEATED)
ok("a clause repeated on every paragraph is caught", "repeated-lines" in c, True)
ok("and it counts the fragments, not the lines", c["repeated-lines"]["count"], 1)
ok("links are reduced so the fragment reads cleanly",
   "⟨link⟩" in c["repeated-lines"]["examples"][0], True)

# the Decision Log written as stacked bold-label blocks
LOG = "## Decision Log\n" + "\n\n".join(
    f"### entry-{i}\n**Status:** established\n**Finding:** something\n"
    f"**Key evidence:** counts\n**Consequence:** it changes the table"
    for i in range(4))
c = checks(LOG)
ok("a stacked decision log is caught", "decision-log" in c, True)
ok("with one finding per record", c["decision-log"]["count"], 4)
ok("and it names the shared columns",
   "Status | Finding | Key evidence | Consequence" in c["decision-log"]["detail"], True)

# the enumeration shape from the brief: A: [...], B: [...]
c = checks("Categories: [drama, comedy, documentary, short] and Editions: [2023, 2024, 2025]")
ok("a bracketed enumeration is caught", "inline-enumeration" in c, True)

# a markdown link is not an enumeration, and its title is not a list
c = checks("See [BBC Culture | Arts, Film, Reviews, Books, Music, Style](https://bbc.com/culture) for context.")
ok("a link is not read as an enumeration", "inline-enumeration" in c, False)
ok("and commas in a link title are not a comma run", "comma-run" in c, False)

ok("six items in a sentence is a comma run",
   "comma-run" in checks("Resolve identity, authority, period, denominator, unit, and scope."), True)
ok("three is not",
   "comma-run" in checks("Resolve identity, authority, and scope."), False)
ok("a 700-character line is a wall of text",
   "wall-of-text" in checks("word " * 150), True)
ok("a file with a table is not flagged for having none",
   "no-tables" in checks("| a | b |\n|---|---|\n| 1 | 2 |"), False)
ok("code fences are not read as prose",
   prose_findings("```\n" + "x, " * 40 + "\n```\n", "f.md"), [])
ok("strip_links leaves plain prose alone", strip_links("no links here"), "no links here")

# the analyse skill and its schema have to stay in step with each other
schema = _json.loads((Path(m["__file__"]).parent / "schemas" / "analyse.json").read_text())
ok("the schema requires the two questions the command reports on",
   {"instruction_is_answerable", "report_answers_instruction"} <= set(schema["required"]), True)
ok("every area carries a reads_as_human judgement",
   "reads_as_human" in schema["properties"]["areas"]["items"]["required"], True)
ok("a finding must name where it is and what shape it wants",
   {"where", "suggested_shape"} <= set(schema["properties"]["findings"]["items"]["required"]), True)
tmpl = (Path(m["__file__"]).parent / "prompts" / "analyse.md").read_text()
ok("the skill fills every slot the command passes",
   {"repo_slug", "branch", "pr_number", "bundle_facts", "required_paths", "findings"},
   set(__import__("re").findall(r"{(\w+)}", tmpl)))
ok("and it is read-only by construction — no edit instruction",
   "Edit anything" in tmpl and "read-only" in tmpl, True)
