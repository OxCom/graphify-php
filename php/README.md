# PHPStan call-graph extension

A PHPStan collector and rule that write one JSON record per resolved method call. It knows
nothing about whatever consumes the file — no node ids, no graph schema — so it can be adopted
by anything that wants a call graph out of PHPStan.

Nothing is installed. There is no composer package and no dependency to resolve; the extension
is analysed inside somebody else's project, whose lockfile is not ours to move.

## Wiring

Add this to the project's `phpstan.neon`:

```neon
services:
	-
		class: CallGraph\JsonLinesWriter
	-
		class: CallGraph\MethodCallCollector
		tags:
			- phpstan.collector
	-
		class: CallGraph\CallEdgeRule
		tags:
			- phpstan.rules.rule
```

Then make the `CallGraph\` classes loadable. PHPStan builds its DI container before
`parameters.bootstrapFiles` run, so a bootstrap file is too late and fails with
`Class 'CallGraph\JsonLinesWriter' not found`. Use the `--autoload-file` option instead:

```sh
PHP_CALL_GRAPH_OUT=/tmp/calls.jsonl \
  vendor/bin/phpstan analyse --autoload-file=/path/to/graphify-php/php/autoload.php
```

A project that would rather own the autoloading can add `"CallGraph\\": "php/src/CallGraph/"`
to its composer `autoload-dev` and drop the option.

`graphify-php`'s `phpstan` source needs neither step from the adopter: it passes
`--autoload-file` itself and writes the `services:` block into a wrapper config in its own
temporary directory that `includes:` the project's `phpstan.neon`. The block is deliberately not
committed to the project, because the `CallGraph\` classes arrive only through that option and
a plain `vendor/bin/phpstan` — the one CI runs — would fail on
`Class 'CallGraph\JsonLinesWriter' not found` before analysing anything. A project that adds
the block to its own config anyway is run on that config untouched; registering the collector
twice duplicates every record.

Verified against PHPStan 2.2.8 on PHP 8.4.

## Output

`PHP_CALL_GRAPH_OUT` names the destination; without it the file lands at `php-call-graph.jsonl`
in the working directory. One JSON object per line:

```json
{"callerClass":"App\\Controller","callerMethod":"run","calleeClass":"App\\Mailer","calleeMethod":"send","calleeKind":"interface","line":21,"file":"/srv/app/src/Controller.php","callPos":148,"calleeFile":"/srv/app/src/Mailer.php","callerFile":"/srv/app/src/Controller.php"}
```

`file` and `line` locate the call: the file that holds the body it sits in. `callerClass` and
`callerMethod` name the method the body was analysed as, and `callerFile` is the file of
`callerClass`. The two files differ only for a trait body. PHPStan analyses a trait method once
per class using it, so one call in `src/T.php` yields one record per using class, each with
`file` `src/T.php`, `callerClass` the using class, and `callerFile` that class's file.
`callerMethod` is then the name the trait declares. An alias (`use T { run as traitRun; }`) is
mapped back to `run`, and a body whose name cannot be mapped (an alias set by an intermediate
trait) produces no record. A class that overrides the trait method never runs the trait body,
and PHPStan does not analyse it there.

`callPos` is the token offset of the called method's name, `-1` when the parser did not record
one. A union receiver (`Emails|Cache`) yields one record per arm with the same `callPos`; two
calls of one method on one line yield different values, so a consumer can tell the two apart.

`calleeKind` is `interface` when the method is declared on an interface. That edge names a
contract, not a body: there is no code behind `App\Mailer::send`, and a consumer that treats it
as a call into an implementation acquires a path that was never executed.

`calleeClass` is the *declaring* class, not the receiver. An inherited method lives in the
parent, and an edge to the child would point at a body that does not exist there. When the
body lives in a trait, `calleeClass` is the trait: PHPStan's declaring class for a trait
method is the class using it, whose file holds no such body.

`calleeFile` is the file that declares `calleeClass`, `''` when PHPStan has no file for it (a
built-in class). A consumer uses it to tell a `vendor/` class from project code the graph left
out, without guessing from the namespace.

## What it refuses to report

Nothing is emitted unless PHPStan produced a class reflection for the receiver. A name-only
fallback — matching `->get()` to any class that happens to own a `get` — manufactures edges
indistinguishable from real ones, and a wrong edge is worse than a missing one. So these
produce no record:

- a receiver PHPStan types as `mixed`, which includes an **untyped property**, even one assigned
  from a typed constructor parameter: PHPStan reports `missingType.property` rather than
  inferring it. Give the property a native type or a `@var` docblock and the call resolves.
- a dynamic method name, `$obj->$method()`;
- a call outside any class method (a function body), since there is no class to name;
- a call in a trait method reached through an alias the using class's own alias table does not
  map back to the trait;
- a receiver on a class the analysis cannot load;
- an intersection receiver (`A&B`) whose constituents resolve to more than one declaring class
  for the method. One object, two declarations: the type does not say which body runs, so
  neither is emitted. This matches the parser layer's `ambiguous-intersection` refusal. A union
  receiver (`A|B`) is different: one of two objects, so every arm is emitted, and each arm is
  decided on its own — `(A&B)|C` emits `C` and drops the ambiguous intersection, `(A&B)|null`
  emits nothing. The parser layer is stricter there and refuses `(A&B)|C` wholesale as
  `union-type`.

A nullsafe call `$x?->m()` is recorded by the same collector: PHPStan hands it a `MethodCall` for
it too. The `null` arm of `?Foo` has no class reflection, so it is dropped and `Foo` is kept.

Also out of scope in this version, by omission rather than by refusal: `Foo::bar()` static
calls and `new Foo` construction edges. Each is a different node type and would be a second
collector.

## Cost

PHPStan loads project classes through the composer autoloader, so an analysis executes some
project code at class-load time — attribute constructors, static initialisers, and anything the
project's own bootstrap does. Run it the way any analysis is run: with a timeout, a memory
limit, and no terminal to prompt at.
