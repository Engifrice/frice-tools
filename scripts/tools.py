"""Command line entry point for working on the tools repository.

    python scripts/tools.py lint                 check the whole repository
    python scripts/tools.py list [--json]        list tools in build order
    python scripts/tools.py new <group>/<name>   create a tool from tools/_template
    python scripts/tools.py build [<tool>...]    stage tools into build/<platform>/stage/ (--jobs N, --no-cache)
    python scripts/tools.py assemble             pack the staged tools into out/<zip>
    python scripts/tools.py smoke                run the smoke tests against out/<zip>
    python scripts/tools.py matrix               print the CI build matrix as JSON
    python scripts/tools.py version              print the toolset version (toolset.toml)
    python scripts/tools.py release-check        check the release against the previous one (--previous)
    python scripts/tools.py release-index        write the release index and notes for out/ (--assets, --notes)

build, assemble and smoke work on the host platform unless --platform is given.
Downloads are cached in .cache/downloads/ by SHA-256, git sources in .cache/git/ by commit,
tools built from source in .cache/stages/ (see stagecache.py).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path

from archive import ArchiveError
from assemble import AssembleError, assemble, zip_name
from common import GROUP_RE, NAME_RE, REPO_ROOT, TOOL_FILE, display_path, host_platform, load_toml
from discover import discover, order_by_dependencies
from fetch import FetchError
from lint import PLATFORMS_FILE, TOOLSET_FILE, lint_repo
from parallel import Logger, default_jobs, run_ordered
from release import ReleaseError, build_index, check_release, index_name, release_notes
from smoke import run_smoke
import stagecache
from stage import StageError, stage_tool

TEMPLATE_DIR = "tools/_template"
NAME_PLACEHOLDER = "@NAME@"


def _print_problems(problems) -> None:
    for problem in problems:
        print(problem, file=sys.stderr)
    count = len(problems)
    print(f"{count} problem{'s' if count != 1 else ''}", file=sys.stderr)


def command_lint(args: argparse.Namespace) -> int:
    root = Path(args.root)
    tools, problems = lint_repo(root)
    if problems:
        _print_problems(problems)
        return 1
    count = len(tools)
    print(f"ok: {count} tool{'s' if count != 1 else ''}")
    return 0


class Repo:
    """A linted repository: tools in dependency order plus platforms and toolset version."""

    def __init__(self, root: Path):
        self.root = root
        tools, problems = lint_repo(root)
        if problems:
            _print_problems(problems)
            raise SystemExit(1)
        self.tools, _ = order_by_dependencies(tools, root)
        self.platforms = load_toml(root / PLATFORMS_FILE)["platforms"]
        self.version = load_toml(root / TOOLSET_FILE)["version"]
        self.build_dir = root / "build"
        self.cache_dir = root / ".cache" / "downloads"
        self.stage_cache_dir = root / ".cache" / "stages"
        self.out_dir = root / "out"

    def platform(self, requested: str | None) -> str:
        platform = requested or host_platform()
        if platform is None:
            print("cannot tell this machine's platform; pass --platform", file=sys.stderr)
            raise SystemExit(2)
        if platform not in self.platforms:
            known = ", ".join(sorted(self.platforms))
            print(f"unknown platform '{platform}' (known: {known})", file=sys.stderr)
            raise SystemExit(2)
        return platform


PIPELINE_ERRORS = (ArchiveError, AssembleError, FetchError, StageError)


def command_build(args: argparse.Namespace) -> int:
    repo = Repo(Path(args.root))
    platform = repo.platform(args.platform)
    by_name = {tool.name: tool for tool in repo.tools}
    for name in args.tools:
        if name not in by_name:
            print(f"unknown tool '{name}'", file=sys.stderr)
            return 2
        if platform not in by_name[name].spec["platforms"]:
            print(f"{name} does not ship on {platform}", file=sys.stderr)
            return 2
    # Named tools bring the tools they depend on.
    wanted = set(args.tools)
    queue = list(args.tools)
    while queue:
        for dependency in by_name[queue.pop()].dependency_names():
            if dependency in by_name and dependency not in wanted:
                wanted.add(dependency)
                queue.append(dependency)
    selected = [
        tool
        for tool in repo.tools
        if platform in tool.spec["platforms"] and (not args.tools or tool.name in wanted)
    ]

    if not args.no_cache:
        for name in stagecache.prune(repo.stage_cache_dir, platform, set(by_name)):
            print(f"removed the cached build of '{name}', which is no longer a tool")

    jobs = args.jobs or default_jobs(len(selected))
    logger = Logger(lambda message: print(message, flush=True))

    def work(name: str) -> None:
        stage_tool(
            by_name[name],
            platform,
            repo.build_dir,
            repo.cache_dir,
            log=logger.prefixed(name),
            platform_info=repo.platforms[platform],
            tools=by_name,
            stage_cache=None if args.no_cache else repo.stage_cache_dir,
        )

    failures, not_started = run_ordered(
        [tool.name for tool in selected],
        {tool.name: tool.dependency_names() for tool in selected},
        work,
        jobs,
    )
    for name, error in failures.items():
        if isinstance(error, PIPELINE_ERRORS):
            print(f"{name}: {error}", file=sys.stderr)
        else:
            print(f"{name}: unexpected error", file=sys.stderr)
            traceback.print_exception(error)
    if not_started:
        print(f"not built after the failure: {', '.join(not_started)}", file=sys.stderr)
    if failures:
        return 1
    count = len(selected)
    print(f"built {count} tool{'s' if count != 1 else ''} for {platform} ({jobs} job{'s' if jobs != 1 else ''})")
    return 0


def command_assemble(args: argparse.Namespace) -> int:
    repo = Repo(Path(args.root))
    platform = repo.platform(args.platform)
    try:
        assemble(repo.tools, repo.version, platform, repo.platforms[platform], repo.build_dir, repo.out_dir)
    except PIPELINE_ERRORS as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def command_smoke(args: argparse.Namespace) -> int:
    repo = Repo(Path(args.root))
    platform = repo.platform(args.platform)
    zip_path = Path(args.zip) if args.zip else repo.out_dir / zip_name(repo.version, platform)
    if not zip_path.is_file():
        print(f"{zip_path} not found; run assemble first", file=sys.stderr)
        return 1
    try:
        failures = run_smoke(repo.tools, repo.version, platform, zip_path, repo.build_dir / platform / "smoke")
    except PIPELINE_ERRORS as error:
        print(error, file=sys.stderr)
        return 1
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


def command_matrix(args: argparse.Namespace) -> int:
    repo = Repo(Path(args.root))
    include = [
        {
            "platform": token,
            "runner": info["runner"],
            "container": info.get("container", ""),
        }
        for token, info in repo.platforms.items()
    ]
    print(json.dumps({"include": include}, separators=(",", ":")))
    return 0


def command_version(args: argparse.Namespace) -> int:
    print(Repo(Path(args.root)).version)
    return 0


def command_release_check(args: argparse.Namespace) -> int:
    repo = Repo(Path(args.root))
    previous = None
    if args.previous:
        previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
    removed = load_toml(repo.root / TOOLSET_FILE).get("removed", [])
    problems = check_release(repo.tools, repo.version, removed, previous)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    after = f" after {previous['toolset']}" if previous else " (first release)"
    print(f"ok: {repo.version} can be released{after}")
    return 0


def command_release_index(args: argparse.Namespace) -> int:
    repo = Repo(Path(args.root))
    asset_dir = Path(args.assets) if args.assets else repo.out_dir
    try:
        index = build_index(repo.tools, repo.version, list(repo.platforms), asset_dir)
    except (ReleaseError, OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        return 1
    path = asset_dir / index_name(repo.version)
    path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {path}")
    if args.notes:
        Path(args.notes).write_text(release_notes(index, list(repo.platforms)), encoding="utf-8", newline="\n")
        print(f"wrote {args.notes}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    root = Path(args.root)
    tools, problems = discover(root / "tools", root)
    ordered, cycle_problems = order_by_dependencies(tools, root)
    problems = sorted(problems + cycle_problems)
    if problems:
        _print_problems(problems)
        return 1

    rows = [
        {
            "name": tool.name,
            "group": tool.group,
            "path": display_path(tool.directory, root),
            "version": tool.spec.get("version"),
            "platforms": tool.spec.get("platforms", []),
            "depends": tool.dependency_names(),
        }
        for tool in ordered
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no tools")
        return 0
    width = max(len(row["path"]) for row in rows)
    for row in rows:
        platforms = ", ".join(str(p) for p in row["platforms"])
        print(f"{row['path']:<{width}}  {row['version']}  [{platforms}]")
    return 0


def command_new(args: argparse.Namespace) -> int:
    root = Path(args.root)
    parts = args.path.strip("/").split("/")
    if len(parts) != 2:
        print("expected <group>/<name>, for example media/ffmpeg", file=sys.stderr)
        return 2
    group, name = parts
    if not GROUP_RE.match(group):
        print(f"invalid group '{group}': use [a-z][a-z0-9_-]*", file=sys.stderr)
        return 2
    if not NAME_RE.match(name):
        print(f"invalid tool name '{name}': use [a-z][a-z0-9_]*", file=sys.stderr)
        return 2

    tools_root = root / "tools"
    group_dir = tools_root / group
    if not group_dir.is_dir():
        groups = sorted(
            p.name for p in tools_root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))
        )
        print(
            f"group '{group}' does not exist (existing: {', '.join(groups)}); "
            "create the folder first if a new group is intended",
            file=sys.stderr,
        )
        return 2

    tools, _ = discover(tools_root, root)
    for tool in tools:
        if tool.name == name:
            print(f"a tool named '{name}' already exists at {display_path(tool.directory, root)}", file=sys.stderr)
            return 2
    target = group_dir / name
    if target.exists():
        print(f"{display_path(target, root)} already exists", file=sys.stderr)
        return 2

    template = root / TEMPLATE_DIR
    shutil.copytree(template, target)
    for path in sorted(target.rglob("*")):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace(NAME_PLACEHOLDER, name), encoding="utf-8", newline="\n")

    print(f"created {display_path(target / TOOL_FILE, root)}")
    print("fill in its values, then run: python scripts/tools.py lint")
    return 0


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tools.py", description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository root (default: this checkout)")
    commands = parser.add_subparsers(dest="command", required=True)

    lint = commands.add_parser("lint", help="check platforms.toml, toolset.toml and every tool.toml")
    lint.set_defaults(handler=command_lint)

    listing = commands.add_parser("list", help="list tools in dependency order")
    listing.add_argument("--json", action="store_true", help="print JSON")
    listing.set_defaults(handler=command_list)

    new = commands.add_parser("new", help="create tools/<group>/<name>/ from the template")
    new.add_argument("path", metavar="<group>/<name>")
    new.set_defaults(handler=command_new)

    platform_help = "host platform token (default: this machine)"
    build = commands.add_parser("build", help="stage tools for one platform")
    build.add_argument("tools", nargs="*", metavar="<tool>", help="tools to build (default: all)")
    build.add_argument("--platform", help=platform_help)
    build.add_argument(
        "--jobs", type=_positive_int, default=0, metavar="N", help="tools built at once (default: automatic)"
    )
    build.add_argument(
        "--no-cache",
        action="store_true",
        help="build tools from source even when .cache/stages/ has them (release builds)",
    )
    build.set_defaults(handler=command_build)

    assemble_parser = commands.add_parser("assemble", help="pack the staged tools into the release zip")
    assemble_parser.add_argument("--platform", help=platform_help)
    assemble_parser.set_defaults(handler=command_assemble)

    smoke = commands.add_parser("smoke", help="run the smoke tests against the release zip")
    smoke.add_argument("--platform", help=platform_help)
    smoke.add_argument("--zip", help="zip to test (default: the one assemble writes)")
    smoke.set_defaults(handler=command_smoke)

    matrix = commands.add_parser("matrix", help="print the CI build matrix as JSON")
    matrix.set_defaults(handler=command_matrix)
    version = commands.add_parser("version", help="print the toolset version from toolset.toml")
    version.set_defaults(handler=command_version)

    release_check = commands.add_parser("release-check", help="check the release against the previous one")
    release_check.add_argument("--previous", metavar="FILE", help="the previous release's -release.json")
    release_check.set_defaults(handler=command_release_check)

    release_index = commands.add_parser("release-index", help="write the release index next to the host zips")
    release_index.add_argument("--assets", metavar="DIR", help="folder holding every host zip (default: out/)")
    release_index.add_argument("--notes", metavar="FILE", help="also write the release notes (Markdown) here")
    release_index.set_defaults(handler=command_release_index)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
