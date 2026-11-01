# Calibration: how far to trust a reviewer

A reviewer that is right four times in five and one that is right one time in five both print
"MAJOR". Daedalus asks for more, borrowing the habits that make forecasters good:

- **Probabilities, not labels.** Every finding carries `confidence`: the reviewer's probability that it
  is real and the code must change (0.05 to 0.95). Every review carries `residual_risk`: its probability
  that a MAJOR-or-worse defect it did not report remains.
- **Consider the opposite.** Every finding carries `against`, the strongest reason it could be wrong.
  The prompt names the commonest one: the failure was already there before this change. Checks that also
  fail on the base revision are marked as such ([configuration.md](configuration.md#was-it-already-failing)).
- **Make it cheap to settle.** A finding may carry `failing_test`, a test that fails until it is fixed. The
  builder sees it, with the confidence, in its feedback.
- **Keep score.** Outcomes come from what happens next, and the Brier score (mean squared gap between
  forecast and outcome; 0 is perfect) is computed from the audit log.
- **Outside view first.** Once a perspective has five findings with a known outcome, its next review
  prompt opens with its track record in this repository: how many held up, whether its stated confidence ran
  high or low, and its recent false alarms (quoted), so it starts from its base rate.

None of this changes acceptance: a finding blocks by its severity, as before.

## Outcomes

| A finding is scored as | when |
|---|---|
| real (1) | a later review of a changed candidate confirmed the fix, the arbiter upheld it, or a person judged it `valid` |
| a false alarm (0) | the arbiter overruled it, or a person judged it `invalid` |
| not yet scored | anything else |

A person settles the rest, after the run if need be:

```bash
daedalus findings judge F3 invalid "the test was already failing on main"
daedalus findings judge F4 valid
```

A judgment is a human decision (agents cannot record one, so a builder cannot grade its reviewers), resolves
nothing, and is kept in the audit log (`finding.judged`). A review's `residual_risk` is scored once its run is
accepted: 1 if a later commit says it fixes that run (`Fixes-Run: <run id>`, see [reviews.md](reviews.md)),
else 0, so recent runs read as clean until time tells.

## Reading the score

```bash
daedalus calibration             # by review perspective
daedalus calibration --by model  # or --by severity; --json for everything
```

It prints, for findings and for residual risk:

- the **Brier score**, beside the score of always saying the base rate, and the **skill** (above 0 beats it);
- **reliability** (how far stated probabilities sit from what came true: lower is better) and **resolution**
  (how well the forecasts separate real from false: higher is better), Murphy's decomposition;
- a calibration table: for each band of stated confidence, how often findings in it came true;
- the most confident false alarms, which are the ones to learn from.

Under 15 scored forecasts the numbers say little, and the report says so.

## Benchmark: what a reviewer misses

Scores from real runs say how often findings hold up, not what the reviewer missed. `daedalus bench`
measures that on planted bugs:

```bash
daedalus bench                           # the configured adapter, full review prompt
daedalus bench --lite                    # the lite prompt and model
daedalus bench --reviewer-cmd "my-tool"  # any reviewer: the prompt on stdin, review JSON on stdout
daedalus bench --cases my-cases.yaml --repeat 3
```

The built-in corpus (`daedalus/data/bench/cases.yaml`) has ten small changes with one planted bug each
(an off-by-one, an integrity check that fails open, a counts-only rule broken, a test flag that defaults to
live, a check moved behind an early exit, a crash on a malformed value, a mutable default, path traversal, a
time-zone slip, an unanchored validation) and four clean controls. It reports recall, MAJOR-or-worse findings
that name no planted bug, clean changes that drew one, the Brier score of findings and of residual risk, and cost
and time per review. `--reviewer-cmd` puts another tool on the same cases, so the comparison is like for like.
Results are kept in `.daedalus/bench/`. Billed adapters need `--yes`.

A finding counts as naming a bug when it names the bug's file and either a line within two of it or a word a
finding about it would use. That is generous to a reviewer that names the right file for the wrong reason, so read
the unfounded-findings count beside recall.

### First results (October 2026, built-in corpus, one pass each)

| Reviewer | Planted bugs caught | Clean changes with a false alarm | Findings Brier | Residual-risk Brier | Cost per review |
|---|---|---|---|---|---|
| Sonnet, full prompt | 10 / 10 | 0 / 4 | 0.062 | 0.013 | $0.018 |
| Haiku, lite prompt (`mode: lite`) | 10 / 10 | 0 / 4 | 0.077 | 0.044 | $0.0016 |
| Haiku, full prompt | 10 / 10 | 1 / 4 | 0.164 | 0.045 | $0.0028 |

What it shows, and what it doesn't:

- Every setup caught every planted bug, so this corpus is too easy to rank reviewers by recall. What separates
  them is noise and calibration: Sonnet's confidence tracked the truth best, and the lite prompt made Haiku both
  cheaper and better calibrated than the full prompt did, at about a tenth of Sonnet's cost.
- One pass, 14 cases: a single run moves these numbers. Use `--repeat` and your own cases before deciding.
- The one "false alarm" (Haiku, full prompt, on the retry change) said the new `attempts` argument lets callers
  exceed "at most three attempts". That is arguably a fair reading of the criterion, which is the point of
  reading the findings beside the score.
- Harder cases (bugs spread over two files, bugs that need the code the diff does not show) are the next
  addition; until then, read recall here as a floor test, not a ranking.
