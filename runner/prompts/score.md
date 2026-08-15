You are estimating the reward a strong AI agent would earn on an offline-search
benchmark task. Do NOT run the task. Produce a calibrated numeric estimate.

Task bundle: `{bundle}`
Task name: `{task_name}`

## The key idea

Do not guess from first principles. This repository usually contains **recorded
runs of real solver agents on this very task**, including their exact rewards
and their per-criterion MET / NOT MET verdicts. Anchor on that measurement, then
adjust only for what has changed since it was recorded.

A blind estimate is systematically optimistic. A measured baseline plus a
reasoned delta is not.

## Step 1 — The measured baseline (already extracted for you)

{measured}

These numbers were read directly out of the recorded trial files, so treat them
as fact — do not re-derive them, and do not contradict them. An oracle near 1.0
means the task is solvable as written; a NOP near 0.0 means the rubric is not
leaking free points.

## Step 2 — Recover the per-criterion evidence

For each solver trial read `verifier/judgment.json`. It contains:

- `criteria[]` — each with `id`, `verdict` (MET / NOT MET) and a `reason`,
- `criterion_contributions` — what each criterion actually contributed,
- `formula`, `formula_id`, `positive_denominator` — the exact scoring rule,
- `score` — the resulting reward.

This is ground truth about **which requirements real solvers actually
satisfied**. Note especially any negative/penalty criteria that fired.

## Step 3 — Work out what has changed since those runs

The task has been edited since the traces were recorded. Find out how:

- `git log --oneline -- {bundle}` and `git diff` the relevant commits,
- compare the criterion ids and weights in `judgment.json` against the current
  `{bundle}/tests/rubrics.json` — which were added, removed, merged, reworded,
  or reweighted,
- read the current `instruction.md` and diff it against what the traces' solvers
  were given; added exact counts, formulas, or precision rules make the task
  harder, while clarifications can make it easier.

## Step 4 — Recompute against the CURRENT rubric

For every criterion in the current `tests/rubrics.json`:

- if it corresponds to one the traces judged, **carry that verdict forward** —
  unless a wording or scope change plausibly flips it, which you must justify,
- if it is new or substantially rewritten, you have **no evidence** about it.
  Do not assume a solver will satisfy it. Default it to the task's historical
  hit rate — the measured baseline is exactly that number — and move away from
  that default only with a specific reason tied to the actual failure.

### The trap to avoid

Rubric rewrites are the main source of bad estimates. When a fix replaces many
exact criteria with fewer, broader, more "semantic" ones, it is tempting to
conclude that solvers will now pass them and the score will roughly double.

That reasoning is usually wrong. Read the `reason` fields on the NOT MET
verdicts in `judgment.json` and ask what actually defeated the solver:

- If it failed because it **never found the source**, or **could not reconcile
  conflicting evidence**, or **missed the distinction entirely** — rewording the
  criterion does not fix that. It still fails.
- Only when the edit removes the **specific barrier named in the judgment
  reason** — the value is now supplied, the ambiguity is resolved, the
  requirement is dropped — should you flip it to MET.

A broader criterion is not automatically an easier one. "Demonstrates
authority-aware synthesis" can be harder to satisfy than a lookup.

### Sanity check before you answer

If your estimate is more than ~0.15 away from the measured baseline, stop and
justify it against the evidence. State, per criterion group, which specific
recorded failure the edit removed. If you cannot name the failures you are
claiming are now fixed, your estimate is too far from the baseline — pull it
back toward the measurement and lower your confidence.

Shrinking the denominator alone does not raise the score: removing criteria
solvers *passed* lowers it, removing ones they *failed* raises it. Check which,
using the recorded verdicts, rather than assuming.

Remember the grading is **binary per criterion** — directionally right scores
zero. Exact row counts, prescribed rounding, full-population ranking and
tie-breaks are routinely missed. Penalties are heavy and can dominate.

Then apply the current formula and `positive_denominator` to get the score.

## Step 5 — Sanity-check

Your estimate should differ from the measured baseline only as far as the edits
justify. If it moves a lot, state exactly which change caused it. If the rubric
grew more demanding, the score should fall; if criteria were removed or relaxed,
it may rise.

Sanity bounds: the estimate cannot exceed the oracle reward, and should not sit
below the nop reward.

## Output

Return ONLY the JSON object described by the output schema. No prose around it.

- `estimated_score` — expected mean reward in [0, 1] for the task as it stands
  now.
- `measured_baseline` — the mean solver reward you recovered, or null if there
  were no traces.
- `baseline_trials` — the individual solver rewards you found.
- `delta_reason` — why your estimate differs from the baseline, naming the
  specific edits. If it does not differ, say why the edits were neutral.
- `positive_criteria_total` / `positive_criteria_expected_met` — counts against
  the current rubric.
- `expected_penalties` — ids of negative criteria likely to fire.
- `hardest_requirements` — the specific requirements you expect to sink the
  score.
- `rationale` — how you got there, including the arithmetic.
- `confidence` — `high` only if you had traces AND the edits were small.

Do not modify any file. Do not run git commands that change state. Read only.
