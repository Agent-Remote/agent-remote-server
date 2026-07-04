from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_PASSWORD_HASHER = PasswordHasher()


def hash_password(password: str) -> str:
    """
    使用 Argon2id 哈希密码

    :param password (str): 明文密码

    :return str: 密码哈希
    """

    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """
    校验明文密码

    :param password (str): 明文密码
    :param password_hash (str): 密码哈希

    :return bool: 密码是否匹配
    """

    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False
