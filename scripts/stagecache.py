"""Reuse the staged output of tools built from source when nothing that shapes it changed.

A tool built with strategy cmake takes minutes; its stage directory is stored
under .cache/stages/<platform>/<tool>/<key>/ and copied back on the next build
with the same key. The key is a SHA-256 over everything known to affect the
result:

  - CACHE_VERSION below (bump it to throw every entry away)
  - the tool's tool.toml, LICENSES/ files and build.py
  - the platform's entry in platforms.toml (build_env, cmake_args, ...)
  - the keys of the tools its build uses (a new CMake rebuilds it)
  - the pipeline code that produces a stage
  - the compiler's version banner

It cannot cover everything: SDKs, system headers and dependencies a build
downloads by tag are outside it. Release builds therefore run with --no-cache.
Only the newest entry per tool and platform is kept.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from common import BUILD_SCRIPT, LICENSES_DIR, TOOL_FILE, effective_build, visible_files
from discover import Tool

CACHE_VERSION = 1

# Pipeline code whose changes can change a stage directory.
PIPELINE_FILES = ("archive.py", "common.py", "fetch.py", "sourcebuild.py", "stage.py", "stagecache.py")
SCRIPTS_DIR = Path(__file__).resolve().parent


def cacheable(tool: Tool, platform: str) -> bool:
    """Only builds from source are worth caching; extracting a download is quick."""
    return effective_build(tool.spec, platform)["strategy"] == "cmake"


@functools.lru_cache(maxsize=None)
def compiler_identity(os_name: str, isa: str) -> str:
    """The version banner of the C and C++ compilers a source build would use."""
    if os_name == "windows":
        from sourcebuild import BuildError, msvc_environment

        try:
            environment = os.environ if shutil.which("cl") else msvc_environment(isa)
        except BuildError as error:
            return f"unavailable: {error}"
        path = environment.get("PATH") or environment.get("Path") or ""
        commands = [[shutil.which("cl", path=path) or "cl"]]
    else:
        environment = os.environ
        commands = []
        for variable, names in (("CC", ("cc", "gcc", "clang")), ("CXX", ("c++", "g++", "clang++"))):
            chosen = environment.get(variable) or next((n for n in names if shutil.which(n)), names[0])
            commands.append([chosen, "--version"])
    banners = []
    for command in commands:
        try:
            result = subprocess.run(command, env=dict(environment), capture_output=True, text=True, errors="replace")
            banners.append((result.stdout + result.stderr).strip().splitlines()[:2])
        except OSError:
            banners.append(["unavailable"])
    return json.dumps(banners)


def _hash_file(digest, label: str, path: Path) -> None:
    data = path.read_bytes()
    digest.update(f"{label}\0{len(data)}\0".encode())
    digest.update(data)


def stage_key(
    tool: Tool,
    platform: str,
    platform_info: dict,
    tools: dict[str, Tool],
    *,
    compiler=compiler_identity,
) -> str:
    """The cache key of a tool's stage directory on one platform."""
    digest = hashlib.sha256()
    digest.update(f"frice-tools stage cache {CACHE_VERSION}\0{platform}\0".encode())
    digest.update(json.dumps(platform_info, sort_keys=True).encode())
    _hash_file(digest, TOOL_FILE, tool.toml_path)
    for license_file in visible_files(tool.directory / LICENSES_DIR):
        _hash_file(digest, f"{LICENSES_DIR}/{license_file.name}", license_file)
    if (tool.directory / BUILD_SCRIPT).is_file():
        _hash_file(digest, BUILD_SCRIPT, tool.directory / BUILD_SCRIPT)
    for name in PIPELINE_FILES:
        _hash_file(digest, f"scripts/{name}", SCRIPTS_DIR / name)
    for name in sorted(tool.dependency_names()):
        used = tools.get(name)
        if used is None:
            digest.update(f"missing {name}\0".encode())
        else:
            digest.update(f"uses {name} {stage_key(used, platform, platform_info, tools, compiler=compiler)}\0".encode())
    if cacheable(tool, platform):
        digest.update(compiler(platform_info["os"], platform_info["isa"]).encode())
    return digest.hexdigest()


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


def store(cache_dir: Path, platform: str, tool: str, key: str, stage: Path) -> None:
    """Keep a copy of a freshly built stage directory, replacing older entries of the tool."""
    tool_dir = cache_dir / platform / tool
    if tool_dir.exists():
        shutil.rmtree(tool_dir)
    partial = tool_dir / f".partial-{key}"
    shutil.copytree(stage, partial)
    os.replace(partial, entry_dir(cache_dir, platform, tool, key))


def prune(cache_dir: Path, platform: str, keep: set[str]) -> list[str]:
    """Remove the entries of tools that no longer exist; return their names."""
    platform_dir = cache_dir / platform
    if not platform_dir.is_dir():
        return []
    removed = sorted(entry.name for entry in platform_dir.iterdir() if entry.is_dir() and entry.name not in keep)
    for name in removed:
        shutil.rmtree(platform_dir / name)
    return removed
