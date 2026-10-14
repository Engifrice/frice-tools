"""Produce a tool's directory for one platform: build/<platform>/stage/<tool>/.

The stage directory is exactly what goes into the zip under <tool>/. Each
strategy fills it from the tool's source; the license texts from the tool's
LICENSES/ folder are added last.

The repackage strategy extracts only the files its pick patterns select,
directly into the stage directory, in one pass over the archive. The cmake
strategy (sourcebuild.py) builds the source and picks from the build tree.
In pick patterns, {exe} stands for the platform's executable suffix.

Strategies implemented so far: repackage, cmake.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path, PurePosixPath

from archive import LINK, extract
from common import LICENSES_DIR, effective_build, entry_path, visible_files
from discover import Tool
from fetch import fetch
import stagecache
from sourcebuild import BuildError, cmake_build, copy_picked

EXE_PLACEHOLDER = "{exe}"


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
            found = tuple(pattern for pattern, regex in self.patterns if regex.match(prefix))
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
                raise StageError(f"{target}: picked more than once into the same place ({source} and {path})")
            return target
        target = None
        # Check every prefix, so each pattern is marked used even when a
        # shorter prefix already picked the member.
        for depth in range(1, len(parts) + 1):
            found = self._patterns_matching("/".join(parts[:depth]))
            if found:
                self.used.update(found)
                if target is None:
                    target = self._place(PurePosixPath(*parts[depth - 1 :]) if self.flatten else path)
        if target is not None and kind != "dir":
            source = self._sources.setdefault(target, path)
            if source != path:
                raise StageError(f"{target}: picked more than once into the same place ({source} and {path})")
        return target

    def _place(self, target: PurePosixPath) -> PurePosixPath:
        return self.into / target if self.into else target

    def check(self) -> None:
        """Fail if a pattern matched nothing; call after extracting every archive."""
        for pattern, _ in self.patterns:
            if pattern not in self.used:
                top = ", ".join(sorted(self.top_level)) or "(empty)"
                raise StageError(f"pick pattern {pattern!r} matches nothing; the source's top level holds: {top}")


def default_platform_info(platform: str) -> dict:
    """The platforms.toml keys the stage step needs, for callers without the file."""
    os_name, isa = platform.split("-", 1)
    return {"os": os_name, "isa": isa, "exe_suffix": ".exe" if os_name == "windows" else ""}


def program_dirs(used: list[Tool], platform: str, build_dir: Path, exe_suffix: str) -> list[Path]:
    """The folders holding the staged programs of the tools a build uses."""
    dirs: list[Path] = []
    for tool in used:
        stage = stage_dir(build_dir, platform, tool.name)
        for key, value in sorted(tool.spec.get("executables", {}).items()):
            relative = entry_path(value, platform)
            program = stage / f"{relative}{exe_suffix}"
            if not program.is_file():
                raise StageError(f"'{tool.name}' is not staged ({program} is missing); build it first")
            if program.parent not in dirs:
                dirs.append(program.parent)
    return dirs


def stage_tool(
    tool: Tool,
    platform: str,
    build_dir: Path,
    cache_dir: Path,
    *,
    log=print,
    platform_info: dict | None = None,
    tools: dict[str, Tool] | None = None,
    stage_cache: Path | None = None,
) -> Path:
    """Stage one tool for one platform and return its stage directory.

    cache_dir holds the downloads; git sources and CPM dependencies are cached
    next to it. tools maps names to tools, for the ones a build 'uses'. With
    stage_cache, a tool built from source is reused from there when its key
    matches, and stored there after a build (stagecache.py).
    """
    spec = tool.spec
    if platform not in spec["platforms"]:
        raise StageError(f"{tool.name} does not ship on {platform}")
    if spec.get("depends"):
        raise StageError("[[depends]] is not supported by the pipeline yet")
    platform_info = platform_info or default_platform_info(platform)

    build = effective_build(spec, platform)
    strategy = build["strategy"]
    if strategy not in ("repackage", "cmake"):
        raise StageError(f"strategy '{strategy}' is not supported by the pipeline yet")

    sources = sources_for(spec, platform)
    if strategy == "repackage" and (not sources or any("url" not in source for source in sources)):
        raise StageError(f"strategy 'repackage' needs url sources for {platform}")

    if "pick" not in build:
        raise StageError(f"strategy '{strategy}' needs 'pick'")
    patterns = [pattern.replace(EXE_PLACEHOLDER, platform_info["exe_suffix"]) for pattern in build["pick"]]

    stage = stage_dir(build_dir, platform, tool.name)
    key = None
    if stage_cache is not None and stagecache.cacheable(tool, platform):
        key = stagecache.stage_key(tool, platform, platform_info, tools or {})
        if stagecache.restore(stage_cache, platform, tool.name, key, stage):
            log(f"reused the cached build ({key[:12]})")
            return stage
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # Several archives go into the same stage directory; a file in two of
    # them is an error, and every pick pattern must match in at least one.
    picker = Picker(patterns, build.get("flatten", False), build.get("into", ""))
    if strategy == "cmake":
        known = tools or {}
        missing = [name for name in build.get("uses", []) if name not in known]
        if missing:
            raise StageError(f"unknown tools in 'uses': {', '.join(missing)}")
        used = [known[name] for name in build.get("uses", [])]
        try:
            tree = cmake_build(
                build=build,
                sources=sources,
                platform_info=platform_info,
                tool_dirs=program_dirs(used, platform, build_dir, platform_info["exe_suffix"]),
                work=build_dir / platform / "work" / tool.name,
                log_file=build_dir / platform / "logs" / f"{tool.name}.log",
                caches={"downloads": cache_dir, "git": cache_dir.parent / "git", "cpm": cache_dir.parent / "cpm"},
                log=log,
            )
            copy_picked(tree, stage, picker)
        except BuildError as error:
            raise StageError(str(error)) from None
        picker.check()
    else:
        skipped = shipped = 0
        for source in sources:
            downloaded = fetch(source["url"], source["sha256"], cache_dir, log=log)
            result = extract(downloaded, stage, source.get("strip_components", 0), picker, merge=True)
            skipped += len(result.skipped_links)
            shipped += len(result.shipped_links)
        picker.check()
        if shipped:
            log(f"shipped {shipped} picked symbolic link{'s' if shipped != 1 else ''} as files")
        if skipped:
            log(f"skipped {skipped} symbolic link{'s' if skipped != 1 else ''} in the archive")

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

    if key is not None:
        stagecache.store(stage_cache, platform, tool.name, key, stage)
    log(f"staged for {platform}")
    return stage
