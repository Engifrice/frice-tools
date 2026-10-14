"""Reuse the staged output of tools built from source when nothing that shapes it changed.

A tool built with strategy cmake takes minutes; its stage directory is stored
under .cache/stages/<platform>/<tool>/<key>/ and copied back on the next build
with the same key. The key is a SHA-256 over everything known to affect the
result:

  - CACHE_VERSION below (bump it to throw every entry away)
  - the tool's tool.toml, LICENSES/ files and build.py
  - the platform's entry in platforms.toml (build_env, cmake_args, ...)
  - the keys of the tools its build uses (a new CMake rebuilds it)
  - the pipeline code that produces a source build's stage (archive.py
    only when the source is an archive)
  - the compiler's version banner

It cannot cover everything: SDKs, system headers and dependencies a build
downloads by tag are outside it. Release builds therefore run with --no-cache.

Only the newest entry per tool and platform is kept, with a small JSON file
recording the hash of each of the parts above, so a rebuild can say which
part changed.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from common import BUILD_SCRIPT, LICENSES_DIR, TOOL_FILE, effective_build, sources_for, visible_files
from discover import Tool

CACHE_VERSION = 1

# Pipeline code whose changes can change a source build's stage directory.
# archive.py counts only for archive sources; stagecache.py itself does not
# (CACHE_VERSION covers changes to how entries are stored).
PIPELINE_FILES = ("common.py", "fetch.py", "sourcebuild.py", "stage.py")
ARCHIVE_FILE = "archive.py"
SCRIPTS_DIR = Path(__file__).resolve().parent


def cacheable(tool: Tool, platform: str) -> bool:
    """Only builds from source are worth caching; extracting a download is quick."""
    return effective_build(tool.spec, platform)["strategy"] == "cmake"


@functools.lru_cache(maxsize=None)
def compiler_identity(os_name: str, isa: str, cc: str | None = None, cxx: str | None = None) -> str:
    """The version banners of the C and C++ compilers a source build would use."""
    from sourcebuild import BuildError, compiler_environment

    try:
        environment = compiler_environment(os_name, isa, cc, cxx)
    except BuildError as error:
        return f"unavailable: {error}"
    path = environment.get("PATH", "")
    defaults = {"CC": ("cc", "gcc", "clang"), "CXX": ("c++", "g++", "clang++")}
    banners = []
    for variable, names in defaults.items():
        chosen = environment.get(variable) or next((n for n in names if shutil.which(n, path=path)), names[0])
        program = shutil.which(chosen, path=path) or chosen
        # cl prints its version when run without arguments; the others take --version.
        command = [program] if Path(chosen).stem.lower() == "cl" else [program, "--version"]
        try:
            result = subprocess.run(command, env=environment, capture_output=True, text=True, errors="replace")
            banners.append((result.stdout + result.stderr).strip().splitlines()[:2])
        except OSError:
            banners.append(["unavailable"])
    return json.dumps(banners)


def _sha(*chunks: bytes) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(f"{len(chunk)}\0".encode())
        digest.update(chunk)
    return digest.hexdigest()


def _files_sha(files: list[tuple[str, Path]]) -> str:
    return _sha(*(part for label, path in files for part in (label.encode(), path.read_bytes())))


def key_parts(
    tool: Tool,
    platform: str,
    platform_info: dict,
    tools: dict[str, Tool],
    *,
    compiler=compiler_identity,
) -> dict[str, str]:
    """The hash of each input of a tool's stage on one platform, by name."""
    tool_files = [(TOOL_FILE, tool.toml_path)]
    tool_files += [(f"{LICENSES_DIR}/{f.name}", f) for f in visible_files(tool.directory / LICENSES_DIR)]
    if (tool.directory / BUILD_SCRIPT).is_file():
        tool_files.append((BUILD_SCRIPT, tool.directory / BUILD_SCRIPT))
    scripts = list(PIPELINE_FILES)
    if any("url" in source for source in sources_for(tool.spec, platform)):
        scripts.append(ARCHIVE_FILE)
    parts = {
        "cache version": str(CACHE_VERSION),
        "platform": _sha(platform.encode(), json.dumps(platform_info, sort_keys=True).encode()),
        "tool files": _files_sha(tool_files),
        "pipeline scripts": _files_sha([(name, SCRIPTS_DIR / name) for name in sorted(scripts)]),
    }
    for name in sorted(tool.dependency_names()):
        used = tools.get(name)
        parts[f"tool '{name}'"] = (
            "missing" if used is None else stage_key(used, platform, platform_info, tools, compiler=compiler)
        )
    if cacheable(tool, platform):
        settings = {**platform_info.get("build_env", {}), **effective_build(tool.spec, platform).get("env", {})}
        identity = compiler(platform_info["os"], platform_info["isa"], settings.get("CC"), settings.get("CXX"))
        parts["compiler"] = _sha(identity.encode())
    return parts


def key_of(parts: dict[str, str]) -> str:
    return _sha(json.dumps(parts, sort_keys=True).encode())


def stage_key(
    tool: Tool,
    platform: str,
    platform_info: dict,
    tools: dict[str, Tool],
    *,
    compiler=compiler_identity,
) -> str:
    """The cache key of a tool's stage directory on one platform."""
    return key_of(key_parts(tool, platform, platform_info, tools, compiler=compiler))


def entry_dir(cache_dir: Path, platform: str, tool: str, key: str) -> Path:
    return cache_dir / platform / tool / key


def restore(cache_dir: Path, platform: str, tool: str, key: str, stage: Path) -> bool:
    """Copy a cached stage directory into place; False if there is none for this key."""
    entry = entry_dir(cache_dir, platform, tool, key)
    if not entry.is_dir():
        return False
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(entry, stage)
    return True


def store(cache_dir: Path, platform: str, tool: str, parts: dict[str, str], stage: Path) -> None:
    """Keep a copy of a freshly built stage directory, replacing older entries of the tool."""
    key = key_of(parts)
    tool_dir = cache_dir / platform / tool
    if tool_dir.exists():
        shutil.rmtree(tool_dir)
    partial = tool_dir / f".partial-{key}"
    shutil.copytree(stage, partial)
    (tool_dir / f"{key}.json").write_text(json.dumps(parts, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, entry_dir(cache_dir, platform, tool, key))


def miss_reason(cache_dir: Path, platform: str, tool: str, parts: dict[str, str]) -> str:
    """Why there is no entry for these parts: nothing cached, or which parts changed."""
    tool_dir = cache_dir / platform / tool
    recorded = sorted(tool_dir.glob("*.json")) if tool_dir.is_dir() else []
    if not recorded:
        return "no cached build"
    try:
        old = json.loads(recorded[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "the cached build has no readable record"
    changed = sorted(name for name in set(parts) | set(old) if parts.get(name) != old.get(name))
    return "changed since the cached build: " + ", ".join(changed)


def prune(cache_dir: Path, platform: str, keep: set[str]) -> list[str]:
    """Remove the entries of tools that no longer exist; return their names."""
    platform_dir = cache_dir / platform
    if not platform_dir.is_dir():
        return []
    removed = sorted(entry.name for entry in platform_dir.iterdir() if entry.is_dir() and entry.name not in keep)
    for name in removed:
        shutil.rmtree(platform_dir / name)
    return removed
