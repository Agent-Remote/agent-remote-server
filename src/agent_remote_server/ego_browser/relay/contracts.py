"""定义 ego-browser relay 的共享身份和接口。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from agent_remote_server.relay.binding import RelayBinding

EgoBrowserRelayRole = Literal["bridge", "wrapper"]
EGO_BROWSER_RELAY_CHANNEL = "ego_browser_bridge"
EGO_BROWSER_RELAY_KIND = "ego_browser"
EGO_BROWSER_PROTOCOL = "ego-browser-bridge-v1"


@dataclass(frozen=True)
class EgoBrowserRelayBinding:
    """ego-browser 中继的完整绑定身份。"""

    user_id: UUID
    ego_browser_device_id: UUID
    tool_session_id: UUID
    binding_id: UUID
    node_id: UUID
    generation: int

    @property
    def relay_binding(self) -> RelayBinding:
        """
        返回与浏览器业务表解耦的通用 relay 身份。

        :return RelayBinding: 与业务字段解耦的通用 relay 身份
        """

        return RelayBinding(
            kind="ego_browser",
            binding_id=self.binding_id,
            generation=self.generation,
        )


@dataclass(frozen=True)
class EgoBrowserRelayTicketClaims:
    """一次性 ego-browser 中继票据声明。"""

    binding: EgoBrowserRelayBinding
    role: EgoBrowserRelayRole


@dataclass(frozen=True)
class EgoBrowserProofChallengeClaims:
    """一次性设备 PoP challenge 的认证上下文。"""

    user_id: UUID
    ego_browser_device_id: UUID
    operation: str
    generation: int
    binding_id: UUID | None


FrameValidator = Callable[
    [EgoBrowserRelayTicketClaims, bytes, dict[str, object]],
    Awaitable[None],
]
RevocationHandler = Callable[[UUID, int], Awaitable[None]]


class EgoBrowserRevocationPublisher(Protocol):
    """ego-browser 中继撤销发布接口。"""

    async def publish(self, binding_id: UUID, generation: int) -> None:
        """
        发布一个不含内容的 binding generation 撤销事件。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation
        """
