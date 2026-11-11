"""Tool definitions for the MCP layer. Plain data + sync callables: no MCP SDK import here."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool.

    ``handler(mcp_site, request, arguments) -> dict`` runs synchronously in a Django request
    context (``request.user``, ``request.is_api``, ``request.is_mcp``) and raises
    :class:`admin_api.errors.ApiError` on failure.
    """

    name: str
    title: str
    description: str
    input_schema: dict
    handler: Callable[..., dict]
    output_schema: dict | None = None
    writes: bool = False
    annotations: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    """What a tool call returns: ``data`` is the structured result (or the error envelope)."""

    data: dict
    is_error: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"data": self.data, "is_error": self.is_error}


def obj(properties: dict, required=(), *, additional=False, description=None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": additional}
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


MODEL_ARG = {
    "type": "string",
    "description": "Model exactly as returned by list_models: 'app_label.model_name' (e.g. 'store.sales'), "
    "or a nested path 'app_label.parent/child' for models listed with nested_under (e.g. 'store.client/sales').",
}
PARENTS_ARG = {
    "type": "array",
    "items": {"type": ["integer", "string"]},
    "description": "Nested models only: the pk of each parent object, outermost first, one per 'nested_under' "
    "entry (e.g. model 'store.client/sales/salesitem' with parents [client_pk, sales_pk]).",
}
PK_ARG = {"type": ["integer", "string"], "description": "Primary key of the object."}
DATA_ARG = {
    "type": "object",
    "description": "Field values, matching describe_model's input_schema. Inline rows go under 'inlines'.",
}
DRY_RUN_ARG = {
    "type": "boolean",
    "default": False,
    "description": "Validate and run the full save pipeline, then roll everything back. "
    "Use it to check a write before doing it.",
}
FILTERS_ARG = {
    "type": "object",
    "additionalProperties": {"type": ["string", "integer", "number", "boolean", "array"]},
    "description": "Admin changelist filter parameters as listed in describe_model's filters[].choices[].params "
    "(e.g. {'status__exact': 'posted', 'client__id__exact': 2}). Arrays repeat the parameter.",
}

# Output schemas are permissive on purpose: the shape depends on each admin's configuration.
ANY_OBJECT = {"type": "object", "additionalProperties": True}
