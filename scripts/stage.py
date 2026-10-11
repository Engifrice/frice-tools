"""Produce a tool's directory for one platform: build/<platform>/stage/<tool>/.

The stage directory is exactly what goes into the zip under <tool>/. Each
strategy fills it from the tool's source; the license texts from the tool's
LICENSES/ folder are added last.

The repackage strategy extracts only the files its pick patterns select,
directly into the stage directory, in one pass over the archive.

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


class Picker:
    """The select callback for extraction: decides, per member, whether it ships and where.

    A member is picked when a pattern matches its path or one of its parent
    directories, so a pattern naming a directory picks everything inside it.
    With flatten, the matched file or directory lands at the stage root under
    its own name; otherwise the archive layout is kept.
    """

    def __init__(self, patterns: list[str], flatten: bool):
        self.patterns = [(pattern, glob_regex(pattern)) for pattern in patterns]
        self.flatten = flatten
        self.used: set[str] = set()
        self.top_level: set[str] = set()
        self._matches: dict[str, tuple[str, ...]] = {}
        self._sources: dict[PurePosixPath, PurePosixPath] = {}

    def _patterns_matching(self, prefix: str) -> tuple[str, ...]:
        found = self._matches.get(prefix)
        if found is None:
            found = tuple(
                pattern for pattern, regex in self.patterns if regex.match(prefix)
            )
            self._matches[prefix] = found
        return found

    def __call__(self, path: PurePosixPath, is_dir: bool) -> PurePosixPath | None:
        parts = path.parts
        self.top_level.add(parts[0])
        target = None
        # Check every prefix, so each pattern is marked used even when a
        # shorter prefix already picked the member.
        for depth in range(1, len(parts) + 1):
            found = self._patterns_matching("/".join(parts[:depth]))
            if found:
                self.used.update(found)
                if target is None:
                    target = (
                        PurePosixPath(*parts[depth - 1 :]) if self.flatten else path
                    )
        if target is not None and not is_dir:
            source = self._sources.setdefault(target, path)
            if source != path:
                raise StageError(
                    f"{target}: picked more than once into the same place ({source} and {path})"
                )
        return target

    def check(self) -> None:
        """Fail if a pattern matched nothing; call after extraction."""
        for pattern, _ in self.patterns:
            if pattern not in self.used:
                top = ", ".join(sorted(self.top_level)) or "(empty)"
                raise StageError(
                    f"pick pattern {pattern!r} matches nothing; the archive's top level holds: {top}"
                )


def stage_tool(
    tool: Tool, platform: str, build_dir: Path, cache_dir: Path, *, log=print
) -> Path:
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

    if "pick" not in build:
        raise StageError("strategy 'repackage' needs 'pick'")

    stage = stage_dir(build_dir, platform, tool.name)
    if stage.exists():
        shutil.rmtree(stage)
    stage.parent.mkdir(parents=True, exist_ok=True)

    downloaded = fetch(source["url"], source["sha256"], cache_dir, log=log)
    picker = Picker(build["pick"], build.get("flatten", False))
    links = extract(downloaded, stage, source.get("strip_components", 0), picker)
    picker.check()
    if links:
        log(
            f"skipped {len(links)} symbolic link{'s' if len(links) != 1 else ''} in the archive"
        )

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

    log(f"staged for {platform}")
    return stage
