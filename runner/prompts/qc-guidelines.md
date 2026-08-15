# Rubric standard

Your goal is a **fair** rubric that satisfies the numeric rules. Nothing else. A
fair rubric that is hard to satisfy is the point — difficulty is the result, not
a target you aim at directly.

## Fairness — the one rule everything else serves

Judge only what the solver could see: the agent-facing instructions, the
solver-visible task files, and the submitted artifacts.

Golden solutions, hidden tests, and solution-only files may be used to spot a
wrong expected answer. They must never become a requirement the solver had no
way to meet.

A criterion is unfair if it depends on data that was hidden, unavailable, not
requested, or not uniquely implied by solver-visible files. Strict grading is
fine when the requirement is visible, deterministic and unambiguous.

Never invent a requirement, never split one mistake into several penalties, and
never change weights, expected answers or instructions to reach a target score.
If a criterion cannot be made fair, say so in your final message and leave it.

## Every criterion must be

- **Atomic** — one checkable fact, one pass/fail condition. Do not bundle several
  requirements into one criterion, and do not make one criterion's outcome depend
  on another. Split a bundled criterion only when it already contains independent
  solver-visible obligations.
- **Evaluable** — a grader can decide pass/fail. "Response is helpful" cannot.
- **Accurate** — it does not contradict the prompt or the data. Where a criterion
  embeds a count, ID, date or total, reproduce that value from the source files
  before trusting it, and check it through the interface the solver actually uses:
  raw files often contain more than the solver sees.
- **Correctly signed** — desired behaviour positive, undesired negative. Watch the
  inversions: "does NOT do X", "avoids X" and "excludes X" describe *desired*
  behaviour, so a negative weight there punishes a correct solver.

## Criterion shape

These five fields appear in every bundle: `id`, `axis`, `weight`, `requirement`,
`evidence_claim_ids`.

Beyond those, **bundle generations differ legitimately**. Some carry `references`,
`error_category`, `category`, `polarity`, `materiality`, `safe_harbor`, `trigger`,
`citation_rule` or task-specific fields; others do not. **The file you are looking
at is the authority.** Match the shape its existing criteria already use, and do
not add or remove fields to match some other bundle.

A missing field is only a defect when this bundle's own criteria are inconsistent
with each other, or when its validator or `pre-push-contract.json` demands it.
Never report a schema issue merely because a field you expected is absent
throughout the file — that is this generation's shape, not a fault.

Where `error_category` is used, positive criteria leave it empty and negative
criteria carry exactly one category, matching their axis.

| Axis | Negative categories that belong to it |
|---|---|
| `factual-accuracy` | `factual-grounding-failure`, `wrong-scope-or-entity`, `quantitative-or-unit-error`, `stale-or-superseded-source`, `decoy-reliance`, `unsupported-negative-evidence` |
| `breadth-and-depth-of-analysis` | `undisclosed-conflict` |
| `citation-quality` | `orphan-citation`, `cited-but-unvisited`, `citation-entailment-failure`, `decoy-reliance` |
| `presentation-quality` | none unless the bundle's own contract defines one |

This table applies to bundles that use `error_category`; treat a bundle's own
declared vocabulary as authoritative where it differs. Do not reach for a
different axis or category to justify a larger penalty. IDs must be unique and
`evidence_claim_ids` must resolve to real visible evidence.

`tests/reference/pre-push-contract.json`, where present, is this bundle's
authoritative record: its `checks.rubric` block gives the valid `positive_axes`,
the current `positive_total`, `negative_capacity` and counts, and
`checks.source_tiers` gives the required tier minimums against what the bundle
actually has. Read it before changing weights, and prefer it over any assumption
here if the two disagree.

## Weights

Reward is `sum(weights of passed items) / sum(all positive weights)`, over the
shared pool of rubrics and mechanical tests. A requirement covered by a test does
not also need a rubric.

When the positive pool is below the required floor, raise it by adding genuine
criteria for requirements that are real but unscored, or by reweighting toward
what actually matters. Do not pad with filler criteria, and do not simply
multiply every weight by a constant — that clears the floor while changing
nothing, since the ratio above is unchanged.

Keep the most important requirements weighted highest, and never let one failure
be scored twice.

## Context you need

Only the task's input corpus is visible at runtime. The rubric file, `tests/`,
and answer keys are applied by the grader afterwards — a rubric sitting in the
task folder is not something the solver could read.

## Do not "fix" these — they are by design

- A value the rubric asserts that the prompt never restates, when the data
  determines it.
- A requirement implied by the data rather than named in the prompt.
- Absent data, where criteria cluster around the absence (penalising fabrication,
  rewarding graceful handling). The absence *is* the challenge.
- Unspecified file paths, or a prompt that does not enumerate every sub-task.
- One arbitrary-looking choice among equally valid options — a defect only when
  the rubric picks one with no justification.

Call a criterion inaccurate only when it **contradicts** the prompt or the data,
never merely because the prompt does not repeat it.

**These are for judging the bundle yourself — they do not overrule a reviewer.**
When the fairness review names a specific requirement as hidden from the solver
(an exact heading, exact citation text, a required visit), that is a finding to
answer, not a by-design case to dismiss. Relax the criterion in place, keeping
its id; see "Two reviewers, and the loop between them" above.

## If you get stuck

If two requirements appear to contradict each other, do not loop. Make the
smallest change that satisfies the numeric rules without breaking fairness, and
state the conflict plainly in your final message.
