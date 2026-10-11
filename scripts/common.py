"""Shared constants and helpers for the frice-tools scripts.

The scripts use only the Python standard library (3.12 or newer), so every CI
runner and build container can run them without installing packages.
"""

from __future__ import annotations

import platform
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# File that marks a directory under tools/ as a tool.
TOOL_FILE = "tool.toml"

# Tool names: also the directory name and the key used by the engine.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Group folder names under tools/.
GROUP_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Host tokens: <os>-<isa>, the engine's OS and ISA enum names in lowercase.
PLATFORM_RE = re.compile(r"^(windows|linux|macos)-(x86_64|arm64)$")

# Operating system names, the first half of a host token.
OS_RE = re.compile(r"^(windows|linux|macos)$")

# Toolset release versions: YYYY.MM.N.
TOOLSET_VERSION_RE = re.compile(r"^[0-9]{4}\.(0[1-9]|1[0-2])\.(0|[1-9][0-9]*)$")

# Placeholders allowed in smoke test arguments.
SMOKE_PLACEHOLDERS = ("smoke", "tmp", "stage")

# Folder next to tool.toml holding license texts, copied to <tool>/LICENSES/.
LICENSES_DIR = "LICENSES"

# Folder next to tool.toml holding files used by smoke tests.
SMOKE_DIR = "smoke"

# Per-tool build script, used by the "script" strategy.
BUILD_SCRIPT = "build.py"

# Archive paths in the zips: letters, digits and ._+@- in each segment, plus
# single spaces between them (CMake ships "Ninja Multi-Config.rst"). Nothing
# that is invalid or trimmed on Windows, no backslashes, no leading or
# trailing spaces.
_SEGMENT = r"[A-Za-z0-9._+@-]+(?: [A-Za-z0-9._+@-]+)*"
ARCHIVE_PATH_RE = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT})*$")


@dataclass(frozen=True, order=True)
class Problem:
    """One thing wrong with the repository, reported as 'path: message'."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def display_path(path: Path, root: Path) -> str:
    """Return path relative to root with '/' separators, for messages."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def host_platform() -> str | None:
    """The platform token of the machine running this script, or None if unknown."""
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system())
    machine = platform.machine().lower()
    isa = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if system is None or isa is None:
        return None
    return f"{system}-{isa}"


def visible_files(directory: Path) -> list[Path]:
    """Regular files directly in directory whose names do not start with '.', sorted."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and not p.name.startswith("."))


def effective_build(spec: dict, platform: str) -> dict:
    """[build] as seen by one platform.

    [build.<os>] applies first, then [build.<platform>]. Override keys replace
    the value from before, except 'args', which are appended.
    """
    build = spec["build"]
    merged = {key: value for key, value in build.items() if not (PLATFORM_RE.match(key) or OS_RE.match(key))}
    os_name = platform.split("-", 1)[0]
    for override in (build.get(os_name, {}), build.get(platform, {})):
        for key, value in override.items():
            if key == "args":
                merged["args"] = list(merged.get("args", [])) + list(value)
            else:
                merged[key] = value
    return merged


def load_toml(path: Path) -> dict:
    """Parse a TOML file. Raises OSError, UnicodeDecodeError or TOMLDecodeError."""
    with path.open("rb") as handle:
        return tomllib.load(handle)
