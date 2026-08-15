# PR fix runner

Batch-runs `codx` over every repo checkout in this folder: reads each PR's QC
check, fixes what it complains about, and — after you've reviewed the diff —
pushes and re-triggers the bot.

## The loop

```
  check ──►  fix  ──►  review  ──►  push  ──►  (bot re-reviews)  ──►  check ──► ...
                                                     ▲
                                                  rescore
```

0. `check` — ask GitHub which PRs are failing, and get the exact next command
0b. `analyse` — before fixing anything, judge whether the bundle can even be
    read: is the instruction answerable, does the report answer it. Read-only.
1. `fix` — agents edit the repos in parallel, then stop
2. `review` — you read the diffs
3. `push` — commit, push, post `/bot2 qc-check` + `/bot2 fairness-review`
4. bot posts a new review
5. `fix --task <failing PRs>` — round two, seeing only the new review
6. once it passes — `rescore` posts `/bot2 rescore`

`qc-check` and `fairness-review` are always posted together — they are two
halves of one review and a task needs a current pass from each. `trigger` posts
that same pair on a newly cloned task.

## Commands

Every command takes `--task 1392,1393` or bare PR numbers / name substrings —
both forms accept commas, so `fix 1392,1393` and `fix --task 1392,1393` are the
same thing. With no selection, they act on all repos.

### `list`

Shows every repo, its status, and whether its working tree is dirty. Read-only.

### `check`

The `SCORE` column shows `old→new` when the bot has rescored since you last
pulled, e.g. `0.373→0.076`. Two things make that necessary: a `/bot2 rescore`
rewrites `trace/*/verifier/reward.txt` but leaves `result.json` at its original
value, and it commits the result to the **remote** branch. So the score on disk
can be several rounds stale. `check` fetches each branch and reads the newer
value out of `origin/<branch>` without touching your working tree; the `!`
too-easy marker and the threshold both use the new number.

Fetches every PR's current QC result, rescore and score from GitHub (in
parallel) and tells you what to run next. This is the command to start from.

```
as of 17:48:07 — AGE is how long ago the result this row rests on was posted
PR     DO THIS   QC       FAIR     RESCORE  SCORE          TOKENS  AGE   WHY
#2312  wait      ok       ok       FAILED   0.310          1754k   17m   trace run started 17m ago — no result posted yet
#2313  done      ok       ok       ok       0.260→0.235    202k    76m   QC and fairness both passed, scores 0.235 — done
#2352  repair    ok       ok       FAILED   0.569          1429k   14m   ground truth maps an unknown, duplicate, or negative
#2358  fix       2 bad    advised  ok       0.437→0.397    2337k   2h    QC found Phase 2 raised issues; fairness advises …
#2359  trigger   ok       —        ok       0.946→0.958!   272k    78m   no fairness result yet
```

**One line per PR, and the first column is the verb.** `DO THIS` is the whole
answer for that task — no cross-referencing the summary to work out which bot
mattered:

| `DO THIS` | meaning |
|---|---|
| `fix` | QC or fairness found something to repair |
| `repair` | the verifier rejected the bundle — the rescore names what to change |
| `harden` | passes review but scores above the cap |
| `rescore` | passes review, needs a score |
| `trigger` | a review has not usably answered: never ran, crashed, or pinned an older commit than yours |
| `escalate` | a solver-visible file changed, so it cannot be rescored without a solver run |
| `push` | un-pushed work, review it first |
| `drop` | a straight fail — under the token bar, or too easy having passed review unchanged |
| `wait` | the bot is working on this one right now; a result is on its way |
| `done` | nothing to do |

`QC` reads `ok` / `ok·N` / `N bad` / `crashed` / `—`, `FAIR` reads `ok` /
`advised` / `N bad` / `crashed` / `—`, `RESCORE` reads `ok` / `partial` /
`FAILED` / `—`, and any of them gains a `*` when that result pinned a commit
older than your newest one. `-v` prints what each bot actually said under
every row, plus a `posted` line giving the age of each result and of anything
queued; the grouped commands at the bottom stay either way.

### Reading a QC result

**The heading is the verdict; the body is detail.** The bot states its own
conclusion on the first line — `## ✅ Task QC check — requested phases passed`,
`## ⚠️ … issues found`, `## ❌ Task QC check infrastructure failure` — and that
line is what the `QC` column reports. Judging by the body instead gets it wrong
in both directions, because a body can carry warnings under a pass and a crash
carries no findings at all:

| Heading | `QC` | why |
|---|---|---|
| `✅ requested phases passed` | `ok` | the bot passed the task |
| `✅ …` with ⚠️ dimensions | `ok·N` | passed, with N observations under the bot's own 10%-of-weight threshold — notes to read, not a fix round to spend. `-v` lists them |
| `⚠️ issues found` | `N bad` | findings to repair |
| `⚠️ one or more phases incomplete` | `N bad` | a phase that could not run because the *task package* is broken is a finding; the bot says so itself |
| `❌ infrastructure failure` | `crashed` | auth, sandbox, runner or model trouble. It judged nothing, so it is neither a pass nor a set of findings — the row asks for another QC and names the cause (`Selected model is at capacity`) |

A crashed QC is the one that used to read as a clean pass: with no findings in
the body there was nothing to count, and nothing to count looked like nothing
wrong.

### Reading a fairness result

`/bot2 fairness-review` states its verdict the same way, and it is read the
same way — by the words of the heading, not the mark:

| Heading | `FAIR` | why |
|---|---|---|
| `✅ no confirmed issue found` | `ok` | passed |
| `🟡 review advised` | `advised` | something to answer. `--fail-only` lets these through |
| `⚠️ issues found` | `N bad` | findings to repair |
| `⚠️/⚪ partially completed` | `crashed` | it judged nothing; ask for another |

Both `advised` and `N bad` send the row to `fix`, but they do not read the
same. **Red is for something that failed** — QC findings, or a fairness FAIL.
An advisory-only row is yellow like every other warning, and its reason says
`fairness advises …` rather than `found`: the verb is still `fix` because it is
still work, but it does not shout as though the bundle had been rejected.
`--fail-only` drops advisories out of the fix list entirely.

