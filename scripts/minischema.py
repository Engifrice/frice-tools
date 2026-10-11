"""A small JSON Schema validator for the subset used by schema/tool.schema.json.

Supported keywords: type, enum, const, pattern, minLength, minimum, properties,
required, additionalProperties, patternProperties, minProperties, items,
minItems, uniqueItems, oneOf and local $ref ("#/...").
Annotation keywords ($schema, $id, $comment, $defs, title, description,
default, examples) are accepted and ignored.

Any other keyword makes the schema itself invalid (SchemaError), so the schema
can never silently depend on a check this validator does not perform.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ANNOTATIONS = frozenset(
    {"$schema", "$id", "$comment", "$defs", "title", "description", "default", "examples"}
)
CHECKS = frozenset(
    {
        "type",
        "enum",
        "const",
        "pattern",
        "minLength",
        "minimum",
        "properties",
        "required",
        "additionalProperties",
        "patternProperties",
        "minProperties",
        "items",
        "minItems",
        "uniqueItems",
        "oneOf",
        "$ref",
    }
)


class SchemaError(Exception):
    """The schema uses something this validator does not support, or is malformed."""


def _type_name(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _is_type(value: object, expected: str) -> bool:
    actual = _type_name(value)
    if expected == "number":
        return actual in ("integer", "number")
    return actual == expected


def _canonical(value: object) -> str:
    """A string that is equal for equal JSON values and distinguishes 1 from true."""
    return json.dumps(value, sort_keys=True, default=str)


def _join(path: str, key: str) -> str:
    return key if not path else f"{path}.{key}"


class Validator:
    """Validates instances against one schema document."""

    def __init__(self, schema: dict):
        self.root = schema
        self._regexes: dict[str, re.Pattern[str]] = {}
        self._check_schema(schema, "#")

    @classmethod
    def from_file(cls, path: Path) -> "Validator":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # Schema checking --------------------------------------------------------

    def _regex(self, pattern: str) -> re.Pattern[str]:
        compiled = self._regexes.get(pattern)
        if compiled is None:
            try:
                compiled = re.compile(pattern)
            except re.error as error:
                raise SchemaError(f"invalid pattern {pattern!r}: {error}") from error
            self._regexes[pattern] = compiled
        return compiled

    def _resolve(self, ref: str) -> object:
        if not ref.startswith("#/"):
            raise SchemaError(f"only local references are supported: {ref!r}")
        node: object = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SchemaError(f"unresolved reference {ref!r}")
            node = node[part]
        return node

    def _check_schema(self, node: object, where: str) -> None:
        if isinstance(node, bool):
            return
        if not isinstance(node, dict):
            raise SchemaError(f"{where}: a schema must be an object or a boolean")
        unknown = set(node) - ANNOTATIONS - CHECKS
        if unknown:
            raise SchemaError(f"{where}: unsupported keywords {sorted(unknown)}")
        for key in ("properties", "patternProperties", "$defs"):
            for name, sub in node.get(key, {}).items():
                self._check_schema(sub, f"{where}/{key}/{name}")
        for pattern in node.get("patternProperties", {}):
            self._regex(pattern)
        if "pattern" in node:
            self._regex(node["pattern"])
        for key in ("items", "additionalProperties"):
            if key in node:
                self._check_schema(node[key], f"{where}/{key}")
        for index, sub in enumerate(node.get("oneOf", [])):
            self._check_schema(sub, f"{where}/oneOf/{index}")
        if "$ref" in node:
            self._resolve(node["$ref"])

    # Validation -------------------------------------------------------------

    def validate(self, instance: object) -> list[tuple[str, str]]:
        """Return (path, message) pairs; an empty list means the instance is valid."""
        errors: list[tuple[str, str]] = []
        self._validate(instance, self.root, "", errors)
        return errors

    def _validate(self, value: object, schema: object, path: str, errors: list) -> None:
        if schema is True:
            return
        if schema is False:
            errors.append((path, "is not allowed"))
            return
        assert isinstance(schema, dict)

        if "$ref" in schema:
            self._validate(value, self._resolve(schema["$ref"]), path, errors)

        if "type" in schema:
            expected = schema["type"]
            expected_list = expected if isinstance(expected, list) else [expected]
            if not any(_is_type(value, name) for name in expected_list):
                wanted = " or ".join(expected_list)
                errors.append((path, f"must be {wanted}, not {_type_name(value)}"))
                return

        if "const" in schema and _canonical(value) != _canonical(schema["const"]):
            errors.append((path, f"must be {json.dumps(schema['const'])}"))

        if "enum" in schema and _canonical(value) not in {_canonical(e) for e in schema["enum"]}:
            allowed = ", ".join(json.dumps(e) for e in schema["enum"])
            errors.append((path, f"must be one of {allowed}"))

        if isinstance(value, str):
            minimum_length = schema.get("minLength")
            if minimum_length is not None and len(value) < minimum_length:
                if minimum_length == 1:
                    errors.append((path, "must not be empty"))
                else:
                    errors.append((path, f"must be at least {minimum_length} characters"))
            pattern = schema.get("pattern")
            if pattern is not None and not self._regex(pattern).search(value):
                errors.append((path, f"{json.dumps(value)} does not match {pattern}"))

        if _type_name(value) in ("integer", "number") and "minimum" in schema:
            if value < schema["minimum"]:  # type: ignore[operator]
                errors.append((path, f"must be at least {schema['minimum']}"))

        if isinstance(value, dict):
            self._validate_object(value, schema, path, errors)

        if isinstance(value, list):
            self._validate_array(value, schema, path, errors)

        if "oneOf" in schema:
            branches: list[list] = []
            for sub in schema["oneOf"]:
                branch_errors: list = []
                self._validate(value, sub, path, branch_errors)
                branches.append(branch_errors)
            passing = sum(1 for branch in branches if not branch)
            if passing == 0:
                # Report the branch that came closest: its errors are the useful ones.
                errors.extend(min(branches, key=len))
            elif passing > 1:
                errors.append((path, "matches more than one allowed form"))

    def _validate_object(self, value: dict, schema: dict, path: str, errors: list) -> None:
        for key in schema.get("required", []):
            if key not in value:
                errors.append((path, f"missing required key '{key}'"))

        minimum_properties = schema.get("minProperties")
        if minimum_properties is not None and len(value) < minimum_properties:
            if minimum_properties == 1:
                errors.append((path, "must not be empty"))
            else:
                errors.append((path, f"must have at least {minimum_properties} keys"))

        properties = schema.get("properties", {})
        pattern_properties = schema.get("patternProperties", {})
        additional = schema.get("additionalProperties", True)
        for key in sorted(value):
            child = _join(path, key)
            matched = False
            if key in properties:
                self._validate(value[key], properties[key], child, errors)
                matched = True
            for pattern, sub in pattern_properties.items():
                if self._regex(pattern).search(key):
                    self._validate(value[key], sub, child, errors)
                    matched = True
            if not matched:
                if additional is False:
                    errors.append((child, "unknown key"))
                elif isinstance(additional, dict):
                    self._validate(value[key], additional, child, errors)

    def _validate_array(self, value: list, schema: dict, path: str, errors: list) -> None:
        minimum_items = schema.get("minItems")
        if minimum_items is not None and len(value) < minimum_items:
            if minimum_items == 1:
                errors.append((path, "must not be empty"))
            else:
                errors.append((path, f"must have at least {minimum_items} items"))
        if schema.get("uniqueItems"):
            seen: set[str] = set()
            for item in value:
                canonical = _canonical(item)
                if canonical in seen:
                    errors.append((path, f"contains {json.dumps(item, default=str)} more than once"))
                    break
                seen.add(canonical)
        if "items" in schema:
            for index, item in enumerate(value):
                self._validate(item, schema["items"], f"{path}[{index}]", errors)
