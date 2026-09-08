from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "agent_remote_server"
MIGRATIONS_ROOT = REPO_ROOT / "migrations"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
CHECK_ROOTS = (SRC_ROOT, MIGRATIONS_ROOT, SCRIPTS_ROOT)
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
DOCSTRING_DIRECTIVES = (":param ", ":return ", ":raises ")
PARAM_DIRECTIVE_RE = re.compile(
    r"^:param (?P<name>[A-Za-z_][A-Za-z0-9_]*) \((?P<type>[^)]+)\): (?P<description>.+)$"
)
RETURN_DIRECTIVE_RE = re.compile(r"^:return (?P<type>[^:]+): (?P<description>.+)$")
RAISES_DIRECTIVE_RE = re.compile(r"^:raises (?P<type>[^:]+): (?P<description>.+)$")
TRIPLE_DOUBLE_QUOTE_RE = re.compile(r'^[rubfRUBF]*"""')
MIXED_ENGLISH_PREFIX_RE = re.compile(
    r"^(?:binding|bindings|session|sessions|workspace|workspaces|generation|relay|ticket|"
    r"profile|runtime|challenge|peer|allowlist|revision|result|response|data|list|status|"
    r"type|mode|kind|path|role|platform|credential|proof|secret|command|attach|token)"
    r"\s+[\u4e00-\u9fff]{1,4}[。.!！?？]?$",
    re.IGNORECASE,
)
ENGLISH_PROSE_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "for",
        "invalid",
        "is",
        "missing",
        "name",
        "or",
        "prose",
        "render",
        "result",
        "returns",
        "the",
        "this",
    }
)


def has_chinese(text: str | None) -> bool:
    """
    判断文本是否包含中文字符

    :param text (str): 待检查文本

    :return bool: 是否包含中文字符
    """

    return bool(text and CHINESE_RE.search(text))


