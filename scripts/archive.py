"""Safe extraction of zip, tar.gz and tar.xz archives.

Every member path is checked before anything is written: no absolute paths,
no '..', no backslashes, no duplicates, no special files. The format is
detected from the first bytes, so cached files need no extension.

Symbolic links are never created. A link the select callback picks is written
as a regular file holding its target's content (the target must be a regular
file inside the archive; chains of links are followed). Every other link is
skipped and reported. Tar hard links are written as copies of their target.

A select callback chooses which members to write and where, so callers
extract only what they need in a single pass over the archive. A second pass
happens only when a picked link points to a file the first pass did not keep.
Every member is still checked, whether it is written or not.

Unix permissions are kept only as "executable or not": extracted files are
0755 when the archive marks any execute bit, 0644 otherwise.
"""

from __future__ import annotations

import posixpath
import shutil
import stat
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAGIC_ZIP = b"PK\x03\x04"
MAGIC_GZIP = b"\x1f\x8b"
MAGIC_XZ = b"\xfd7zXZ\x00"
MAX_LINK_HOPS = 32

# Kinds of members passed to a select callback.
FILE = "file"
DIRECTORY = "dir"
LINK = "link"


class ArchiveError(Exception):
    """The archive is unsupported or contains something unsafe."""


# select(path, kind) -> where to write the member below dest, or None to skip it.
Select = Callable[[PurePosixPath, str], "PurePosixPath | None"]


def select_all(path: PurePosixPath, kind: str) -> PurePosixPath | None:
    """Write every file and directory where it is; skip links."""
    return None if kind == LINK else path


@dataclass
class ExtractResult:
    skipped_links: list[str] = field(default_factory=list)
    shipped_links: list[str] = field(default_factory=list)


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


def _parts(name: str) -> list[str]:
    """The checked path components of a member name."""
    if "\\" in name:
        raise ArchiveError(f"member {name!r}: backslash in path")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ArchiveError(f"member {name!r}: absolute path")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ArchiveError(f"member {name!r}: '..' in path")
    return parts


def _member_path(name: str, strip: int, is_dir: bool) -> PurePosixPath | None:
    """The checked, stripped relative path of a member, or None for a stripped directory."""
    parts = _parts(name)
    if len(parts) <= strip:
        if is_dir:
            return None
        raise ArchiveError(
            f"member {name!r}: a file above the {strip} stripped directory level(s)"
        )
    return PurePosixPath(*parts[strip:])


