import hmac
import secrets
from hashlib import sha256


def create_opaque_token(prefix: str) -> str:
    """
    创建不透明令牌

    :param prefix (str): 令牌前缀

    :return str: 原始令牌
    """

    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(secret_key: str, token: str) -> str:
    """
    使用服务端密钥哈希令牌

    :param secret_key (str): 服务端密钥
    :param token (str): 原始令牌

    :return str: 令牌哈希
    """

    digest = hmac.new(secret_key.encode("utf-8"), token.encode("utf-8"), sha256).hexdigest()
    return f"sha256:{digest}"
