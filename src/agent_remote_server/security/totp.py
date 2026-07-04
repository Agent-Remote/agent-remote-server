import hmac
import secrets
from base64 import b32decode, b32encode
from datetime import UTC, datetime
from hashlib import sha1
from struct import pack, unpack


def generate_totp_secret() -> str:
    """
    生成 TOTP secret

    :return str: Base32 secret
    """

    return b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def generate_totp_code(secret: str, *, at_time: datetime | None = None) -> str:
    """
    生成 TOTP 验证码

    :param secret (str): Base32 secret
    :param at_time (datetime): 可选时间

    :return str: 六位验证码
    """

    current_time = at_time or datetime.now(UTC)
    counter = int(current_time.timestamp()) // 30
    padded_secret = secret + ("=" * ((8 - len(secret) % 8) % 8))
    key = b32decode(padded_secret.encode("ascii"), casefold=True)
    digest = hmac.new(key, pack(">Q", counter), sha1).digest()
    offset = digest[-1] & 0x0F
    code = unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


def verify_totp_code(secret: str, code: str, *, at_time: datetime | None = None) -> bool:
    """
    校验 TOTP 验证码

    :param secret (str): Base32 secret
    :param code (str): 用户输入验证码
    :param at_time (datetime): 可选时间

    :return bool: 验证码是否有效
    """

    current_time = at_time or datetime.now(UTC)
    candidates = [
        generate_totp_code(
            secret, at_time=datetime.fromtimestamp(current_time.timestamp() + offset, UTC)
        )
        for offset in (-30, 0, 30)
    ]
    return any(hmac.compare_digest(candidate, code) for candidate in candidates)
