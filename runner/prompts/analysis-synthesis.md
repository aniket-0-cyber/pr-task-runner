You have reviews of several offline-search benchmark tasks. Write a short
briefing for a team lead.

## How scoring works

The score is what a strong solver agent achieves.

- **Below 0.5 is GOOD** — the task is hard enough to be worth shipping.
- **0.5 or above is BAD** — too easy, does not discriminate, gets rejected.

A low score is the goal, not a problem. Never frame low scores as failures and
never suggest making a task easier.

## The question

**What keeps these tasks appropriately difficult, and what makes them drift too
easy?**

And separately: where the difficulty is real research depth versus artificial
clerical work. A task can score well for the wrong reason — that is still a
defect, because the difficulty is busywork rather than research.

## The reviews

```json
{analyses}
```

## Scores

```json
{scores}
```

## Output

- **headline** — the single most important thing, one or two sentences.
- **keeps_difficulty_real** — construction properties producing legitimate
  difficulty. Name the tasks; note where the pattern breaks.
- **makes_tasks_too_easy** — what pushes scores up toward or past 0.5.
- **false_difficulty** — where tasks are hard for the wrong reasons: clerical
  transcription, ambiguity, brittle exact-matching. This is the failure mode
  that hides behind a healthy-looking score.
- **recommendations** — a few concrete actions, most valuable first.

## Rules

- **Short.** One line per item. This is read in two minutes.
- Plain language. No criterion ids. A number only when it carries the point.
- Only these five fields matter — do not pad with caveats, methodology, or
  restatements of the scores.
- Be honest where the evidence is thin, but say it once, not in every line.

Return ONLY the JSON object described by the output schema.
