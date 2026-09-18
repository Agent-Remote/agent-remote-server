"""
验证项目文档字符串检查器的完整覆盖范围。
"""

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
            '"""模块说明。"""',
            "module needs a multiline triple-double-quoted docstring",
        ),
        (
            """
            class PublicType:
                \'\'\'公开类型。\'\'\'
            """,
            "needs a multiline triple-double-quoted docstring",
        ),
        (
            """
            def _private_case() -> None:
                pass
            """,
            "function '_private_case' needs a Chinese summary",
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
            def render(name: str) -> None:
                """
                渲染名称

                :param name (int): 待渲染名称
                """
            ''',
            "has type 'int' for :param 'name'; expected 'str'",
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
            def render() -> str:
                """
                渲染名称

                :return int: 渲染结果
                """
                return "name"
            ''',
            "has return type 'int'; expected 'str'",
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
def test_check_file_rejects_incomplete_documentation(
    tmp_path: Path, source: str, expected_error: str
) -> None:
    """
    检查器应拒绝缺少结构、类型或字段说明的合同。

    :param tmp_path (Path): pytest 临时目录
    :param source (str): 待检查的 Python 源码
    :param expected_error (str): 预期错误片段
    """

    path = tmp_path / "sample.py"
    path.write_text(dedent(source), encoding="utf-8")

    errors = check_file(path)

    assert any(expected_error in error for error in errors)


def test_type_annotations_do_not_replace_parameter_or_return_entries(tmp_path: Path) -> None:
    """
    验证类型注解不能替代参数和返回值说明。

    :param tmp_path (Path): pytest 临时目录
    """

    path = tmp_path / "sample.py"
    path.write_text(
        dedent(
            '''
            """
            定义渲染测试模块。
            """

            def render(name: str) -> str:
                """
                渲染名称。
                """

                return name
            '''
        ),
        encoding="utf-8",
    )

    errors = check_file(path)

    assert any("typed Chinese :param entry for 'name'" in error for error in errors)
    assert any("typed Chinese :return entry" in error for error in errors)


def test_check_file_accepts_complete_inherited_pydantic_model(tmp_path: Path) -> None:
    """
    检查器应接受完整记录的间接 Pydantic 模型与方法。

    :param tmp_path (Path): pytest 临时目录
    """

    path = tmp_path / "sample.py"
    path.write_text(
        dedent(
            '''
            """
            定义完整记录的模型测试模块。
            """

            from pydantic import BaseModel, Field

            class SchemaBase(BaseModel):
                """
                模型基类。
                """

            class Payload(SchemaBase):
                """
                请求数据。
                """

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


def test_check_file_documents_annotated_value_type(tmp_path: Path) -> None:
    """
    检查器应忽略 Annotated 的依赖元数据并校验实际值类型。

    :param tmp_path (Path): pytest 临时目录
    """

    path = tmp_path / "sample.py"
    path.write_text(
        dedent(
            '''
            """
            定义依赖参数测试模块。
            """

            from typing import Annotated

            def dependency() -> None:
                """
                提供依赖。
                """

            def render(value: Annotated[str, dependency]) -> str:
                """
                渲染名称

                :param value (str): 待渲染名称
                :return str: 渲染结果
                """
                return value
            '''
        ),
        encoding="utf-8",
    )

    assert check_file(path) == []


def test_check_file_rejects_mixed_pydantic_field_description(tmp_path: Path) -> None:
    """
    检查器应拒绝只拼接英文业务词的字段说明。

    :param tmp_path (Path): pytest 临时目录
    """

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
    """
    检查器应拒绝重复或不属于函数签名的参数说明。

    :param tmp_path (Path): pytest 临时目录
    """

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


def test_main_scans_migrations_scripts_and_tests_for_all_functions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    检查器主入口应覆盖迁移、脚本、测试和私有函数。

    :param tmp_path (Path): pytest 临时目录
    :param capsys (pytest.CaptureFixture[str]): pytest 输出捕获器
    """

    migrations_root = tmp_path / "migrations"
    scripts_root = tmp_path / "scripts"
    tests_root = tmp_path / "tests"
    migrations_root.mkdir()
    scripts_root.mkdir()
    tests_root.mkdir()
    (migrations_root / "sample.py").write_text(
        "def upgrade() -> None:\n    pass\n", encoding="utf-8"
    )
    (scripts_root / "sample.py").write_text(
        'def seed() -> dict[str, str]:\n    """创建测试 fixture。"""\n    return {}\n',
        encoding="utf-8",
    )
    (tests_root / "sample.py").write_text(
        "def _private_case() -> None:\n    pass\n", encoding="utf-8"
    )

    original_roots = checker_globals["CHECK_ROOTS"]
    checker_globals["CHECK_ROOTS"] = (migrations_root, scripts_root, tests_root)
    try:
        assert check_main() == 1
    finally:
        checker_globals["CHECK_ROOTS"] = original_roots

    output = capsys.readouterr().err
    assert "function 'upgrade' needs a Chinese summary" in output
    assert "function 'seed' needs a typed Chinese :return entry" in output
    assert "function '_private_case' needs a Chinese summary" in output
