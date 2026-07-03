from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "agent_remote_server"
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")


def has_chinese(text: str | None) -> bool:
    """
    判断文本是否包含中文字符

    :param text (str): 待检查文本

    :return bool: 是否包含中文字符
    """

    return bool(text and CHINESE_RE.search(text))


def public_name(name: str) -> bool:
    """
    判断名称是否属于公开符号

    :param name (str): Python 符号名称

    :return bool: 是否公开
    """

    return not name.startswith("_")


def is_pydantic_model(node: ast.ClassDef) -> bool:
    """
    判断类是否继承 Pydantic 基类

    :param node (ast.ClassDef): 类定义节点

    :return bool: 是否为 Pydantic 模型或配置类
    """

    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in {"BaseModel", "BaseSettings"}:
            return True
        if isinstance(base, ast.Attribute) and base.attr in {"BaseModel", "BaseSettings"}:
            return True
    return False


def field_description(node: ast.AST) -> str | None:
    """
    提取 Field 调用中的 description

    :param node (ast.AST): 字段赋值节点

    :return str: description 文本
    """

    if not isinstance(node, ast.Call):
        return None

    function = node.func
    is_field = isinstance(function, ast.Name) and function.id == "Field"
    is_field_attr = isinstance(function, ast.Attribute) and function.attr == "Field"
    if not (is_field or is_field_attr):
        return None

    for keyword in node.keywords:
        if (
            keyword.arg == "description"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return None


def check_file(path: Path) -> list[str]:
    """
    检查单个 Python 文件的文档规范

    :param path (Path): Python 文件路径

    :return list: 错误信息列表
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    errors: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and public_name(node.name):
            if not has_chinese(ast.get_docstring(node)):
                errors.append(
                    f"{path}:{node.lineno}: public class '{node.name}' needs a Chinese docstring"
                )

            if is_pydantic_model(node):
                for statement in node.body:
                    if not isinstance(statement, ast.AnnAssign):
                        continue
                    if not isinstance(statement.target, ast.Name):
                        continue
                    field_name = statement.target.id
                    if field_name.startswith("_") or field_name == "model_config":
                        continue
                    description = field_description(statement.value) if statement.value else None
                    if not has_chinese(description):
                        errors.append(
                            f"{path}:{statement.lineno}: Pydantic field '{node.name}.{field_name}' "
                            "needs Field(..., description='中文描述')"
                        )

        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and public_name(node.name)
            and not has_chinese(ast.get_docstring(node))
        ):
            errors.append(
                f"{path}:{node.lineno}: public function '{node.name}' needs a Chinese docstring"
            )

    return errors


def main() -> int:
    """
    执行源码文档规范检查

    :return int: 进程退出码
    """

    errors: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        errors.extend(check_file(path))

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    print("Docstring checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
