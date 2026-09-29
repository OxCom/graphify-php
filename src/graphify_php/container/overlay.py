"""Serialises container facts into an overlay artifact written beside the graph.

**Why this is not graph edges.** The mesh that consumes these graphs enforces one invariant: a
cross-project overlay is never merged into the structural graph, and a forbidden-edge validator
checks it. The reason is that the structural graph claims to describe the code, while every
fact in here is true only of one environment and one build profile — `kernel.environment: dev`,
with the debug bundle loaded, on the machine that compiled the cache. A `calls` edge asserted
from a dev container would be a claim about the code that the code does not make.

So: a separate file, a relation name of this layer's own (`binds_injection_to`), the
environment recorded inside the artifact, and `structural: false` stated rather than implied.
A consumer joins it against the graph at query time, and a consumer that does not want
environment-dependent facts simply does not read the file.

**Why there is no value anywhere in the output.** The artifact is assembled only from the
fields of `facts`, and that model has nowhere to put a value. An env reference contributes its
variable NAME; a scalar contributes the word `scalar`. `eval/canaries.py` plants fake secrets
and asserts none reaches an artifact — this writer passes by construction, not by filtering.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from .facts import ArgumentKind, ContainerFacts, Injection

SCHEMA = 1
RELATION = "binds_injection_to"
ARTIFACT_KIND = "container-overlay"
DEFAULT_ARTIFACT_NAME = "container-overlay.json"

# Kinds that name another service, and so make an injection site joinable to a class node.
_BINDING_KINDS = (
    ArgumentKind.SERVICE,
    ArgumentKind.SERVICE_CLOSURE,
    ArgumentKind.TAGGED_ITERATOR,
)


class OverlayWriter:
    """Builds the artifact and, separately, writes it.

    Split so a caller can assert on the document without a filesystem, which is what every
    test here does.
    """

    def __init__(self, relation: str = RELATION) -> None:
        self._relation = relation

    def build(self, facts: ContainerFacts) -> dict:
        bindings = [self._binding(i) for i in facts.injections]
        return {
            "schema": SCHEMA,
            "kind": ARTIFACT_KIND,
            "producer": "graphify-php.container",
            "relation": self._relation,
            # Stated, not implied. The validator on the consuming side rejects an overlay that
            # claims to be structural, and a future writer that flips this is making a visible
            # change rather than a quiet one.
            "structural": False,
            "generated_at": time.time(),
            "environment": dict(facts.environment),
            "counts": self.counts(facts),
            "aliases": [
                {"alias": a.alias_id, "target": a.target_id, "target_class": facts.class_of(a.target_id)}
                for a in sorted(facts.aliases.values(), key=lambda a: a.alias_id)
            ],
            "bindings": bindings,
            "tags": [
                {
                    "tag": t.tag,
                    "service": t.service_id,
                    "class": t.class_fqn,
                    "attributes": t.attributes,
                }
                for t in facts.tags
            ],
            "decorations": [asdict(d) for d in facts.decorations],
            "factories": [
                {
                    "service": s.service_id,
                    "class": s.class_fqn,
                    "factory_class": s.factory_class,
                    "factory_service": s.factory_service,
                    "factory_method": s.factory_method,
                    "constructor": s.constructor,
                }
                for s in facts.services.values()
                if s.factory_class or s.factory_service or s.constructor
            ],
            "dropped": dict(facts.skipped),
        }

    def counts(self, facts: ContainerFacts) -> dict:
        interface_bindings = [
            a for a in facts.interface_bindings if facts.class_of(a.target_id) is not None
        ]
        resolved = [i for i in facts.injections if i.resolved_class]
        return {
            "services": len(facts.services),
            "aliases": len(facts.aliases),
            "interface_bindings": len(interface_bindings),
            "injection_sites": len(facts.injections),
            # The denominator travels with the numerator: "resolved 2900" says nothing without
            # "out of 8389", and a regression that halves coverage still looks large alone.
            "injection_sites_resolved_to_class": len(resolved),
            "tag_memberships": len(facts.tags),
            "distinct_tags": len({t.tag for t in facts.tags}),
            "decorations": len(facts.decorations),
            "env_arguments": len([i for i in facts.injections if i.kind is ArgumentKind.ENV]),
        }

    def write(self, facts: ContainerFacts, out_dir: Path, name: str = DEFAULT_ARTIFACT_NAME) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / name
        path.write_text(json.dumps(self.build(facts), indent=2, sort_keys=False))
        return path

    def _binding(self, injection: Injection) -> dict:
        document = {
            "relation": self._relation,
            "consumer_service": injection.consumer_id,
            "consumer_class": injection.consumer_class,
            "member": injection.member.value,
            "member_name": injection.member_name,
            "position": injection.position,
            "parameter": injection.parameter,
            "kind": injection.kind.value,
        }
        if injection.kind in _BINDING_KINDS or injection.requested_id:
            document["requested_service"] = injection.requested_id
            document["resolved_service"] = injection.resolved_id
            document["resolved_class"] = injection.resolved_class
            document["via_alias"] = injection.via_alias
        if injection.tag:
            document["tag"] = injection.tag
        if injection.kind is ArgumentKind.ENV:
            # The name only. There is no branch anywhere in this file that reads a value.
            document["env_name"] = injection.env_name
        return document
