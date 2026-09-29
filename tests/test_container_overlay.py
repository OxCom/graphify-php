"""The artifact: its shape, its provenance, and the values it must never contain."""

from __future__ import annotations

import json
from pathlib import Path

from graphify_php.container import OverlayWriter
from graphify_php.container.facts import (
    Alias,
    ArgumentKind,
    ContainerFacts,
    Decoration,
    Injection,
    Member,
    Service,
    TagMembership,
)


def sample_facts() -> ContainerFacts:
    facts = ContainerFacts(environment={"kernel_environment": "dev", "kernel_debug": "true"})
    facts.services["app.logger"] = Service(service_id="app.logger", class_fqn="App\\MonologLogger")
    facts.aliases["Psr\\Log\\LoggerInterface"] = Alias(
        alias_id="Psr\\Log\\LoggerInterface", target_id="app.logger"
    )
    facts.injections.append(
        Injection(
            consumer_id="App\\Reports",
            consumer_class="App\\Reports",
            member=Member.CONSTRUCTOR,
            member_name=None,
            position=1,
            parameter=None,
            kind=ArgumentKind.SERVICE,
            requested_id="Psr\\Log\\LoggerInterface",
            resolved_id="app.logger",
            resolved_class="App\\MonologLogger",
            via_alias=True,
        )
    )
    facts.injections.append(
        Injection(
            consumer_id="App\\Reports",
            consumer_class="App\\Reports",
            member=Member.CONSTRUCTOR,
            member_name=None,
            position=2,
            parameter=None,
            kind=ArgumentKind.ENV,
            env_name="MAILER_API_KEY",
        )
    )
    facts.tags.append(TagMembership(tag="app.cron_job", service_id="App\\Cron\\Nightly", class_fqn="App\\Cron\\Nightly"))
    facts.decorations.append(
        Decoration(
            decorator_id="App\\Serializer\\Serializer",
            decorator_class="App\\Serializer\\CachingSerializer",
            inner_id="App\\Serializer\\Serializer.inner",
            inner_class="App\\Serializer\\NativeSerializer",
        )
    )
    return facts


def test_artifact_declares_itself_non_structural_with_its_own_relation() -> None:
    document = OverlayWriter().build(sample_facts())
    assert document["structural"] is False
    assert document["relation"] == "binds_injection_to"
    assert document["kind"] == "container-overlay"
    # Never graphify's own edge vocabulary: a merged overlay would assert code facts that hold
    # only in one environment.
    assert document["relation"] not in {"calls", "extends", "implements", "imports"}
    assert all(b["relation"] == "binds_injection_to" for b in document["bindings"])


def test_binding_identifies_the_site_not_just_the_interface() -> None:
    binding = OverlayWriter().build(sample_facts())["bindings"][0]
    assert binding["consumer_class"] == "App\\Reports"
    assert binding["member"] == "constructor"
    assert binding["position"] == 1
    assert binding["resolved_class"] == "App\\MonologLogger"
    assert binding["via_alias"] is True


def test_env_binding_carries_the_name_and_no_value_field() -> None:
    binding = OverlayWriter().build(sample_facts())["bindings"][1]
    assert binding["env_name"] == "MAILER_API_KEY"
    assert set(binding) & {"value", "default", "resolved_value", "expansion"} == set()


def test_environment_is_recorded_in_the_artifact() -> None:
    document = OverlayWriter().build(sample_facts())
    assert document["environment"]["kernel_environment"] == "dev"


def test_counts_carry_their_denominator() -> None:
    counts = OverlayWriter().counts(sample_facts())
    assert counts["injection_sites"] == 2
    assert counts["injection_sites_resolved_to_class"] == 1
    assert counts["interface_bindings"] == 1
    assert counts["tag_memberships"] == 1
    assert counts["decorations"] == 1


def test_write_puts_the_artifact_beside_the_graph(tmp_path: Path) -> None:
    path = OverlayWriter().write(sample_facts(), tmp_path / "graphify-out")
    assert path.name == "container-overlay.json"
    assert json.loads(path.read_text())["kind"] == "container-overlay"


def test_no_planted_canary_value_reaches_the_artifact(tmp_path: Path) -> None:
    from eval import canaries

    facts = sample_facts()
    # Every canary identifier wired in as it would arrive from a real container: as an env
    # name, a service id, a class and a tag. The values stay in the fixtures, where the
    # reader has no path to them.
    for canary in canaries.CANARIES:
        facts.injections.append(
            Injection(
                consumer_id="App\\Secrets",
                consumer_class="App\\Secrets",
                member=Member.CONSTRUCTOR,
                member_name=None,
                position=len(facts.injections),
                parameter=None,
                kind=ArgumentKind.ENV,
                env_name=canary.identifier,
            )
        )
    path = OverlayWriter().write(facts, tmp_path)
    canaries.assert_no_leaks(canaries.collect_artifacts([path]))
    assert not canaries.verify_planted()
