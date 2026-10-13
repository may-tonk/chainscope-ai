"""Migrate with a private connection provided by `chainscope init-db`."""

from alembic import context

from chainscope.config import load_settings
from chainscope.database import engine_for
from chainscope.models import metadata

config = context.config


def run(connection):
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        dialect_name="mysql", target_metadata=metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    run(config.attributes["connection"])
else:
    engine = engine_for(load_settings())
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
