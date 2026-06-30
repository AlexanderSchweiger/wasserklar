"""API-Schluessel (Pro-REST-API + MCP-Server).

Liegt im OSS, damit das ``ApiKey``-Model (app/models.py) und die Hash-/
Erzeugungs-Helfer mit der Tenant-DB mitwandern (Provisioning + Alembic). Die
eigentliche REST-/MCP-Schicht und das Pro-Gating leben im SaaS-Layer; im
OSS-Standalone ist das Feature aus (``FEATURE_API_ENABLED`` default False).
"""

from app.api_keys.service import (  # noqa: F401
    KEY_ENV,
    KEY_PRODUCT,
    create_api_key,
    hash_key,
    parse_slug,
    verify_key,
)
