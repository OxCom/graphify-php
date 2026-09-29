"""Locating the container, and every way that legitimately produces nothing."""

from __future__ import annotations

import json
from pathlib import Path

from graphify_php.container import ContainerOverlayLayer
from graphify_php.container.layer import LAYER_NAME

SYMFONY_COMPOSER = json.dumps({"require": {"symfony/framework-bundle": "7.1.*"}})
CONTAINER = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<container xmlns="http://symfony.com/schema/dic/services">'
    '<parameters><parameter key="kernel.environment">dev</parameter></parameters>'
    "<services>"
    '<service id="app.logger" class="App\\MonologLogger"/>'
    '<service id="Psr\\Log\\LoggerInterface" alias="app.logger"/>'
    '<service id="App\\Reports" class="App\\Reports">'
    '<argument type="service" id="Psr\\Log\\LoggerInterface"/>'
    "</service>"
    "</services></container>"
)


def symfony_repo(tmp_path: Path, xml: str | None = CONTAINER, environment: str = "dev") -> Path:
    (tmp_path / "composer.json").write_text(SYMFONY_COMPOSER)
    if xml is not None:
        cache = tmp_path / "var" / "cache" / environment
        cache.mkdir(parents=True)
        (cache / "App_KernelDevDebugContainer.xml").write_text(xml)
    return tmp_path


def test_complete_run_writes_the_artifact_and_counts_its_sites(tmp_path: Path) -> None:
    record = ContainerOverlayLayer().run(symfony_repo(tmp_path), tmp_path / "out")
    assert record.name == LAYER_NAME
    assert record.state == "completed"
    assert (record.eligible_sites, record.resolved_sites) == (1, 1)
    document = json.loads((tmp_path / "out" / "container-overlay.json").read_text())
    assert document["bindings"][0]["resolved_class"] == "App\\MonologLogger"


def test_no_compiled_xml_is_a_skip_with_a_reason(tmp_path: Path) -> None:
    # The dump exists only when kernel.debug is on, so a production-only cache has none.
    (tmp_path / "composer.json").write_text(SYMFONY_COMPOSER)
    (tmp_path / "var" / "cache" / "prod").mkdir(parents=True)
    record = ContainerOverlayLayer().run(tmp_path, tmp_path / "out")
    assert (record.state, record.reason) == ("skipped", "container-xml-absent")
    assert not (tmp_path / "out").exists()


def test_no_cache_directory_is_its_own_reason(tmp_path: Path) -> None:
    record = ContainerOverlayLayer().run(symfony_repo(tmp_path, xml=None), tmp_path / "out")
    assert record.reason == "container-no-cache-directory"


def test_non_symfony_repository_is_a_skip_not_a_failure(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"laravel/framework": "11.*"}}))
    record = ContainerOverlayLayer().run(tmp_path, tmp_path / "out")
    assert (record.state, record.reason) == ("skipped", "container-not-a-symfony-project")


def test_repository_without_composer_json_is_a_skip(tmp_path: Path) -> None:
    record = ContainerOverlayLayer().run(tmp_path, tmp_path / "out")
    assert (record.state, record.reason) == ("skipped", "container-no-composer-json")


def test_malformed_xml_is_a_counted_skip(tmp_path: Path) -> None:
    record = ContainerOverlayLayer().run(symfony_repo(tmp_path, xml="<container><servi"), tmp_path / "out")
    assert (record.state, record.reason) == ("skipped", "container-xml-malformed")
    assert record.files_seen == 1


def test_dev_cache_is_preferred_over_an_unconventional_environment(tmp_path: Path) -> None:
    symfony_repo(tmp_path)
    other = tmp_path / "var" / "cache" / "acceptance"
    other.mkdir(parents=True)
    (other / "App_KernelAcceptanceDebugContainer.xml").write_text(CONTAINER)
    from graphify_php.container import SymfonyCacheLocator

    path, reason = SymfonyCacheLocator().locate(tmp_path)
    assert reason == ""
    assert path.parent.name == "dev"