`review advised` is the one that used to read as a clean pass. Its findings are
often tagged `Provenance warning` rather than `Fairness`, so filtering on the
fairness tag alone counted nothing and passed the task — 17 of 21 such reviews
on this repo. The heading said otherwise every time.

### Why a row can be trusted

Every row states how old it is and which commit it describes, because neither
is guaranteed and both change the answer.

`AGE` is the age of the result the row *rests on* — the QC verdict for a review
outcome, the rescore for a score outcome — not the newest comment on the PR. A
row deciding on an eight-hour-old QC and one deciding on a three-minute-old QC
are otherwise identical on screen.

Freshness is settled by commit, not by clock. Every bot result names the commit
it ran against (`Pinned PR head: 475300a7…`), and that SHA is compared against
your newest commit on the branch. Timestamps cannot do this job: after a
rescore the bot pushes its own `chore(rescore)` commit onto the branch, which
moves the PR head without invalidating anything, so those commits are excluded
when working out which commit is yours.

A result that pinned an older commit than yours never settles a row, whatever
it says — it judged a task that no longer exists. Nor does one that is still
being produced: an `ack` ("📊 Rescore queued …") or a `🚀 Trace Run Started`
with no result after it means what is posted is the *previous* answer, so the
row reads `wait` and no group offers to queue the same job a second time.

Waiting expires. A QC or rescore normally posts within five minutes, a trace
run takes hours because it queues behind every other job, so silence past 30
minutes (6 hours for a trace run) is treated as a lost job rather than a slow
one and the row goes back to `trigger` / `escalate`. A row parked on `wait` for
something that died is the same stale-data problem wearing a different hat.

Two things that could be wrong quietly are instead marked: a score `check`
could not confirm against `origin` (the branch fetch failed) is printed as
`~0.310`, and a PR that could not be read at all gets its error in `WHY` with
no verb.

The `TOKENS` column is mean fresh input context, with `!` when it is under the
200k bar. **A PR under the bar is settled before any review is consulted** — it
fails on tokens alone, so it is held out of the fix list *and* out of the
"needs a review" list, whatever its verdict says or would say. The shortfall is
corpus depth, which no rubric edit can supply. `-q` omits them from the generated
command too. The number is free — read from the bundle's trace summary, or, when
the bundle ships none, out of the PR payload `check` already fetched. Those two
are not equally current: the PR figure is what the task was **submitted** with
and does not move as the task is edited, so `-v` names which one a row used.

### How a PR is judged

`decide()` encodes the pipeline's actual order, and the `NEXT` column always
states the reasoning rather than just a label:

1. **Un-pushed work first.** A verdict that hasn't seen your changes is stale.
2. **Under the token bar is a straight fail.** Nothing else matters; no review or
   fix will help.
3. **A result must describe this branch.** One that is still being produced, or
   that pinned a commit older than your newest one, settles nothing — see *Why a
   row can be trusted* above.
4. **QC and fairness both gate.** Each is asked separately and each must come
   back clean and current; either one's findings mean `fix`, and the row names
   which reviewer wanted what. A review that has not run, or that crashed, is a
   missing review — `trigger` asks for both.
5. **Rescore is a score, not a review.** It only routes work when it failed *and*
   blamed the task (`Failure categories: … task compatibility`, or an
   `INFRA_ERROR`) — that goes to `repair`. Any other rescore failure is the
   harness's problem, so rescoring again is the answer. Otherwise the number
   decides: at or under the cap the task is done, above it it is too easy — and
   a score measured before your newest commit needs re-measuring first.
6. **A task that passed review as submitted is judged on the score it arrived
   with.** See below.

So a row reads `QC and fairness both passed, scores 0.424 — done`, or names
both complaints at once — `QC found Phase 2 raised issues; fairness found
confirmed: hidden citation quota` — rather than leaving you to work out which
bot mattered.

### Tasks that pass QC unchanged

When QC passes a task **we have never edited**, the score it was submitted with
is the whole answer: at or under the cap it is `done`, above it is `drop`, and
neither outcome involves us doing any work. There is nothing to re-measure,
because nothing changed; and hardening would mean rewriting a task the review
has already accepted.

```
#2503  drop      ok       —        0.957!   429k   64m   passed QC unchanged and scores 0.957 > 0.6 — a straight fail
#2498  done      ok       —        0.486    305k   63m   passed QC unchanged, scores 0.486 — done
```

"Never edited" means no commit of ours on the PR, judged two ways so that
neither alone has to hold: the runner's own commit message (`Address PR review
feedback`) and the authenticated GitHub account from `gh api user`. The
contributor's commits carry neither, and the bot's `chore(rescore)` commits are
not ours either. Once we *have* pushed to a PR, the ordinary route returns —
too easy means `harden`, and a clean review under the cap wants a `rescore` to
confirm the number our edits changed.

The rule only fires when **both** reviews have passed on the current commit.
Findings still mean `fix`, a missing or crashed review still means `trigger`,
and the token bar still fails first.

`fix` follows the same order for its prompt: it feeds the agent the **newest QC
check and the newest fairness review**, each whole and each introduced by what
it is, so the agent knows the fairness half asks whether the task is solvable as
written rather than how the bundle is built. A combined `Task review` comment
carries both halves at once and is sent as one.

At the bottom it groups every PR by what it needs, and each group prints the
same three things — what it is, which PRs, and the command to run:

```
TOO EASY AS SUBMITTED — passed QC with no changes and scores above 0.6, so it is a straight fail  (1)
  2503
  nothing to run — the score it was submitted with is the answer
  hardening it would rewrite a task the review already passed

NEED FIXING — a review raised something to answer  (2)
  2358,2378
  python3 run.py fix --task 2358,2378

NEED REPAIR — the verifier rejected the bundle  (2)
  2352,2353
  python3 run.py repair --task 2352,2353

CANNOT BE FIXED HERE — a solver-visible file changed after their traces were recorded  (7)
  2314,2315,2316,2354,2355,2380,2381
  rescore cannot help; the solver must be re-run
  python3 run.py comment --task 2314,… -b "/bot mm-trace-run"
  escalate if you do not have solver access

