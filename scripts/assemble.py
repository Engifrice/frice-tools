"""Pack the staged tools of one platform into the release zip.

The zip is deterministic: entries are sorted (manifest.json first), every
timestamp is 1980-01-01, permissions are set by rule rather than copied, and
there are no directory entries. Packing the same staged files twice with the
same Python gives the same bytes.

Entries use deflate level 6, zlib's default: on CMake it is 2.4 times faster
than level 9 for a zip 0.4% larger.

Permission rule: on Windows platforms, .exe and .dll files are 0755. On other
platforms, declared executables and libraries, and files that were executable
in the upstream archive, are 0755. Everything else is 0644.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from common import ARCHIVE_PATH_RE, entry_path
from discover import Tool
from stage import stage_dir

MANIFEST = "manifest.json"
MANIFEST_SCHEMA = 1
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
ZIP_LEVEL = 6
WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".dll")


class AssembleError(Exception):
    """The staged files do not form a valid release zip."""


@dataclass(frozen=True)
class Assembled:
    path: Path
    size: int
    sha256: str


def zip_name(version: str, platform: str) -> str:
    return f"frice-tools-{version}-{platform}.zip"


def library_path(path: str, platform_info: dict) -> str:
    """Add the platform's prefix and suffix to the last component of a library path."""
    relative = PurePosixPath(path)
    name = f"{platform_info['lib_prefix']}{relative.name}{platform_info['lib_suffix']}"
    return str(relative.with_name(name))


def tool_entry(tool: Tool, platform: str, platform_info: dict) -> dict:
    """The manifest entry of one tool, with paths relative to the zip root."""
    spec = tool.spec

    def paths(kind: str) -> dict[str, str]:
        found = {}
        for key, value in sorted(spec.get(kind, {}).items()):
            path = entry_path(value, platform)
            if path is None:
                raise AssembleError(
                    f"{tool.name}: {kind} '{key}' has no path for {platform}"
                )
            found[key] = path
        return found

    executables = {
        key: f"{tool.name}/{path}{platform_info['exe_suffix']}"
        for key, path in paths("executables").items()
    }
    libraries = {
        key: f"{tool.name}/{library_path(path, platform_info)}"
        for key, path in paths("libraries").items()
    }
    return {
        "version": spec["version"],
        "upstream_version": spec["upstream_version"],
        "executables": executables,
        "libraries": libraries,
    }


def build_manifest(
    tools: list[Tool], version: str, platform: str, platform_info: dict
) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "toolset": version,
        "platform": platform,
        "tools": {
            tool.name: tool_entry(tool, platform, platform_info)
            for tool in sorted(tools, key=lambda t: t.name)
        },
    }


def _collect(tool: Tool, stage: Path) -> list[tuple[str, Path]]:
    """(archive path, file) for every file staged for one tool."""
    if not stage.is_dir():
        raise AssembleError(
            f"{tool.name}: nothing staged at {stage}; run the build first"
        )
    entries = []
    for path in sorted(stage.rglob("*")):
        relative = path.relative_to(stage).as_posix()
        if path.is_symlink():
            raise AssembleError(f"{tool.name}: {relative} is a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise AssembleError(f"{tool.name}: {relative} is not a regular file")
        name = f"{tool.name}/{relative}"
        if not ARCHIVE_PATH_RE.match(name):
            raise AssembleError(
                f"{tool.name}: {relative} has characters not allowed in archive paths"
            )
        entries.append((name, path))
    return entries


def _mode(name: str, path: Path, platform_info: dict, declared: set[str]) -> int:
    if platform_info["os"] == "windows":
        return 0o755 if name.lower().endswith(WINDOWS_EXECUTABLE_SUFFIXES) else 0o644
    if name in declared or path.stat().st_mode & 0o111:
        return 0o755
    return 0o644


def _info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    info.create_system = 3  # Unix, so the permission bits are honoured
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def assemble(
    tools: list[Tool],
    version: str,
    platform: str,
    platform_info: dict,
    build_dir: Path,
    out_dir: Path,
    *,
    log=print,
) -> Assembled:
    """Write out/frice-tools-<version>-<platform>.zip from the staged tools."""
    shipped = [tool for tool in tools if platform in tool.spec["platforms"]]
    manifest = build_manifest(shipped, version, platform, platform_info)

    entries: list[tuple[str, Path]] = []
    declared: set[str] = set()
    for tool in shipped:
        files = _collect(tool, stage_dir(build_dir, platform, tool.name))
        names = {name for name, _ in files}
        entry = manifest["tools"][tool.name]
        for kind in ("executables", "libraries"):
            for key, name in entry[kind].items():
                if name not in names:
                    raise AssembleError(
                        f"{tool.name}: {kind} '{key}' points to {name}, which was not staged"
                    )
                declared.add(name)
        entries.extend(files)
    entries.sort(key=lambda item: item[0])

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / zip_name(version, platform)
    partial = target.with_name(target.name + ".partial")
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    with zipfile.ZipFile(partial, "w") as archive:
        archive.writestr(
            _info(MANIFEST, 0o644), manifest_bytes, compresslevel=ZIP_LEVEL
        )
        for name, path in entries:
            info = _info(name, _mode(name, path, platform_info, declared))
            archive.writestr(info, path.read_bytes(), compresslevel=ZIP_LEVEL)
    os.replace(partial, target)

    data = target.read_bytes()
    result = Assembled(target, len(data), hashlib.sha256(data).hexdigest())
    log(
        f"wrote {target.name}: {len(shipped)} tools, {len(entries) + 1} files, {result.size} bytes"
    )
    log(f"sha256 {result.sha256}")
    return result
