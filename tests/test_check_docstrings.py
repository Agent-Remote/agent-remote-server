from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from textwrap import dedent
from typing import cast

import pytest

CHECKER_PATH = Path(__file__).resolve().parents[1] / "scripts/check_docstrings.py"
checker_namespace = run_path(str(CHECKER_PATH))
check_file = cast(Callable[[Path], list[str]], checker_namespace["check_file"])
check_main = cast(Callable[[], int], checker_namespace["main"])
checker_globals = check_main.__globals__


@pytest.mark.parametrize(
    ("source", "expected_error"),
    [
        (
            """
            class PublicType:
                \'\'\'公开类型。\'\'\'
            """,
            "needs a triple-double-quoted docstring",
        ),
        (
            '''
            def render(name: str) -> None:
                """渲染名称。"""
            ''',
            "needs a typed Chinese :param entry for 'name'",
        ),
        (
            '''
            def render() -> str:
                """渲染名称。"""
                return "name"
            ''',
            "needs a typed Chinese :return entry",
        ),
        (
            '''
            def render(name: str) -> str:
                """
                Render a name.

                :param name (str): 待渲染名称

                :return str: 渲染结果
                """

                return name
            ''',
            "needs a Chinese summary",
        ),
        (
            '''
            def render() -> None:
                """
                渲染名称。

                :raises ValueError: invalid name
                """
            ''',
            "directive without a Chinese description",
        ),
        (
            '''
            def render() -> str:
                """
                渲染名称。

                :return str: binding 结果
                """

                return "name"
            ''',
            "directive without a Chinese description",
        ),
        (
            '''
            def render() -> str:
                """binding 结果。"""

                return "name"
            ''',
            "needs a Chinese summary",
        ),
        (
            '''
            from pydantic import BaseModel

            class SchemaBase(BaseModel):
                """模型基类。"""

            class Payload(SchemaBase):
                """请求数据。"""

                value: str
            ''',
            "Pydantic field 'Payload.value' needs Field",
        ),
    ],
)
def test_check_file_rejects_incomplete_public_documentation(
    tmp_path: Path, source: str, expected_error: str
) -> None:
    """检查器应拒绝缺少结构、类型或字段说明的公开合同。"""

    path = tmp_path / "sample.py"
    path.write_text(dedent(source), encoding="utf-8")

    errors = check_file(path)

    assert any(expected_error in error for error in errors)


def test_check_file_accepts_complete_inherited_pydantic_model(tmp_path: Path) -> None:
    """检查器应接受完整记录的间接 Pydantic 模型与公开方法。"""

    path = tmp_path / "sample.py"
    path.write_text(
        dedent(
            '''
            from pydantic import BaseModel, Field

            class SchemaBase(BaseModel):
                """模型基类。"""

            class Payload(SchemaBase):
                """请求数据。"""

                value: str = Field(..., description="待渲染文本")

                def render(self, prefix: str) -> str:
                    """
                    渲染带前缀的文本

                    :param prefix (str): 文本前缀

                    :return str: 拼接后的文本
                    """

                    return f"{prefix}{self.value}"
            '''
        ),
        encoding="utf-8",
    )

    assert check_file(path) == []


def test_check_file_rejects_mixed_pydantic_field_description(tmp_path: Path) -> None:
    """检查器应拒绝只拼接英文业务词的字段说明。"""

    path = tmp_path / "sample.py"
    path.write_text(
        dedent(
            '''
            from pydantic import BaseModel, Field

            class Payload(BaseModel):
                """请求数据。"""

                value: str = Field(..., description="binding 结果")
            '''
        ),
        encoding="utf-8",
    )

    errors = check_file(path)

    assert any("Pydantic field 'Payload.value' needs Field" in error for error in errors)


def test_check_file_rejects_duplicate_and_unknown_parameters(tmp_path: Path) -> None:
    """检查器应拒绝重复或不属于函数签名的参数说明。"""

    path = tmp_path / "sample.py"
    path.write_text(
        dedent(
            '''
            def render(name: str) -> None:
                """
                渲染名称。

                :param name (str): 待渲染名称
                :param name (str): 重复的名称说明
                :param extra (str): 不存在的参数说明
                """
            '''
        ),
        encoding="utf-8",
    )

    errors = check_file(path)

    assert any("duplicate :param entry for 'name'" in error for error in errors)
    assert any("unknown :param entry for 'extra'" in error for error in errors)


def test_main_scans_migrations_and_scripts_for_public_contracts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """检查器主入口应覆盖迁移和脚本中的公开函数合同。"""

    migrations_root = tmp_path / "migrations"
    scripts_root = tmp_path / "scripts"
    migrations_root.mkdir()
    scripts_root.mkdir()
    (migrations_root / "sample.py").write_text(
        "def upgrade() -> None:\n    pass\n", encoding="utf-8"
    )
    (scripts_root / "sample.py").write_text(
        'def seed() -> dict[str, str]:\n    """创建测试 fixture。"""\n    return {}\n',
        encoding="utf-8",
    )

    original_roots = checker_globals["CHECK_ROOTS"]
    checker_globals["CHECK_ROOTS"] = (migrations_root, scripts_root)
    try:
        assert check_main() == 1
    finally:
        checker_globals["CHECK_ROOTS"] = original_roots

    output = capsys.readouterr().err
    assert "public function 'upgrade' needs a Chinese summary" in output
    assert "public function 'seed' needs a typed Chinese :return entry" in output