BOT IS STILL WORKING — a result is on its way; re-run check rather than queueing another  (1)
  2312
  nothing to run

TOO EASY — score above 0.6  (3)
  2359,2361,2377
  python3 run.py harden --task 2359,2361,2377

DONE — review clean, score under the cap  (5)
  2313,2356,2357,2360,2379
```

Groups appear in the order the pipeline is worked, most blocking first:
un-pushed work, token-bar failures, too-easy-as-submitted, fixes, repairs,
un-fixable, reviews, out-of-date, in-flight, too-easy, needs-a-score, done.
Empty groups are omitted. A PR the bot is still working on never appears in a
group that would queue the same job again, and neither straight-fail group
offers a command — there is no work to do on either.

| Flag | Effect |
|---|---|
| `--fail-only` | Only FAIL counts as needing work. Default includes WARN. |
| `-q, --quiet` | Print just the fix command, nothing else. |

`-q` makes it usable directly:

```bash
$(python3 run.py check -q)
```

Read-only — it never posts, commits, or changes a file.

### `score`

Estimates each task's reward locally, as a stand-in for `/bot rescore` while
that's in maintenance. Flags anything above the threshold (default 0.6) as too
easy — a mean of 0.6 is the maximum a task may score.

```bash
python3 run.py score --task 1393,1395
python3 run.py score --threshold 0.45
```

It does **not** guess from scratch. Each repo already contains recorded runs of
real solver agents on that exact task, so the script reads their measured
rewards out of `result.json` directly — exact, instant, and free — and hands
them to the agent as a fixed anchor. The agent's job is only to reason about
the *delta*: what the recent edits to `instruction.md` and `rubrics.json` do to
that measured number. Per-criterion MET/NOT-MET verdicts from those same runs
are in `verifier/judgment.json`, so it can carry individual verdicts forward
rather than re-judging blind.

Two trial layouts and two result schemas are both handled: `traces/solver-*`
with the reward under `verifier_result.rewards.reward`, and
`trace/<task>/codex-trial-*` with it at the top level.

Output shows three numbers per PR, from three different sources, plus the
signed movement:

- **OLD** — the mean stated in the PR's opening comment, i.e. what was
  submitted. Reads the `Mean SOTA score` line, falling back to averaging the
  trial table when that line is absent.
- **MEASURED** — the mean recovered from the recorded trial files on disk.
- **NEW** — the estimate for the task as it stands now, after your edits.
- **REVIEW** — the bot's current PASS / WARN / FAIL fairness verdict.
- **SCORE** — whether the estimate is below or above the threshold.

The two verdict columns are independent, and the combination worth watching is
`REVIEW=PASS` with `SCORE=ABOVE`: the bot is satisfied but the task has become
too easy to be useful. Nothing else in the pipeline catches that, so `score`
calls it out explicitly.

Runs under `read-only`, so estimating can never modify a task. Parallel by
default.

| Flag | Effect |
|---|---|
| `--threshold N` | Flag scores above this. Default 0.6 (0.6 itself passes). |
| `-j, --jobs N` | How many at once. Default: all. |
| `-n, --dry-run` | Build the prompts and stop. |

Full reasoning lands in `.runner/<repo>/score-result.json`.

`check` also shows the measured baseline for every PR, at no cost, since it's
just reading files. A `!` marks one above the threshold.

### `harden`

For tasks that score **above** the threshold — too easy, and they will be
rejected. `fix` is the wrong tool for these: it responds to review comments,
and a task can pass fairness review while still being too easy.

```bash
python3 run.py harden --task 1406
python3 run.py harden --task 1406,1407 --attempts 3
```

Each attempt: the agent reads the recorded runs to find which criteria every
solver passed (those measure nothing), edits the task to remove whatever is
being given away, then the score is re-estimated. If it drops below the
threshold it stops; otherwise it tries again, up to `--attempts` (default 2).

The prompt pushes difficulty toward research depth — stop handing over source
populations and conclusions, require resolving conflicting authorities — and
explicitly forbids adding clerical burden, breaking solvability, or making the
task ambiguous. It also has to keep the bundle coherent: instruction, rubric,
golden solution and reference data must still line up.

Only touches tasks measured above the threshold; `--force` overrides.
Leaves changes uncommitted, so `review` and `push` work as usual.

| Flag | Effect |
|---|---|
| `--threshold N` | Target to reach or beat. Default 0.6. |
| `--attempts N` | Tries per task. Default 2. |
| `--force` | Harden even tasks already below the threshold. |

**The after-score is an estimate, not a measurement.** Only a real solver run
gives the true number, so treat a pass here as promising rather than proven.

### `analysis-score`

Diagnoses why each task scores what it does, then synthesises the trend across
all of them.

```bash
python3 run.py analysis-score
python3 run.py analysis-score --synthesis-only    # reuse cached per-task work
```

Two stages. First, one agent per task in parallel reviews six parts of the
bundle — instruction, rubric, tests/verifier, solution, data/environment, and
overall task design — rating each good/adequate/poor and saying whether its
condition pushes the score up or down. It also judges compliance against what
the fairness review checks, and classifies the task as hard or easy *for good
or bad reasons*, since a task that is hard because of genuine research depth
and one that is hard because its rubric is brittle need opposite fixes.

Second, a synthesis pass turns those reviews into a short briefing: headline,
what pushes scores up, what pushes them down, recurring problems, and
recommendations.

This is a qualitative review, not a numbers exercise. Measured figures are
passed in as context so the agent spends its budget on judgement rather than
arithmetic.

Stage one is expensive; `--synthesis-only` reuses the cached
`analysis-result.json` files and redoes just the cross-task pass.

The report at `.runner/analysis/report.md` is written to be shared as-is. Its
first ~40 lines are the briefing; below that sits a one-row-per-task table and
short per-task notes listing only the parts rated poor.

Transient artefacts (event streams, prompts, logs) are pruned when a run
finishes — they are most of `.runner`'s size. `--keep` retains them.

Treat synthesis claims as leads, not conclusions — verify before acting. In
practice it has both found real defects and misdiagnosed ordinary edit history
as evaluator bugs.

### `clean`

Everything the runner leaves behind, in layers. Takes the usual selection
arguments, so it can be pointed at one task or run over all of them.

```bash
python3 run.py clean                  # drop event streams, prompts, logs
python3 run.py clean --results        # also drop diffs, summaries, estimates, analyses
python3 run.py clean --work           # delete the work dirs outright
python3 run.py clean --purge          # empty work/ completely, state.json included
python3 run.py clean 1709 --repos     # put that checkout back to HEAD
python3 run.py clean --all            # purge, and restore every checkout
python3 run.py clean --purge -n       # show what that would remove, and stop
```

| Flag | Effect |
|---|---|
| `--results` | Also delete results: `*.diff`, `last-message.md`, score and analysis output. |
| `--work` | Delete each selected repo's whole `work/<repo>/` directory. Implies `--results`. `state.json` survives. |
| `--state` | Forget the recorded status for the selected repos, plus every entry whose checkout is gone. Removes `state.json` once it is empty. |
| `--purge` | **Empty `work/` completely** — every work dir, the analysis output, and `state.json`. What you want when you are done with a batch of repos. Implies `--work --state`. |
| `--repos` | `git reset --hard` + `git clean -fd` in each checkout. **Destructive** — the diff is saved to the work dir first, and ignored files like `TASK.md` survive. |
| `--all` | Every layer above: purge `work/` and restore the checkouts. |
| `-y, --yes` | Skip the confirmations. |
| `-n, --dry-run` | List what would go and stop. |

Everything in `work/` is regenerable except **`state.json`** — it is the record
of which tasks have already been pushed, and losing it means `fix` will happily
re-run them. That is the one thing `clean` confirms before removing, so plain
`clean`, `--results` and `--work` all leave it alone. `--state` and `--purge`
are how you say you actually mean it.

When the run covers every repo (no PR numbers given), `clean` also sweeps
**orphans** — work dirs and state entries belonging to checkouts that are no
longer in the folder. Nothing else ever walks them, so they accumulate
indefinitely; they are usually most of what `work/` is holding. A scoped run
like `clean 1709 --state` never touches them.

### `analyse`

Judges whether a bundle is **written for a person** — structure, instruction,
report, decision log. Read-only throughout: it never edits anything, because
what to do about a bad instruction is a separate and expensive decision.

Two stages.

**The counters** always run, free and offline:

```
PR     FILE                 CHECK                  N  WHAT
#2600  instruction.md       wall-of-text           9  9 line(s) over 400 characters with no structure
#2600  solution/report.md   repeated-lines         8  8 fragment(s) repeat verbatim, 83 occurrence(s)
#2600  solution/report.md   decision-log          12  12 record(s) written as runs of `**Label:** value`
#2600  solution/report.md   comma-run              5  5 sentence(s) list six or more items inline
#2600  solution/report.md   no-tables              1  no markdown table anywhere in 247 line(s)
```

| Check | What it looks for |
|---|---|
| `structure` | expected paths missing: `instruction.md`, `task.toml`, `tests/rubrics.json`, `tests/test.sh`, `solution/report.md`, `solution/solve.sh`, `environment/`, `traces/` |
| `instruction-drift` | `instruction.md` and `tests/instruction.md` differ — the solver and the grader reading different papers |
| `decision-log` | records stacked as runs of `**Label:** value` sharing one set of labels. That is a table with its columns turned sideways, and it is what makes a decision log unreadable in bulk |
| `record-not-table` | the same shape outside a decision log |
| `repeated-lines` | sentence-length fragments recurring three or more times — boilerplate a column would state once |
| `inline-enumeration` | `Categories: [drama, comedy, short]` in prose — parallel lists a reader has to align in their head |
| `comma-run` | a sentence enumerating six or more items |
| `wall-of-text` | a line over 400 characters with no structure |
| `no-tables` | no markdown table anywhere in the file |

Markdown links are reduced to a token before anything counts commas or measures
a sentence, so a captured page title like `[BBC Culture | Arts, Film, Reviews,
Books, Music](…)` is not read as a six-item list. Fenced code is skipped.

**`--deep`** then puts a read-only agent on the part a regex cannot see:

```bash
python3 run.py analyse --task 2600 --deep
```

```
PR     VERDICT   ANSWERABLE  ANSWERED  HEADLINE
#2600  poor      yes         NO        Explicit requirements are buried in prose, while the …

  instruction    adequate  The destination and requirements are explicit, but each case …  (reads generated)
  report         poor      Bold labels expose topics, but requested fields remain abstract  (reads generated)
  decision-log   poor      Entries bundle multiple conclusions under opaque slugs …         (reads generated)
  structure      good      All expected artifacts occupy conventional locations …

  high   solution/report.md:55
         The requested population, period and outcome fields remain unfindable because the
         report only says the source provides them.
         wants: one complete record: Edition | Population | Period | Outcome
