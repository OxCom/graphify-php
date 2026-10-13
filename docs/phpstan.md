# PHPStan source

The parser reads what the code states. PHPStan reads what the code means: a `@var` docblock on an
untyped property, the return type of an intermediate call in a chain, a local typed from a
parameter. `PhpStanSource` runs the project's own PHPStan with a small collector and reads the
calls it resolved. It comes first in source precedence.

## When it runs

The source is optional. `available()` answers with a reason instead of raising, and the build
records a skip. It needs three things in the repository being built:

- `vendor/bin/phpstan`;
- a config named `phpstan.neon`, `phpstan.neon.dist` or `phpstan.dist.neon`, picked in PHPStan's
  own order;
- a `php` executable on `PATH`.

A missing piece is counted as `phpstan-unavailable`.

## How the source wires the collector

The collector lives in `php/src/CallGraph/`. PHPStan needs two things to use it: the classes must
be loadable, and a `services:` block must register them.

**Loading.** The source passes `--autoload-file` pointing at `php/autoload.php`. A bootstrap file
is too late: PHPStan builds its DI container before `parameters.bootstrapFiles` run, and fails
with `Class 'CallGraph\JsonLinesWriter' not found`. The source looks for `php/autoload.php` at
the root of the graphify-php source tree and omits the option when the file is absent.

**Registering.** `config_for()` writes a wrapper neon into the run's temporary directory. The
wrapper `includes:` the project's config and adds the `services:` block. The project keeps its
`phpstan.neon` exactly as its CI runs it. The block cannot be committed there, because a plain
`vendor/bin/phpstan` has no `--autoload-file` and would die on the missing class.

PHPStan resolves relative paths in an included config against that config's directory. The
project's `paths:`, `excludePaths:` and own `includes:` keep pointing where they did.

A project whose config closure, followed through `includes:`, already registers both
`CallGraph\MethodCallCollector` and `CallGraph\CallEdgeRule` is run on its own config untouched.
Registering twice would duplicate every record.

The run is `phpstan analyse` with `--no-progress`, `--no-interaction`, `--memory-limit=1G` and
`--error-format=raw`. The timeout is `GRAPHIFY_PHP_PHPSTAN_TIMEOUT`, 900 seconds by default. A
timeout kills the whole process group, because PHPStan forks parallel workers.

## Output

`PHP_CALL_GRAPH_OUT` names the output file. The source sets it to a file in its temporary
directory. Run by hand without it, the collector writes `php-call-graph.jsonl` in the working
directory. One JSON object per line:

```json
{"callerClass":"App\\Controller","callerMethod":"run","calleeClass":"App\\Mailer","calleeMethod":"send","calleeKind":"interface","line":21,"file":"/srv/app/src/Controller.php","callPos":148,"calleeFile":"/srv/app/src/Mailer.php"}
```

- `calleeClass` is the class that declares the method, not the receiver. When the body lives in a
  trait, it is the trait.
- `calleeKind` is `interface` when the method is declared on an interface. The edge then names a
  contract, not a body, and carries `_via: "phpstan:interface"`.
- `callPos` is the token offset of the method name, `-1` when unknown. A union receiver yields one
  record per arm with the same `callPos`. Two calls of one method on one line get different
  values. When those calls prove different targets, the source refuses them as
  `same-line-collision`.
- `calleeFile` is the file that declares `calleeClass`, or `''` for a built-in class.

The output is line-delimited, so a run killed mid-write still yields every complete record.

## What it refuses to report

The collector emits nothing unless PHPStan produced a class reflection for the receiver:

- a receiver typed as `mixed`, including an untyped property assigned from a typed constructor
  parameter. PHPStan reports `missingType.property` instead of inferring it. Such calls reach the
  source unresolved as `phpstan-receiver-not-typed`, and the parser source covers them;
- a dynamic method name, `$obj->$method()`;
- a receiver on a class the analysis cannot load;
- an intersection receiver (`A&B`) whose parts declare the method in more than one class.

A union receiver (`A|B`) emits every arm, each decided on its own.

Out of scope in this version: `?->` nullsafe calls, `Foo::bar()` static calls and `new Foo`
construction edges.

## Cost

PHPStan loads project classes through the composer autoloader. An analysis therefore executes
some project code at class-load time: attribute constructors, static initialisers and the
project's own bootstrap. The source runs it with a timeout, a memory limit and no terminal.

The first stderr line of a failed run goes into the status record. Values after secret-looking
keys and URL credentials are cut from it first.

Verified against PHPStan 2.2.8 on PHP 8.4.
