import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI

from agent_remote_server import port_forward_cleanup


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_cleanup_runner_processes_batch_advances_cursor_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台任务必须持续推进游标，并能在下一周期及时响应关闭。"""

    stop = asyncio.Event()
    calls: list[UUID | None] = []
    next_cursor = UUID("11111111-1111-4111-8111-111111111111")

    class Service:
        def __init__(self, *_args: object) -> None:
            pass

        async def cleanup(self, *, after_id: UUID | None = None) -> SimpleNamespace:
            calls.append(after_id)
            stop.set()
            return SimpleNamespace(changed=2, next_cursor=next_cursor)

    monkeypatch.setattr(port_forward_cleanup, "PortForwardService", Service)
    app = FastAPI()
    app.state.settings = SimpleNamespace(port_forward_cleanup_interval_seconds=0.001)
    app.state.session_factory = _Session
    app.state.port_forward_token_store = object()

    asyncio.run(port_forward_cleanup.run_port_forward_cleanup(app, stop))

    assert calls == [None]