```

Two columns carry most of the value. **ANSWERABLE** is whether a solver reading
only `instruction.md` could tell what is wanted, where it goes, and what counts
as done. **ANSWERED** is whether `solution/report.md` visibly answers it. A task
can be perfectly countable and still fail both.

`reads_as_human` is the other one worth watching: it marks an area whose prose
reads as a template a generator filled in rather than something written for a
reader — field lists strung into sentences, one frame reused per row, hedging
boilerplate on every claim.

Each finding names a location and **the shape the content wants** — "a 12-row
table: Status | Finding | Evidence | Consequence" — never prose to paste. The
agent is told not to restate the counted findings, so `--deep` adds judgement
rather than volume.

The skill is `prompts/analyse.md`, its output schema `schemas/analyse.json`.
It judges readability only: whether the task is *fair*, whether the rubric is
right and what it scores are other passes' jobs.

| Flag | Effect |
|---|---|
| `-v` | show the offending lines under each counted finding |
| `--deep` | run the agent judgement pass |
| `-n, --dry-run` | write the prompt, start no agent |
| `-j, --jobs` | agents in parallel (default 4) |

### `rules`

Checks the task-quality rules every bundle must satisfy. Free and read-only.

```bash
python3 run.py rules
python3 run.py rules --task 1576 -v    # -v shows passing checks too
```

| # | Rule | Checked |
|---|---|---|
| 1 | Total negative weight < total positive weight | automatic |
| 2 | Negative criteria count ≤ 60% of positive count | automatic |
| 3 | Positive criteria total > 300 points | automatic |
| 4 | Criteria binary, atomic and independent (no bundling) | by the agent during `fix` |
| 5 | Negative weights in -1..-100 (-500 for severe cases) | automatic |
| 6 | ≥3 Tier 0 decoys, ≥25 Tier 1, ≥5 Tier 2 sources | automatic |
| 7 | Fresh input context > 200k tokens | automatic |

Tier counts come from `tests/source_tier.txt` or `source_tiers.txt`.

Rule 7 reads `mean_solver_fresh_input_tokens` from `trace/<task>/trace-summary.json`
— a file read, not a GitHub call, which matters because the rule block is built
into every fix and audit prompt. Where that file declares its own
`fresh_context_reference`, that bar is used instead of the 200k default.

It is the one rule a fix round **cannot** repair: it measures how much corpus the
recorded solver runs actually pulled, which is a property of the task's data and
instruction, not its rubric. The fix prompt says so explicitly and tells the agent
to report the failure rather than padding the instruction or corpus to inflate the
number.

`fix` runs these same checks and injects the results into its prompt, so any
violation gets fixed in that round whether or not the reviewer mentioned it.

### `fix`

Runs an agent per repo, **all in parallel**, and stops before anything is
pushed. For each repo it fetches the newest fairness review, builds a prompt,
runs `codx exec` in a sandbox, and saves the resulting diff.

| Flag | Effect |
|---|---|
| `-t, --task PRS` | Only these PRs. Also overrides the status filter, so already-pushed repos are eligible. |
| `-j, --jobs N` | How many at once. Default: all. `-j 1` = sequential. |
| `-n, --dry-run` | Build the prompts and stop. Nothing runs, nothing is charged. |
| `--all-comments` | Send the whole PR thread instead of just the fairness review. |
| `--redo` | Re-run repos already marked fixed or pushed. |
| `--discard-dirty` | `git reset --hard` first, throwing away uncommitted changes left by a run that died partway. Destructive — it discards work. |
| `--sandbox MODE` | `read-only`, `workspace-write` (default), `danger-full-access`. |
| `--network` | Let the agent's shell commands reach the network. |
| `--timeout MIN` | Minutes before an agent is killed (default 30, `0` = no limit). |

Each repo is **reported the moment it lands**, with a running `[n/17 done]`
count, and the last few stragglers are named so you can see what the batch is
waiting on. Results are also written to `state.json` per repo as they finish, so
`review` and `push` work on the completed ones from another terminal while the
rest are still running — you never have to wait for the slowest agent to look at
the others.

No agent can hold the batch open indefinitely. At the timeout its **whole process
group** is killed — codx is a bash wrapper around node, so signalling just the
pid it hands you leaves the real agent running. Anything the agent had already
edited is kept, captured to `changes.diff`, and reported as `timed-out` with the
file count, because a half-finished bundle is usually still worth reading.

Per-repo outcomes: `fixed`, `waiting` (bot hasn't re-reviewed yet),
`up-to-date`, `skipped` (dirty tree), `no-context`, `failed`.

The prompt is `prompts/fix.md` plus four injected blocks: a fact sheet for the
bundle, the rule results, `prompts/qc-guidelines.md`, and the PR discussion.

The **fact sheet** (`bundle_facts`) is the anti-roaming measure. Without it an
agent burns its first dozen round trips locating the bundle, hunting for the tier
and contract files, and paging through a 1,500-line `rubrics.json` to learn what
the criteria are — PR 2080 made ~40 reads of six files before its first edit. So
the runner computes it up front: every key file that exists with line count and
size, the rubric totals, and a criterion index of id, axis, weight and error
category. The agent opens the one criterion it needs instead of reading the file.

It costs ~1,075 tokens re-sent per call, about 27K over a 25-call task. One
avoided exploratory call saves ~60K (measured tokens-per-call), so it pays for
itself several times over.

`qc-guidelines.md` is one rubric standard covering fairness (judge only what the
solver could see), the criterion schema and axis vocabulary, atomicity, accuracy,
weight sign, how to raise a positive pool honestly, and the "do not fix these —
they are by design" list.

`fix.md` also sets a work order — read once, decide the complete edit set, apply,
verify once — with an explicit rule that opening the same file a third time means
researching rather than fixing.

Deliberately **not** injected, though they stay in `prompts/` as reference:

- `review-prompt-rubric-calibration.md` — the reviewer's full standard. Written
  from the reviewer's seat: it asks for a JSON verdict, counts MET/UNMET across
  traces, and caps any weight change at 50%. That cap contradicts the 300-point
  floor for any bundle starting below ~200 points (PR 1877 had 33 criteria worth
  101 points: the floor demanded 300, the cap allowed 151), and agents looped on
  it. `build_prompt` still passes it as `rubric_calibration`, so adding that
  placeholder back to `fix.md` re-enables it with no code change.
- `qc_pipeline.txt` and the vendor setup guide — 80KB of runbook for a separate
  audit tool. `qc-guidelines.md` is condensed from these.

**The standard is the prompt.** `fix.md` is now just a wrapper around it —
~1.9KB of the 13.4KB sent for a typical task, against 10.2KB for the standard
itself. The wrapper carries only what the standard cannot know: which repo and
branch this is, what the checks found, the pipeline rules (leave work
uncommitted, never touch `trace/`), and the framing that marks PR text as data.

It does not restate the six rules in prose — the injected block already names
each rule and its threshold — and rule 4 (atomicity) is covered in far more
depth by the standard's own "Atomicity and adding criteria" section.

The standard is written from the reviewer's seat, so the wrapper re-frames it in
one paragraph: its "Required output" JSON section is overridden, and where its
weight bounds (positive ≤ +30, calibration steps ≤ 50%) disagree with the
automated checks, the agent obeys the stricter and flags the conflict. Edit the
standard to change how `fix` behaves; the wrapper should rarely need touching.
Delete it and the prompt still renders, minus the guidance.

### `review`

Prints each agent's summary and full diff for repos in the `fixed` state.
Read-only. Narrow it with `--task` — the diffs are large.

### `audit`

Round two. Answers **one question**: did the recorded solver runs pull at least
200k fresh input tokens? No agent, no GitHub call, no tokens — it reads
`mean_solver_fresh_input_tokens` out of each bundle's `trace-summary.json`, so
the whole batch takes about 70ms and works offline.

```bash
python3 run.py audit                  # every task
python3 run.py audit --task 1893,1894 # just these
python3 run.py audit --deep           # run an agent for a rubric review instead
```

```
Fresh input context — 14 task(s), need >= 200,000

