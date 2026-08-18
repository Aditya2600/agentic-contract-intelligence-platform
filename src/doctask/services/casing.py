"""snake_case -> camelCase for API responses.

Every domain dataclass is snake_case, matching the database columns it mirrors.
The web client's TypeScript types are camelCase, matching JS convention. This is
applied once, at the response boundary, so the domain layer never has to think
about it and every route that serializes a dataclass stays consistent with every
other one.
"""

from __future__ import annotations

from typing import Any


def _to_camel(key: str) -> str:
    head, *tail = key.split("_")
    return head + "".join(word.capitalize() for word in tail)


def camelize(value: Any) -> Any:
    if isinstance(value, dict):
        return {_to_camel(k) if isinstance(k, str) else k: camelize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [camelize(v) for v in value]
    return value
