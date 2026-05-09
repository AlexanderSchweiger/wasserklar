import logging
import os
from logging.config import fileConfig

from flask import current_app
from sqlalchemy import text

from alembic import context

# Multi-Tenant: wenn diese ENV gesetzt ist, migrieren wir das angegebene
# Schema (z.B. "tenant_alm") und legen die alembic_version-Tabelle DORT ab,
# nicht in public. Wird von SaaS upgrade-tenants und vom Platform-Provisioner
# pro Tenant gesetzt.
TARGET_SCHEMA = os.environ.get("ALEMBIC_TENANT_SCHEMA")

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
fileConfig(config.config_file_name)
logger = logging.getLogger('alembic.env')


def get_engine():
    try:
        # this works with Flask-SQLAlchemy<3 and Alchemical
        return current_app.extensions['migrate'].db.get_engine()
    except (TypeError, AttributeError):
        # this works with Flask-SQLAlchemy>=3
        return current_app.extensions['migrate'].db.engine


def get_engine_url():
    try:
        return get_engine().url.render_as_string(hide_password=False).replace(
            '%', '%%')
    except AttributeError:
        return str(get_engine().url).replace('%', '%%')


# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
config.set_main_option('sqlalchemy.url', get_engine_url())
target_db = current_app.extensions['migrate'].db

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_metadata():
    if hasattr(target_db, 'metadatas'):
        return target_db.metadatas[None]
    return target_db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=get_metadata(), literal_binds=True
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # this callback is used to prevent an auto-migration from being generated
    # when there are no changes to the schema
    # reference: http://alembic.zzzcomputing.com/en/latest/cookbook.html
    def process_revision_directives(context, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info('No changes in schema detected.')

    conf_args = current_app.extensions['migrate'].configure_args
    if conf_args.get("process_revision_directives") is None:
        conf_args["process_revision_directives"] = process_revision_directives

    connectable = get_engine()

    with connectable.connect() as connection:
        # Multi-Tenant: search_path muss BEVOR context.configure() korrekt
        # stehen. Der Pool-checkout-Listener (saas/tenant_middleware.py) setzt
        # search_path standardmaessig auf "public" — wir uebersteuern hier
        # fuer die Dauer dieser einen Connection.
        if TARGET_SCHEMA and connection.dialect.name == "postgresql":
            connection.execute(
                text(f'SET search_path TO "{TARGET_SCHEMA}", public')
            )

        # Defaults aus Flask-Migrate uebernehmen, dann mit unseren Multi-Tenant-
        # und Dialect-Optionen ueberschreiben (conf_args setzt sonst eigenes
        # compare_type/render_as_batch und es gibt einen Doppel-Kwarg-Crash).
        configure_kwargs = dict(conf_args)
        configure_kwargs.update(
            connection=connection,
            target_metadata=get_metadata(),
            # alembic_version-Tabelle landet im Tenant-Schema — sonst kollidieren
            # alle Tenants in public.alembic_version.
            version_table_schema=TARGET_SCHEMA,
            include_schemas=False,
            # Pflicht fuer SQLite (Self-Host-Default) — ohne batch-mode kracht
            # jede ALTER COLUMN. Auf Postgres no-op.
            render_as_batch=(connection.dialect.name == "sqlite"),
            compare_type=True,
            compare_server_default=True,
        )
        context.configure(**configure_kwargs)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
