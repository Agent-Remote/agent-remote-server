"""
验证普通工具会话停止回报与原任务冻结的运行时身份。
"""

from uuid import UUID

from agent_remote_server.errors import ApiError
from agent_remote_server.models import NodeTask


class SessionStopResultGuard:
    """
    在首次接受普通停止结果前验证不可变会话与运行时身份。

    节点可以省略旧协议中未必存在的资源字段，但一旦回报这些字段，
    它们必须与原停止任务完全一致。这样 `status=stopped` 才能成为
    Server 当前会话状态和停止回执的可信输入。
    """

    def authorize(self, task: NodeTask, result: dict[str, object]) -> None:
        """
        验证普通停止成功回报。

        :param task (NodeTask): 原始停止任务
        :param result (dict[str, object]): 节点成功回报
        :raises ApiError: 回报缺少或改变冻结身份
        """
        if task.task_type != "stop_tool_session":
            return
        expected_session = task.payload.get("session_id")
        expected_backend = task.payload.get("runtime_backend")
        expected_resource = task.payload.get("runtime_resource_id")
        if not isinstance(expected_session, str):
            raise self._invalid()
        # UUID validation prevents an arbitrary string in a malformed task from
        # being treated as a trustworthy lifecycle identity.
        try:
            UUID(expected_session)
        except ValueError:
            raise self._invalid() from None
        if result.get("status") != "stopped" or result.get("session_id") != expected_session:
            raise self._invalid()
        if not isinstance(expected_backend, str) or expected_backend not in {
            "native",
            "docker_sandbox",
        }:
            raise self._invalid()
        reported_backend = result.get("runtime_backend")
        if reported_backend is not None and reported_backend != expected_backend:
            raise self._invalid()
        reported_resource = result.get("runtime_resource_id")
        if reported_resource is not None and reported_resource != expected_resource:
            raise self._invalid()
        unclean = result.get("unclean")
        if unclean is not None and type(unclean) is not bool:
            raise self._invalid()

    @staticmethod
    def _invalid() -> ApiError:
        """
        返回不泄露节点回报正文的固定错误。

        :return ApiError: 停止结果身份不匹配
        """
        return ApiError(
            code="SESSION_STOP_RESULT_INVALID",
            message="Session stop result does not match its original runtime identity.",
            status_code=409,
        )
