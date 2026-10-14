"""Checks and files for publishing a toolset release (CONTRACT.md sections 2, 5 and 6).

check_release compares the tools about to be released with the previous
release's index: the version must be newer, and a tool or an executable or
library key may only disappear when toolset.toml lists it in 'removed'.

build_index writes frice-tools-<version>-release.json, the release index the
private SDK reads: every host zip with its size and SHA-256, and every tool
with its versions, platforms and keys. release_notes renders the same
information as the release's description.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from discover import Tool

INDEX_SCHEMA = 1
ENTRY_KINDS = ("executables", "libraries")


class ReleaseError(Exception):
    """The release cannot be published as it is."""


def version_tuple(version: str) -> tuple[int, int, int]:
    year, month, counter = version.split(".")
    return int(year), int(month), int(counter)


def index_name(version: str) -> str:
    return f"frice-tools-{version}-release.json"


def tool_entries(tools: list[Tool]) -> dict[str, dict]:
    """The 'tools' table of a release index."""
    return {
        tool.name: {
            "version": tool.spec["version"],
            "upstream_version": tool.spec["upstream_version"],
            "platforms": sorted(tool.spec["platforms"]),
            **{kind: sorted(tool.spec.get(kind, {})) for kind in ENTRY_KINDS},
        }
        for tool in sorted(tools, key=lambda tool: tool.name)
    }


def check_release(tools: list[Tool], version: str, removed: list[str], previous: dict | None) -> list[str]:
    """Problems that block releasing these tools as 'version' after 'previous' (an index, or None)."""
    if previous is None:
        return []
    problems = []
    previous_version = previous.get("toolset", "")
    try:
        newer = version_tuple(version) > version_tuple(previous_version)
    except ValueError:
        newer = False
    if not newer:
        problems.append(f"version {version} is not newer than the previous release {previous_version}")

    current = tool_entries(tools)
    acknowledged = set(removed)
    for name, entry in sorted(previous.get("tools", {}).items()):
        if name not in current:
            if name not in acknowledged:
                problems.append(f"tool '{name}' was in {previous_version} and is gone; list it in toolset.toml 'removed'")
            continue
        for kind in ENTRY_KINDS:
            for key in sorted(set(entry.get(kind, [])) - set(current[name][kind])):
                label = f"{name}.{kind}.{key}"
                if label not in acknowledged:
                    problems.append(f"'{label}' was in {previous_version} and is gone; list it in toolset.toml 'removed'")
    return problems


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_index(tools: list[Tool], version: str, platforms: list[str], asset_dir: Path) -> dict:
    """The release index for the host zips in asset_dir; each must be there and match its name."""
    assets = {}
    for platform in sorted(platforms):
        name = f"frice-tools-{version}-{platform}.zip"
        path = asset_dir / name
        if not path.is_file():
            raise ReleaseError(f"{name} is missing from {asset_dir}")
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("toolset") != version or manifest.get("platform") != platform:
            raise ReleaseError(
                f"{name}: manifest.json says toolset {manifest.get('toolset')!r}, platform {manifest.get('platform')!r}"
            )
        assets[platform] = {"name": name, "size": path.stat().st_size, "sha256": _sha256(path)}
    return {
        "schema": INDEX_SCHEMA,
        "toolset": version,
        "assets": assets,
        "sources": {},
        "tools": tool_entries(tools),
    }


def release_notes(index: dict, all_platforms: list[str]) -> str:
    """Markdown for the release description."""
    version = index["toolset"]
    lines = [
        f"Toolset {version}: one zip per host, each with a `manifest.json` listing its tools.",
        "",
        "| Tool | Version | Hosts |",
        "| --- | --- | --- |",
    ]
    for name, entry in index["tools"].items():
        hosts = "all" if sorted(entry["platforms"]) == sorted(all_platforms) else ", ".join(entry["platforms"])
        lines.append(f"| {name} | {entry['upstream_version']} | {hosts} |")
    lines += [
        "",
        f"Sizes and SHA-256 of every zip are in `{index_name(version)}`. Each tool's licenses ship in "
        "`<tool>/LICENSES/` inside the zips.",
        "",
    ]
    return "\n".join(lines)
