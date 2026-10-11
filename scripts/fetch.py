"""Download files into a cache keyed by their SHA-256.

A file is only ever stored under the hash it was checked against, so a cached
file can be reused without contacting the server again. Downloads go to a
temporary file first and are renamed into place only after the hash matches.
"""

from __future__ import annotations

import hashlib
import os
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
