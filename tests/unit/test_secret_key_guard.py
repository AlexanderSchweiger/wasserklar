"""Regressionstest fuer den SECRET_KEY-Boot-Guard (Security-Audit #2).

In produktiven Configs darf der oeffentlich bekannte Dev-Default (oder ein leerer
Wert) NICHT als SECRET_KEY durchgehen — sonst sind Session-Cookies und alle
itsdangerous-Tokens faelschbar. Dev/Test bleiben bewusst unberuehrt.
"""

from __future__ import annotations

import config as config_module
import pytest

from app import create_app


def test_production_rejects_default_secret_key(monkeypatch):
    monkeypatch.setattr(
        config_module.ProductionConfig, "SECRET_KEY", "dev-secret-change-in-production"
    )
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app("production")


def test_production_rejects_empty_secret_key(monkeypatch):
    monkeypatch.setattr(config_module.ProductionConfig, "SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app("production")


def test_testing_config_allows_default_like_key():
    # TESTING ist prod-ungated -> der Guard darf hier NICHT feuern.
    app = create_app("testing")
    assert app.config["TESTING"] is True
