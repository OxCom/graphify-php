<!-- Superseded by SwarmVault vault page: raw/agentscopex/graphify-php/explanation/measurement-harness.md -->
# Measurement harness

This directory answers one question about a PHP call-target resolver: **how many of the edges
it adds are right, and how many of the right ones is it still missing?**

It exists because the obvious number cannot answer that. The reference application carries
2172 `calls` edges against 3426 callable nodes — 24 % of callables with an outgoing call edge,
20 % with an incoming one — and the temptation is to run a resolver, watch that count rise,
and call it fixed. A rise is compatible with two opposite outcomes. The upstream project whose
fix is closest to this one recorded **+1,566 correct edges and −454 false ones in the same
change**: nearly a quarter of its value was in edges it stopped emitting, and an edge count
scores that as a loss. Six upstream issues across four code-graph projects trace their false
edges to the same cause — a name-only fallback that wired `->get()` to an unrelated class and
`empty()` to a method named `empty`.

So the harness scores against labels written by reading PHP source, never against a resolver's
own output.

## Layout

| File | What it is |
|---|---|
| `reference_set.py` | 33 call sites labelled by hand, with the targets a correct resolver must produce |
| `fixtures/` | The PHP (and one YAML) files those labels cite |
| `score.py` | Turns predictions + labels into numbers. Imports nothing from `graphify_php` |
| `canaries.py` | Planted fake secrets, and the check that none reaches an artifact |

## Running it

Tests, including every scorer defect case and the leak check:

```sh
PYTHONPATH=/usr/lib/python3/dist-packages \
  /home/oxcom/.local/share/pipx/venvs/graphify-mesh/bin/python -m pytest tests/test_eval_*.py
```

Scoring a resolver. Write its output as JSON and hand it over; exit status is non-zero if any
predicted target was wrong, so a build can gate on precision without parsing the text:

```sh
python -m eval.score predictions.json
```

```json
{
  "predictions": [
    {"site_id": "promoted.php:15:$this->emails->reminderSubmitWeek",
     "class_fqn": "Eval\\Mail\\Emails", "method": "reminderSubmitWeek",
     "confidence": "EXTRACTED", "via": "php_types"}
  ],
  "unresolved": {"dynamic.php:34:$this->emails->$method": "dynamic_method_name"},
  "observed": ["promoted.php:15:$this->emails->reminderSubmitWeek", "..."]
}
```

`site_id` is the only coupling between a resolver and this harness. Nothing here imports the
package, so a resolver written in PHP, or one that does not exist yet, is scored by the same
code.

From Python, `score(SITES, output)` takes any sequence of objects with the `LabelledSite`
attributes — a subset, a single stratum, or a different reference set entirely.

## What each number means

Reading the sample output of a resolver that answers every in-scope site correctly:

```
  target precision           1.000 (20/20)
  recall (in scope, exact)   1.000 (20/20)
  multi-target exact match   0.500 (1/2)
  negatives held             1.000 (10/10)
  operational coverage       0.606 (20/33)
```

**target precision** — correct predicted targets over all predicted targets. This is the one
that falls when a resolver guesses. A resolver raises it by predicting *less*, and that is the
correct trade whenever the alternative is a wrong edge.

**recall (in scope, exact)** — sites whose predicted target set exactly matches an accepted
answer, over sites the labels call resolvable *and* in scope for the version under test.
"In scope" comes from the refusals `php_types.py` documents — chained calls, unions, a local
rebound after its `new`. Out-of-scope sites are excluded here and still counted in coverage, so
declining to support a pattern cannot quietly inflate recall.

**recall (in scope, any hit)** — the same denominator, counting a site as a hit if *any*
predicted target is correct. The gap between the two rows is over-prediction on sites the
resolver otherwise got right.

**multi-target exact match** — over sites with more than one legitimate target: a union
receiver, an interface with several implementations. Picking one arm of `Emails|Cache` is a
wrong answer, not a partial one, and per-target precision would score it as half right. Its
denominator includes out-of-scope sites, so a resolver that refuses every union scores 0 on
those rather than disappearing from the row.

**negatives held** — sites where the only correct output is no edge at all: dynamic method
names, `__call`, reflection, a service locator, an untyped receiver whose method name exists on
two unrelated classes. 10 of the 33 labels are these, and they are the closest thing here to a
direct measurement of the upstream failure mode.

**operational coverage** — sites with at least one prediction over every eligible site offered.
This is the number that tracks the 24 % figure. It is also the number that rises when a
resolver starts guessing, which is why it is reported last and never alone.

**pair metrics** — the same precision and recall over unique `(caller, class, method)` triples.
Two sites calling two methods on one property are two sites; a graph may hold them as fewer
edges. Pooling the two counts lets one hot caller carry the average, so they stay apart.

**sample sizes** — every denominator, printed. `20/20` and `1.000` are the same claim with very
different weight.

**zero-error upper bound** — with no errors observed, there is still an error rate; the sample
just did not reach it. The rule of three puts the one-sided 95 % upper bound at about `3/n`
(Hanley & Lippman-Scott, *If nothing goes wrong, is everything all right?*, JAMA
1983;249:1743-5). At 20 predicted targets that is **0.15** — a clean run here is consistent
with one wrong edge in seven. The rule assumes independent trials and these are not: several
sites come from one fixture, written in one pass, so a single systematic mistake shows up in a
group of them at once and the real bound is looser than 3/n. Read it as the most optimistic
number available, never as the expected field error rate.

## What the numbers do not establish

- **A rise in edge count is corroboration, not proof.** It is consistent with a resolver that
  added correct edges and with one that added wrong ones. Only precision separates them, and
  precision needs labels the resolver did not write.
- **Leaf methods and framework entry points legitimately have no call edges.** An entity
  getter calls nothing; a Symfony `__invoke` is called by the messenger, not by indexed PHP.
  100 % of callables having an edge would mean the resolver is inventing them. There is no
  target figure for the 24 %, and there should not be one.
- **33 sites is a small sample.** Sufficient to catch a systematic defect — a stratum wired
  wrong, a trap taken — and not sufficient to estimate a field error rate to two decimals.
- **The strata are not weighted by their real frequency.** Promoted constructor properties are
  652 of 715 injected properties on the reference application; here they are 7 of 33. Per-stratum
  rows are the readable part; the pooled figures are not a population estimate.
- **In-scope is a claim about this version, not about PHP.** Moving a pattern into scope moves
  the recall denominator, and two recall figures from different versions are not comparable
  without the denominators beside them.
- **28 of 33 labels are synthetic.** They are minimal on purpose, so a failure names one cause.
  Real code combines patterns, and 5 labels from the reference application are not enough to
  show what that combination costs.

## Canaries

`canaries.py` plants five fake secrets in the fixtures — an env-variable default, a DSN with an
inline password, a token literal, an attribute argument, a YAML parameter value — and checks
that none of them appears in any artifact: graph output, the status record, logs, diagnostics.

The rule being tested is that **identifiers may be indexed and values never**. `API_TOKEN`,
`FALLBACK_DSN` and `mailer_password` are expected to appear in a graph; the strings they are
bound to must not. Both halves are tested, because a check that fired on the constant's name
would force the package to stop indexing identifiers.

Every planted value carries the marker `CANARY` and opens nothing. `verify_planted()` confirms
they are still in the fixtures — a leak check whose canaries were removed passes for the wrong
reason. Feed artifacts in as text:

```python
from eval import canaries
canaries.assert_no_leaks(canaries.collect_artifacts([Path("graphify-out")]))
```

The failure message names the canary and the file and redacts the value, so the report cannot
become the second copy of the leak.
