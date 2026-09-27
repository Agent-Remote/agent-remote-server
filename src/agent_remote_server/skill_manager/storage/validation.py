"""
逐块验证内容身份，避免把大型运行数据库整体载入内存。
"""

import codecs
import hashlib

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry


class ContentVerifier:
    """
    同时验证长度、SHA-256 与完整 UTF-8 文本分类。
    """

    def __init__(self, entry: SkillTreeEntry) -> None:
        """
        创建仅用于普通文件的验证器。

        :param entry (SkillTreeEntry): 预期文件元数据
        :raises ValueError: 条目不是普通文件
        """
        if entry.kind != "file":
            raise ValueError("content requires a file entry")
        self._entry = entry
        self._size = 0
        self._digest = hashlib.sha256()
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._is_text = True

    def update(self, chunk: bytes) -> None:
        """
        在写盘前检查大小并累积摘要和文本分类。

        :param chunk (bytes): 下一个内容块
        :raises ValueError: 实际字节数超过声明
        """
        self._size += len(chunk)
        if self._size > self._entry.size:
            raise ValueError("skill content exceeds declared size")
        self._digest.update(chunk)
        if self._is_text:
            try:
                self._decoder.decode(chunk, final=False)
                self._is_text = b"\x00" not in chunk
            except UnicodeDecodeError:
                self._is_text = False

    def finish(self) -> None:
        """
        验证流结束时的精确元数据，包括被截断的 UTF-8 字符。

        :raises ValueError: 完整内容与声明不一致
        """
        if self._size != self._entry.size:
            raise ValueError("skill content size does not match")
        if self._digest.hexdigest() != self._entry.sha256:
            raise ValueError("skill content digest does not match")
        if self._is_text:
            try:
                self._decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                self._is_text = False
        if (self._entry.content_kind == "text") != self._is_text:
            raise ValueError("skill content classification does not match")
