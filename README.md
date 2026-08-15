# PR task runner

Batch tooling for reviewing and repairing benchmark task bundles across many
open pull requests at once.

It answers three questions, in this order:

1. **What is the state of every PR right now?** — `check` reads each PR's QC
   check, fairness review and score from GitHub and prints one line per task
   with the exact next command.
2. **Is this bundle even readable?** — `analyse` judges whether the instruction
   is answerable and the report answers it, before anyone spends a round fixing
   things.
3. **Can an agent repair what the reviewers found?** — `fix` runs one agent per
   repo in parallel, then stops so a human reads the diff before anything is
   pushed.

Nothing pushes, comments or commits without you asking for it.

## Requirements

| | |
|---|---|
| `python3` | 3.10+, standard library only — nothing to `pip install` |
| [`gh`](https://cli.github.com) | authenticated (`gh auth login`) — used to read PRs and post the two bot commands |
| `codx` | the coding agent the `fix`, `harden`, `repair`, `score` and `analyse --deep` commands drive. Everything else works without it |

`check`, `analyse` (without `--deep`), `rules`, `audit` and `list` need no
agent at all and cost nothing to run.

## Layout

Clone this next to your task checkouts, so the working directory looks like:

```
some-folder/
├── run.py            ← this repo
├── runner/           ← this repo
├── pr-2312/          ← a task checkout (one git repo per PR)
├── pr-2313/
└── pr-2314/
```

`run.py` discovers every sibling directory that is a git repo and takes the PR
number from its folder name — `pr-2312`, `…-pr-2312-some-task`, or a bare
`2312` all work.

Run everything from the folder holding the checkouts:

```bash
python3 run.py check
```

## The loop

```
  check ──►  analyse  ──►  fix  ──►  review  ──►  push  ──►  (bot re-reviews)  ──►  check
                                                    ▲
                                                 rescore
```

```bash
python3 run.py check                    # where everything stands
python3 run.py analyse --task 2312      # is the bundle readable at all
python3 run.py fix --task 2312,2313     # agents repair what the reviewers found
python3 run.py review --task 2312       # you read the diffs
python3 run.py push --task 2312         # commit, push, ask both bots to re-review
```

## Commands

| Command | What it does | Costs |
|---|---|---|
| `check` | every PR's QC, fairness, rescore and score, with the next command per task | GitHub reads |
| `analyse` | is the bundle written for a person: structure, instruction, report, decision log | free; `--deep` runs an agent |
| `fix` | agents repair what QC and fairness raised | one agent per repo |
| `review` | show the diffs an agent produced | free |
| `push` | commit, push, post `/bot2 qc-check` + `/bot2 fairness-review` | posts to GitHub |
| `trigger` | post that same pair on a freshly cloned task | posts to GitHub |
| `rescore` | post `/bot2 rescore` once both reviews are clean | posts to GitHub |
| `harden` | make a task that scores too easily harder | one agent per repo |
| `repair` | act on a verifier rejection the rescore reported | one agent per repo |
| `audit` | did the recorded solver runs pull enough fresh context | free |
| `rules` | the numeric task-quality rules, measured | free |
| `score` | estimate a task's reward locally | one agent per repo |
| `analysis-score` | why tasks score what they do, and the trend across them | one agent per repo |
| `comment` | post an arbitrary comment on selected PRs | posts to GitHub |
| `clean` | delete run artefacts | free |

Every command takes `--task 2312,2313` or bare PR numbers / folder-name
substrings. With no selection they act on every checkout.

**[`runner/README.md`](runner/README.md) is the real reference** — every column,
every flag, and the reasoning behind each routing decision. Start there when a
row says something you did not expect.

## What it will not do without you

- **Nothing is committed or pushed by an agent.** Agents leave work uncommitted;
  `push` is a separate step you run after reading the diff.
- **`push`, `trigger`, `rescore` and `comment` ask before posting**, and list
  exactly what they will post and where. `-y` skips the prompt.
- **`analyse` never edits anything**, on purpose. `instruction.md` is
  solver-visible: changing one byte makes every recorded solver trace stale and
  forces a full trace re-run that no rescore can substitute for. Deciding to pay
  that is yours, not a tool's.
- **`fix` refuses a dirty working tree** unless you pass `--discard-dirty`, and
  backs the tree up first when you do.

## How it decides things

Two ideas run through the whole tool, and both exist because the obvious
version was wrong in practice.

**A result is only about the branch as it stands now.** Every bot result names
the commit it ran against (`Pinned PR head: 475300a7…`), and that SHA — not a
timestamp — decides whether the result still applies. Timestamps cannot do this
job, because the bot pushes its own commits to the branch after a rescore. A
verdict from an older commit never settles a row, and neither does one still
being produced.

**The heading is the verdict; the body is detail.** Both review bots state
their conclusion on the first line. Reading the body instead gets it wrong in
both directions: a crash that judged nothing has no findings to count and looks
like a pass, while a pass with a sub-threshold note has findings and looks like
a failure.

## Tests

```bash
python3 runner/test_freshness.py
```

121 checks over the freshness rules, the QC and fairness readers, the routing
table and the readability checks. No network, no agent — it runs in about a
second. Every fixture is shaped from a real comment, and the comments say which
failure each one is pinning down.

## Configuration

There is no config file. The numbers that matter are constants at the top of
`runner/run.py`:

| Constant | Default | Meaning |
|---|---|---|
| `MAX_MEAN_SCORE` | `0.6` | above this a task is too easy |
| `MIN_FRESH_CONTEXT` | `200_000` | the fresh-input-token bar |
| `MIN_POSITIVE_POINTS` | `300` | positive rubric points a task must carry |
| `MAX_NEG_RATIO` | `0.60` | negative criteria as a share of positive |
| `NEG_WEIGHT_MIN/MAX` | `-100 / -1` | allowed negative weight range |
| `TIER_MINIMUMS` | `3 / 25 / 5` | Tier 0 decoys, Tier 1 and Tier 2 sources |
| `JOB_PATIENCE_MIN` | `30`, `360` | how long a queued bot job may stay silent before it counts as lost |

The prompts each agent runs are plain markdown in `runner/prompts/`, and the
structured outputs they return are JSON Schema in `runner/schemas/`. Both are
meant to be edited — that is where most of the tuning lives.
