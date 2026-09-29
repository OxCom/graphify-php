"""The readers and the parser, each against the shapes a real Symfony `config/` holds."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from graphify_php.config import (ConfigDocument, ConfigFileFinder, EnvReferenceReader,
                                 MessageRoutingReader, SafeDocumentParser)
from graphify_php.config import safe_yaml
from graphify_php.config.document import file_key
from graphify_php.config.environment import RELATION_READS_ENV, env_key
from graphify_php.config.messaging import RELATION_DECLARES, RELATION_ROUTED_TO, transport_key

# The file this package was written against, reduced to the part it reads.
MESSENGER_YAML = """
    framework:
        messenger:
            transports:
                cron: '%env(MESSENGER_TRANSPORT_DSN)%'
            routing:
                'App\\Cron\\Message\\AbstractCronMessage': cron
                'App\\Messages\\TimezoneInitializedMessage': cron
"""


def parse(tmp_path: Path, name: str, body: str) -> ConfigDocument:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip())
    result = SafeDocumentParser().parse(path)
    assert result.data is not None, result.reason
    return ConfigDocument(path=name, data=result.data)


def test_routing_binds_each_message_class_to_its_transport(tmp_path):
    facts = MessageRoutingReader().read(parse(tmp_path, "messenger.yaml", MESSENGER_YAML))

    assert [n.label for n in facts.nodes] == ["cron", "cron"]
    routed = [e for e in facts.edges if e.relation == RELATION_ROUTED_TO]
    assert {e.source.value for e in routed} == {
        "App\\Cron\\Message\\AbstractCronMessage",
        "App\\Messages\\TimezoneInitializedMessage",
    }
    assert {e.target.value for e in routed} == {transport_key("cron")}
    # The file declares the transport whatever happens to the message classes.
    assert [e.source.value for e in facts.edges if e.relation == RELATION_DECLARES] == [
        file_key("messenger.yaml")] * 2


def test_a_bare_top_level_routing_table_is_read_too(tmp_path):
    document = parse(tmp_path, "messenger.yaml", """
        routing:
            'App\\Messages\\TimezoneInitializedMessage': cron
    """)
    facts = MessageRoutingReader().read(document)
    assert [e.target.value for e in facts.edges if e.relation == RELATION_ROUTED_TO] == [
        transport_key("cron")]


@pytest.mark.parametrize("body,expected", [
    ("'App\\M': [async, audit]", {"async", "audit"}),
    ("'App\\M': {senders: [async]}", {"async"}),
])
def test_the_list_and_senders_forms_name_the_same_transports(tmp_path, body, expected):
    document = parse(tmp_path, "m.yaml", f"routing:\n    {body}\n")
    facts = MessageRoutingReader().read(document)
    assert {n.label for n in facts.nodes} == expected


def test_a_routing_key_that_is_not_a_class_is_counted_not_guessed(tmp_path):
    document = parse(tmp_path, "m.yaml", """
        routing:
            '*': async
    """)
    facts = MessageRoutingReader().read(document)

    assert facts.unresolved == {"routing_key_is_not_a_class": 1}
    assert not [e for e in facts.edges if e.relation == RELATION_ROUTED_TO]
    # The transport is still recorded: the application has it whether or not this reader can
    # say what it carries.
    assert [n.label for n in facts.nodes] == ["async"]


def test_a_file_with_no_routing_produces_nothing(tmp_path):
    document = parse(tmp_path, "cache.yaml", """
        framework:
            cache:
                app: cache.adapter.filesystem
    """)
    facts = MessageRoutingReader().read(document)
    assert (facts.nodes, facts.edges, facts.unresolved) == ([], [], {})


@pytest.mark.parametrize("placeholder,name", [
    ("%env(MICROSOFT_CLIENT_SECRET)%", "MICROSOFT_CLIENT_SECRET"),
    ("%env(bool:APP_DEBUG)%", "APP_DEBUG"),
    ("%env(int:WORKER_COUNT)%", "WORKER_COUNT"),
    ("%env(default:foo:DATABASE_URL)%", "DATABASE_URL"),
    ("%env(resolve:default:app.dsn:MAILER_DSN)%", "MAILER_DSN"),
])
def test_every_processor_form_yields_the_innermost_name(tmp_path, placeholder, name):
    document = parse(tmp_path, "services.yaml", f"parameters:\n    k: '{placeholder}'\n")
    facts = EnvReferenceReader().read(document)

    assert [n.label for n in facts.nodes] == [name]
    assert [(e.source.value, e.relation, e.target.value) for e in facts.edges] == [
        (file_key("services.yaml"), RELATION_READS_ENV, env_key(name))]


def test_the_surrounding_value_is_never_kept(tmp_path):
    """The placeholder is usually embedded. Only the name may survive this reader."""
    document = parse(tmp_path, "services.yaml", """
        parameters:
            dsn: 'smtp://user:hunter2@mail.invalid:587/?token=%env(MAILER_TOKEN)%'
    """)
    facts = EnvReferenceReader().read(document)

    assert [n.label for n in facts.nodes] == ["MAILER_TOKEN"]
    text = repr(facts)
    assert "hunter2" not in text and "mail.invalid" not in text


def test_one_variable_read_twice_is_one_node(tmp_path):
    document = parse(tmp_path, "services.yaml", """
        parameters:
            a: '%env(APP_SECRET)%'
            b: '%env(string:APP_SECRET)%'
    """)
    assert [n.label for n in EnvReferenceReader().read(document).nodes] == ["APP_SECRET"]


def test_a_processor_argument_that_is_not_a_name_is_counted(tmp_path):
    document = parse(tmp_path, "services.yaml", "parameters:\n    a: '%env(json:)%'\n")
    facts = EnvReferenceReader().read(document)
    assert facts.unresolved == {"env_name_not_an_identifier": 1}
    assert facts.nodes == []


def test_symfony_php_tags_stay_opaque_strings(tmp_path):
    """`!php/const` needs PHP to mean anything. Parsing must neither resolve nor refuse it."""
    path = tmp_path / "services.yaml"
    path.write_text(textwrap.dedent("""
        parameters:
            version: !php/const App\\Kernel::VERSION
            status: !php/enum App\\Status::Draft
    """).lstrip())
    result = SafeDocumentParser().parse(path)

    assert result.data is not None, result.reason
    assert result.data["parameters"]["version"] == "!php/const App\\Kernel::VERSION"


def test_a_malformed_document_is_named_not_raised(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("framework:\n  messenger:\n   - bad\n  indent: [unclosed\n")
    result = SafeDocumentParser().parse(path)

    assert result.data is None
    assert result.reason == safe_yaml.REASON_MALFORMED


def test_an_empty_document_is_not_a_mapping(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("# nothing here\n")
    assert SafeDocumentParser().parse(path).reason == safe_yaml.REASON_NOT_A_MAPPING


def test_alias_resolution_is_bounded(tmp_path, monkeypatch):
    """The cap is lowered here on purpose.

    At its real value this file parses in milliseconds: PyYAML resolves an alias to the node it
    already composed, so a 9-way four-level bomb costs 27 alias events, not 9**4. The behaviour
    under test is the refusal, not the bomb.
    """
    monkeypatch.setattr(safe_yaml, "MAX_ALIASES", 5)
    path = tmp_path / "bomb.yaml"
    path.write_text(
        "a: &a [x, x, x, x, x, x, x, x, x]\n"
        "b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
        "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
        "d: [*c, *c, *c, *c, *c, *c, *c, *c, *c]\n"
    )
    assert SafeDocumentParser().parse(path).reason == safe_yaml.REASON_ALIAS_BUDGET


def test_ordinary_merge_keys_still_parse(tmp_path):
    path = tmp_path / "services.yaml"
    path.write_text(
        "defaults: &d {autowire: true}\n"
        "services:\n"
        "  _defaults: {<<: *d}\n"
    )
    result = SafeDocumentParser().parse(path)
    assert result.data["services"]["_defaults"]["autowire"] is True


def test_an_oversized_file_is_refused_unread(tmp_path, monkeypatch):
    path = tmp_path / "huge.yaml"
    path.write_text("parameters: {}\n")
    monkeypatch.setattr(safe_yaml, "MAX_BYTES", 1)
    assert SafeDocumentParser().parse(path).reason == safe_yaml.REASON_TOO_LARGE


def test_the_finder_stays_inside_config(tmp_path):
    for name in ("config/services.yaml", "config/packages/messenger.yaml",
                 "config/packages/.hidden.yaml"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("parameters: {}\n")
    for name in ("vendor/pkg/config.yaml", "docker-compose.yaml", "config/notes.md"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")

    found = [relative for relative, _ in ConfigFileFinder(tmp_path).find()]
    assert found == ["config/packages/.hidden.yaml", "config/packages/messenger.yaml",
                     "config/services.yaml"]


def test_a_symlink_inside_config_is_not_followed(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "outside.yaml").write_text("parameters: {}\n")
    (tmp_path / "config" / "linked.yaml").symlink_to(tmp_path / "outside.yaml")
    assert ConfigFileFinder(tmp_path).find() == []
