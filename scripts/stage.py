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

from archive import LINK, extract
from common import LICENSES_DIR, effective_build, visible_files
from discover import Tool
from fetch import fetch


class StageError(Exception):
    """A tool could not be staged."""


def stage_dir(build_dir: Path, platform: str, tool: str) -> Path:
    return build_dir / platform / "stage" / tool


def sources_for(spec: dict, platform: str) -> list[dict]:
    """The source tables used on this platform: one, several, or none."""
    source = spec.get("source")
    if source is None:
        return []
    if "url" in source or "git" in source:
        return [source]
    tables = source.get(platform, [])
    return tables if isinstance(tables, list) else [tables]


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
    its own name; otherwise the archive layout is kept. With into, everything
    lands below that folder instead of the stage root.

    A symbolic link is picked only when a pattern matches its own path; it
    then ships as a regular file with its target's content (Linux sonames
    such as libavcodec.so.63). Links inside a picked directory are skipped.
    One picker can serve several archives; check() then covers them all.
    """

    def __init__(self, patterns: list[str], flatten: bool, into: str = ""):
        self.patterns = [(pattern, glob_regex(pattern)) for pattern in patterns]
        self.flatten = flatten
        self.into = PurePosixPath(into) if into else None
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

    def __call__(self, path: PurePosixPath, kind: str) -> PurePosixPath | None:
        parts = path.parts
        self.top_level.add(parts[0])
        if kind == LINK:
            found = self._patterns_matching(path.as_posix())
            if not found:
                return None
            self.used.update(found)
            target = self._place(PurePosixPath(path.name) if self.flatten else path)
            source = self._sources.setdefault(target, path)
            if source != path:
                raise StageError(
                    f"{target}: picked more than once into the same place ({source} and {path})"
                )
            return target
        target = None
        # Check every prefix, so each pattern is marked used even when a
        # shorter prefix already picked the member.
        for depth in range(1, len(parts) + 1):
            found = self._patterns_matching("/".join(parts[:depth]))
            if found:
                self.used.update(found)
                if target is None:
                    target = self._place(
                        PurePosixPath(*parts[depth - 1 :]) if self.flatten else path
                    )
        if target is not None and kind != "dir":
            source = self._sources.setdefault(target, path)
            if source != path:
                raise StageError(
                    f"{target}: picked more than once into the same place ({source} and {path})"
                )
        return target

    def _place(self, target: PurePosixPath) -> PurePosixPath:
        return self.into / target if self.into else target

    def check(self) -> None:
        """Fail if a pattern matched nothing; call after extracting every archive."""
        for pattern, _ in self.patterns:
            if pattern not in self.used:
                top = ", ".join(sorted(self.top_level)) or "(empty)"
                raise StageError(
                    f"pick pattern {pattern!r} matches nothing; the archives' top level holds: {top}"
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

    sources = sources_for(spec, platform)
    if not sources or any("url" not in source for source in sources):
        raise StageError(f"strategy 'repackage' needs url sources for {platform}")

    if "pick" not in build:
        raise StageError("strategy 'repackage' needs 'pick'")

    stage = stage_dir(build_dir, platform, tool.name)
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # Several archives go into the same stage directory; a file in two of
    # them is an error, and every pick pattern must match in at least one.
    picker = Picker(build["pick"], build.get("flatten", False), build.get("into", ""))
    skipped = shipped = 0
    for source in sources:
        downloaded = fetch(source["url"], source["sha256"], cache_dir, log=log)
        result = extract(
            downloaded, stage, source.get("strip_components", 0), picker, merge=True
        )
        skipped += len(result.skipped_links)
        shipped += len(result.shipped_links)
    picker.check()
    if shipped:
        log(
            f"shipped {shipped} picked symbolic link{'s' if shipped != 1 else ''} as files"
        )
    if skipped:
        log(
            f"skipped {skipped} symbolic link{'s' if skipped != 1 else ''} in the archive"
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
