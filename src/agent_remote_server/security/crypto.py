from base64 import urlsafe_b64encode
from hashlib import sha256

from cryptography.fernet import Fernet


def _fernet(secret_key: str) -> Fernet:
    key = urlsafe_b64encode(sha256(secret_key.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_text(secret_key: str, value: str) -> bytes:
    """
    加密文本

    :param secret_key (str): 服务端密钥
    :param value (str): 明文

    :return bytes: 密文
    """

    return _fernet(secret_key).encrypt(value.encode("utf-8"))


def decrypt_text(secret_key: str, value: bytes) -> str:
    """
    解密文本

    :param secret_key (str): 服务端密钥
    :param value (bytes): 密文

    :return str: 明文
    """

    return _fernet(secret_key).decrypt(value).decode("utf-8")
