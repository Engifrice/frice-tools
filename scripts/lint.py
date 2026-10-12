"""Check the repository: platforms.toml, toolset.toml and every tool.toml.

Each tool.toml is first validated against schema/tool.schema.json. Tools that
pass get the semantic checks the schema cannot express (declared platforms,
sources per platform, smoke tests, dependencies). The result is a sorted list
of problems; an empty list means the repository is consistent.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from common import (
    BUILD_SCRIPT,
    LICENSES_DIR,
    NAME_RE,
    OS_RE,
    PLATFORM_RE,
    SMOKE_DIR,
    SMOKE_PLACEHOLDERS,
    TOOLSET_VERSION_RE,
    Problem,
    display_path,
    effective_build,
    load_toml,
    visible_files,
)
from discover import Tool, discover, order_by_dependencies
from minischema import SchemaError, Validator

PLATFORMS_FILE = "platforms.toml"
TOOLSET_FILE = "toolset.toml"
SCHEMA_FILE = "schema/tool.schema.json"

PLATFORM_REQUIRED = ("os", "isa", "runner", "exe_suffix", "lib_prefix", "lib_suffix")
PLATFORM_OPTIONAL = ("container",)
ZERO_SHA256 = "0" * 64
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
SMOKE_FILE_RE = re.compile(r"\{smoke\}/([A-Za-z0-9._+@/-]+)")
SOURCE_KINDS = ("url", "git")


class _Reporter:
    """Collects problems for one file."""

    def __init__(self, problems: list[Problem], path: str):
        self.problems = problems
        self.path = path

    def __call__(self, message: str) -> None:
        self.problems.append(Problem(self.path, message))


def _load(root: Path, relative: str, problems: list[Problem]) -> dict | None:
    path = root / relative
    try:
        return load_toml(path)
    except FileNotFoundError:
        problems.append(Problem(relative, "file not found"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        problems.append(Problem(relative, f"invalid TOML: {error}"))
    return None


# platforms.toml ---------------------------------------------------------------


def check_platforms(data: dict, report: _Reporter) -> dict[str, dict]:
    """Validate platforms.toml and return the well-formed platforms by token."""
    for key in sorted(set(data) - {"schema", "platforms"}):
        report(f"unknown key '{key}'")
    if data.get("schema") != 1:
        report("schema must be 1")

    table = data.get("platforms")
    if not isinstance(table, dict) or not table:
        report("[platforms] must define at least one platform")
        return {}

    platforms: dict[str, dict] = {}
    for token in sorted(table):
        entry = table[token]
        where = f"platforms.{token}"
        match = PLATFORM_RE.match(token)
        if not match:
            report(
                f"{where}: '{token}' is not <os>-<isa> with os in windows|linux|macos and isa in x86_64|arm64"
            )
            continue
        if not isinstance(entry, dict):
            report(f"{where}: must be a table")
            continue
        ok = True
        for key in sorted(set(entry) - set(PLATFORM_REQUIRED) - set(PLATFORM_OPTIONAL)):
            report(f"{where}.{key}: unknown key")
            ok = False
        for key in PLATFORM_REQUIRED + PLATFORM_OPTIONAL:
            if key in entry and not isinstance(entry[key], str):
                report(f"{where}.{key}: must be a string")
                ok = False
        for key in PLATFORM_REQUIRED:
            if key not in entry:
                report(f"{where}: missing required key '{key}'")
                ok = False
        os_name, isa = match.group(1), match.group(2)
        if isinstance(entry.get("os"), str) and entry["os"] != os_name:
            report(f'{where}.os: must be "{os_name}" to match the token')
            ok = False
        if isinstance(entry.get("isa"), str) and entry["isa"] != isa:
            report(f'{where}.isa: must be "{isa}" to match the token')
            ok = False
        for key in ("runner", "container"):
            if entry.get(key) == "":
                report(f"{where}.{key}: must not be empty")
                ok = False
        if ok:
            platforms[token] = entry
    return platforms


# toolset.toml -----------------------------------------------------------------


def check_toolset(data: dict, report: _Reporter) -> list[str]:
    """Validate toolset.toml and return the valid names in 'removed'."""
    for key in sorted(set(data) - {"schema", "version", "removed"}):
        report(f"unknown key '{key}'")
    if data.get("schema") != 1:
        report("schema must be 1")

    version = data.get("version")
    if not isinstance(version, str) or not TOOLSET_VERSION_RE.match(version):
        report(
            f'version must be YYYY.MM.N (for example "2026.10.0"), found {version!r}'
        )

    removed = data.get("removed", [])
    if not isinstance(removed, list):
        report("removed must be an array of tool names")
        return []
    names: list[str] = []
    for index, name in enumerate(removed):
        if not isinstance(name, str) or not NAME_RE.match(name):
            report(f"removed[{index}]: {name!r} is not a valid tool name")
        elif name in names:
            report(f"removed: '{name}' is listed more than once")
        else:
            names.append(name)
    return names


# tool.toml --------------------------------------------------------------------


def is_single_source(source: dict) -> bool:
    """True for one source shared by all platforms, False for per-platform tables."""
    return any(kind in source for kind in SOURCE_KINDS)


def source_tables(spec: dict) -> list[tuple[str, dict]]:
    """(key path, table) for every source table in a tool spec."""
    source = spec.get("source")
    if source is None:
        return []
    if is_single_source(source):
        return [("source", source)]
    tables = []
    for key in sorted(source):
        value = source[key]
        if isinstance(value, list):
            tables.extend(
                (f"source.{key}[{index}]", table) for index, table in enumerate(value)
            )
        else:
            tables.append((f"source.{key}", value))
    return tables


def check_tool(tool: Tool, platforms: dict[str, dict], report: _Reporter) -> None:
    """Semantic checks for one schema-valid tool."""
    spec = tool.spec
    declared = spec["platforms"]

    for token in declared:
        if token not in platforms:
            report(f"platforms: '{token}' is not defined in {PLATFORMS_FILE}")

    # Build overrides only for declared platforms and their operating systems.
    declared_os = {token.split("-", 1)[0] for token in declared}
    for key in sorted(spec["build"]):
        if PLATFORM_RE.match(key) and key not in declared:
            report(f"build.{key}: override for a platform the tool does not declare")
        elif OS_RE.match(key) and key not in declared_os:
            report(f"build.{key}: override for an OS none of the tool's platforms use")

    # Sources.
    source = spec.get("source")
    if source is not None and not is_single_source(source):
        for key in sorted(source):
            if key != "src" and key not in declared:
                report(f"source.{key}: source for a platform the tool does not declare")
    for where, table in source_tables(spec):
        if table.get("sha256") == ZERO_SHA256:
            report(
                f"{where}.sha256: placeholder hash; set the real sha256 of the download"
            )
        if ("sig_url" in table) != ("pgp_fingerprint" in table):
            report(f"{where}: sig_url and pgp_fingerprint must be given together")

    # Strategy per platform.
    strategies: dict[str, str] = {}
    for token in declared:
        build = effective_build(spec, token)
        strategy = build["strategy"]
        strategies[token] = strategy
        if strategy == "script":
            continue
        has_source = source is not None and (
            is_single_source(source) or token in source
        )
        if not has_source:
            report(f'source: no source for {token} (strategy "{strategy}")')
        if strategy == "repackage" and "pick" not in build:
            report(
                f"build: strategy \"repackage\" on {token} needs 'pick' to choose the shipped files"
            )

    has_script = (tool.directory / BUILD_SCRIPT).is_file()
    uses_script = sorted(t for t, s in strategies.items() if s == "script")
    if uses_script and not has_script:
        report(
            f"build: strategy \"script\" on {', '.join(uses_script)} needs {BUILD_SCRIPT} next to tool.toml"
        )
    if has_script and not uses_script:
        report(f'{BUILD_SCRIPT} exists but no platform uses strategy "script"')

    # License texts.
    licenses = tool.directory / LICENSES_DIR
    if not licenses.is_dir():
        report(
            f"{LICENSES_DIR}/ is missing; add the license texts of everything the tool ships"
        )
    else:
        if not visible_files(licenses):
            report(
                f"{LICENSES_DIR}/ is empty; add the license texts of everything the tool ships"
            )
        for entry in sorted(licenses.iterdir()):
            if entry.is_dir():
                report(f"{LICENSES_DIR}/{entry.name}: subdirectories are not allowed")

    # Entry points.
    executables = spec.get("executables", {})
    libraries = spec.get("libraries", {})
    if not executables and not libraries:
        report("a tool must declare at least one entry in [executables] or [libraries]")

    # Smoke tests.
    for index, smoke in enumerate(spec.get("smoke", [])):
        where = f"smoke[{index}]"
        if smoke["entrypoint"] not in executables:
            report(
                f"{where}.entrypoint: '{smoke['entrypoint']}' is not a key of [executables]"
            )
        for token in smoke.get("platforms", []):
            if token not in declared:
                report(
                    f"{where}.platforms: '{token}' is not one of the tool's platforms"
                )
        for arg_index, arg in enumerate(smoke.get("args", [])):
            for placeholder in PLACEHOLDER_RE.findall(arg):
                if placeholder not in SMOKE_PLACEHOLDERS:
                    allowed = ", ".join("{" + p + "}" for p in SMOKE_PLACEHOLDERS)
                    report(
                        f"{where}.args[{arg_index}]: unknown placeholder {{{placeholder}}} (allowed: {allowed})"
                    )
            for relative in SMOKE_FILE_RE.findall(arg):
                if (
                    ".." in relative.split("/")
                    or not (tool.directory / SMOKE_DIR / relative).is_file()
                ):
                    report(
                        f"{where}.args[{arg_index}]: {SMOKE_DIR}/{relative} not found next to tool.toml"
                    )


def check_dependencies(tool: Tool, by_name: dict[str, Tool], report: _Reporter) -> None:
    """Checks on [[depends]] that need the other tools."""
    declared = set(tool.spec["platforms"])
    for index, entry in enumerate(tool.spec.get("depends", [])):
        where = f"depends[{index}]"
        name = entry["tool"]
        if name == tool.name:
            report(f"{where}: a tool cannot depend on itself")
            continue
        if "libraries" not in entry and "executables" not in entry:
            report(
                f"{where}: name the 'libraries' or 'executables' of '{name}' to copy"
            )
        dependency = by_name.get(name)
        if dependency is None:
            report(f"{where}: unknown tool '{name}'")
            continue
        missing = sorted(declared - set(dependency.spec["platforms"]))
        if missing:
            report(f"{where}: '{name}' does not ship on {', '.join(missing)}")
        for kind in ("libraries", "executables"):
            available = dependency.spec.get(kind, {})
            for key in entry.get(kind, []):
                if key not in available:
                    report(
                        f"{where}.{kind}: '{key}' is not a key of [{kind}] in '{name}'"
                    )


# Repository -------------------------------------------------------------------


def lint_repo(root: Path) -> tuple[list[Tool], list[Problem]]:
    """Lint the repository at root. Returns the tools found and all problems."""
    problems: list[Problem] = []

    platforms: dict[str, dict] = {}
    data = _load(root, PLATFORMS_FILE, problems)
    if data is not None:
        platforms = check_platforms(data, _Reporter(problems, PLATFORMS_FILE))

    removed: list[str] = []
    data = _load(root, TOOLSET_FILE, problems)
    if data is not None:
        removed = check_toolset(data, _Reporter(problems, TOOLSET_FILE))

    validator: Validator | None = None
    try:
        validator = Validator.from_file(root / SCHEMA_FILE)
    except FileNotFoundError:
        problems.append(Problem(SCHEMA_FILE, "file not found"))
    except (ValueError, SchemaError) as error:
        problems.append(Problem(SCHEMA_FILE, f"unusable schema: {error}"))

    tools, found = discover(root / "tools", root)
    problems.extend(found)

    valid: list[Tool] = []
    for tool in tools:
        report = _Reporter(problems, display_path(tool.toml_path, root))
        if validator is None:
            continue
        errors = validator.validate(tool.spec)
        for path, message in errors:
            report(f"{path}: {message}" if path else message)
        if not errors:
            valid.append(tool)

    by_name = {tool.name: tool for tool in valid}
    for tool in valid:
        report = _Reporter(problems, display_path(tool.toml_path, root))
        check_tool(tool, platforms, report)
        check_dependencies(tool, by_name, report)

    for name in removed:
        if any(tool.name == name for tool in tools):
            problems.append(
                Problem(
                    TOOLSET_FILE, f"removed: '{name}' is still present under tools/"
                )
            )

    _, cycle_problems = order_by_dependencies(valid, root)
    problems.extend(cycle_problems)

    return tools, sorted(set(problems))
