"""The PHPStan-backed `CallTargetSource`.

Why a second source at all. The parser in `php_types` reads what the code states: a promoted
constructor parameter, a typed property, a `new` binding. PHPStan reads what the code *means* —
a `@var` docblock on an untyped property, the return type of an intermediate call in a chain,
a local whose type came from a parameter three statements up. Measured on the reference Symfony
application before either source existed: 2172 `calls` edges against 3426 callable nodes, 24 %
of callables with an outgoing edge. The chain case alone is a large part of that gap and the
parser is not allowed to guess at it.

Why it must be optional. Nine of the twenty repositories indexed on this machine have no
PHPStan at all. `available()` therefore answers with a reason instead of raising, and the build
records a skip and carries on; a source that cannot run is not a source that failed.

What PHPStan does not do, checked against 2.2.8 rather than assumed: it does **not** infer the
type of an untyped property from its constructor assignment. It reports `missingType.property`
and types the receiver as `mixed`, so such a call arrives here unresolved and is counted as
`phpstan-receiver-not-typed`. The parser source is what covers that case.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from graphify_php.ports import CallSite, CallTarget, Confidence, Resolution

_BINARY = "vendor/bin/phpstan"

# PHPStan's own precedence, so a project with both a committed and a local config gets the one
# PHPStan itself would pick.
_CONFIG_NAMES = ("phpstan.neon", "phpstan.neon.dist", "phpstan.dist.neon")

_TIMEOUT_ENV = "GRAPHIFY_PHP_PHPSTAN_TIMEOUT"
_DEFAULT_TIMEOUT_S = 900.0
_MEMORY_LIMIT = "1G"

# Fixed vocabulary. These strings are a work list for the next version, so they never carry a
# variable in them — a histogram of unique strings cannot be worked through.
REASON_UNAVAILABLE = "phpstan-unavailable"
REASON_TIMEOUT = "phpstan-timeout"
REASON_RUN_FAILED = "phpstan-run-failed"
REASON_NO_OUTPUT = "phpstan-no-output"
REASON_MALFORMED = "phpstan-malformed-record"
REASON_NOT_TYPED = "phpstan-receiver-not-typed"
REASON_NOT_WIRED = "phpstan-collector-not-registered"

# The two classes the project's own config must register. Without them PHPStan runs, analyses
# the whole tree and writes nothing, which is indistinguishable from a project whose receivers
# are all untyped — the one failure the reason vocabulary could not name.
_COLLECTOR_CLASS = "CallGraph\\MethodCallCollector"
_RULE_CLASS = "CallGraph\\CallEdgeRule"

# A config may `includes:` another, so the registration is looked for across the closure rather
# than in one file. Both bounds are guards, not tuning: a config that needs more than this is
# not a config this source can read.
_MAX_INCLUDE_DEPTH = 8
_MAX_CONFIG_BYTES = 1 << 20

# The one exception, and it is a diagnostic rather than a work item: without the first stderr
# line an unattended failure is indistinguishable from a project that simply has no calls.
STDERR_PREFIX = "phpstan-stderr: "

_LINE_RE = re.compile(r"\d+")

VIA_CLASS = "phpstan"
VIA_INTERFACE = "phpstan:interface"


@dataclass(frozen=True)
class PhpStanCall:
    """One record from the collector's output file.

    The only place in the Python half that knows the PHP half's wire shape. Everything below
    reads attributes, so a key rename on the PHP side is one edit here.
    """

    caller_class: str
    caller_method: str
    callee_class: str
    callee_method: str
    callee_is_interface: bool
    file: str
    line: int

    @staticmethod
    def from_json(raw: object) -> "PhpStanCall | None":
        """Return the call, or None when the record is not one.

        None rather than an exception: the output is line-delimited precisely so that a run
        killed mid-write still yields every complete record, and a truncated tail line is an
        expected input, not an error.
        """
        if not isinstance(raw, dict):
            return None
        try:
            callee_class = str(raw["calleeClass"])
            callee_method = str(raw["calleeMethod"])
            file = str(raw["file"])
            line = int(raw["line"])
        except (KeyError, TypeError, ValueError):
            return None
        if not callee_class or not callee_method:
            return None
        return PhpStanCall(
            caller_class=str(raw.get("callerClass", "")),
            caller_method=str(raw.get("callerMethod", "")),
            callee_class=callee_class,
            callee_method=callee_method,
            callee_is_interface=raw.get("calleeKind") == "interface",
            file=file,
            line=line,
        )

    @property
    def via(self) -> str:
        """An interface target names a contract, not a body.

        The consumer has to be able to tell the two apart: an edge to `MailerInterface::send`
        points at a signature with no code behind it, and treating it as a call into an
        implementation is how a call graph acquires a body that was never executed.
        """
        if self.callee_is_interface:
            return VIA_INTERFACE
        return VIA_CLASS


@dataclass(frozen=True)
class RunOutcome:
    """What the subprocess did, separated from what it produced."""

    ok: bool
    reason: str = ""
    stderr_line: str = ""


class EnvironmentProbe:
    """Answers whether PHPStan can run in a given checkout, and nothing else."""

    def check(self, repo_root: str) -> tuple[bool, str]:
        ok, _code, message = self.inspect(repo_root)
        return ok, message

    def inspect(self, repo_root: str) -> tuple[bool, str, str]:
        """`check` plus the fixed reason code, so a caller can count what stopped it.

        `check` answers a human; the histogram needs a constant. Splitting them here keeps the
        `available()` port signature untouched and keeps the two answers from drifting apart.
        """
        root = Path(repo_root)
        if not (root / _BINARY).is_file():
            return False, REASON_UNAVAILABLE, f"no {_BINARY} in {repo_root}"
        config = self.config_path(repo_root)
        if config is None:
            return False, REASON_UNAVAILABLE, f"no {' or '.join(_CONFIG_NAMES)} in {repo_root}"
        if shutil.which("php") is None:
            return False, REASON_UNAVAILABLE, "no php executable on PATH"
        missing = self.missing_wiring(config)
        if missing:
            return False, REASON_NOT_WIRED, (
                f"{config.name} does not register {' and '.join(missing)}; "
                f"add the services block from php/README.md"
            )
        return True, "", ""

    def config_path(self, repo_root: str) -> Path | None:
        for name in _CONFIG_NAMES:
            candidate = Path(repo_root) / name
            if candidate.is_file():
                return candidate
        return None

    def missing_wiring(self, config: Path) -> tuple[str, ...]:
        """Which of the two required classes the config closure never names.

        PHPStan builds its rules from the config alone: `--autoload-file` makes the classes
        loadable, it does not register them. An unregistered collector produces a clean exit,
        an empty output file and a full-length run, so this is checked before launching rather
        than inferred from the silence afterwards.
        """
        text = self._config_closure(config)
        return tuple(name for name in (_COLLECTOR_CLASS, _RULE_CLASS) if name not in text)

    def _config_closure(self, config: Path) -> str:
        """Every config file reachable from this one through `includes:`, concatenated.

        Registration is often kept in a separate neon that the project's own config pulls in,
        so looking only at the entry file would report a wired project as unwired.
        """
        seen: set[Path] = set()
        pending: list[tuple[Path, int]] = [(config, 0)]
        chunks: list[str] = []
        while pending:
            current, depth = pending.pop()
            try:
                resolved = current.resolve()
            except OSError:
                continue
            if resolved in seen or depth > _MAX_INCLUDE_DEPTH:
                continue
            seen.add(resolved)
            text = _read_config(resolved)
            if not text:
                continue
            chunks.append(text)
            for include in _includes(text):
                pending.append(((resolved.parent / include), depth + 1))
        return "\n".join(chunks)


def extension_autoload_file() -> Path | None:
    """The PSR-4 registrar for the PHP collector, shipped beside this package.

    Passed to PHPStan as `-a`. `parameters.bootstrapFiles` does not work for this and the
    failure is not obvious: PHPStan builds its DI container before bootstrap files run, so the
    services block fails with "Class 'CallGraph\\JsonLinesWriter' not found". Verified against
    PHPStan 2.2.8.
    """
    candidate = Path(__file__).resolve().parents[3] / "php" / "autoload.php"
    if candidate.is_file():
        return candidate
    return None


class PhpStanRunner:
    """Launches PHPStan and reports how it went. Owns the subprocess, nothing above it."""

    def __init__(self, probe: EnvironmentProbe | None = None) -> None:
        self._probe = probe or EnvironmentProbe()

    def timeout_s(self) -> float:
        raw = os.environ.get(_TIMEOUT_ENV)
        if not raw:
            return _DEFAULT_TIMEOUT_S
        try:
            return float(raw)
        except ValueError:
            return _DEFAULT_TIMEOUT_S

    def command(self, repo_root: str, config: Path) -> list[str]:
        argv = [
            str(Path(repo_root) / _BINARY),
            "analyse",
            "--configuration",
            str(config),
            "--no-progress",
            "--no-interaction",
            f"--memory-limit={_MEMORY_LIMIT}",
            # Findings go to the collector's file; PHPStan's own report is noise here, and the
            # table formatter would render megabytes of it into the captured pipe.
            "--error-format=raw",
        ]
        autoload = extension_autoload_file()
        if autoload is not None:
            argv += ["--autoload-file", str(autoload)]
        return argv

    def run(self, repo_root: str, output_path: Path) -> RunOutcome:
        config = self._probe.config_path(repo_root)
        if config is None:
            return RunOutcome(False, REASON_UNAVAILABLE)

        env = dict(os.environ)
        env["PHP_CALL_GRAPH_OUT"] = str(output_path)

        # PHPStan resolves project classes through the composer autoloader, which means this
        # subprocess executes project code at class-load time: attribute constructors, static
        # initialisers, and anything a bootstrap or bootstrapFiles entry does. It is not a pure
        # read of the source tree, so it runs with a timeout and captured output and is never
        # given a terminal to prompt at.
        try:
            completed = subprocess.run(
                self.command(repo_root, config),
                cwd=repo_root,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout_s(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RunOutcome(False, REASON_TIMEOUT)
        except OSError as exc:
            return RunOutcome(False, REASON_UNAVAILABLE, str(exc).splitlines()[0])

        stderr_line = _first_line(completed.stderr)
        if completed.returncode == 0:
            return RunOutcome(True, "", stderr_line)

        # A non-zero exit is the normal outcome on any project that has analysis errors of its
        # own, and the collector's file is complete by then regardless. So the exit code alone
        # decides nothing; whether records arrived does. The caller treats an empty file as the
        # source being unavailable for this build rather than as a crash.
        return RunOutcome(False, REASON_RUN_FAILED, stderr_line)


class PhpStanSource:
    """Resolves call sites by asking PHPStan what the receiver's type is.

    Confidence is `EXTRACTED` for everything it returns: the type engine proved the receiver,
    it did not derive it from a declaration the way the parser source does.
    """

    name = "phpstan"

    def __init__(self, probe: EnvironmentProbe | None = None, runner: PhpStanRunner | None = None) -> None:
        self._probe = probe or EnvironmentProbe()
        self._runner = runner or PhpStanRunner(self._probe)

    def available(self, repo_root: str) -> tuple[bool, str]:
        return self._probe.check(repo_root)

    def resolve(self, repo_root: str, sites: Iterable[CallSite]) -> Resolution:
        wanted = list(sites)
        ok, code, _message = self._probe.inspect(repo_root)
        if not ok:
            return Resolution([], {code or REASON_UNAVAILABLE: len(wanted)}, 0)
        if not wanted:
            return Resolution([], {}, 0)

        with tempfile.TemporaryDirectory(prefix="graphify-php-") as workdir:
            output_path = Path(workdir) / "calls.jsonl"
            outcome = self._runner.run(repo_root, output_path)
            text = _read(output_path)

        unresolved: dict[str, int] = {}
        if outcome.stderr_line:
            unresolved[STDERR_PREFIX + outcome.stderr_line] = 1

        calls, malformed = _parse(text)
        if malformed:
            unresolved[REASON_MALFORMED] = malformed

        if not calls:
            unresolved[_failure_reason(outcome)] = len(wanted)
            return Resolution([], unresolved, 0)

        targets = list(_match(wanted, calls, repo_root))
        # CallSite is frozen and hashable, so the set counts distinct sites that got at
        # least one target; a site with two targets is one site, not two.
        missed = len(wanted) - len({target.site for target in targets})
        if missed > 0:
            unresolved[REASON_NOT_TYPED] = missed

        return Resolution(targets, unresolved, len({call.file for call in calls}))


def _failure_reason(outcome: RunOutcome) -> str:
    if outcome.ok:
        return REASON_NO_OUTPUT
    return outcome.reason or REASON_UNAVAILABLE


def _read_config(path: Path) -> str:
    """A config file's text, or nothing. An unreadable include is a file that registers nothing."""
    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _includes(text: str) -> list[str]:
    """The paths under a top-level `includes:` key.

    Deliberately not a NEON parser: this looks for one key whose values are file paths, and a
    line it misreads costs at most one unvisited include. An entry carrying a NEON placeholder
    (`%rootDir%`) is skipped rather than guessed at — expanding it is PHPStan's job.
    """
    found: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if not inside:
            if stripped.startswith("includes:"):
                inside = True
                inline = stripped[len("includes:"):].strip()
                found.extend(_include_items(inline))
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():
            inside = False
            if stripped.startswith("includes:"):
                inside = True
            continue
        if stripped.startswith("-"):
            found.extend(_include_items(stripped[1:]))
    return [item for item in found if item and "%" not in item]


