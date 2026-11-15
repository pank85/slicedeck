#!/usr/bin/env python3
"""Extract every printable model from an uploaded .zip, fail-closed.

An uploaded archive is untrusted input, so every rule here is fail-closed:

  * the archive has to parse (``zipfile.BadZipFile`` is a 400 - a truncated upload is
    this case) and its entry count is capped before anything is inspected (413);
  * **every** member name is validated, not just the ones that will be written: an
    absolute path, a ``..`` component, a symlink, an encrypted member or an unsupported
    compression method rejects the whole archive (400). A hostile archive must not
    "mostly work", so nothing is silently skipped and a later bad entry is not a
    successful prefix;
  * each model is written to ``dest_dir/<ordinal>/<basename>``. The member path is never
    used as a path, so no member can direct the write outside ``dest_dir``, and two
    members that share a basename do not collide;
  * the bytes are written through a ``.part`` sibling and streamed in 1 MiB chunks.
    ``max_uncompressed`` bounds each member and the sum of extracted model bytes. The
    per-member ``file_size`` in the header is attacker-controlled, so it is only an early
    check; the running count is the real bound. A rejection removes files this call
    created and leaves unrelated caller data in ``dest_dir`` alone.

Nested archives are not recursed into: ``.zip`` is removed from ``allowed`` before the
candidate suffixes are computed. Non-model entries are reported in ``skipped`` and are
not extracted.

Order is deterministic (see :func:`_sort_key`): shallower members first, then
case-insensitive, then bytewise. Duplicate names keep their zip order because the sort
is stable and each member is opened by its ``ZipInfo``, not by a name lookup.
"""
from __future__ import annotations

import os
import shutil
import zipfile
import zlib
import lzma
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Entry cap for an uploaded archive: a model zip is a handful of files plus maybe a
# README, so anything this large is either junk or an attack.
ZIP_MAX_MEMBERS = 64

CHUNK_BYTES = 1 << 20
PART_SUFFIX = ".part"
FILE_MODE = 0o644

MACOS_METADATA = "__macosx"

# Methods this process can actually inflate. An unknown method is rejected before any
# write; a method whose codec is missing still fails as an unreadable stream.
_KNOWN_COMPRESSION = {
    zipfile.ZIP_STORED,
    zipfile.ZIP_DEFLATED,
    zipfile.ZIP_BZIP2,
    zipfile.ZIP_LZMA,
}

_DECOMPRESSION_ERRORS = (
    zipfile.BadZipFile,
    RuntimeError,
    NotImplementedError,
    zlib.error,
    lzma.LZMAError,
    EOFError,
    OSError,
)


class ArchiveError(Exception):
    """A rejected archive. ``status`` is the HTTP status to answer with (400 or 413)."""

    status: int

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Selection:
    """One extracted model member, plus what the archive contained."""

    member: str
    written: Path
    candidates: list[str]
    skipped: list[str]
    members: int


def _posix_name(member: str) -> str:
    """The member name with Windows separators folded to '/'.

    ZIP entries are supposed to use '/', but a hostile archive may use '\\' to hide an
    absolute path or a traversal from a '/'-only check.
    """
    return member.replace("\\", "/")


def _is_absolute(name: str) -> bool:
    """True for '/x', '//host/share' and 'C:\\x' (after :func:`_posix_name`)."""
    if name.startswith("/"):
        return True
    head = name.split("/", 1)[0]
    return len(head) == 2 and head[0].isalpha() and head[1] == ":"


def _is_unsafe(member: str) -> bool:
    name = _posix_name(member)
    parts = PurePosixPath(name).parts
    return not parts or _is_absolute(name) or ".." in parts


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _canonical_part(member: str) -> str:
    """Canonical path for duplicate detection: Windows separators folded,
    redundant slashes collapsed, dot segments resolved, and casefolded."""
    name = _posix_name(member)
    is_dir = name.endswith("/")
    norm = PurePosixPath(name).as_posix().casefold()
    return norm + ("/" if is_dir and not norm.endswith("/") else "")


