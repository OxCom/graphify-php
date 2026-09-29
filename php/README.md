<!-- Superseded by SwarmVault vault page: raw/agentscopex/graphify-php/reference/phpstan-call-graph-extension.md -->
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

`graphify-php`'s `phpstan` source passes `--autoload-file` itself, so an adopter using it only
needs the `services:` block above.

Verified against PHPStan 2.2.8 on PHP 8.4.

## Output

`PHP_CALL_GRAPH_OUT` names the destination; without it the file lands at `php-call-graph.jsonl`
in the working directory. One JSON object per line:

```json
{"callerClass":"App\\Controller","callerMethod":"run","calleeClass":"App\\Mailer","calleeMethod":"send","calleeKind":"interface","line":21,"file":"/srv/app/src/Controller.php"}
```

`calleeKind` is `interface` when the method is declared on an interface. That edge names a
contract, not a body: there is no code behind `App\Mailer::send`, and a consumer that treats it
as a call into an implementation acquires a path that was never executed.

`calleeClass` is the *declaring* class, not the receiver. An inherited method lives in the
parent, and an edge to the child would point at a body that does not exist there.

## What it refuses to report

Nothing is emitted unless PHPStan produced a class reflection for the receiver. A name-only
fallback — matching `->get()` to any class that happens to own a `get` — manufactures edges
indistinguishable from real ones, and a wrong edge is worse than a missing one. So these
produce no record:

- a receiver PHPStan types as `mixed`, which includes an **untyped property**, even one assigned
  from a typed constructor parameter: PHPStan reports `missingType.property` rather than
  inferring it. Give the property a native type or a `@var` docblock and the call resolves.
- a dynamic method name, `$obj->$method()`;
- a receiver on a class the analysis cannot load.

Also out of scope in this version, by omission rather than by refusal: `?->` nullsafe calls,
`Foo::bar()` static calls, and `new Foo` construction edges. Each is a different node type and
would be a second collector.

## Cost

PHPStan loads project classes through the composer autoloader, so an analysis executes some
project code at class-load time — attribute constructors, static initialisers, and anything the
project's own bootstrap does. Run it the way any analysis is run: with a timeout, a memory
limit, and no terminal to prompt at.
