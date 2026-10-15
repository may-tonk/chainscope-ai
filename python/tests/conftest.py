"""Shared fixtures: an opt-in, throwaway MySQL database for integration tests."""

import os
from dataclasses import replace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from chainscope.config import load_settings
from chainscope.database import engine_for, initialize

requires_mysql = pytest.mark.skipif(
    os.environ.get("CHAINSCOPE_TEST_MYSQL") != "1",
    reason="requires real MySQL credentials and CHAINSCOPE_TEST_MYSQL=1",
)


@pytest.fixture
def mysql_engine():
    settings = replace(load_settings(), mysql_database="chainscope_test_" + uuid4().hex)
    owned = False
    engine = None
    try:
        # Prove the generated name is absent before taking ownership.
        server = engine_for(settings, server_only=True)
        try:
            with server.connect() as connection:
                assert (
                    settings.mysql_database
                    not in sa.inspect(connection).get_schema_names()
                )
        finally:
            server.dispose()
        owned = True
        assert initialize(settings)["schema_ready"]
        assert initialize(settings)["schema_ready"]  # migration can run again
        engine = engine_for(settings)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if owned:
            server = engine_for(settings, server_only=True)
            try:
                with server.connect() as connection:
                    connection.execute(
                        sa.text(f"DROP DATABASE `{settings.mysql_database}`")
                    )
            finally:
                server.dispose()
