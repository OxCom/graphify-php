"""Reads a Symfony compiled container dump (`*Container.xml`) into `facts`.

This is the only module that knows the Symfony XML schema. It runs no PHP, boots no kernel and
opens nothing but the one file it is given: the compiled dump already contains the result of
every compiler pass, so the answer to "which implementation is injected here" is a parse, not
an execution. `phpstan-symfony` reads the same file for the same reason.

What the compiled form does to the declarations, and what this reader therefore has to undo:

- Interface bindings survive as `<service id="…Interface" alias="…"/>`. 361 of them in the
  reference container.
- Decoration does NOT survive as an attribute. Symfony applies it at compile time: the
  decorated definition is renamed to `<id>.inner` and the decorator takes the original id.
  0 occurrences of `decorates=` in the reference container against 28 `.inner` ids.
- Parameter names do not survive at all. Arguments are positional. This is the single largest
  gap in what a consumer can join on, and `Injection.parameter` stays None rather than being
  filled with an index dressed up as a name.
- `%env(FOO)%` survives unexpanded, which is exactly why only its name is taken.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from . import reasons
from .facts import (
    Alias,
    ArgumentKind,
    ContainerFacts,
    Decoration,
    Injection,
    Member,
    Service,
    TagMembership,
)

NS = "{http://symfony.com/schema/dic/services}"

# `%env(bool:default:some_param:APP_DEBUG)%` — processors and defaults are colon-separated and
# the variable name is always last. Only that last segment is kept; every earlier segment may
# name a parameter whose value we are not entitled to touch.
_ENV_EXPRESSION = re.compile(r"^%env\((?P<body>.*)\)%$")

_KIND_BY_TYPE = {
    "service": ArgumentKind.SERVICE,
    "service_closure": ArgumentKind.SERVICE_CLOSURE,
    "tagged_iterator": ArgumentKind.TAGGED_ITERATOR,
    "iterator": ArgumentKind.ITERATOR,
    "collection": ArgumentKind.COLLECTION,
    "abstract": ArgumentKind.ABSTRACT,
    "constant": ArgumentKind.CONSTANT,
    "string": ArgumentKind.SCALAR,
}

# The two container parameters recorded as provenance. An allowlist, not a filter: the
# parameters block holds project paths, mailer DSNs and API keys, so nothing is copied out of
# it except these two, which say which build profile the overlay describes.
_ENVIRONMENT_PARAMETERS = ("kernel.environment", "kernel.debug")


def env_name(text: str) -> str | None:
    """The variable name inside a `%env(...)%` expression, or None.

    Returns a name and never a value: the caller has no way to obtain the expansion from here,
    which is the point.
    """
    match = _ENV_EXPRESSION.match(text.strip())
    if not match:
        return None
    name = match.group("body").rsplit(":", 1)[-1].strip()
    return name or None


class SymfonyXmlContainerReader:
    """Parses one `App_*Container.xml`."""

    name = "symfony-container-xml"
    format = "symfony/compiled-container-xml"

    def read(self, path: Path) -> ContainerFacts | None:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            return None
        except OSError:
            return None
        if root.tag != NS + "container":
            return None

        facts = ContainerFacts()
        self._read_environment(root, path, facts)
        services_root = root.find(NS + "services")
        if services_root is None:
            return facts

        # Direct children only. A nested `<service>` is an inline anonymous definition owned by
        # the argument it sits in; treating it as a top-level service would invent an id.
        for element in services_root.findall(NS + "service"):
            self._read_service(element, facts)

        self._resolve_injections(facts)
        self._read_decorations(facts)
        return facts

    # --- provenance -------------------------------------------------------------------

    def _read_environment(self, root: ET.Element, path: Path, facts: ContainerFacts) -> None:
        parameters = root.find(NS + "parameters")
        values: dict[str, str] = {}
        if parameters is not None:
            for parameter in parameters.findall(NS + "parameter"):
                key = parameter.get("key")
                if key in _ENVIRONMENT_PARAMETERS:
                    values[key] = (parameter.text or "").strip()
        facts.environment = {
            "container_file": str(path),
            "container_class": path.stem,
            "format": self.format,
            "reader": self.name,
            "kernel_environment": values.get("kernel.environment"),
            "kernel_debug": values.get("kernel.debug"),
        }

    # --- services ---------------------------------------------------------------------

    def _read_service(self, element: ET.Element, facts: ContainerFacts) -> None:
        service_id = element.get("id")
        if not service_id:
            facts.skip(reasons.DROP_SERVICE_WITHOUT_ID)
            return

        alias_target = element.get("alias")
        if alias_target:
            facts.aliases[service_id] = Alias(
                alias_id=service_id,
                target_id=alias_target,
                public=element.get("public") == "true",
            )
            return

        factory = element.find(NS + "factory")
        service = Service(
            service_id=service_id,
            class_fqn=element.get("class"),
            public=element.get("public") == "true",
            abstract=element.get("abstract") == "true",
            lazy=element.get("lazy") == "true",
            synthetic=element.get("synthetic") == "true",
            factory_class=factory.get("class") if factory is not None else None,
            factory_service=factory.get("service") if factory is not None else None,
            factory_method=factory.get("method") if factory is not None else None,
            constructor=element.get("constructor"),
        )
        facts.services[service_id] = service
        if service.class_fqn is None and not service.synthetic:
            facts.skip(reasons.DROP_SERVICE_WITHOUT_CLASS)

        self._read_tags(element, service, facts)
        self._read_arguments(element, service, Member.CONSTRUCTOR, None, facts)
        for call in element.findall(NS + "call"):
            self._read_arguments(call, service, Member.CALL, call.get("method"), facts)
        if factory is not None:
            self._read_arguments(factory, service, Member.FACTORY, service.factory_method, facts)

    def _read_tags(self, element: ET.Element, service: Service, facts: ContainerFacts) -> None:
        for tag in element.findall(NS + "tag"):
            tag_name = tag.get("name")
            if not tag_name:
                facts.skip(reasons.DROP_TAG_WITHOUT_NAME)
                continue
            # Tag attributes are wiring metadata — priority, event, command name, alias. They
            # are identifiers and routing keys, so they are kept; nothing in a tag is a secret
            # by Symfony's own design, since tags are read by compiler passes, not by code.
            facts.tags.append(
                TagMembership(
                    tag=tag_name,
                    service_id=service.service_id,
                    class_fqn=service.class_fqn,
                    attributes={k: v for k, v in tag.attrib.items() if k != "name"},
                )
            )

    # --- arguments --------------------------------------------------------------------

    def _read_arguments(
        self,
        parent: ET.Element,
        service: Service,
        member: Member,
        member_name: str | None,
        facts: ContainerFacts,
    ) -> None:
        for position, argument in enumerate(parent.findall(NS + "argument")):
            facts.injections.append(
                self._read_argument(argument, service, member, member_name, position, facts)
            )

    def _read_argument(
        self,
        argument: ET.Element,
        service: Service,
        member: Member,
        member_name: str | None,
        position: int,
        facts: ContainerFacts,
    ) -> Injection:
        declared = argument.get("type")
        kind = _KIND_BY_TYPE.get(declared) if declared else None
        requested_id = argument.get("id")
        tag = argument.get("tag")
        name: str | None = None

        if kind is None and declared is not None:
            kind = ArgumentKind.UNKNOWN
            facts.skip(reasons.DROP_ARGUMENT_UNKNOWN_KIND)
        elif kind is None:
            # No `type` attribute: a literal. The text is inspected only to tell an env
            # reference from an ordinary scalar, and is then dropped.
            text = argument.text or ""
            name = env_name(text)
            if name:
                kind = ArgumentKind.ENV
            elif text.strip().startswith("%env(") :
                kind = ArgumentKind.ENV
                facts.skip(reasons.DROP_ENV_NAME_UNPARSEABLE)
            else:
                kind = ArgumentKind.SCALAR

        if kind in (ArgumentKind.SERVICE, ArgumentKind.SERVICE_CLOSURE) and not requested_id:
            # An inline anonymous definition: the target has no id to bind to.
            facts.skip(reasons.DROP_INLINE_ANONYMOUS_SERVICE)
            facts.skip(reasons.DROP_ARGUMENT_WITHOUT_TARGET)

        return Injection(
            consumer_id=service.service_id,
            consumer_class=service.class_fqn,
            member=member,
            member_name=member_name,
            position=position,
            # Compiled Symfony XML carries no parameter names. Left None on purpose; see the
            # module docstring.
            parameter=None,
            kind=kind,
            requested_id=requested_id,
            tag=tag,
            env_name=name,
        )

    # --- second pass ------------------------------------------------------------------

    def _resolve_injections(self, facts: ContainerFacts) -> None:
        """Follow aliases once every service is known.

        A second pass rather than resolution in place: the XML lists an alias after its target
        as often as before it, and resolving eagerly would make the result depend on file order.
        """
        resolved: list[Injection] = []
        for injection in facts.injections:
            if injection.requested_id is None:
                resolved.append(injection)
                continue
            target = facts.resolve_id(injection.requested_id)
            service = facts.services.get(target)
            if service is None and target not in facts.aliases:
                facts.skip(reasons.DROP_ALIAS_TARGET_UNDEFINED)
            resolved.append(
                Injection(
                    consumer_id=injection.consumer_id,
                    consumer_class=injection.consumer_class,
                    member=injection.member,
                    member_name=injection.member_name,
                    position=injection.position,
                    parameter=injection.parameter,
                    kind=injection.kind,
                    requested_id=injection.requested_id,
                    resolved_id=target,
                    resolved_class=service.class_fqn if service else None,
                    via_alias=target != injection.requested_id,
                    tag=injection.tag,
                    env_name=injection.env_name,
                )
            )
        facts.injections = resolved

    def _read_decorations(self, facts: ContainerFacts) -> None:
        """Recover decoration from the `.inner` renaming, by finding who holds the reference."""
        consumers: dict[str, str] = {}
        for injection in facts.injections:
            if injection.requested_id and injection.requested_id.endswith(".inner"):
                consumers.setdefault(injection.requested_id, injection.consumer_id)
        for service_id, service in facts.services.items():
            if not service_id.endswith(".inner"):
                continue
            decorator_id = consumers.get(service_id)
            if decorator_id is None:
                facts.skip(reasons.DROP_INNER_WITHOUT_DECORATOR)
                continue
            decorator = facts.services.get(decorator_id)
            facts.decorations.append(
                Decoration(
                    decorator_id=decorator_id,
                    decorator_class=decorator.class_fqn if decorator else None,
                    inner_id=service_id,
                    inner_class=service.class_fqn,
                )
            )
