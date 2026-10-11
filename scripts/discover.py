"""Find the tools under tools/ and order them by their dependencies.

Discovery rules (see README.md):
  1. Every directory under tools/ that contains a tool.toml is a tool.
  2. Directories whose name starts with '_' or '.' are skipped, with
     everything below them.
  3. A tool lives inside a group folder: tools/<group>/<name>/.
  4. Tools never nest.
  5. The tool's name is its directory name, repeated as 'name' in tool.toml.
  6. Names are unique across all groups.
"""

from __future__ import annotations

import heapq
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from common import GROUP_RE, NAME_RE, TOOL_FILE, Problem, display_path, load_toml


@dataclass
class Tool:
    """A discovered tool: its name, group path, directory and parsed tool.toml."""

    name: str
    group: str
    directory: Path
    spec: dict

    @property
    def toml_path(self) -> Path:
        return self.directory / TOOL_FILE

    def dependency_names(self) -> list[str]:
        """Names listed in [[depends]], ignoring malformed entries (the schema reports those)."""
        depends = self.spec.get("depends", [])
        if not isinstance(depends, list):
            return []
        return [
            entry["tool"]
            for entry in depends
            if isinstance(entry, dict) and isinstance(entry.get("tool"), str)
        ]


def discover(tools_root: Path, repo_root: Path | None = None) -> tuple[list[Tool], list[Problem]]:
    """Return the tools found under tools_root, sorted by name, and the problems seen."""
    repo_root = repo_root if repo_root is not None else tools_root.parent
    problems: list[Problem] = []
    tools: dict[str, Tool] = {}
    tool_dirs: list[Path] = []

    def report(path: Path, message: str) -> None:
        problems.append(Problem(display_path(path, repo_root), message))

    if not tools_root.is_dir():
        report(tools_root, "directory not found")
        return [], problems

    for dirpath, dirnames, filenames in os.walk(tools_root):
        # Prune skipped directories and visit the rest in a stable order.
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(("_", ".")))
        here = Path(dirpath)
        if TOOL_FILE not in filenames:
            continue

        toml_path = here / TOOL_FILE
        relative = here.relative_to(tools_root)

        owner = next((d for d in tool_dirs if d in here.parents), None)
        if owner is not None:
            report(
                toml_path,
                f"tools cannot be nested; this is inside the tool at {display_path(owner, repo_root)}/",
            )
            continue
        if not relative.parts:
            report(toml_path, "a tool.toml cannot sit directly in tools/; use tools/<group>/<name>/")
            continue
        tool_dirs.append(here)
        if len(relative.parts) == 1:
            report(toml_path, f"a tool must be inside a group folder: tools/<group>/{here.name}/")
            continue
        for part in relative.parts[:-1]:
            if not GROUP_RE.match(part):
                report(toml_path, f"group folder '{part}' must match [a-z][a-z0-9_-]*")

        name = here.name
        if not NAME_RE.match(name):
            report(toml_path, f"folder name '{name}' is not a valid tool name ([a-z][a-z0-9_]*)")
            continue

        try:
            spec = load_toml(toml_path)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            report(toml_path, f"invalid TOML: {error}")
            continue

        declared = spec.get("name")
        if declared != name:
            report(toml_path, f"name must equal the folder name \"{name}\", found {declared!r}")

        if name in tools:
            report(
                toml_path,
                f"duplicate tool name '{name}', also at {display_path(tools[name].toml_path, repo_root)}",
            )
            continue

        tools[name] = Tool(name=name, group="/".join(relative.parts[:-1]), directory=here, spec=spec)

    return [tools[name] for name in sorted(tools)], sorted(problems)


def order_by_dependencies(
    tools: list[Tool], repo_root: Path
) -> tuple[list[Tool], list[Problem]]:
    """Order tools so every tool comes after the tools it depends on.

    Ties are broken by name, so the order is stable. Dependencies on unknown
    tools and on the tool itself are ignored here; the linter reports them.
    Tools caught in a cycle are reported and appended at the end.
    """
    by_name = {tool.name: tool for tool in tools}
    depends_on = {
        tool.name: sorted({d for d in tool.dependency_names() if d in by_name and d != tool.name})
        for tool in tools
    }
    dependents: dict[str, list[str]] = {name: [] for name in by_name}
    for name, dependencies in depends_on.items():
        for dependency in dependencies:
            dependents[dependency].append(name)

    waiting = {name: len(dependencies) for name, dependencies in depends_on.items()}
    ready = [name for name, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        ordered.append(name)
        for dependent in sorted(dependents[name]):
            waiting[dependent] -= 1
            if waiting[dependent] == 0:
                heapq.heappush(ready, dependent)

    problems: list[Problem] = []
    stuck = sorted(name for name, count in waiting.items() if count > 0)
    for name in stuck:
        blockers = ", ".join(d for d in depends_on[name] if waiting[d] > 0)
        problems.append(
            Problem(
                display_path(by_name[name].toml_path, repo_root),
                f"dependency cycle: '{name}' waits on {blockers}",
            )
        )

    return [by_name[name] for name in ordered + stuck], problems
