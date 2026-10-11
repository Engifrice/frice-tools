"""Safe extraction of zip, tar.gz and tar.xz archives.

Every member path is checked before anything is written: no absolute paths,
no '..', no backslashes, no duplicates, no special files. The format is
detected from the first bytes, so cached files need no extension.

Symbolic links are never created: they are skipped and reported, so nothing
shipped can depend on one, and a link can never point outside the
destination. Tar hard links are written as ordinary copies of their target.

Unix permissions are kept only as "executable or not": extracted files are
0755 when the archive marks any execute bit, 0644 otherwise.
"""

from __future__ import annotations

import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

MAGIC_ZIP = b"PK\x03\x04"
MAGIC_GZIP = b"\x1f\x8b"
MAGIC_XZ = b"\xfd7zXZ\x00"


class ArchiveError(Exception):
    """The archive is unsupported or contains something unsafe."""


def detect_format(path: Path) -> str:
    with path.open("rb") as handle:
        head = handle.read(6)
    if head.startswith(MAGIC_ZIP):
        return "zip"
    if head.startswith(MAGIC_GZIP):
        return "tar.gz"
    if head.startswith(MAGIC_XZ):
        return "tar.xz"
    raise ArchiveError(f"{path}: not a zip, tar.gz or tar.xz archive")


def _member_path(name: str, strip: int, is_dir: bool) -> PurePosixPath | None:
    """The checked, stripped relative path of a member, or None for a stripped directory."""
    if "\\" in name:
        raise ArchiveError(f"member {name!r}: backslash in path")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ArchiveError(f"member {name!r}: absolute path")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ArchiveError(f"member {name!r}: '..' in path")
    if len(parts) <= strip:
        if is_dir:
            return None
        raise ArchiveError(f"member {name!r}: a file above the {strip} stripped directory level(s)")
    return PurePosixPath(*parts[strip:])


class _Writer:
    """Writes checked members below dest and rejects duplicates."""

    def __init__(self, dest: Path):
        self.dest = dest
        self.files: set[PurePosixPath] = set()

    def directory(self, relative: PurePosixPath) -> None:
        if relative in self.files:
            raise ArchiveError(f"member {relative}: both a file and a directory")
        (self.dest / relative).mkdir(parents=True, exist_ok=True)

    def file(self, relative: PurePosixPath, source, executable: bool) -> None:
        if relative in self.files:
            raise ArchiveError(f"member {relative}: appears more than once")
        for parent in relative.parents:
            if parent in self.files:
                raise ArchiveError(f"member {relative}: {parent} is a file")
        self.files.add(relative)
        target = self.dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            while chunk := source.read(1 << 20):
                out.write(chunk)
        target.chmod(0o755 if executable else 0o644)


def _extract_zip(path: Path, writer: _Writer, strip: int, skipped: list[str]) -> None:
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            # On Windows, zipfile turns backslashes into '/' in info.filename; check
            # the name as stored so every host accepts and rejects the same archives.
            name = info.orig_filename
            mode = info.external_attr >> 16 if info.create_system == 3 else 0
            kind = stat.S_IFMT(mode)  # 0 when the tool that made the zip left the type out
            if kind == stat.S_IFLNK:
                skipped.append(name)
                continue
            relative = _member_path(name, strip, info.is_dir())
            if relative is None:
                continue
            if info.is_dir():
                writer.directory(relative)
                continue
            if kind not in (0, stat.S_IFREG):
                raise ArchiveError(f"member {name!r}: not a regular file")
            with archive.open(info) as source:
                writer.file(relative, source, executable=bool(mode & 0o111))


def _extract_tar(path: Path, writer: _Writer, strip: int, compression: str, skipped: list[str]) -> None:
    with tarfile.open(path, f"r:{compression}") as archive:
        for member in archive:
            if member.issym():
                skipped.append(member.name)
                continue
            relative = _member_path(member.name, strip, member.isdir())
            if not (member.isfile() or member.isdir() or member.islnk()):
                raise ArchiveError(f"member {member.name!r}: not a regular file or directory")
            if relative is None:
                continue
            if member.isdir():
                writer.directory(relative)
                continue
            try:
                source = archive.extractfile(member)  # a hard link yields its target's data
            except KeyError as error:
                raise ArchiveError(f"member {member.name!r}: hard link to a missing member") from error
            assert source is not None
            with source:
                writer.file(relative, source, executable=bool(member.mode & 0o111))


def extract(path: Path, dest: Path, strip_components: int = 0) -> list[str]:
    """Extract the archive at path into dest, which must not exist yet.

    Returns the names of the symbolic links that were skipped.
    """
    if dest.exists():
        raise ArchiveError(f"{dest}: already exists")
    dest.mkdir(parents=True)
    writer = _Writer(dest)
    skipped: list[str] = []
    kind = detect_format(path)
    try:
        if kind == "zip":
            _extract_zip(path, writer, strip_components, skipped)
        else:
            _extract_tar(path, writer, strip_components, kind.split(".")[1], skipped)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as error:
        raise ArchiveError(f"{path}: corrupt archive: {error}") from error
    return skipped