| PR   | Result | Mean tokens | Reason |
| ---- | ------ | ----------- | ------------------------------------- |
| 1893 | FAIL   | 198,981     | FAIL, mean token size 198,981 < 200k |
| 1894 | PASS   | 309,441     | PASS, mean token size 309,441 >= 200k |
| 1907 | FAIL   | 133,514     | FAIL, mean token size 133,514 < 200k |

4 pass, 10 fail
```

The reason repeats its own verdict so it still reads correctly once pasted into a
sheet, away from the `Result` column.

Below the table the same reasons are repeated **tab-separated**, so selecting
those lines and pasting drops straight into Excel as two columns:

```
Reasons — copy from here into Excel:

1893	FAIL, mean token size 198,981 < 200k
1894	PASS, mean token size 309,441 >= 200k
```

When a bundle ships no `trace-summary.json`, the number is read from the PR
instead — `Mean fresh context:` in the body, or a `Fresh input tokens` column in
the opening trial table. Those lookups run in parallel and only for the bundles
that need them, so a fully local batch still costs nothing.

Rubric quality is deliberately **not** here — that is the bot's fairness review
(`check`) and the numeric rules (`rules`). `unknown` means the bundle ships no
`trace-summary.json`, so the number cannot be measured rather than being measured
and low.

`--deep` runs an agent over the rubric for an independent read. Use it knowing
its opinion is a *second* opinion: the bot's fairness review is what actually
gates a task, and an early `--deep` run called two tasks `major` that the bot had
already passed as fair.

`--deep` produces a different table, from the agent:

```
| PR   | Verdict | Criteria | Fresh context   | Issues |
| ---- | ------- | -------- | --------------------- | ------ |
| 1906 | major   | 79       | ok                    | neg-cit-001 and neg-cit-002 use negative weights for requirements phrased as absence of citation errors... |
| 1907 | major   | 90       | points 195/300; Tier 1 15/25 | p-material-citations bundles independent citation obligations into one pass/fail score... |
```

Issues are one paragraph per task, written impersonally — about the bundle, never
"I confirmed…".

**`audit` reports one numeric rule only: fresh input context.** Point totals,
negative ratios and tier counts are a fix-round concern and stay in `rules`,
`fix` and `check`; a round-two review should be about fairness and rubric
quality, not arithmetic a fix round already handles. The `Fresh context` column
is computed by the runner, and the prompt forbids the agent from mentioning any
other numeric rule.
`verdict` is `major` (a fairness or rule violation — not usable), `minor` (real
but not blocking) or `ok`. Majors come with the `fix --redo` command to re-run
them. Low-severity findings are hidden unless you pass `-v`, and the full JSON
stays at `work/<repo>/audit-result.json`.

The schema forces the agent to commit to a verdict and an issue list rather than
writing an essay, and the prompt tells it that an empty issue list is the correct
answer for a sound task — so it does not manufacture findings to look useful.
Because it gets the same bundle fact sheet `fix` does, an audit costs a fraction
of a fix round: the first real run examined 39 criteria in **6 commands and 361K
tokens**, against 0.6–2M for a fix.

### `push`

For repos in the `fixed` state: `git add -A`, commit, push, then post
`/bot fairness-review`. Lists everything and asks before doing any of it.

| Flag | Effect |
|---|---|
| `-y, --yes` | Skip the confirmation. |
| `-m, --message` | Commit message. Default: "Address PR review feedback". |

Records a `pushed_at` timestamp — the cutoff the next round uses to decide
what counts as new.

### `repair`

For PRs where the **rescore** failed. A task can pass its fairness review and
still be unscoreable: the verifier exits with its own contract error, e.g.

```
INFRA_ERROR: rubric must contain exactly one correctly named decoy-reliance criterion
```

```bash
python3 run.py repair --task 2263
python3 run.py repair --task 2263 -n     # build the prompt and stop
```

The agent gets the rescore comment verbatim, the bundle map, and the rule
results, and is asked two questions in order: **is the verifier right**, and if
so what is the smallest change that satisfies it without breaking fairness or the
numeric rules.

**Not every failed rescore is repairable.** Two classes turn up, and only one
belongs here:

| rescore says | means | next step |
|---|---|---|
| `INFRA_ERROR: …`, `Failure categories: … task compatibility` — e.g. *"ground truth must map every positive criterion exactly once"* | the bundle breaks a contract the verifier enforces | **`repair`** |
| `[stale-solver-inputs]` — *"Fresh solver traces are required; verifier-only rescoring cannot repair changed solver inputs"* | `instruction.md` changed after the traces were recorded | **re-run the solver** — `/bot mm-trace-run`. Not a dead task, but not a `repair` either |

**Task drift needs solver access, so treat it as a one-way door.** The bot's own
description of rescore is the tell: it re-scores existing traces *"after
rubric/test-only changes"*. Edit a solver-visible file such as `instruction.md`
and the traces no longer match the task, so rescoring can never succeed. The only
cure is a trace run (`/bot mm-trace-run`, `/bot run-grok --5`) — and if you cannot
run the solver, the task is stuck until someone who can does it.

That is why `fix.md` makes editing `instruction.md` a **last resort**: a rubric,
test or reference edit keeps a task scoreable, an instruction edit does not. Where
a finding can be answered by making the rubric match the instruction rather than
the other way round, the agent is told to do that, and to flag it explicitly when
it genuinely has to touch the instruction.

`check` separates them, so the second class is never sent to `repair` and never
told to rescore again.

It is explicitly allowed to answer "no". A verifier that exited on a missing file
it should have staged, or timed out, is not something a rubric edit can fix — the
prompt tells the agent to report that and change nothing, because a wrong "fix"
there corrupts a sound task. `no-changes` is a real outcome, not a miss.

Same guarantees as `fix`: `workspace-write`, `traces/` off limits, corpus pointer
off limits, work left uncommitted for `review` and `push`, and the same timeout.

`check` routes here on its own — a PR whose rescore failed is reported separately
from the ones that are genuinely ready, with the verifier's own sentence as the
detail.

### `comment`

Posts any comment on the selected PRs.

```bash
python3 run.py comment 2262 -m "Regenerating the trace bundle at the reviewed head."
python3 run.py comment 2262,2263 -m "First paragraph." -m "Second paragraph."
python3 run.py comment --task 2262 -F notes.md      # body from a file, markdown fine
```

| Flag | Effect |
|---|---|
| `-m, --message` | Comment body. Repeat for extra paragraphs. |
| `-F, --file` | Read the body from a file instead. |
| `-y, --yes` | Skip the confirmation. |

It prints the body and the list of PRs, then asks before posting — the same
confirmation `push` and `rescore` use, since a comment is public and cannot be
unsent. Bodies over 500 characters are previewed truncated.

### `rescore`

Posts `/bot rescore` on the selected PRs. That is the entire action: one
`gh pr comment` each. No commits, no pushes, no file changes. Asks first.

| Flag | Effect |
|---|---|
| `-y, --yes` | Skip the confirmation. |
| `-b, --body` | Post something else, e.g. `--body "/bot fairness-review"`. |

`push` posts `/bot2 qc-check` after the commit lands.

Before each push the branch is **fetched and rebased onto its remote**. This is
routine, not an error path: `/bot2 rescore` pushes refreshed judge artefacts to
the same branch between rounds, so by the time you push, the remote is usually
one commit ahead and a plain push is rejected. Those artefacts live under
`traces/`, which the agent may not touch, so the rebase applies cleanly. If a
real conflict shows up the rebase is aborted, your commit is left intact, and
the repo is reported failed with the `git pull --rebase` command to run by hand.
Nothing is ever force-pushed.

### `rescore --check`

Shows what the last rescore measured, and posts nothing.

```bash
python3 run.py rescore --check
python3 run.py rescore --check --task 2358 -v    # per-trial numbers
```

```
PR         OLD     NEW    DELTA  STATUS   WHEN        DETAIL
#2358    0.437   0.397   -0.040  ok       08-12 11:00
       solver-01-Z2RM6Up        0.362 →   0.384    +0.022
       solver-02-hqEQSBd        0.448 →   0.287    -0.160
       solver-03-oPbr5cK        0.500 →   0.519    +0.019
