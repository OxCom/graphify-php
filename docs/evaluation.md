# Evaluation

The `eval/` harness answers one question about a PHP call-target resolver: how many of the edges
it adds are right, and how many right ones it still misses.

An edge count cannot answer that. A rise fits a resolver that added correct edges and one that
added wrong ones. So the harness scores against labels written by reading PHP source, never
against a resolver's own output.

## Layout

| File | Content |
|---|---|
| `reference_set.py` | 33 call sites labelled by hand, with the targets a correct resolver must produce |
| `fixtures/` | The PHP files, and one YAML file, that the labels cite |
| `score.py` | Turns predictions and labels into numbers. Imports nothing from `graphify_php` |
| `canaries.py` | Planted fake secrets, and the check that none reaches an artifact |

`site_id` is the only coupling between a resolver and the harness. A resolver written in PHP, or
one that does not exist yet, is scored by the same code.

## Run it

The tests cover every scorer defect case and the leak check:

```sh
pip install -e ".[test]"
python -m pytest tests/test_eval_*.py
```

To score a resolver, write its output as JSON and pass the file, or `-` for stdin:

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

The exit status is `1` when any predicted target is wrong: a wrong class, a wrong method, or
both. A build can gate on precision without parsing the report. A missing target is not an
error here. Under-resolving is the safe failure, and it shows up in recall.

From Python, `score(SITES, output)` takes any sequence of objects with the `LabelledSite`
attributes: a subset, one stratum, or a different reference set.

## What the numbers mean

Sample output of a resolver that answers every in-scope site correctly:

```
  target precision           1.000 (20/20)
  recall (in scope, exact)   1.000 (20/20)
  multi-target exact match   0.500 (1/2)
  negatives held             1.000 (10/10)
  operational coverage       0.606 (20/33)
```

- **target precision**: correct predicted targets over all predicted targets. It falls when a
  resolver guesses. Predicting less raises it, which is the right trade against a wrong edge.
- **recall (in scope, exact)**: sites whose predicted set exactly matches an accepted answer,
  over sites labelled resolvable and in scope for this version. Out-of-scope sites still count in
  coverage, so refusing a pattern cannot inflate recall.
- **multi-target exact match**: sites with more than one legitimate target, such as a union
  receiver. Picking one arm of `Emails|Cache` is a wrong answer, not a partial one.
- **negatives held**: sites where the only correct output is no edge. Examples are dynamic method
  names, `__call`, reflection, a service locator, and an untyped receiver whose method exists on
  two unrelated classes. 10 of the 33 labels are these.
- **operational coverage**: sites with at least one prediction over every eligible site. It also
  rises when a resolver starts guessing, so it is reported last and never alone.

Report precision and negatives held alongside coverage. Every row prints its denominator, because
`20/20` and `1.000` carry different weight.

With no errors observed there is still an error rate. The rule of three puts the one-sided 95 %
upper bound at about `3/n`, so 0.15 at 20 predicted targets. Sites from one fixture are not
independent, so the real bound is looser.

## What the numbers do not establish

- A rise in edge count is corroboration, not proof. Only precision separates correct edges from
  wrong ones.
- Leaf methods and framework entry points legitimately have no call edges. There is no target
  figure for coverage.
- 33 sites catch a systematic defect. They do not estimate a field error rate to two decimals.
- The strata are not weighted by their real frequency. Pooled figures are not a population
  estimate.
- In-scope is a claim about this version. Two recall figures from different versions need their
  denominators beside them.

## Canaries

`canaries.py` plants five fake secrets in the fixtures: an env-variable default, a DSN with an
inline password, a token literal, an attribute argument and a YAML parameter value. The check
asserts that none appears in any artifact: graph output, the status record, logs, diagnostics.

The rule is that identifiers may be indexed and values never. `API_TOKEN`, `FALLBACK_DSN` and
`mailer_password` may appear in a graph. The strings bound to them must not. Both halves are
tested.

Every planted value carries the marker `CANARY` and opens nothing. `verify_planted()` confirms the
canaries are still in the fixtures, since a leak check without canaries passes for the wrong
reason. Do not remove them. Feed artifacts in as text:

```python
from pathlib import Path

from eval import canaries

canaries.assert_no_leaks(canaries.collect_artifacts([Path("graphify-out")]))
```

The failure message names the canary and the file and redacts the value.
