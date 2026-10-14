"""The cmake strategy: build a tool from source with CMake and Ninja.

The source (a git ref or an archive) is put in build/<platform>/work/<tool>/src
and each CMake project in it (the source root, or every folder of cmake_dirs)
is configured and built under build/<platform>/work/<tool>/build. The pick
patterns then choose the shipped files from that build tree, like they choose
them from an archive for repackage.

CMake and Ninja are this repository's own: the tools named in 'uses' are
built first, and their programs are the only ones searched for 'cmake' and
'ninja'. The compiler is the one the environment provides, or the one CC and CXX
name in build_env or the tool's env (on Windows: MSVC, found with vswhere
when cl is not already on PATH, or Visual Studio's clang-cl).

The compiler and CMake settings shared by every source build of a platform
(CPU baseline, C runtime, macOS deployment target) come from platforms.toml:
build_env is added to the environment, cmake_args to every configure.
Commands write to build/<platform>/logs/<tool>.log; a failure shows its end.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections import deque
from pathlib import Path, PurePosixPath

from archive import FILE, LINK, extract, select_all
from fetch import checkout_git, commit_time, fetch, fetch_git

LOG_TAIL = 60

# vcvarsall.bat argument for a native build on each Windows ISA.
VCVARS_ARCH = {"x86_64": "x64", "arm64": "arm64"}
VCVARS_COMPONENT = {
    "x86_64": "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
    "arm64": "Microsoft.VisualStudio.Component.VC.Tools.ARM64",
}


class BuildError(Exception):
    """A source build failed."""


# Environment ----------------------------------------------------------------------


def visual_studio(isa: str) -> Path:
    """The newest Visual Studio installation with the C++ tools for isa."""
    program_files = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        raise BuildError(f"Visual Studio not found ({vswhere} is missing)")
    found = subprocess.run(
        [str(vswhere), "-latest", "-products", "*", "-requires", VCVARS_COMPONENT[isa], "-property", "installationPath"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    installation = found.stdout.strip().splitlines()[0] if found.stdout.strip() else ""
    if not installation:
        raise BuildError(f"no Visual Studio installation with the C++ tools for {isa}")
    return Path(installation)


def msvc_environment(isa: str) -> dict[str, str]:
    """The environment of a Visual Studio developer prompt for a native build on isa."""
    vcvarsall = visual_studio(isa) / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    if not vcvarsall.is_file():
        raise BuildError(f"{vcvarsall} not found")
    # A string, not a list: cmd.exe needs these exact quotes, which
    # subprocess would otherwise escape.
    command = f'cmd.exe /d /s /c ""{vcvarsall}" {VCVARS_ARCH[isa]} >nul && set"'
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False)
    if result.returncode != 0:
        raise BuildError(f"{vcvarsall} {VCVARS_ARCH[isa]} failed: {result.stdout.strip()[-500:]}")
    environment = {}
    for line in result.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator and name:
            environment[name] = value
    return environment


def _take_path(environment: dict[str, str]) -> str:
    """Remove PATH from environment whatever its case (Windows: 'Path') and return it."""
    value = ""
    for name in [name for name in environment if name.upper() == "PATH"]:
        value = environment.pop(name) or value
    return value


def compiler_environment(os_name: str, isa: str, cc: str | None = None, cxx: str | None = None) -> dict[str, str]:
    """The environment with the C and C++ compilers a source build uses.

    cc and cxx (from build_env or the tool's env) choose the compilers; by
    default the environment's own (CC, CXX, or what CMake finds). On Windows
    the default is MSVC's cl, from a developer prompt set up with vswhere when
    cl is not on PATH; clang-cl (Visual Studio's LLVM tools) can be chosen too,
    and its folder is then added to PATH.
    """
    if os_name != "windows":
        environment = dict(os.environ)
        environment.update({name: value for name, value in (("CC", cc), ("CXX", cxx)) if value})
        return environment
    environment = dict(os.environ) if shutil.which("cl") else msvc_environment(isa)
    # Not MinGW or clang that may also be on PATH.
    environment["CC"] = cc or "cl"
    environment["CXX"] = cxx or "cl"
    path = _take_path(environment)
    if any(shutil.which(environment[name], path=path) is None for name in ("CC", "CXX")):
        llvm = visual_studio(isa) / "VC" / "Tools" / "Llvm" / ("ARM64" if isa == "arm64" else "x64") / "bin"
        if llvm.is_dir():
            path = os.pathsep.join([path, str(llvm)])
    environment["PATH"] = path
    return environment


def _append_flags(environment: dict[str, str], name: str, flags: list[str]) -> None:
    current = environment.get(name, "").strip()
    environment[name] = " ".join([current, *flags] if current else flags)


def build_environment(
    os_name: str,
    isa: str,
    platform_env: dict[str, str],
    tool_env: dict[str, str],
    tool_dirs: list[Path],
    source: Path,
    build: Path,
    cpm_cache: Path,
    source_date_epoch: int | None = None,
) -> dict[str, str]:
    """The environment for configuring and building one tool."""
    settings = {**platform_env, **tool_env}
    environment = compiler_environment(os_name, isa, settings.get("CC"), settings.get("CXX"))
    environment.update(settings)
    path = _take_path(environment)
    environment["PATH"] = os.pathsep.join([*(str(d) for d in tool_dirs), path])
    # Dependencies downloaded by CPM (Tracy) are kept between builds.
    environment.setdefault("CPM_SOURCE_CACHE", str(cpm_cache))

    # Keep build paths and times out of the binaries where it is cheap.
    # GCC and Clang use SOURCE_DATE_EPOCH for __DATE__ and __TIME__ (Tracy
    # shows its build date); it is the source's commit time.
    if source_date_epoch is not None:
        environment.setdefault("SOURCE_DATE_EPOCH", str(source_date_epoch))
    if os_name == "windows":
        for name in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            _append_flags(environment, name, ["/Brepro"])
    else:
        prefixes = [(source, "/src"), (build, "/build"), (cpm_cache, "/cpm")]
        flags = [f"-ffile-prefix-map={old}={new}" for old, new in prefixes if " " not in str(old)]
        for name in ("CFLAGS", "CXXFLAGS"):
            _append_flags(environment, name, flags)
        if os_name == "macos":
            environment.setdefault("ZERO_AR_DATE", "1")
    return environment


# Running commands -----------------------------------------------------------------


def run_logged(command: list[str], *, cwd: Path, environment: dict[str, str], log_file: Path, step: str) -> None:
    """Run a command, appending its output to log_file; on failure raise with the log's end."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"\n$ {' '.join(command)}\n")
        handle.flush()
        try:
            result = subprocess.run(command, cwd=cwd, env=environment, stdout=handle, stderr=subprocess.STDOUT, check=False)
        except OSError as error:
            raise BuildError(f"{step}: cannot run {command[0]}: {error}") from None
    if result.returncode != 0:
        with log_file.open(encoding="utf-8", errors="replace") as handle:
            tail = "".join(deque(handle, LOG_TAIL))
        raise BuildError(f"{step} failed (exit {result.returncode}); end of {log_file}:\n{tail.rstrip()}")


# Picking from a directory -------------------------------------------------------------


def copy_picked(root: Path, stage: Path, select) -> None:
    """Copy the files of a directory tree that select picks, like extracting an archive.

    A picked symbolic link is copied as the file it points to, which must be
    inside root. Directories are walked, not picked themselves.
    """
    for directory, dirnames, filenames in os.walk(root):
        directory_path = Path(directory)
        dirnames.sort()
        entries = sorted(filenames) + [name for name in dirnames if (directory_path / name).is_symlink()]
        for name in entries:
            path = directory_path / name
            relative = PurePosixPath(path.relative_to(root).as_posix())
            kind = LINK if path.is_symlink() else FILE
            target = select(relative, kind)
            if target is None:
                continue
            source = path
            if kind == LINK:
                source = Path(os.path.realpath(path))
                if not source.is_relative_to(root.resolve()) or not source.is_file():
                    raise BuildError(f"{relative}: picked link does not point to a file in the build tree")
            destination = stage / target
            if destination.exists():
                raise BuildError(f"{target}: picked more than once")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            executable = source.stat().st_mode & stat.S_IXUSR
            destination.chmod(0o755 if executable else 0o644)
        # Symbolic links to directories were handled above; never walk them.
        dirnames[:] = [name for name in dirnames if not (directory_path / name).is_symlink()]


# The strategy ---------------------------------------------------------------------


def find_program(name: str, tool_dirs: list[Path]) -> str:
    found = shutil.which(name, path=os.pathsep.join(str(d) for d in tool_dirs))
    if found is None:
        raise BuildError(f"no '{name}' among the programs of the tools in 'uses'")
    return found


def prepare_source(sources: list[dict], src: Path, caches: dict[str, Path], log) -> int | None:
    """Put the tool's single source (git or archive) into src; return a git source's commit time."""
    if len(sources) != 1:
        raise BuildError("strategy 'cmake' needs exactly one source per platform")
    source = sources[0]
    if "git" in source:
        repository = fetch_git(source["git"], source["ref"], source["commit"], caches["git"], log=log)
        checkout_git(repository, source["ref"], source["commit"], src, submodules=source.get("submodules", False))
        return commit_time(src)
    else:
        downloaded = fetch(source["url"], source["sha256"], caches["downloads"], log=log)
        extract(
            downloaded,
            src,
            source.get("strip_components", 0),
            select_all,
            backslash_paths=source.get("backslash_paths", False),
        )
        return None


def cmake_build(
    *,
    build: dict,
    sources: list[dict],
    platform_info: dict,
    tool_dirs: list[Path],
    work: Path,
    log_file: Path,
    caches: dict[str, Path],
    log=print,
) -> Path:
    """Fetch, configure and build; return the build tree the pick patterns apply to."""
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    log_file.unlink(missing_ok=True)
    src = work / "src"
    tree = work / "build"
    source_date_epoch = prepare_source(sources, src, caches, log)

    cmake = find_program("cmake", tool_dirs)
    ninja = find_program("ninja", tool_dirs)
    environment = build_environment(
        platform_info["os"],
        platform_info["isa"],
        platform_info.get("build_env", {}),
        build.get("env", {}),
        tool_dirs,
        src,
        tree,
        caches["cpm"],
        source_date_epoch,
    )
    directories = build.get("cmake_dirs", ["."])
    for directory in directories:
        source_dir = (src / directory).resolve()
        binary_dir = (tree / directory).resolve()
        if not (source_dir / "CMakeLists.txt").is_file():
            raise BuildError(f"{directory}/CMakeLists.txt not found in the source")
        name = "the source" if directory == "." else directory
        log(f"configuring {name}")
        configure = [
            cmake,
            "-S", str(source_dir),
            "-B", str(binary_dir),
            "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_MAKE_PROGRAM={ninja}",
            *platform_info.get("cmake_args", []),
            *build.get("args", []),
        ]
        run_logged(configure, cwd=work, environment=environment, log_file=log_file, step=f"configuring {name}")
        log(f"building {name}")
        command = [cmake, "--build", str(binary_dir)]
        if "target" in build:
            command += ["--target", build["target"]]
        run_logged(command, cwd=work, environment=environment, log_file=log_file, step=f"building {name}")
    return tree