#2352    0.569       —        —  FAILED   08-12 10:54 ground truth maps an unknown, duplicate, or negative
```

`OLD` is each trial's reward at the pinned head, `NEW` is what the fresh run
produced — both read from the rescore comment's own table, so no local files are
involved.

A **failed** rescore never produces that committed column, so `OLD` falls back to
the mean the task was submitted with (`Mean SOTA score` in the PR body, or the
bold Mean row of the opening trial table) and the value is marked `*`. That way
every row shows a baseline instead of a dash.

The footer counts how many moved, how many are above the cap and how many failed.

### `reset`

Forgets the recorded status for the selected repos, so `fix` treats them as
new. Local bookkeeping only — never touches git or GitHub.

## The two bot commands

The pipeline uses exactly two, and nothing else:

```
/bot2 qc-check    posted on a new task, and again after every fix round
/bot2 rescore     posted once a task passes its review
```

`/bot2 fairness-review` is **deprecated** — the pipeline never posts it. Its
comments are still read, because open PRs carry them and dropping them would
blank a task mid-flight, but QC decides.

```bash
python3 run.py trigger    # new tasks — posts qc-check
python3 run.py push       # after a fix round — posts qc-check again
python3 run.py rescore    # once it passes
```

`trigger` defaults to repos the pipeline has never acted on (status `new`); name
PRs explicitly to override. It confirms before posting, and **skips anything
below the fresh-context bar** — those fail on tokens alone, so a review run on
them is wasted. `--starved` includes them anyway.

Anything else the bot offers, pass by hand:

```bash
python3 run.py rescore --task 2157 --body "/bot2 review"
```

**`check` reports the fairness review and nothing else** — verdict, its findings,
the measured score, and the token count. `fix` gets exactly one comment: the
newest fairness review. A PR accumulates several over a round, and anything older
may already have been answered, so `latest_review()` picks one and only one.

Comments the bot posts *about* a review are not reviews. The queue
acknowledgement reads "🧭 Human fairness review queued", which matches the review
heading as a substring — so detection keys off the bot's HTML markers
(`github-review-bot:fairness-review`, `github-review-bot:human-review`) instead,
falling back to the phrase only when it appears as a heading. Without that, the
ack shadows the review it announces, because it is posted after it.

## Three comment formats

The bot has posted three shapes and all are live on open PRs, so everything reads
any of them:

| | v1 | v2 | v3 (current) |
|---|---|---|---|
| Heading | `Task Fairness Review - PASS/WARN/FAIL` | `Task Fairness Audit` | `Task review` / `Human fairness review` |
| Marker | `fairness-review` | `fairness-review` | `human-review` |
| Covers | fairness | fairness | fairness, sometimes with QC sections |
| Verdict | stated outright | none — non-gating | none — advisory |
| Detail | `## ❌ <section>` | `### 🔴 \`FR-…\`` findings | `### At a glance` rows + numbered items |

