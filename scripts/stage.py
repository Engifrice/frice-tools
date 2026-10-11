"""Produce a tool's directory for one platform: build/<platform>/stage/<tool>/.

The stage directory is exactly what goes into the zip under <tool>/. Each
strategy fills it from the tool's source; the license texts from the tool's
LICENSES/ folder are added last.

Strategies implemented so far: repackage.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path, PurePosixPath

from archive import extract
from common import LICENSES_DIR, effective_build, visible_files
from discover import Tool
from fetch import fetch


class StageError(Exception):
    """A tool could not be staged."""


def work_dir(build_dir: Path, platform: str, tool: str) -> Path:
    return build_dir / platform / "work" / tool


def stage_dir(build_dir: Path, platform: str, tool: str) -> Path:
    return build_dir / platform / "stage" / tool


def source_for(spec: dict, platform: str) -> dict | None:
    """The source table used on this platform, or None."""
    source = spec.get("source")
    if source is None:
        return None
    if "url" in source or "git" in source:
        return source
    return source.get(platform)


# Pick patterns ------------------------------------------------------------------


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a pick pattern: '*' and '?' stay within one path segment, '**' spans any."""
    out = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def _tree(root: Path) -> list[PurePosixPath]:
    """Every file and directory below root, as relative paths, sorted."""
    return sorted(PurePosixPath(p.relative_to(root).as_posix()) for p in root.rglob("*"))


def pick(source_root: Path, patterns: list[str]) -> list[PurePosixPath]:
    """Paths matched by the patterns; a matched directory brings everything inside it.

    Every pattern must match something, so an upstream layout change fails
    the build instead of silently shipping less.
    """
    entries = _tree(source_root)
    matched: set[PurePosixPath] = set()
    for pattern in patterns:
        regex = glob_regex(pattern)
        hits = [entry for entry in entries if regex.match(entry.as_posix())]
        if not hits:
            top = ", ".join(sorted(p.name for p in source_root.iterdir())) or "(empty)"
            raise StageError(f"pick pattern {pattern!r} matches nothing; the archive's top level holds: {top}")
        matched.update(hits)
    # Drop entries already covered by a matched parent directory.
    return sorted(entry for entry in matched if not any(parent in matched for parent in entry.parents))


def _copy(source: Path, target: Path) -> None:
    if target.exists():
        raise StageError(f"{target.name}: picked more than once into the same place")
    if source.is_dir():
        shutil.copytree(source, target, copy_function=shutil.copyfile)
        for path in [target, *target.rglob("*")]:
            original = source / path.relative_to(target)
            path.chmod(original.stat().st_mode & 0o777)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(source.stat().st_mode & 0o777)


# Strategies -----------------------------------------------------------------------


def _repackage(tool: Tool, build: dict, source_root: Path, stage: Path) -> None:
    if "pick" not in build:
        raise StageError("strategy 'repackage' needs 'pick'")
    flatten = build.get("flatten", False)
    for relative in pick(source_root, build["pick"]):
        destination = PurePosixPath(relative.name) if flatten else relative
        _copy(source_root / relative, stage / destination)


def stage_tool(tool: Tool, platform: str, build_dir: Path, cache_dir: Path, *, log=print) -> Path:
    """Stage one tool for one platform and return its stage directory."""
    spec = tool.spec
    if platform not in spec["platforms"]:
        raise StageError(f"{tool.name} does not ship on {platform}")
    if spec.get("depends"):
        raise StageError("[[depends]] is not supported by the pipeline yet")

    build = effective_build(spec, platform)
    strategy = build["strategy"]
    if strategy != "repackage":
        raise StageError(f"strategy '{strategy}' is not supported by the pipeline yet")

    source = source_for(spec, platform)
    if source is None or "url" not in source:
        raise StageError(f"strategy 'repackage' needs a url source for {platform}")

    work = work_dir(build_dir, platform, tool.name)
    stage = stage_dir(build_dir, platform, tool.name)
    for directory in (work, stage):
        if directory.exists():
            shutil.rmtree(directory)
    stage.mkdir(parents=True)

    downloaded = fetch(source["url"], source["sha256"], cache_dir, log=log)
    source_root = work / "src"
    extract(downloaded, source_root, source.get("strip_components", 0))
    _repackage(tool, build, source_root, stage)

    licenses = visible_files(tool.directory / LICENSES_DIR)
    if not licenses:
        raise StageError(f"{LICENSES_DIR}/ next to tool.toml is empty")
    target = stage / LICENSES_DIR
    if target.exists():
        raise StageError(f"the picked files already contain {LICENSES_DIR}/")
    target.mkdir()
    for license_file in licenses:
        shutil.copyfile(license_file, target / license_file.name)
        (target / license_file.name).chmod(0o644)

    log(f"staged {tool.name} for {platform}")
    return stage
