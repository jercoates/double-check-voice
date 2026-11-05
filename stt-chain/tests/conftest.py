"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _open_by_default_for_tests(monkeypatch):
    """The proxy refuses to start with an empty peer allowlist. Most tests read the
    config from the environment and are not about that, so they opt in to open access;
    the tests that are about it remove this setting first."""
    monkeypatch.setenv("DCV_ALLOW_ANY_PEER", "1")
