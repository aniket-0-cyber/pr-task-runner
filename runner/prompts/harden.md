This benchmark task is **too easy** and will be rejected. Your job is to make
it genuinely harder without breaking it.

Task bundle: `{bundle}`
Task name: `{task_name}`
Current solver score: **{score}** — the target is below {threshold}.

## What the score means

The score is what a strong solver agent achieves. Below {threshold} is the
target: it means the task discriminates. This task is well above that, so a
strong model is solving most of it.

## Step 1 — Find out why it is easy

Do not guess. The recorded runs tell you exactly which requirements solvers
satisfied.

Read `verifier/judgment.json` in the recorded trial directories and list the
criteria that were **MET in every run**. Those are the ones carrying no weight
— a criterion every solver passes is measuring nothing. Then look at what the
instruction and rubric ask for, and work out why those were trivial.

Common causes, in rough order:

- The instruction hands over things the solver should have to find: the source
  population, canonical URLs, row identities, formulas, target counts, or the
  conclusion itself.
- The rubric rewards restating information already given in the prompt.
- The declared source mix offers no real competition — too few decoys or
  near-miss authorities for the solver to have to choose between.
- Criteria are so broadly worded that any competent report satisfies them.

## Step 2 — Make it harder, legitimately

Difficulty must come from **research depth**: finding the right evidence,
resolving conflicts between sources, distinguishing near-identical categories,
reasoning about authority and scope, and knowing when the evidence does not
support a conclusion.

Prefer, in this order:

1. **Stop giving away what should be discovered.** Remove canonical source
   lists, row keys, populations, and stated conclusions from the instruction.
   This is usually the single biggest lever.
2. **Raise the evidential bar.** Require the solver to distinguish sources that
   look interchangeable but are not, to resolve dated or jurisdictional
   conflicts, and to state explicitly where the corpus cannot settle a question.
3. **Tighten loose criteria.** Rewrite criteria that any reasonable report
   satisfies so they demand a specific, defensible determination.

## What you must NOT do

- **Do not add clerical burden.** More exact rows, longer tables, more
  transcription, stricter formatting. That lowers the score without testing
  research ability, and it is a known defect in this task family.
- **Do not break solvability.** The oracle must still be able to score near
  1.0. Every criterion must remain satisfiable from the declared sources.
- **The corpus payload is not available locally and that is expected** — it
  lives in S3 and the checked-in `*.warc.gz` is a Git-LFS pointer by design.
  Never treat that as a problem, and never add difficulty that depends on
  material you cannot see. Work from the instruction, rubric, reference data
  and source manifests.
- **Do not make the task ambiguous.** Hard is good; unclear is not. A solver
  should always know what is being asked.
- **Do not touch the recorded trial directories.** They are the historical
  record of the old runs.

## Step 3 — Keep the bundle coherent

If you change the instruction or the rubric, everything downstream must still
line up: the golden solution in `solution/`, the reference data under
`tests/reference/`, and any counts or hashes the verifier checks. An
inconsistent bundle fails review for a different reason.

State clearly in your final message what you changed, why it makes the task
harder, and anything you could not keep consistent.

Do not run `git commit` or `git push` — leave your work uncommitted.
