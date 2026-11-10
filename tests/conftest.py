"""Suite-wide fixtures."""

import pytest

from graphify_php.resolver import EXTERNAL_STUBS_ENV


@pytest.fixture(autouse=True)
def _external_stubs_off(monkeypatch):
    """Start every test with external stubs off, whatever the shell exports.

    The resolver reads the flag from the environment, so an ambient value adds `php_external_*`
    edges to tests that assert an exact edge set. A test that covers stubs sets the flag itself.
    """
    monkeypatch.delenv(EXTERNAL_STUBS_ENV, raising=False)
