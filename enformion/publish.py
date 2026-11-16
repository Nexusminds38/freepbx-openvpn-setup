"""Publish a complete file without a durable partial copy.

The bytes live in an unnamed inode until ``linkat`` creates the final name.
A crash before that call leaves no directory entry. Replacing an existing
single-episode file stages ``name.replacing`` and then renames it. The staged
file is a complete episode file, never a mixed log. Recovery finishes or
discards that stage without moving a deadline later.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import stat

AT_EMPTY_PATH = 0x1000
_LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
FAULT_AFTER: str | None = None


def checkpoint(name: str) -> None:
    """Test seam. Raising here models a crash after a durable step."""
    if FAULT_AFTER == name:
        raise RuntimeError(f"injected fault after {name}")


class PublishError(OSError):
    """A file could not be published without risking another episode."""


def _link_unnamed(descriptor: int, directory_fd: int, name: str) -> None:
    result = _LIBC.linkat(
        descriptor,
        ctypes.c_char_p(b""),
        directory_fd,
        ctypes.c_char_p(name.encode()),
        AT_EMPTY_PATH,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise PublishError(error, os.strerror(error), name)


def create_complete_file(directory: str, name: str, data: bytes) -> None:
    """Create ``name`` only after ``data`` is fully written. The name must be new."""
    if os.path.basename(name) != name or name in {".", ".."}:
        raise PublishError("publication name must be a single path segment")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        descriptor = os.open(directory, os.O_TMPFILE | os.O_RDWR, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            _link_unnamed(descriptor, directory_fd, name)
            os.fsync(directory_fd)
            checkpoint(f"linked:{name}")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def read_exclusive(path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat_is_regular(info) or info.st_nlink != 1:
            raise PublishError("refusing to read a linked file")
        chunks: list[bytes] = []
        while True:
            piece = os.read(descriptor, 65536)
            if not piece:
                break
            chunks.append(piece)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _fsync_directory(directory: str) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stat_is_regular(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def replace_exclusive_file(directory: str, name: str, new_data: bytes, expected: bytes) -> None:
    """Replace one file if its bytes are still ``expected``.

    The staged file is complete. A crash before rename leaves ``name.replacing``
    beside the unchanged original. Recovery keeps the earlier deadline.
    """
    destination = os.path.join(directory, name)
    if read_exclusive(destination) != expected:
        raise PublishError("file changed before replace")
    staged = name + ".replacing"
    if os.path.lexists(os.path.join(directory, staged)):
        raise PublishError("a staged replacement is already present")
    create_complete_file(directory, staged, new_data)
    checkpoint(f"staged:{name}")
    if read_exclusive(destination) != expected:
        raise PublishError("file changed after the replacement was staged")
    os.replace(os.path.join(directory, staged), destination)
    _fsync_directory(directory)


def _episode_deadline(data: object) -> tuple[str | None, str | None]:
    if not isinstance(data, dict):
        return None, None
    episode_id = data.get("episode_id")
    if not isinstance(episode_id, str) or not episode_id.strip():
        nested = data.get("episode")
        if isinstance(nested, dict) and isinstance(nested.get("episode_id"), str):
            episode_id = nested["episode_id"]
        else:
            episode_id = None
    expires = data.get("expires_at")
    provenance = data.get("provenance")
    if not isinstance(expires, str) and isinstance(provenance, dict):
        expires = provenance.get("expires_at")
    if not isinstance(expires, str):
        expires = None
    return episode_id, expires


def _single_episode_deadline(raw: bytes) -> tuple[str, str] | None:
    text = raw.decode("utf-8")
    if not text.strip():
        return None
    episodes: set[str] = set()
    deadlines: set[str] = set()
    try:
        if text.lstrip().startswith("{") and "\n{" not in text:
            data = json.loads(text)
            episode_id, expires = _episode_deadline(data)
            if episode_id and expires:
                return episode_id, expires
            return None
    except json.JSONDecodeError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        episode_id, expires = _episode_deadline(data)
        if not episode_id or not expires:
            return None
        episodes.add(episode_id)
        deadlines.add(expires)
    if len(episodes) == 1 and len(deadlines) == 1:
        return next(iter(episodes)), next(iter(deadlines))
    return None


def recover_staged_replacements(root: str) -> int:
    """Finish or discard ``*.replacing`` files. Never move a deadline later."""
    finished = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in list(filenames):
            if not name.endswith(".replacing"):
                continue
            staged = os.path.join(dirpath, name)
            info = os.lstat(staged)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PublishError("staged replacement is linked; refusing to guess")
            final = staged[: -len(".replacing")]
            if not os.path.lexists(final):
                raise PublishError("staged replacement has no original; refusing to guess")
            staged_id = _single_episode_deadline(read_exclusive(staged))
            final_id = _single_episode_deadline(read_exclusive(final))
            if staged_id is None or final_id is None or staged_id[0] != final_id[0]:
                raise PublishError("staged replacement does not match the original episode")
            import retention
            staged_deadline = retention.try_aware_deadline(staged_id[1])
            final_deadline = retention.try_aware_deadline(final_id[1])
            if staged_deadline is None or final_deadline is None:
                raise PublishError("staged replacement deadline is not timezone-aware")
            if staged_deadline > final_deadline:
                os.unlink(staged)
            else:
                os.replace(staged, final)
            finished += 1
    return finished