v3 keeps growing headings under the same `human-review` marker — `issues found`,
`review advised`, `no issues`, `partially completed` — so detection keys off the
HTML marker, never the heading.

**The verdict comes from the words, not the emoji.** The At-a-glance mark has
been ✅, ⚠️, ❌, ⚪ and 🟡 so far; enumerating them meant every new one parsed as
`?` with no detail. The row is now read whole and classified by its text:

| row text | verdict |
|---|---|
| `N confirmed issue(s)` | FAIL |
| `N probable warning(s)` | WARN |
| `no issues` / `passed` / `0 flag(s)` | PASS |
| `could not complete` | `incomplete` |

Each finding carries a confidence in its tag — `Fairness · probable · high ·
trace-task-drift` — and that is what decides whether to act, so `check` leads the
detail with it:

```
#2208  WARN   probable: Trace manifest binds instruction.md to different bytes
#2209  FAIL   confirmed: Trace manifest hashes do not match reviewed instruction
```

With more than one finding the detail opens with the tally
(`2 confirmed, 1 probable — …`), so a narrow column still says how much is
established versus judgement.

An unrecognised wording with numbered findings under it counts those findings
rather than silently passing the task. `incomplete` means the engine crashed, not
that the task failed — `check` groups it with "no usable review yet", because the
action is re-running the review. Its QC row is a plain `- **QC:**` with no phase
number, which the pattern also accepts.

