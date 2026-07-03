import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from agent_remote_server.context import get_request_id


class JsonFormatter(logging.Formatter):
    """
    JSON 日志格式化器
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        格式化日志记录

        :param record (LogRecord): 日志记录

        :return str: JSON 字符串
        """

        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = getattr(record, "request_id", None) or get_request_id()
        if request_id:
            payload["request_id"] = request_id

        for key in ("method", "path", "status_code", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """
    配置结构化日志

    :param level (str): 日志级别
    """

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root_logger.addHandler(handler)

    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = True
