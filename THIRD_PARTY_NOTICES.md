# Third-Party Notices

This repository is licensed under GPL-3.0-only. See `LICENSE`.

## Python Runtime Dependencies

| Component | Use | License |
| --- | --- | --- |
| FastAPI | HTTP API framework | MIT. Source: https://github.com/fastapi/fastapi/blob/master/LICENSE |
| SQLAlchemy / Alembic | Database access and migrations | MIT. Sources: https://github.com/sqlalchemy/sqlalchemy/blob/main/LICENSE and https://github.com/sqlalchemy/alembic/blob/main/LICENSE |
| asyncpg | PostgreSQL driver | Apache-2.0. Source: https://github.com/MagicStack/asyncpg/blob/master/LICENSE |
| redis-py | Redis client | MIT. Source: https://github.com/redis/redis-py/blob/master/LICENSE |
| Pydantic Settings | Configuration and validation | MIT. Source: https://github.com/pydantic/pydantic-settings/blob/main/LICENSE |
| Uvicorn | ASGI server | BSD-3-Clause. Source: https://github.com/Kludex/uvicorn/blob/master/LICENSE.md |
| HTTPX | HTTP client | BSD-3-Clause. Source: https://github.com/encode/httpx/blob/master/LICENSE.md |
| Cryptography | Cryptographic primitives | Apache-2.0 OR BSD-3-Clause. Source: https://github.com/pyca/cryptography/blob/main/LICENSE |
| argon2-cffi | Password hashing bindings | MIT. Source: https://github.com/hynek/argon2-cffi/blob/main/LICENSE |
| websockets | WebSocket support | BSD-3-Clause. Source: https://github.com/python-websockets/websockets/blob/main/LICENSE |

The production image is based on `python:3.13-slim`. Python is distributed
under the PSF License; operating-system packages retain their individual
licenses. Derived images must retain the notices from the exact base-image
digest.

The exact Python dependency graph is recorded in `uv.lock`.

The server references `kasmweb/chrome:1.18.0` as an external browser runtime;
it does not embed that image. Mirrors and derived images must retain notices
from the exact image digest. Source: https://hub.docker.com/r/kasmweb/chrome

## Distribution Requirements

When a release artifact redistributes third-party software, it must include:

- the exact component name and version;
- the source URL and checksum;
- the applicable license and notice text;
- any required source code, source offer, or relinking instructions.
