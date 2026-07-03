from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """
    获取当前请求 ID

    :return str: 当前请求 ID
    """

    return request_id_var.get()
