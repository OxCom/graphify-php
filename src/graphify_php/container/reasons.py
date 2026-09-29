"""The fixed vocabulary for everything this layer declines to do.

Two families, kept apart because they answer different questions. A SKIP_* reason explains why
the whole layer produced nothing and appears once, in the status record. A DROP_* reason
explains one fact left out of an artifact that was still produced, and appears as a counter.

Collapsing them would hide the difference between "no container here" and "a container with
900 unreadable arguments", which look identical in a total of zero.
"""

from __future__ import annotations

# Layer-level: the overlay was not produced at all.
SKIP_NOT_PHP_PROJECT = "container-no-composer-json"
SKIP_NOT_SYMFONY = "container-not-a-symfony-project"
SKIP_NO_CACHE_DIR = "container-no-cache-directory"
SKIP_XML_ABSENT = "container-xml-absent"
SKIP_XML_UNREADABLE = "container-xml-unreadable"
SKIP_XML_MALFORMED = "container-xml-malformed"
SKIP_XML_NOT_A_CONTAINER = "container-xml-wrong-root-element"

# Fact-level: the overlay exists, this piece of it does not.
DROP_SERVICE_WITHOUT_ID = "service-without-id"
DROP_INLINE_ANONYMOUS_SERVICE = "inline-anonymous-service"
DROP_SERVICE_WITHOUT_CLASS = "service-without-class"
DROP_ALIAS_TARGET_UNDEFINED = "alias-target-not-defined"
DROP_ARGUMENT_WITHOUT_TARGET = "argument-service-without-id"
DROP_ARGUMENT_UNKNOWN_KIND = "argument-kind-unrecognised"
DROP_ENV_NAME_UNPARSEABLE = "env-expression-without-name"
DROP_INNER_WITHOUT_DECORATOR = "inner-service-without-decorator"
DROP_TAG_WITHOUT_NAME = "tag-without-name"
