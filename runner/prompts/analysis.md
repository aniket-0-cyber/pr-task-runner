You are reviewing ONE offline-search benchmark task the way an experienced
reviewer would: reading it, judging whether it is well built, and saying what
about its construction pushes its score up or down.

Task bundle: `{bundle}`
Task name: `{task_name}`
Pull request: #{pr_number}
Measured solver score: **{score}**

## How scoring works — read this first

The score is what a strong solver agent achieves on the task.

- **Below 0.5 is GOOD.** It means the task is genuinely difficult and worth
  shipping. This is the target.
- **0.5 or above is BAD.** The task is too easy — a strong model solves it, so
  it does not discriminate and will be rejected.

So a low score is **not a problem to explain away**. Do not treat it as a
failure or recommend making the task easier. The questions are:

1. Is this task in the healthy range, or has it drifted too easy?
2. Is its difficulty **legitimate** — real research depth, judgement,
   synthesis — or **artificial**, from clerical transcription, ambiguity, or
   brittle exact-matching that a domain expert would also fail?

Both matter. A task can score healthily *for the wrong reasons*: that is still
a defect, because the difficulty is busywork rather than research. And a task
that is too easy usually got that way through a construction mistake, most
often the instruction handing over answers.

## What this is not

Not a numbers exercise. Do not recompute rates or weights — they are below if
you want them. Do not produce a per-criterion audit.

Useful output sounds like: *"difficulty is real — the sources carry genuine
authority conflicts a solver must resolve"*, or *"it scores low but for the
wrong reason: the rubric demands exact transcription of 149 rows, which tests
patience not research"*, or *"too easy because the instruction lists the source
population outright"*.

## Task-quality rules — automated results for this bundle

```
{rules}
```

Rule 4 (criteria must be **binary, atomic and independent** — one checkable
fact each, no bundling, no criterion depending on another) is not machine
checked. Read `tests/rubrics.json` and judge it yourself as part of your rubric
assessment.

Any FAIL above is a construction defect. Reflect it in the relevant component's
quality rating rather than treating it as a separate topic.

## Reference numbers (context only)

```json
{metrics}
```

## Review these parts of the task

Read the actual files. For each part, judge its quality and say whether its
condition pushes the score **up**, **down**, or neither, and why.

1. **Instruction** (`instruction.md`) — Is it clear and unambiguous? Does it
   define its deliverables and populations well? Is it bloated, repetitive, or
   under-specified? Does it hand the solver things it should have to find?

2. **Rubric** (`tests/rubrics.json`) — Are criteria well formed and genuinely
   distinct, or padded and overlapping? Do they reward understanding, or
   clerical exactness? Are weights proportionate? Are the penalties fair and
   correctly aimed? Would a competent expert pass this rubric?

3. **Tests / verifier** (`tests/`) — Is the grading machinery sound and
   consistent with the rubric? Anything that would mis-grade a good answer?

4. **Solution / reference** (`solution/`, `tests/reference/`) — Is the golden
   answer complete, correct, and internally consistent with the instruction and
   rubric? You cannot verify it against the corpus and should not try.

5. **Source coverage** (`tests/source_tier*.txt`, the manifests) — Judge the
   declared source mix only: are there enough decoys and secondary sources to
   test judgement, and is the tiering sensible?

   **The corpus payload is not available locally and that is expected.** It
   lives in S3 and is many gigabytes; the checked-in `*.warc.gz` is a Git-LFS
   pointer by design. Never treat that as a defect, never call the task
   unreproducible because of it, and do not comment on corpus size, packaging,
   hydration or byte counts at all. Judge only what the manifests and tier
   files declare.

6. **Overall task design** — Is the difficulty coming from good reasons
   (genuine research depth, judgement, synthesis) or bad ones (clerical
   transcription, guessing at ambiguity, brittle exact-match)? Is this a task
   you would ship?

## Guideline compliance

The fairness review checks Task Realism and Data Quality, Golden Solution,
Rubric Similarity, and Rubric Fairness. Say plainly whether this task looks
compliant with each, and where it does not.

## Rules

- Be blunt. If the rubric is bad, say the rubric is bad, and say why.
- Say when something is *fine* — do not manufacture criticism. A task below 0.5
  built on real research difficulty is a good task; say so plainly.
- One or two sentences per field. This gets read by a team lead, not archived.
- Never recommend making a task easier to raise its score. Raising the score is
  a failure. Recommend fixes that make the difficulty *legitimate*, or that
  bring a too-easy task back down.

Return ONLY the JSON object described by the output schema. Read only — do not
modify any file.
