"""Run the smoke tests against an assembled zip.

The zip is extracted into an empty folder and every program is found through
its manifest.json, exactly as the engine will do, so a test also proves the
manifest paths are right. Placeholders in arguments:

    {smoke}  the tool's smoke/ folder in this repository
    {tmp}    an empty folder for this test, also the working directory
    {stage}  the tool's directory in the extracted zip
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from archive import extract
from common import SMOKE_DIR
from discover import Tool

DEFAULT_TIMEOUT = 60
OUTPUT_TAIL = 2000


@dataclass(frozen=True)
class Failure:
    tool: str
    test: str
    message: str

    def __str__(self) -> str:
        return f"{self.tool}: {self.test}: {self.message}"


def _substitute(arg: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        arg = arg.replace("{" + key + "}", value)
    return arg


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) > OUTPUT_TAIL:
        text = "..." + text[-OUTPUT_TAIL:]
    return text or "(no output)"


def check_manifest(manifest: dict, version: str, platform: str, expected: set[str]) -> list[str]:
    problems = []
    if manifest.get("schema") != 1:
        problems.append(f"manifest schema is {manifest.get('schema')!r}, expected 1")
    if manifest.get("toolset") != version:
        problems.append(f"manifest toolset is {manifest.get('toolset')!r}, expected {version!r}")
    if manifest.get("platform") != platform:
        problems.append(f"manifest platform is {manifest.get('platform')!r}, expected {platform!r}")
    present = set(manifest.get("tools", {}))
    if present != expected:
        problems.append(f"manifest tools are {sorted(present)}, expected {sorted(expected)}")
    return problems


def run_smoke(
    tools: list[Tool],
    version: str,
    platform: str,
    zip_path: Path,
    work: Path,
    *,
    log=print,
) -> list[Failure]:
    """Extract zip_path into work/ and run every smoke test for this platform."""
    if work.exists():
        shutil.rmtree(work)
    root = work / "zip"
    extract(zip_path, root)

    shipped = sorted((t for t in tools if platform in t.spec["platforms"]), key=lambda t: t.name)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    failures = [
        Failure("manifest.json", "check", problem)
        for problem in check_manifest(manifest, version, platform, {t.name for t in shipped})
    ]
    if failures:
        return failures

    count = 0
    for tool in shipped:
        entry = manifest["tools"][tool.name]
        for index, smoke in enumerate(tool.spec.get("smoke", [])):
            if platform not in smoke.get("platforms", tool.spec["platforms"]):
                continue
            label = f"smoke[{index}]"
            program = root / entry["executables"][smoke["entrypoint"]]
            if not program.is_file():
                failures.append(Failure(tool.name, label, f"{program.relative_to(root).as_posix()} not found"))
                continue

            tmp = work / "tmp" / f"{tool.name}-{index}"
            tmp.mkdir(parents=True)
            values = {
                "smoke": str((tool.directory / SMOKE_DIR).resolve()),
                "tmp": str(tmp.resolve()),
                "stage": str((root / tool.name).resolve()),
            }
            args = [_substitute(arg, values) for arg in smoke.get("args", [])]
            command = " ".join([smoke["entrypoint"], *smoke.get("args", [])])
            timeout = smoke.get("timeout", DEFAULT_TIMEOUT)
            count += 1
            try:
                result = subprocess.run(
                    [str(program.resolve()), *args],
                    cwd=tmp,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                failures.append(Failure(tool.name, command, f"timed out after {timeout} s"))
                continue
            except OSError as error:
                failures.append(Failure(tool.name, command, f"could not start: {error}"))
                continue

            output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
            expected_exit = smoke.get("expect_exit", 0)
            problem = None
            if result.returncode != expected_exit:
                problem = f"exit code {result.returncode}, expected {expected_exit}"
            elif "expect_output" in smoke and smoke["expect_output"] not in output:
                problem = f"output does not contain {smoke['expect_output']!r}"
            elif "forbid_output" in smoke and smoke["forbid_output"] in output:
                problem = f"output contains {smoke['forbid_output']!r}"
            if problem:
                failures.append(Failure(tool.name, command, f"{problem}; output:\n{_tail(output)}"))
            else:
                log(f"ok   {tool.name}: {command}")

    log(f"{count} smoke tests, {len(failures)} failed")
    return failures
