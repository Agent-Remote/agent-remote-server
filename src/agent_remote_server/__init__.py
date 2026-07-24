import os
from importlib.metadata import PackageNotFoundError, version

_version = os.getenv("AGENT_REMOTE_VERSION")
if not _version:
    try:
        _version = version("agent-remote-server")
    except PackageNotFoundError:
        _version = "0.0.4+fix.14"

__version__: str = _version

__all__ = ["__version__"]
