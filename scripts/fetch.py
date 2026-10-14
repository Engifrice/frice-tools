"""Download sources into caches: files keyed by SHA-256, git refs by commit.

A file is only ever stored under the hash it was checked against, so a cached
file can be reused without contacting the server again. Downloads go to a
temporary file first and are renamed into place only after the hash matches.
Git sources are fetched the same way: into a temporary bare repository that
is renamed into place only once the ref is known to point at the commit.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 1 << 20
USER_AGENT = "frice-tools-fetch/1"


class FetchError(Exception):
    """A download failed or did not match its expected hash."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, target: Path, timeout: float) -> str:
    """Stream url into target and return the SHA-256 of what was written."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as out:
        for chunk in iter(lambda: response.read(CHUNK), b""):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def fetch(
    url: str,
    sha256: str,
    cache_dir: Path,
    *,
    attempts: int = 3,
    timeout: float = 60.0,
    log=print,
) -> Path:
    """Return the path of the cached file with this hash, downloading it if needed."""
    sha256 = sha256.lower()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / sha256
    if cached.is_file():
        if sha256_file(cached) == sha256:
            return cached
        log(f"cache entry {cached.name} is corrupt; downloading again")
        cached.unlink()

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        handle, temporary = tempfile.mkstemp(dir=cache_dir, prefix=".partial-")
        os.close(handle)
        temporary_path = Path(temporary)
        try:
            log(f"downloading {url}" + (f" (attempt {attempt})" if attempt > 1 else ""))
            actual = _download(url, temporary_path, timeout)
            if actual != sha256:
                # A wrong hash is not retried: the server returned a different file.
                raise FetchError(f"{url}: expected sha256 {sha256}, got {actual}")
            os.replace(temporary_path, cached)
            return cached
        except FetchError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            last_error = error
            if attempt < attempts:
                time.sleep(2 ** attempt)
        finally:
            temporary_path.unlink(missing_ok=True)
    raise FetchError(f"{url}: download failed after {attempts} attempts: {last_error}")


# Git sources --------------------------------------------------------------------

# Never prompt for credentials, and leave Git LFS files as pointers: the
# sources we build do not need them, and some repositories hold gigabytes.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
GIT_CONFIG = ["-c", "advice.detachedHead=false", "-c", "core.autocrlf=false"]


class _GitFailed(Exception):
    """A git command failed; may be retried when it was a network operation."""


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    environment = dict(os.environ, **GIT_ENV)
    try:
        result = subprocess.run(
            ["git", *GIT_CONFIG, *args],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        raise FetchError("git is not installed") from None
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise _GitFailed(f"git {' '.join(args)} failed: {output}")
    return result.stdout.strip()


def tag_ref(ref: str) -> str:
    """The full name of a source's ref: a tag name, or a full refs/... name."""
    return ref if ref.startswith("refs/") else f"refs/tags/{ref}"


def fetch_git(url: str, ref: str, commit: str, cache_dir: Path, *, attempts: int = 3, log=print) -> Path:
    """Return a cached bare repository holding ref, checked to point at commit.

    Only the ref itself is fetched (depth 1), under its own name, so a tag
    stays available to 'git describe'. The repository is cached by commit.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{commit}.git"
    if cached.is_dir():
        return cached
    full_ref = tag_ref(ref)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        partial = Path(tempfile.mkdtemp(dir=cache_dir, prefix=".partial-"))
        try:
            log(f"fetching {url} {ref}" + (f" (attempt {attempt})" if attempt > 1 else ""))
            _git(["init", "-q", "--bare", str(partial)])
            _git(["fetch", "-q", "--depth", "1", "--no-tags", url, f"+{full_ref}:{full_ref}"], cwd=partial)
            actual = _git(["rev-parse", f"{full_ref}^{{commit}}"], cwd=partial)
            if actual != commit:
                # Not retried: the ref moved, or the tool.toml is wrong.
                raise FetchError(f"{url}: {ref} is commit {actual}, expected {commit}")
            os.replace(partial, cached)
            return cached
        except _GitFailed as error:
            last_error = error
            if attempt < attempts:
                time.sleep(2 ** attempt)
        finally:
            if partial.exists():
                shutil.rmtree(partial, ignore_errors=True)
    raise FetchError(f"{url}: fetch failed after {attempts} attempts: {last_error}")


def checkout_git(repository: Path, ref: str, commit: str, dest: Path, *, submodules: bool = False) -> None:
    """Make dest a working tree of the cached repository at commit, with ref present.

    The clone shares the cache's objects through hard links, so it is cheap,
    and the build can write into it without touching the cache.
    """
    try:
        _git(["clone", "-q", "--no-checkout", str(repository), str(dest)])
        full_ref = tag_ref(ref)
        if not full_ref.startswith("refs/tags/"):  # a clone only brings tags and branches
            _git(["fetch", "-q", str(repository), f"+{full_ref}:{full_ref}"], cwd=dest)
        _git(["checkout", "-q", "--detach", commit], cwd=dest)
        if submodules:
            _git(["submodule", "update", "-q", "--init", "--recursive", "--depth", "1"], cwd=dest)
    except _GitFailed as error:
        raise FetchError(str(error)) from None


def commit_time(checkout: Path) -> int:
    """The commit time of a working tree's HEAD, in seconds since the epoch."""
    try:
        return int(_git(["show", "-s", "--format=%ct", "HEAD"], cwd=checkout))
    except _GitFailed as error:
        raise FetchError(str(error)) from None
