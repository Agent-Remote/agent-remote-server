# 05 Comment Style

## Public Docstrings

Every public class, method, and function in `src/` must have a Chinese docstring.

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

- Use triple-quoted `"""` docstrings.
- The summary and descriptions must be Chinese.
- Include `:param` entries when parameters are not self-evident.
- Include `:return` when a value is returned.
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