def _validate_entry(info: zipfile.ZipInfo) -> None:
    if _is_unsafe(info.filename):
        raise ArchiveError(400, f"archive member is not safe: {info.filename}")
    if _is_symlink(info):
        raise ArchiveError(400, f"archive member is a symlink: {info.filename}")
    if info.flag_bits & 0x1 or info.compress_type not in _KNOWN_COMPRESSION:
        raise _unreadable(info)


def _is_skipped(member: str) -> bool:
    """Entries that are noise rather than models: directories and macOS metadata.

    Dotfiles and ``._*`` basenames are macOS resource forks, which would otherwise be
    treated as models in an archive exported from Finder.
    """
    name = _posix_name(member)
    parts = PurePosixPath(name).parts
    if name.endswith("/") or not parts:
        return True
    if parts[0].casefold() == MACOS_METADATA:
        return True
    return parts[-1].startswith(".")


def _is_model(info: zipfile.ZipInfo, models: set[str]) -> bool:
    if _is_skipped(info.filename):
        return False
    return PurePosixPath(_posix_name(info.filename)).suffix.lower() in models


def _basename(member: str) -> str:
    return PurePosixPath(_posix_name(member)).name


def _sort_key(member: str) -> tuple[int, str, str]:
    name = _posix_name(member)
    return (len(PurePosixPath(name).parts), name.casefold(), name)


def _member_limit(max_uncompressed: int) -> str:
    return f"archive member expands to more than {max_uncompressed} bytes"


def _aggregate_limit(max_uncompressed: int) -> str:
    return f"archive models expand to more than {max_uncompressed} bytes"


def _unreadable(exc: BaseException) -> ArchiveError:
    return ArchiveError(400, "not a readable .zip archive")


def _remove_created(paths: list[Path]) -> None:
    """Drop only the ordinal directories this call created. Caller data stays."""
    for path in reversed(paths):
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)


def extract_models(archive: Path, dest_dir: Path, *, allowed: set[str],
                   max_uncompressed: int,
                   max_members: int = ZIP_MAX_MEMBERS) -> list[Selection]:
    """Extract every model member of ``archive`` into ordinal subdirectories of ``dest_dir``.

    ``allowed`` is the caller's suffix set (``.zip`` is dropped here, so nested archives
    are never followed). One :class:`Selection` is returned per printable member, in
    :func:`_sort_key` order. ``max_uncompressed`` bounds each member and the sum of the
    extracted model bytes. Raises :class:`ArchiveError` for anything rejected; files this
    call created are removed, and pre-existing files in ``dest_dir`` are not.
    """
    models = {suffix.lower() for suffix in allowed} - {".zip"}
    try:
        handle = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise _unreadable(exc) from exc

    created: list[Path] = []
    try:
        with handle:
            infos = handle.infolist()
            if len(infos) > max_members:
                raise ArchiveError(413, f"archive has more than {max_members} entries")
            for info in infos:
                _validate_entry(info)

            printable = [info for info in infos if _is_model(info, models)]
            considered = [info.filename for info in infos if not _is_skipped(info.filename)]
            if not printable:
                found = ", ".join(considered) or "nothing"
                raise ArchiveError(
                    400, "archive contains no .stl, .3mf or .obj file "
                         f"(found: {found})")

            # Stable: equal names keep infolist order, and each ZipInfo is opened as itself.
            ordered = sorted(printable, key=lambda info: _sort_key(info.filename))
            declared = 0
            for info in ordered:
                if info.file_size > max_uncompressed:
                    raise ArchiveError(413, _member_limit(max_uncompressed))
                if declared + info.file_size > max_uncompressed:
                    raise ArchiveError(413, _aggregate_limit(max_uncompressed))
                declared += info.file_size

            printable_ids = {id(info) for info in ordered}
            candidates = [info.filename for info in ordered]
            skipped = sorted(
                (info.filename for info in infos if id(info) not in printable_ids),
                key=_sort_key)
            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            selections: list[Selection] = []
            spent = 0
            for index, info in enumerate(ordered):
                sub = dest_dir / f"{index:02d}"
                sub.mkdir()
                created.append(sub)
                written, nbytes = _extract(
                    handle, info, sub,
                    member_limit=max_uncompressed,
                    remaining=max_uncompressed - spent)
                spent += nbytes
                selections.append(Selection(
                    member=info.filename, written=written, candidates=candidates,
                    skipped=skipped, members=len(infos)))
            return selections
    except BaseException:
        _remove_created(created)
        raise


