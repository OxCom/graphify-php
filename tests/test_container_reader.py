"""Reader behaviour, against small hand-written containers.

Fixtures are built here rather than taken from the reference application's 1.36 MB dump: a unit
test that needs a 1.36 MB file on one particular machine is an integration test wearing the
wrong name, and it stops telling you which fact broke.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from graphify_php.container import ArgumentKind, Member, SymfonyXmlContainerReader
from graphify_php.container.reader import env_name

HEAD = '<?xml version="1.0" encoding="utf-8"?>\n<container xmlns="http://symfony.com/schema/dic/services">\n'
TAIL = "</container>\n"


def container(tmp_path: Path, services: str, parameters: str = "") -> Path:
    path = tmp_path / "App_KernelTestDebugContainer.xml"
    path.write_text(
        HEAD + parameters + "<services>\n" + textwrap.dedent(services) + "\n</services>\n" + TAIL
    )
    return path


def read(tmp_path: Path, services: str, parameters: str = ""):
    return SymfonyXmlContainerReader().read(container(tmp_path, services, parameters))


def test_alias_binds_interface_to_implementation(tmp_path: Path) -> None:
    facts = read(
        tmp_path,
        """
        <service id="App\\Service\\CacheService" class="App\\Service\\CacheService"/>
        <service id="App\\Service\\CacheServiceInterface" alias="App\\Service\\CacheService"/>
        """,
    )
    assert facts.resolve_id("App\\Service\\CacheServiceInterface") == "App\\Service\\CacheService"
    assert facts.class_of("App\\Service\\CacheServiceInterface") == "App\\Service\\CacheService"
    assert [a.alias_id for a in facts.interface_bindings] == ["App\\Service\\CacheServiceInterface"]


def test_alias_declared_before_its_target_still_resolves(tmp_path: Path) -> None:
    # File order must not decide the answer; the reader resolves in a second pass for this.
    facts = read(
        tmp_path,
        """
        <service id="App\\LoggerInterface" alias="app.logger"/>
        <service id="app.consumer" class="App\\Consumer">
          <argument type="service" id="App\\LoggerInterface"/>
        </service>
        <service id="app.logger" class="App\\MonologLogger"/>
        """,
    )
    injection = facts.injections[0]
    assert injection.resolved_id == "app.logger"
    assert injection.resolved_class == "App\\MonologLogger"
    assert injection.via_alias is True


def test_argument_bound_to_a_named_service_keeps_the_injection_site(tmp_path: Path) -> None:
    facts = read(
        tmp_path,
        """
        <service id="app.fast_cache" class="App\\RedisCache"/>
        <service id="app.slow_cache" class="App\\FileCache"/>
        <service id="App\\Reports" class="App\\Reports">
          <argument type="service" id="app.slow_cache"/>
          <argument type="service" id="app.fast_cache"/>
          <call method="setAudit">
            <argument type="service" id="app.fast_cache"/>
          </call>
        </service>
        """,
    )
    sites = [i for i in facts.injections if i.consumer_id == "App\\Reports"]
    # The same interface-free type arriving twice at different positions is the case a global
    # map gets wrong, so identity is asserted per position, not per class.
    assert [(i.member, i.member_name, i.position, i.resolved_class) for i in sites] == [
        (Member.CONSTRUCTOR, None, 0, "App\\FileCache"),
        (Member.CONSTRUCTOR, None, 1, "App\\RedisCache"),
        (Member.CALL, "setAudit", 0, "App\\RedisCache"),
    ]


def test_tagged_iterator_records_the_tag_and_the_members(tmp_path: Path) -> None:
    facts = read(
        tmp_path,
        """
        <service id="App\\Cron\\Nightly" class="App\\Cron\\Nightly">
          <tag name="app.cron_job" priority="10"/>
        </service>
        <service id="App\\Cron\\Hourly" class="App\\Cron\\Hourly">
          <tag name="app.cron_job"/>
        </service>
        <service id="App\\Cron\\Runner" class="App\\Cron\\Runner">
          <argument type="tagged_iterator" tag="app.cron_job"/>
        </service>
        """,
    )
    injection = next(i for i in facts.injections if i.consumer_id == "App\\Cron\\Runner")
    assert injection.kind is ArgumentKind.TAGGED_ITERATOR
    assert injection.tag == "app.cron_job"
    members = {(t.service_id, t.class_fqn) for t in facts.tags if t.tag == "app.cron_job"}
    assert members == {
        ("App\\Cron\\Nightly", "App\\Cron\\Nightly"),
        ("App\\Cron\\Hourly", "App\\Cron\\Hourly"),
    }
    assert next(t for t in facts.tags if t.service_id == "App\\Cron\\Nightly").attributes == {
        "priority": "10"
    }


def test_decorator_is_recovered_from_the_inner_renaming(tmp_path: Path) -> None:
    # Compiled containers carry no `decorates=`: Symfony renames the decorated definition.
    facts = read(
        tmp_path,
        """
        <service id="App\\Serializer\\Serializer.inner" class="App\\Serializer\\NativeSerializer"/>
        <service id="App\\Serializer\\Serializer" class="App\\Serializer\\CachingSerializer">
          <argument type="service" id="App\\Serializer\\Serializer.inner"/>
        </service>
        """,
    )
    assert len(facts.decorations) == 1
    decoration = facts.decorations[0]
    assert decoration.decorator_class == "App\\Serializer\\CachingSerializer"
    assert decoration.inner_class == "App\\Serializer\\NativeSerializer"


def test_factory_class_and_method_are_kept(tmp_path: Path) -> None:
    facts = read(
        tmp_path,
        """
        <service id="app.redis" class="Redis">
          <argument>%env(REDIS_URL)%</argument>
          <factory class="Symfony\\Component\\Cache\\Adapter\\RedisAdapter" method="createConnection"/>
        </service>
        """,  # noqa: E501
    )
    service = facts.services["app.redis"]
    assert (service.factory_class, service.factory_method) == (
        "Symfony\\Component\\Cache\\Adapter\\RedisAdapter",
        "createConnection",
    )


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("%env(REDIS_URL)%", "REDIS_URL"),
        ("%env(resolve:DATABASE_URL)%", "DATABASE_URL"),
        ("%env(default:kernel.environment:APP_RUNTIME_ENV)%", "APP_RUNTIME_ENV"),
        ("%env(int:default::key:web:default:kernel.runtime_mode:)%", None),
        ("plain string", None),
    ],
)
def test_env_expression_yields_a_name_or_nothing(expression: str, expected: str | None) -> None:
    assert env_name(expression) == expected


def test_env_argument_records_only_the_name(tmp_path: Path) -> None:
    facts = read(
        tmp_path,
        """
        <service id="app.mailer" class="App\\Mailer">
          <argument>%env(MAILER_API_KEY)%</argument>
          <argument>smtp://user:hunter2@mail.example/</argument>
        </service>
        """,
    )
    env_arg, scalar_arg = facts.injections
    assert (env_arg.kind, env_arg.env_name) == (ArgumentKind.ENV, "MAILER_API_KEY")
    assert scalar_arg.kind is ArgumentKind.SCALAR
    # The model has no field that could hold it, which is the guarantee under test.
    assert not hasattr(scalar_arg, "value")
    assert "hunter2" not in repr(facts.injections)


def test_malformed_xml_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "App_KernelTestDebugContainer.xml"
    path.write_text(HEAD + '<services><service id="broken"')
    assert SymfonyXmlContainerReader().read(path) is None


def test_wrong_root_element_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "App_KernelTestDebugContainer.xml"
    path.write_text('<?xml version="1.0"?><routes/>')
    assert SymfonyXmlContainerReader().read(path) is None


def test_inline_anonymous_service_is_counted_not_invented(tmp_path: Path) -> None:
    facts = read(
        tmp_path,
        """
        <service id="app.consumer" class="App\\Consumer">
          <argument type="service">
            <service class="App\\Anonymous"/>
          </argument>
        </service>
        """,
    )
    assert "app.consumer" in facts.services
    assert list(facts.services) == ["app.consumer"]
    assert facts.skipped["inline-anonymous-service"] == 1


def test_environment_provenance_is_two_parameters_and_nothing_else(tmp_path: Path) -> None:
    facts = read(
        tmp_path,
        '<service id="app.noop" class="App\\Noop"/>',
        parameters=(
            "<parameters>"
            '<parameter key="kernel.environment">dev</parameter>'
            '<parameter key="kernel.debug">true</parameter>'
            '<parameter key="mailer_password">hunter2</parameter>'
            "</parameters>"
        ),
    )
    assert facts.environment["kernel_environment"] == "dev"
    assert facts.environment["kernel_debug"] == "true"
    assert "hunter2" not in repr(facts.environment)


@pytest.mark.parametrize(
    "expression",
    [
        "%env(A)%:%env(B)%",
        "%env(HOST)%:5432/db?password=X)%",
        "%env(not a name)%",
    ],
)
def test_an_env_expression_that_is_not_one_placeholder_yields_nothing(expression: str) -> None:
    # A greedy match would return `%env(B` or a DSN fragment as the "name"; neither is one.
    assert env_name(expression) is None


def test_a_run_on_env_argument_is_dropped_with_a_reason_and_leaks_nothing(tmp_path: Path) -> None:
    facts = read(
        tmp_path,
        """
        <service id="app.db" class="App\\Db">
          <argument>%env(HOST)%:5432/db?password=X)%</argument>
        </service>
        """,
    )
    (injection,) = facts.injections
    assert injection.env_name is None
    assert facts.skipped == {"env-expression-without-name": 1}
    assert "password" not in repr(facts.injections)


def test_factory_built_service_arguments_belong_to_the_factory_method(tmp_path: Path) -> None:
    # Symfony's XmlDumper writes the factory's arguments at service level and `<factory/>` is
    # always childless, so they are the factory's, not the constructor's.
    facts = read(
        tmp_path,
        """
        <service id="repo" class="App\\Repo">
          <argument type="service" id="em"/>
          <factory service="doctrine" method="getRepository"/>
        </service>
        <service id="em" class="App\\Em"/>
        <service id="doctrine" class="App\\Doctrine"/>
        """,
    )
    (injection,) = facts.injections
    assert (injection.member, injection.member_name, injection.position) == (
        Member.FACTORY,
        "getRepository",
        0,
    )


def test_named_constructor_arguments_carry_the_constructor_name(tmp_path: Path) -> None:
    facts = read(
        tmp_path,
        """
        <service id="app.clock" class="App\\Clock" constructor="fromSystem">
          <argument>UTC</argument>
        </service>
        """,
    )
    (injection,) = facts.injections
    assert (injection.member, injection.member_name) == (Member.FACTORY, "fromSystem")


def test_the_decorator_is_the_service_holding_the_inner_id_not_the_first_referrer(
    tmp_path: Path,
) -> None:
    facts = read(
        tmp_path,
        """
        <service id="app.collector" class="App\\Collector">
          <argument type="service" id="app.serializer.inner"/>
        </service>
        <service id="app.serializer.inner" class="App\\NativeSerializer"/>
        <service id="app.serializer" class="App\\LoggingSerializer">
          <argument type="service" id="app.serializer.inner"/>
        </service>
        """,
    )
    (decoration,) = facts.decorations
    assert decoration.decorator_id == "app.serializer"
    assert decoration.decorator_class == "App\\LoggingSerializer"
