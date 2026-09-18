# 05 Comment Style

## Docstrings

Every Python module, class, method, and function in `src/`, `migrations/`, `scripts/`, and `tests/`
must have a concise Chinese multiline docstring. This includes private and nested definitions,
test functions, fixtures, BaseModel classes, and dataclasses.

Docstring structure:

```python
def example(name: str) -> str:
    """
    生成示例文本

    :param name (str): 名称

    :return str: 示例文本
    """
```

Rules:

- Put the opening and closing triple-double-quote delimiters on standalone lines.
- Do not use single-line docstrings, including module, BaseModel, dataclass, test, and private
  function docstrings.
- The summary and descriptions must be Chinese.
- Repeat the annotated value type and a Chinese description in one `:param` entry for every
  parameter except `self` and `cls`; for `Annotated`, use its underlying value type.
- Repeat every return annotation except `None`, `Never`, and `NoReturn`, plus a Chinese
  description, in one `:return` entry.
- Include `:raises` when the function intentionally raises a documented exception.
- Do not include usage examples in docstrings.

## Pydantic Field Descriptions

Every Pydantic model and settings field must use `Field(..., description="中文描述")`.

Example:

```python
class HealthResponse(BaseModel):
    """
    健康检查响应
    """

    status: Literal["ok", "degraded"] = Field(..., description="整体健康状态")
```

## Inline Comments

Default to no inline comments. Add comments only when the reason is not obvious:

- A security trade-off.
- A compatibility workaround.
- A dependency-specific behavior.
- A non-obvious failure handling choice.

Do not write comments that merely repeat the code.