def _extract(handle: zipfile.ZipFile, info: zipfile.ZipInfo, dest_dir: Path, *,
             member_limit: int, remaining: int) -> tuple[Path, int]:
    basename = _basename(info.filename)
    if not basename:
        raise ArchiveError(400, f"archive member is not safe: {info.filename}")
    # Header sizes are attacker-controlled. They fail an obvious overflow before the
    # first read; the running count below is what actually bounds the write.
    if info.file_size > member_limit:
        raise ArchiveError(413, _member_limit(member_limit))
    if info.file_size > remaining:
        raise ArchiveError(413, _aggregate_limit(member_limit))

    written = Path(dest_dir) / basename
    part = Path(dest_dir) / (basename + PART_SUFFIX)
    total = 0
    try:
        try:
            src_cm = handle.open(info)
        except _DECOMPRESSION_ERRORS as exc:
            raise _unreadable(exc) from exc

        with src_cm as src, part.open("wb") as out:
            while True:
                try:
                    chunk = src.read(CHUNK_BYTES)
                except _DECOMPRESSION_ERRORS as exc:
                    raise _unreadable(exc) from exc
                if not chunk:
                    break
                total += len(chunk)
                if total > member_limit:
                    raise ArchiveError(413, _member_limit(member_limit))
                if total > remaining:
                    raise ArchiveError(413, _aggregate_limit(member_limit))
                out.write(chunk)
        os.chmod(part, FILE_MODE)
        os.replace(part, written)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return written, total


def validate_archive(archive: Path, *, max_uncompressed: int,
                     max_members: int = ZIP_MAX_MEMBERS) -> None:
    """Validate that ``archive`` is a well-formed, safe zip within bounds.

    Applies all-entry name/type/compression checks, member-count cap,
    duplicate canonical path detection, and stream-discards archive contents
    with declared and actual per-member and aggregate decompression bounds.
    """
    archive_path = Path(archive)
    try:
        handle = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise _unreadable(exc) from exc

    with handle:
        infos = handle.infolist()
        if len(infos) > max_members:
            raise ArchiveError(413, f"archive has more than {max_members} entries")

        seen_canonical: set[str] = set()
        declared_total = 0
        for info in infos:
            _validate_entry(info)

            canonical = _canonical_part(info.filename)
            if canonical in seen_canonical:
                raise ArchiveError(400, f"duplicate archive member: {info.filename}")
            seen_canonical.add(canonical)

            if info.file_size > max_uncompressed:
                raise ArchiveError(413, _member_limit(max_uncompressed))
            declared_total += info.file_size
            if declared_total > max_uncompressed:
                raise ArchiveError(413, _aggregate_limit(max_uncompressed))

        streamed_total = 0
        for info in infos:
            streamed_member = 0
            try:
                src_cm = handle.open(info)
            except _DECOMPRESSION_ERRORS as exc:
                raise _unreadable(exc) from exc

            with src_cm as src:
                while True:
                    try:
                        chunk = src.read(CHUNK_BYTES)
                    except _DECOMPRESSION_ERRORS as exc:
                        raise _unreadable(exc) from exc
                    if not chunk:
                        break
                    chunk_len = len(chunk)
                    streamed_member += chunk_len
                    streamed_total += chunk_len
                    if streamed_member > max_uncompressed:
                        raise ArchiveError(413, _member_limit(max_uncompressed))
                    if streamed_total > max_uncompressed:
                        raise ArchiveError(413, _aggregate_limit(max_uncompressed))

