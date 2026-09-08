"""定义与具体业务会话无关的 relay binding 身份。"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

RelayBindingKind = Literal["device_control", "ego_browser"]


@dataclass(frozen=True)
class RelayBinding:
    """用绑定类型、业务 ID 和代次唯一标识中继。"""

    kind: RelayBindingKind
    binding_id: UUID
    generation: int

    def redis_fragment(self) -> str:
        """
        返回包含 binding kind 的 Redis 键片段。

        :return str: 可用于 Redis 键的稳定 binding 片段
        """

        return f"{self.kind}:{self.binding_id}:{self.generation}"