def has_chinese_prose(text: str | None) -> bool:
    """
    判断文本是否为中文业务说明而非英文词语拼接。

    :param text (str): 待检查的说明文本

    :return bool: 文本包含中文且未命中英文业务短语时为 True
    """

    if not has_chinese(text):
        return False
    assert text is not None
    normalized = text.strip()
    if MIXED_ENGLISH_PREFIX_RE.fullmatch(normalized):
        return False
    prose_words = {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", normalized)}
    return len(prose_words & ENGLISH_PROSE_WORDS) < 2


def public_name(name: str) -> bool:
    """
    判断名称是否属于公开符号

    :param name (str): Python 符号名称

    :return bool: 是否公开
    """

    return not name.startswith("_")


def base_name(node: ast.expr) -> str | None:
    """
    提取类继承表达式的末级名称

    :param node (ast.expr): 类继承表达式

    :return str | None: 可用于本地继承关系解析的基类名称
    """

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return base_name(node.value)
    return None


def pydantic_model_names(tree: ast.Module) -> set[str]:
    """
    解析当前模块内直接或间接继承 Pydantic 的模型名称

    :param tree (ast.Module): Python 模块语法树

    :return set[str]: 当前模块内的 Pydantic 模型名称
    """

    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    model_names = {"BaseModel", "BaseSettings"}
    changed = True
    while changed:
        changed = False
        for class_node in classes:
            if class_node.name in model_names:
                continue
            if any(base_name(base) in model_names for base in class_node.bases):
                model_names.add(class_node.name)
                changed = True
    return model_names


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


def non_chinese_directives(docstring: str | None) -> list[str]:
    """
    返回说明文本不是中文的文档指令

    :param docstring (str | None): 待检查的文档字符串

    :return list: 缺少中文说明的文档指令
    """

    if docstring is None:
        return []
    invalid: list[str] = []
    patterns = (PARAM_DIRECTIVE_RE, RETURN_DIRECTIVE_RE, RAISES_DIRECTIVE_RE)
    for line in docstring.splitlines():
        directive = line.strip()
        if not directive.startswith(DOCSTRING_DIRECTIVES):
            continue
        match: re.Match[str] | None = None
        for pattern in patterns:
            match = pattern.fullmatch(directive)
            if match is not None:
                break
        if match is None or not has_chinese_prose(match.group("description")):
            invalid.append(directive)
    return invalid


def docstring_summary(docstring: str | None) -> str | None:
    """
    提取文档字符串的首个非空叙述行

    :param docstring (str | None): 待检查的文档字符串

    :return str | None: 文档摘要；文档字符串为空时为 None
    """

    if docstring is None:
        return None
    return next((line.strip() for line in docstring.splitlines() if line.strip()), None)


def documented_parameters(docstring: str | None) -> set[str]:
    """
    提取符合项目格式的参数说明名称

    :param docstring (str | None): 待检查的文档字符串

    :return set[str]: 已按规范记录的参数名称
    """

    if docstring is None:
        return set()
    return {
        match.group("name")
        for line in docstring.splitlines()
        if (match := PARAM_DIRECTIVE_RE.fullmatch(line.strip())) is not None
    }


def parameter_directive_names(docstring: str | None) -> list[str]:
    """
    按出现顺序提取文档中的参数说明名称。

    :param docstring (str): 待检查的文档字符串

    :return list[str]: 参数说明名称；无法解析的指令不会出现在结果中
    """

    if docstring is None:
        return []
    names: list[str] = []
    for line in docstring.splitlines():
        match = PARAM_DIRECTIVE_RE.fullmatch(line.strip())
        if match is not None:
            names.append(match.group("name"))
    return names


def function_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """
    返回公共函数需要记录的参数名称

    :param node (ast.FunctionDef | ast.AsyncFunctionDef): 函数定义节点

    :return list[str]: 除 self 与 cls 外的参数名称
    """

    arguments = [
        *node.args.posonlyargs,
        *node.args.args,
        *node.args.kwonlyargs,
    ]
    names = [argument.arg for argument in arguments if argument.arg not in {"self", "cls"}]
    if node.args.vararg is not None:
        names.append(node.args.vararg.arg)
    if node.args.kwarg is not None:
        names.append(node.args.kwarg.arg)
    return names


def returns_value(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """
    判断公开函数的类型合同是否返回值

    :param node (ast.FunctionDef | ast.AsyncFunctionDef): 函数定义节点

    :return bool: 返回注解是否表示一个值
    """

    annotation = node.returns
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and annotation.value is None:
        return False
    return not (isinstance(annotation, ast.Name) and annotation.id in {"None", "Never", "NoReturn"})


def uses_triple_double_quotes(source: str, node: ast.AST) -> bool:
    """
    判断定义的文档字符串是否使用三重双引号

    :param source (str): Python 源码

    :param node (ast.AST): 类或函数定义节点

    :return bool: 文档字符串是否使用三重双引号
    """

    body = getattr(node, "body", None)
    if not body or not isinstance(body[0], ast.Expr):
        return False
    expression = ast.get_source_segment(source, body[0].value)
    return bool(expression and TRIPLE_DOUBLE_QUOTE_RE.match(expression.lstrip()))


def check_file(path: Path) -> list[str]:
    """
    检查单个 Python 文件的文档规范

    :param path (Path): Python 文件路径

    :return list: 错误信息列表
    """

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    pydantic_models = pydantic_model_names(tree)
    errors: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and public_name(node.name):
            if not has_chinese_prose(docstring_summary(ast.get_docstring(node))):
                errors.append(
                    f"{path}:{node.lineno}: public class '{node.name}' needs a Chinese summary"
                )
            elif not uses_triple_double_quotes(source, node):
                errors.append(
                    f"{path}:{node.lineno}: public class '{node.name}' needs a "
                    "triple-double-quoted docstring"
                )

            if node.name in pydantic_models:
                for statement in node.body:
                    if not isinstance(statement, ast.AnnAssign):
                        continue
                    if not isinstance(statement.target, ast.Name):
                        continue
                    field_name = statement.target.id
                    if field_name.startswith("_") or field_name == "model_config":
                        continue
                    description = field_description(statement.value) if statement.value else None
                    if not has_chinese_prose(description):
                        errors.append(
                            f"{path}:{statement.lineno}: Pydantic field '{node.name}.{field_name}' "
                            "needs Field(..., description='中文描述')"
                        )

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and public_name(node.name):
            docstring = ast.get_docstring(node)
            if not has_chinese_prose(docstring_summary(docstring)):
                errors.append(
                    f"{path}:{node.lineno}: public function '{node.name}' needs a Chinese summary"
                )
            elif not uses_triple_double_quotes(source, node):
                errors.append(
                    f"{path}:{node.lineno}: public function '{node.name}' needs a "
                    "triple-double-quoted docstring"
                )
            parameters = function_parameters(node)
            documented_names = parameter_directive_names(docstring)
            documented = set(documented_names)
            for parameter in parameters:
                if parameter not in documented:
                    errors.append(
                        f"{path}:{node.lineno}: public function '{node.name}' needs a typed "
                        f"Chinese :param entry for '{parameter}'"
                    )
            for parameter in sorted(set(documented_names) - set(parameters)):
                errors.append(
                    f"{path}:{node.lineno}: public function '{node.name}' has an unknown "
                    f":param entry for '{parameter}'"
                )
            seen_parameters: set[str] = set()
            for parameter in documented_names:
                if parameter in seen_parameters:
                    errors.append(
                        f"{path}:{node.lineno}: public function '{node.name}' has a duplicate "
                        f":param entry for '{parameter}'"
                    )
                seen_parameters.add(parameter)
            if returns_value(node) and not any(
                RETURN_DIRECTIVE_RE.fullmatch(line.strip())
                for line in (docstring or "").splitlines()
            ):
                errors.append(
                    f"{path}:{node.lineno}: public function '{node.name}' needs a "
                    "typed Chinese :return entry"
                )
            for directive in non_chinese_directives(docstring):
                errors.append(
                    f"{path}:{node.lineno}: public function '{node.name}' has a docstring "
                    f"directive without a Chinese description: {directive}"
                )

    return errors


def main() -> int:
    """
    执行源码文档规范检查

    :return int: 进程退出码
    """

    errors: list[str] = []
    for root in CHECK_ROOTS:
        for path in sorted(root.rglob("*.py")):
            errors.extend(check_file(path))

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    print("Docstring checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
