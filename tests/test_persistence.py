import runpy
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_remote_server.db import Base
from agent_remote_server.models import User
from agent_remote_server.services import PersistenceService

EXPECTED_TABLES = {
    "audit_logs",
    "auth_tokens",
    "browser_sessions",
    "cli_login_codes",
    "developer_credential_profiles",
    "node_heartbeats",
    "node_task_results",
    "node_tasks",
    "nodes",
    "session_events",
    "sessions",
    "ssh_keys",
    "sync_sessions",
    "tool_account_profiles",
    "tool_account_developer_credential_profiles",
    "tool_accounts",
    "user_devices",
    "users",
    "wireguard_peers",
    "workspaces",
}

EXPECTED_INDEXES = {
    "audit_logs_actor_idx",
    "auth_tokens_device_status_idx",
    "auth_tokens_hash_uidx",
    "auth_tokens_user_status_idx",
    "browser_sessions_node_status_idx",
    "browser_sessions_user_status_idx",
    "cli_login_codes_device_hash_uidx",
    "cli_login_codes_status_idx",
    "cli_login_codes_user_code_uidx",
    "developer_credential_profiles_user_idx",
    "node_heartbeats_node_created_idx",
    "nodes_node_token_hash_uidx",
    "nodes_registration_token_hash_uidx",
    "nodes_status_heartbeat_idx",
    "node_task_results_task_id_idx",
    "node_tasks_poll_idx",
    "node_tasks_task_id_uidx",
    "sessions_account_active_idx",
    "sessions_project_idx",
    "ssh_keys_device_idx",
    "sync_sessions_workspace_status_idx",
    "tool_accounts_affinity_node_idx",
    "tool_accounts_user_tool_idx",
    "wireguard_peers_device_idx",
    "wireguard_peers_node_idx",
    "workspaces_project_idx",
}


def test_model_metadata_registers_core_tables() -> None:
    table_names = set(Base.metadata.tables)

    assert table_names == EXPECTED_TABLES


def test_model_metadata_registers_core_indexes() -> None:
    index_names = {
        index.name
        for table in Base.metadata.tables.values()
        for index in table.indexes
        if index.name
    }

    assert index_names >= EXPECTED_INDEXES


def test_initial_migration_revision_identity() -> None:
    migration_path = Path("migrations/versions/0001_core_schema.py")
    migration_globals = runpy.run_path(str(migration_path))

    assert migration_globals["revision"] == "0001_core_schema"
    assert migration_globals["down_revision"] is None


def test_identity_migration_revision_identity() -> None:
    migration_path = Path("migrations/versions/0002_identity_auth.py")
    migration_globals = runpy.run_path(str(migration_path))

    assert migration_globals["revision"] == "0002_identity_auth"
    assert migration_globals["down_revision"] == "0001_core_schema"


def test_node_control_migration_revision_identity() -> None:
    migration_path = Path("migrations/versions/0003_node_control.py")
    migration_globals = runpy.run_path(str(migration_path))

    assert migration_globals["revision"] == "0003_node_control"
    assert migration_globals["down_revision"] == "0002_identity_auth"


def test_connection_fields_migration_revision_identity() -> None:
    migration_path = Path("migrations/versions/0004_connection_fields.py")
    migration_globals = runpy.run_path(str(migration_path))

    assert migration_globals["revision"] == "0004_connection_fields"
    assert migration_globals["down_revision"] == "0003_node_control"


async def test_repository_crud_round_trip() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session:
            service = PersistenceService(session)
            users = service.repository(User)
            user = await users.add(
                User(
                    username="rem",
                    display_name="Rem",
                    role="admin",
                    status="active",
                    password_hash="hashed",
                    totp_enabled=False,
                )
            )
            await session.commit()
            user_id = user.id

        async with session_factory() as session:
            service = PersistenceService(session)
            users = service.repository(User)
            found = await users.get(user_id)

            assert found is not None
            assert found.username == "rem"
            assert [user.username for user in await users.list()] == ["rem"]

            found.display_name = "Rem Updated"
            await session.commit()

        async with session_factory() as session:
            service = PersistenceService(session)
            users = service.repository(User)
            updated = await users.get(user_id)

            assert updated is not None
            assert updated.display_name == "Rem Updated"

        async with session_factory() as session:
            service = PersistenceService(session)
            users = service.repository(User)
            found = await users.get(user_id)

            assert found is not None
            await users.delete(found)
            await session.commit()

        async with session_factory() as session:
            service = PersistenceService(session)
            assert await service.repository(User).get(user_id) is None
    finally:
        await engine.dispose()


async def test_repository_surfaces_unique_constraint_conflict() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with session_factory() as session:
            users = PersistenceService(session).repository(User)
            await users.add(
                User(
                    username="duplicate",
                    display_name="First",
                    role="user",
                    status="active",
                    password_hash="hashed",
                    totp_enabled=False,
                )
            )

            with pytest.raises(IntegrityError):
                await users.add(
                    User(
                        username="duplicate",
                        display_name="Second",
                        role="user",
                        status="active",
                        password_hash="hashed",
                        totp_enabled=False,
                    )
                )
    finally:
        await engine.dispose()