None of v2 or v3 states a verdict, so `fairness_verdict()` derives one — the
pipeline still has to decide what to send to `fix`:

- **v3**: read the `- **Fairness:** ⚠️ 2 confirmed issue(s)` row. Confirmed → FAIL,
  probable only → WARN, ✅ → PASS. QC comes from the two `QC Phase` rows.
- **v2**: count `confirmed` / `probable` in the finding blocks, falling back to
  the headline counts.
- **v1**: unchanged.

Because v3 is one comment covering both halves, the fairness finder and the QC
finder both match it — it is detected and rendered **once**, not pasted into the
prompt twice.

`fix` gets the whole comment plus a format-specific note. Without one an agent
reads "advisory and non-gating" and concludes there is nothing to do. The v3 note
also carries an explicit carve-out: **items reporting that the corpus is a Git LFS
pointer are not defects.** v3 raises those as QC Phase 2 "data issue" flags — 6 of
10 on PR 2157 — and acting on them is exactly the failure that once deleted a
244 MB pointer and started synthesising a corpus.

The emoji marks are matched with their variation selector (`⚠️` is two
codepoints); without that the selector leaks into the parsed text.

## What counts as feedback

The thread is mostly pipeline chatter, and none of it is feedback on the task:
the 6KB `TerminalBench Bot Commands` listing, queue acknowledgements, capacity
notices, infrastructure-failure notices that say so themselves, and the `/bot2 …`
commands we post ourselves. `is_noise()` drops all of it.

This matters on the fallback path. When a PR has no review yet, `fix` sends the
whole thread — and PR 2161's entire thread is 6,943 characters of exactly that
noise, with no review in it. Without filtering, an agent would be started on a
command listing. With it, the repo reports `no-context` and no agent runs.

## How context is chosen

Only the **newest fairness review** is sent to the agent. The thread also
carries the bot's command help, the `/bot ...` commands you post, "review
started" pings, and infrastructure-failure notices that explicitly say they
aren't a quality verdict — all of that is noise and is dropped. This cuts the
prompt by roughly 85%.

On a repo already pushed, the verdict must also be **newer than that push**.
If it isn't, the bot hasn't re-reviewed yet and the repo is reported `waiting`
rather than re-fixing something already addressed.

Human (non-bot) comments are currently dropped too.

`--all-comments` turns the filtering off.

**Both reviews go, and each carries every earlier round in full.** Under the
newest verdict sit all previous rounds from that same reviewer, whole and
labelled as history. The agent is otherwise sent just the newest review, which
is right for *what* to fix and wrong for *how*: it cannot see that the same
complaint has already come back five times, so each round retries the last
round's answer. That runs 60–170k characters of review on a long-running PR,
which is the point — nothing is summarised, because which round said what, in
what words, is exactly the detail that shows a remedy was already tried.

**When only one reviewer has answered, the prompt says so.** If QC has not
re-run since your push, its verdict describes an earlier commit and is left
out — and a short note says which half is missing and that the agent must not
assume it passed.

**The review that is sent goes whole.** Nothing inside it is summarised,
folded or capped — every phase narrative, every flag, and every row of every
per-criterion table. Those tables look like pure repetition, which is what
made an earlier cap on the first N rows seem safe, and it was not: on #2562
the rows naming the actual ID mapping (`fact-claim01` is staged as
`fact-c001`) sat at the bottom of a 52-row table, so the agent was told 52
criteria were unmapped without being told what they mapped to. There is no way
to tell in advance which row carries the detail that matters. The largest of
these comments is about 25k characters against a ~27k-character briefing, so
sending all of it costs little and guessing wrong costs a round.

## Safety model

The agent runs under `workspace-write`: it can only write inside the repo it
was pointed at, and its shell commands have no network. It is told not to
commit or push.

The real guarantee is structural — `git commit`, `git push`, and
`gh pr comment` all live in the script, outside the sandbox, behind a
confirmation. A malicious instruction hidden in a PR comment cannot push code
or post as you. Worst case it writes something odd into the working tree, and
`review` shows you before `push` runs.

The prompt also frames PR discussion as untrusted data and tells the agent to
report anything that tries to direct it.

## Files

```
run.py                     the runner
prompts/fix.md             the fix prompt template — edit freely
prompts/review-prompt-rubric-calibration.md  rubric standard injected into it
prompts/score.md, harden.md, analysis*.md    the other templates
schemas/                   JSON schemas the scoring commands enforce
README.md                  this file
work/state.json            per-repo status and timestamps
work/<repo>/prompt.txt     exact prompt sent
work/<repo>/log.txt        full run log
work/<repo>/events.jsonl   raw codx event stream
work/<repo>/changes.diff   captured diff
work/<repo>/last-message.md  agent's final summary
work/<repo>/discarded-*.diff  work discarded by clean --repos or fix --discard-dirty
work/analysis/             cross-task analysis output
```

Everything under `work/` is regenerable except `state.json`. `clean` is what
removes it — see above.

A `TASK.md` dropped in a repo folder is used when GitHub can't be read. It's
added to `.git/info/exclude`, so it never lands in a commit.

## Requirements

- `codx` on PATH
- `gh`, authenticated (`gh auth login`) — without it, PR comments can't be
  read and bot comments can't be posted
- Python 3.11+