def _include_items(raw: str) -> list[str]:
    raw = raw.strip().strip("[]")
    items = []
    for part in raw.split(","):
        part = part.strip().strip("\'\"")
        if part:
            items.append(part)
    return items


def _first_line(stderr: str) -> str:
    for line in (stderr or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse(text: str) -> tuple[list[PhpStanCall], int]:
    calls: list[PhpStanCall] = []
    malformed = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except ValueError:
            malformed += 1
            continue
        call = PhpStanCall.from_json(raw)
        if call is None:
            malformed += 1
            continue
        calls.append(call)
    return calls, malformed


def _site_key(path: str, line: int, method: str, repo_root: str) -> tuple[str, int, str]:
    """Identity of a call site, agreed between the two halves.

    File and line, because that is all PHPStan reports and all graphify's site carries. The
    method name is in the key as well so that two different calls on one line — a chain — stay
    apart instead of collapsing into whichever the dictionary saw last.
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path(repo_root) / resolved
    return (os.path.normpath(str(resolved)), line, method)


def _line_of(source_location: str) -> int:
    """`source_location` is a string in the port, and its format is graphify's to change.

    The leading integer is the line in every form it has taken so far (`"120"`, `"120:8"`,
    `"120-134"`), so that is what is read, and an unparseable value yields 0 — which matches
    nothing and leaves the site counted as unresolved rather than matched to the wrong call.
    """
    found = _LINE_RE.search(source_location or "")
    if found is None:
        return 0
    return int(found.group())


def _match(sites: list[CallSite], calls: list[PhpStanCall], repo_root: str) -> Iterator[CallTarget]:
    by_key: dict[tuple[str, int, str], list[PhpStanCall]] = {}
    for call in calls:
        by_key.setdefault(_site_key(call.file, call.line, call.callee_method, repo_root), []).append(call)

    for site in sites:
        key = _site_key(site.source_file, _line_of(site.source_location), site.method, repo_root)
        for call in by_key.get(key, ()):
            yield CallTarget(
                site=site,
                class_fqn=call.callee_class,
                method=call.callee_method,
                confidence=Confidence.EXTRACTED,
                via=call.via,
            )
