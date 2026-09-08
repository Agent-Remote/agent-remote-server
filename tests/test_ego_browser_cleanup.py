from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI

from agent_remote_server.ego_browser import cleanup as ego_browser_cleanup


@pytest.mark.asyncio
async def test_cleanup_expires_bindings_and_publishes_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台循环必须在同一批次中过期 binding 并发布 durable outbox。"""

    stop = ego_browser_cleanup.asyncio.Event()
    calls: list[object] = []
    session_marker = object()

    class SessionContext:
        async def __aenter__(self) -> object:
            calls.append("session_opened")
            return session_marker

        async def __aexit__(self, *_: object) -> None:
            calls.append("session_closed")

    class Service:
        def __init__(
            self, session: object, settings: object, *, revocation_publisher: object
        ) -> None:
            calls.append((session, settings, revocation_publisher))

        async def expire_due(self) -> int:
            calls.append("expired")
            return 2

        async def publish_pending_revocations(self, *, limit: int) -> int:
            calls.append(("published", limit))
            stop.set()
            return 3

    class Nodes:
        def __init__(
            self,
            session: object,
            settings: object,
            *,
            ego_browser_revocation_publisher: object,
        ) -> None:
            calls.append(("nodes", session, settings, ego_browser_revocation_publisher))

        async def expire_stale_nodes(self) -> int:
            calls.append("stale_nodes")
            return 1

    settings = SimpleNamespace(
        ego_browser_cleanup_interval_seconds=0.001,
        ego_browser_cleanup_batch_size=17,
    )
    bus = object()
    app = cast(
        FastAPI,
        SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                session_factory=SessionContext,
                ego_browser_revocation_bus=bus,
            )
        ),
    )
    monkeypatch.setattr(ego_browser_cleanup, "EgoBrowserService", Service)
    monkeypatch.setattr(ego_browser_cleanup, "NodeService", Nodes)

    await ego_browser_cleanup.run_ego_browser_cleanup(app, stop)

    assert calls == [
        "session_opened",
        ("nodes", session_marker, settings, bus),
        "stale_nodes",
        (session_marker, settings, bus),
        "expired",
        ("published", 17),
        "session_closed",
    ]