def _link_target(link: str, target: str) -> str | None:
    """The archive name a link points to, or None if it leaves the archive."""
    if target.startswith("/") or "\\" in target:
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(link), target))
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        return None
    return joined


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
        target = self.dest / relative
        # target.exists() catches files written by an earlier archive into the same dest.
        if relative in self.files or target.exists():
            raise ArchiveError(f"member {relative}: appears more than once")
        for parent in relative.parents:
            if parent in self.files:
                raise ArchiveError(f"member {relative}: {parent} is a file")
        self.files.add(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as out:
            shutil.copyfileobj(source, out, 1 << 20)
        target.chmod(0o755 if executable else 0o644)

    def copy(self, existing: PurePosixPath, relative: PurePosixPath) -> None:
        source_path = self.dest / existing
        with source_path.open("rb") as source:
            self.file(
                relative, source, executable=bool(source_path.stat().st_mode & 0o111)
            )


class _Links:
    """Picked links, resolved once the archive's regular files are known."""

    def __init__(self):
        self.targets: dict[str, str | None] = (
            {}
        )  # every link: archive name -> target name
        self.wanted: dict[PurePosixPath, str] = (
            {}
        )  # dest path -> picked link's archive name
        self.written: dict[str, PurePosixPath] = (
            {}
        )  # regular files written: archive name -> dest path
        self.regular: set[str] = set()  # every regular file's archive name

    def resolve(self, link: str) -> str:
        current = link
        for _ in range(MAX_LINK_HOPS):
            target = self.targets[current]
            if target is None:
                raise ArchiveError(f"link {link!r} points outside the archive")
            if target not in self.targets:
                if target not in self.regular:
                    raise ArchiveError(
                        f"link {link!r} points to {target!r}, which is not a file in the archive"
                    )
                return target
            current = target
        raise ArchiveError(f"link {link!r}: more than {MAX_LINK_HOPS} links in a chain")

    def finish(
        self,
        writer: _Writer,
        read_targets: Callable[[dict[str, list[PurePosixPath]]], None],
    ) -> list[str]:
        """Write every picked link; read_targets fetches files the first pass did not keep."""
        missing: dict[str, list[PurePosixPath]] = {}
        for dest, link in sorted(self.wanted.items()):
            target = self.resolve(link)
            if target in self.written:
                writer.copy(self.written[target], dest)
            else:
                missing.setdefault(target, []).append(dest)
        if missing:
            read_targets(missing)
        return sorted(self.wanted.values())


def _extract_zip(
    path: Path, writer: _Writer, strip: int, select: Select, result: ExtractResult
) -> None:
    links = _Links()
    with zipfile.ZipFile(path) as archive:
        infos = {}
        for info in archive.infolist():
            # On Windows, zipfile turns backslashes into '/' in info.filename; check
            # the name as stored so every host accepts and rejects the same archives.
            name = info.orig_filename
            mode = info.external_attr >> 16 if info.create_system == 3 else 0
            kind = stat.S_IFMT(
                mode
            )  # 0 when the tool that made the zip left the type out
            if kind == stat.S_IFLNK:
                relative = _member_path(name, strip, False)
                key = "/".join(_parts(name))
                links.targets[key] = _link_target(
                    key, archive.read(info).decode("utf-8", "replace")
                )
                target = select(relative, LINK)
                if target is None:
                    result.skipped_links.append(name)
                else:
                    links.wanted[target] = key
                continue
            relative = _member_path(name, strip, info.is_dir())
            if not info.is_dir() and kind not in (0, stat.S_IFREG):
                raise ArchiveError(f"member {name!r}: not a regular file")
            if not info.is_dir():
                key = "/".join(_parts(name))
                links.regular.add(key)
                infos[key] = info
            if relative is None:
                continue
            target = select(relative, DIRECTORY if info.is_dir() else FILE)
            if target is None:
                continue
            if info.is_dir():
                writer.directory(target)
                continue
            with archive.open(info) as source:
                writer.file(target, source, executable=bool(mode & 0o111))
            links.written[key] = target

        def read_targets(missing: dict[str, list[PurePosixPath]]) -> None:
            for name, dests in missing.items():
                info = infos[name]
                mode = info.external_attr >> 16 if info.create_system == 3 else 0
                with archive.open(info) as source:
                    writer.file(dests[0], source, executable=bool(mode & 0o111))
                for dest in dests[1:]:
                    writer.copy(dests[0], dest)

        result.shipped_links.extend(links.finish(writer, read_targets))


def _extract_tar(
    path: Path,
    writer: _Writer,
    strip: int,
    compression: str,
    select: Select,
    result: ExtractResult,
) -> None:
    links = _Links()
    with tarfile.open(path, f"r:{compression}") as archive:
        for member in archive:
            if member.issym():
                relative = _member_path(member.name, strip, False)
                key = "/".join(_parts(member.name))
                links.targets[key] = _link_target(key, member.linkname)
                target = select(relative, LINK)
                if target is None:
                    result.skipped_links.append(member.name)
                else:
                    links.wanted[target] = key
                continue
            relative = _member_path(member.name, strip, member.isdir())
            if not (member.isfile() or member.isdir() or member.islnk()):
                raise ArchiveError(
                    f"member {member.name!r}: not a regular file or directory"
                )
            if not member.isdir():
                links.regular.add("/".join(_parts(member.name)))
            if relative is None:
                continue
            target = select(relative, DIRECTORY if member.isdir() else FILE)
            if target is None:
                continue
            if member.isdir():
                writer.directory(target)
                continue
            try:
                source = archive.extractfile(
                    member
                )  # a hard link yields its target's data
            except KeyError as error:
                raise ArchiveError(
                    f"member {member.name!r}: hard link to a missing member"
                ) from error
            assert source is not None
            with source:
                writer.file(target, source, executable=bool(member.mode & 0o111))
            links.written["/".join(_parts(member.name))] = target

    def read_targets(missing: dict[str, list[PurePosixPath]]) -> None:
        # Second pass: the targets came before their links and were not picked.
        # It stops at the last target instead of reading to the end.
        remaining = dict(missing)
        with tarfile.open(path, f"r:{compression}") as archive:
            for member in archive:
                if member.issym() or member.isdir():
                    continue
                dests = remaining.pop("/".join(_parts(member.name)), None)
                if not dests:
                    continue
                source = archive.extractfile(member)
                assert source is not None
                with source:
                    writer.file(dests[0], source, executable=bool(member.mode & 0o111))
                for dest in dests[1:]:
                    writer.copy(dests[0], dest)
                if not remaining:
                    break

    result.shipped_links.extend(links.finish(writer, read_targets))


def extract(
    path: Path,
    dest: Path,
    strip_components: int = 0,
    select: Select = select_all,
    *,
    merge: bool = False,
) -> ExtractResult:
    """Extract the archive at path into dest.

    dest must not exist unless merge is true; then it may already hold files
    from other archives, and writing over any of them is an error. select
    receives each member's path (after strip_components) and its kind (FILE,
    DIRECTORY or LINK) and returns where to write it below dest, or None.
    """
    if dest.exists() and not merge:
        raise ArchiveError(f"{dest}: already exists")
    dest.mkdir(parents=True, exist_ok=True)
    writer = _Writer(dest)
    result = ExtractResult()
    kind = detect_format(path)
    try:
        if kind == "zip":
            _extract_zip(path, writer, strip_components, select, result)
        else:
            _extract_tar(
                path, writer, strip_components, kind.split(".")[1], select, result
            )
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as error:
        raise ArchiveError(f"{path}: corrupt archive: {error}") from error
    return result
