---
title: Install
---

# Install

graphify-php is a plugin that runs inside graphify's own Python process. It has to be installed
into the **same Python environment as graphify**. A copy in any other environment is never
loaded, and graphify then builds without it.

The package is not published on PyPI. Install it from a clone of the repository.

## Requirements

| Component | Version | Needed for |
|---|---|---|
| Python | 3.10 or newer | everything |
| graphify (`graphifyy` on PyPI) | 0.9.66 or newer | the build itself; it also brings `tree-sitter` and `tree-sitter-php` |
| pyyaml | 6.0 or newer | the Symfony configuration pass; installed with the package |
| git | any | cloning the repository |
| PHP and PHPStan in the indexed project | verified with PHPStan 2.2.8 on PHP 8.4 | optional: the precise call source |

Without PHP or PHPStan the build still works. The member-call resolver then uses the
tree-sitter source alone and counts the calls PHPStan would have taken under
`phpstan:phpstan-unavailable`.

## The PHPStan extension

The PHPStan source passes `php/autoload.php` to PHPStan. The wheel ships `php/` inside the
package as `graphify_php/php/`, and an editable install of a clone uses the top-level `php/`.
Both layouts find it.

## Option A: pipx (recommended)

pipx gives graphify its own isolated environment and puts its commands on your `PATH`.

1. Install graphify, if it is not installed yet:

   ```bash
   pipx install graphifyy
   ```

2. Clone graphify-php to a directory that stays put:

   ```bash
   git clone https://github.com/OxCom/graphify-php.git ~/src/graphify-php
   ```

3. Inject it into graphify's environment:

   ```bash
   pipx inject --editable --include-apps graphifyy ~/src/graphify-php
   ```

`--include-apps` is required. Without it pipx installs the package but does not put
`graphify-php-build` on your `PATH`.

## Option B: a virtual environment

Use this when you run graphify from a project-local or CI virtual environment.

```bash
python3 -m venv ~/.venvs/graphify
. ~/.venvs/graphify/bin/activate
pip install "graphifyy>=0.9.66"
git clone https://github.com/OxCom/graphify-php.git ~/src/graphify-php
pip install -e ~/src/graphify-php
```

`graphify-php-build` then lives in `~/.venvs/graphify/bin/`. Activate the environment, or call
the command by its full path.

## Option C: an environment that already has graphify

When graphify is already installed, add graphify-php with that environment's own `pip`. Find the
interpreter graphify runs on from the first line of its launcher:

```bash
head -1 "$(command -v graphify)"
```

Then install with that interpreter, here shown as `/path/to/python`:

```bash
/path/to/python -m pip install -e ~/src/graphify-php
```

If graphify was installed with pipx, use Option A, step 3 instead. On Debian and Ubuntu the
system `python3` rejects `pip install` with `externally-managed-environment`, so prefer
Option A or B there.

## Prepare the PHP project (optional)

The PHPStan source runs the indexed project's own PHPStan. It runs only when all three hold:

- `vendor/bin/phpstan` exists in the project root;
- the project root has `phpstan.neon`, `phpstan.neon.dist` or `phpstan.dist.neon`;
- `php` is on `PATH`.

Nothing has to be added to the project. graphify-php writes a temporary wrapper config that
registers its collector, so the project's PHPStan config stays as it is. See
[PHPStan source](phpstan.html) for how the wiring works.

A PHP project without PHPStan can add it as a dev dependency:

```bash
composer require --dev phpstan/phpstan
```

## Verify the install

1. Check that the command is on `PATH`:

   ```bash
   graphify-php-build --help
   ```

2. Check that graphify's interpreter can import the package. With pipx:

   ```bash
   pipx runpip graphifyy show graphify-php
   ```

   With a virtual environment, run `pip show graphify-php` with that environment active.

3. Run a build in a PHP repository and read the status record:

   ```bash
   cd /path/to/php-project
   graphify-php-build -- update .
   echo "exit: $?"
   python3 -m json.tool graphify-out/.graphify-php-status.json
   ```

Exit code `0` means every expected resolver reported. The record lists `php_member_calls`,
`php_routes` and `php_symfony_config`. Each has a `state` of `completed`, or `skipped` with a
`reason`. Under `php_member_calls`, `resolved_sites` out of `eligible_sites` is the coverage
figure.

Always build through `graphify-php-build`. A plain `graphify update .` never calls `register()`.
It publishes a graph without the PHP call and route edges and reports no error.

## Upgrade

Pull the clone. An editable install picks up the new code on the next build:

```bash
git -C ~/src/graphify-php pull
```

Reinstall only when `pyproject.toml` changed its dependencies or entry points. Repeat the
install command from your option. With pipx, add `--force`:

```bash
pipx inject --force --editable --include-apps graphifyy ~/src/graphify-php
```

## Uninstall

With pipx:

```bash
pipx uninject graphifyy graphify-php
```

With a virtual environment:

```bash
pip uninstall graphify-php
```

Then delete the clone.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `graphify-php-build: command not found` | pipx injected without `--include-apps` | Re-run the inject command with `--force --include-apps` |
| `ModuleNotFoundError: No module named 'graphify'` | graphify-php is installed in an environment that has no graphify | Install it into graphify's environment, Option A or C |
| Exit `3`, `resolver 'php_member_calls' never reported` | graphify ran without the plugin registered | Build through `graphify-php-build`; check the import with step 2 above |
| Exit `2`, `state=failed` | a resolver raised during the build | Read `reason` in the status record; the previous graph was restored |
| `phpstan:phpstan-unavailable` under `unresolved` | no `vendor/bin/phpstan`, no PHPStan config, or no `php` on `PATH` | Expected without PHPStan; otherwise see "Prepare the PHP project" |
| `Class 'CallGraph\JsonLinesWriter' not found` in the status record | the package runs from a wheel, so `php/autoload.php` is missing | Reinstall from the clone with `-e` |

## Next steps

- [Setup](setup.html): register the resolvers and run a gated build.
- [Configuration](configuration.html): environment variables, wrapper flags and exit codes.
