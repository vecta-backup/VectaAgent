"""Hook parameter and capability schema validation."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vecta_agent.hooks import HookDefinition


class HookCatalogError(ValueError):
    """The local hook catalog is unsafe, malformed, or inconsistent."""


def _validate_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise HookCatalogError("parameters_schema must be an object schema.")
    if set(schema) - {"type", "properties", "required", "additionalProperties"}:
        raise HookCatalogError("parameters_schema contains unsupported schema keywords.")
    if schema.get("additionalProperties", False) is not False:
        raise HookCatalogError("Hook parameters must reject additional properties.")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or len(properties) > 32 or not isinstance(required, list):
        raise HookCatalogError("parameters_schema properties/required are malformed.")
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key) for key in properties):
        raise HookCatalogError("Hook parameter names are invalid.")
    if any(any(word in key.lower() for word in ("password", "secret", "token", "credential")) for key in properties):
        raise HookCatalogError("Hook parameter schemas must not request credentials or secrets.")
    if any(not isinstance(key, str) for key in required) or any(key not in properties for key in required) or len(set(required)) != len(required):
        raise HookCatalogError("Hook required parameters must name unique declared properties.")
    for key, prop in properties.items():
        if not isinstance(prop, dict) or set(prop) - {"type", "enum", "minLength", "maxLength", "minimum", "maximum", "pattern"}:
            raise HookCatalogError(f"Unsupported schema for hook parameter {key!r}.")
        kind = prop.get("type")
        if kind not in {"string", "integer", "boolean"}:
            raise HookCatalogError(f"Unsupported type for hook parameter {key!r}.")
        if "enum" in prop and (
            not isinstance(prop["enum"], list)
            or not prop["enum"]
            or len(prop["enum"]) > 64
            or any(not isinstance(value, (str, int, float, bool)) for value in prop["enum"])
            or any(isinstance(value, str) and (len(value) > 128 or "/" in value or "\\" in value) for value in prop["enum"])
        ):
            raise HookCatalogError(f"Invalid enum for hook parameter {key!r}.")
        for field in ("minLength", "maxLength"):
            bound = prop.get(field)
            if bound is not None and (kind != "string" or isinstance(bound, bool) or not isinstance(bound, int) or not 0 <= bound <= 1024):
                raise HookCatalogError(f"Invalid string length bound for hook parameter {key!r}.")
        for field in ("minimum", "maximum"):
            bound = prop.get(field)
            if bound is not None and (kind != "integer" or isinstance(bound, bool) or not isinstance(bound, int)):
                raise HookCatalogError(f"Invalid integer bound for hook parameter {key!r}.")
        if "minLength" in prop and "maxLength" in prop and prop["minLength"] > prop["maxLength"]:
            raise HookCatalogError(f"Reversed string length bounds for hook parameter {key!r}.")
        if "minimum" in prop and "maximum" in prop and prop["minimum"] > prop["maximum"]:
            raise HookCatalogError(f"Reversed integer bounds for hook parameter {key!r}.")
        if "pattern" in prop:
            if kind != "string" or not isinstance(prop["pattern"], str) or len(prop["pattern"]) > 128:
                raise HookCatalogError(f"Invalid pattern for hook parameter {key!r}.")
            try:
                re.compile(prop["pattern"])
            except (TypeError, re.error) as exc:
                raise HookCatalogError(f"Invalid pattern for hook parameter {key!r}.") from exc
    if len(json.dumps(schema, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 8192:
        raise HookCatalogError("parameters_schema must be at most 8 KiB.")
    return schema


def validate_parameters(schema: dict[str, Any], parameters: Any) -> dict[str, Any]:
    if not isinstance(parameters, dict):
        raise HookCatalogError("Hook parameters must be an object.")
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    if set(parameters) - set(properties) or required - set(parameters):
        raise HookCatalogError("Hook parameters do not match the local catalog schema.")
    result: dict[str, Any] = {}
    for key, value in parameters.items():
        spec = properties[key]
        kind = spec["type"]
        if kind == "string":
            valid = isinstance(value, str) and "\x00" not in value and len(value) <= spec.get("maxLength", 1024) and len(value) >= spec.get("minLength", 0)
            if valid and "pattern" in spec:
                valid = re.fullmatch(spec["pattern"], value) is not None
        elif kind == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool) and spec.get("minimum", value) <= value <= spec.get("maximum", value)
        else:
            valid = isinstance(value, bool)
        if "enum" in spec:
            valid = valid and value in spec["enum"]
        if not valid:
            raise HookCatalogError(f"Hook parameter {key!r} does not match its declared schema.")
        result[key] = value
    return result


def capability_metadata(catalog: dict[str, HookDefinition]) -> list[dict[str, Any]]:
    metadata = [catalog[key].public_metadata() for key in sorted(catalog)]
    if len(metadata) > 100 or len(json.dumps(metadata, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 250 * 1024:
        raise HookCatalogError("Hook capability metadata exceeds the backend report limits.")
    return metadata
