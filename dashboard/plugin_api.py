"""PairDown backend routes, mounted by Hermes at /api/plugins/pairdown/.

Filesystem access is pure pathlib/os: no subprocess, no shell. A caller-supplied
relative note path is checked lexically, then opened, and the location of the
file that was actually opened is verified through its handle. Containment is
never decided on a pathname that is looked up again afterwards, so a link or
folder swapped in at any moment cannot make a read leave the vault root.
"""

import errno
import os
import re
import stat
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

IGNORED_DIRS = frozenset({".obsidian", ".trash", "node_modules"})
NOTE_SUFFIX = ".md"

# Windows and macOS volumes are case-insensitive by default, so ".OBSIDIAN" names
# the same folder as ".obsidian" there. Linux stays case-sensitive.
_CASE_INSENSITIVE = sys.platform in ("win32", "darwin")


def _fold(name: str) -> str:
    return name.casefold() if _CASE_INSENSITIVE else name


_IGNORED_FOLDED = frozenset(_fold(name) for name in IGNORED_DIRS)


def _is_ignored(name: str) -> bool:
    return _fold(name) in _IGNORED_FOLDED


# Obsidian refuses these characters in file names on every platform, so a
# note path never legitimately contains them. ":" also blocks NTFS alternate
# data streams and drive-letter paths; "\\" is normalised to "/" first.
_FORBIDDEN_PATH_CHARS = re.compile(r'[:\x00*?"<>|]')

# Windows device names are reserved with any extension ("CON.md" is the console).
_WINDOWS_DEVICE_NAME = re.compile(
    r"^(con|prn|aux|nul|conin\$|conout\$|(com|lpt)[0-9\u00b9\u00b2\u00b3])(\..*)?$", re.IGNORECASE
)

_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    # A named pipe must not block the open; it is rejected after fstat.
    | getattr(os, "O_NONBLOCK", 0)
)


def _root_dir(root: str) -> Path:
    """Return the resolved vault root, or raise a 4xx if it is unusable."""
    if not root or "\x00" in root:
        raise HTTPException(status_code=400, detail="root must be a non-empty path")
    candidate = Path(root)
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="root must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="root does not exist")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="root is not a directory")
    return resolved


def _visible_relative(real: Path, base: Path) -> "list[str] | None":
    """Return real's parts below base, or None if it is outside base or inside an ignored folder."""
    try:
        parts = real.relative_to(base).parts
    except ValueError:
        return None
    if not parts or any(_is_ignored(part) for part in parts[:-1]):
        return None
    return list(parts)


def _note_parts(path: str) -> list[str]:
    """Split a caller-supplied relative note path into safe segments."""
    if not path or _FORBIDDEN_PATH_CHARS.search(path):
        raise HTTPException(status_code=400, detail="invalid path")
    normalised = path.replace("\\", "/")
    if normalised.startswith("/"):
        raise HTTPException(status_code=400, detail="path must be relative")
    parts = normalised.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise HTTPException(status_code=400, detail="path must not contain empty, '.' or '..' segments")
    if any(part != part.rstrip(" .") for part in parts):
        raise HTTPException(status_code=400, detail="path segments must not end with a space or dot")
    if sys.platform == "win32" and any(_WINDOWS_DEVICE_NAME.match(part) for part in parts):
        raise HTTPException(status_code=400, detail="path uses a reserved device name")
    if any(_is_ignored(part) for part in parts[:-1]):
        raise HTTPException(status_code=403, detail="path is inside an ignored folder")
    if not parts[-1].lower().endswith(NOTE_SUFFIX):
        raise HTTPException(status_code=400, detail="only .md notes can be read")
    return parts


def _final_path_windows(fd: int) -> str:
    """Ask Windows for the real path of the file behind an open descriptor."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_final_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final_path.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(fd)
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        # Flags 0: normalised name, DOS volume name. Links and junctions are already resolved.
        needed = get_final_path(handle, buffer, size, 0)
        if needed == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if needed < size:
            break
        size = needed + 1
    path = buffer.value
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _opened_path(fd: int) -> Path:
    """Return the real path of the file behind fd, as the kernel sees it right now."""
    if sys.platform == "win32":
        return Path(_final_path_windows(fd))
    if sys.platform.startswith("linux"):
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    if sys.platform == "darwin":
        import fcntl

        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
        return Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    raise OSError(errno.ENOSYS, "cannot resolve an open file to a path on this platform")


def _is_directory_link(info: os.stat_result) -> bool:
    """True for a symlink or an NTFS junction; other reparse points (cloud placeholders) are plain files."""
    if stat.S_ISLNK(info.st_mode):
        return True
    junction_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    return junction_tag is not None and getattr(info, "st_reparse_tag", 0) == junction_tag


def _walk_notes(base: Path):
    """Yield the Markdown files under base without ever descending into a linked folder."""
    pending = [str(base)]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as scanner:
                entries = list(scanner)
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_directory_link(info):
                try:
                    if entry.is_dir():
                        continue
                except OSError:
                    continue
                if entry.name.lower().endswith(NOTE_SUFFIX):
                    # A linked file is listed only if it really lives inside the vault.
                    try:
                        real = Path(entry.path).resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    if _visible_relative(real, base) is not None:
                        yield Path(entry.path)
            elif stat.S_ISDIR(info.st_mode):
                if not _is_ignored(entry.name):
                    pending.append(entry.path)
            elif stat.S_ISREG(info.st_mode) and entry.name.lower().endswith(NOTE_SUFFIX):
                yield Path(entry.path)


@router.get("/health")
def health():
    return {"ok": True, "plugin": "pairdown", "python": sys.version.split()[0], "platform": sys.platform}


@router.get("/validate")
def validate(root: str):
    """Report whether root is an Obsidian vault, i.e. contains a .obsidian directory."""
    candidate = Path(root) if root and "\x00" not in root else None
    if candidate is None or not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="root must be an absolute path")
    try:
        exists = candidate.is_dir()
        is_vault = exists and (candidate / ".obsidian").is_dir()
    except OSError:
        exists = is_vault = False
    return {"root": root, "exists": exists, "valid": is_vault}


@router.get("/list")
def list_notes(root: str):
    """List Markdown notes under root as sorted, POSIX-style relative paths.

    Symlinked and junctioned folders are never entered, so a link cannot make the
    walk leave the vault or loop. Names are enumerated, not read; the walk is not
    protected against a folder being swapped for a link while it runs.
    """
    base = _root_dir(root)
    notes = sorted(path.relative_to(base).as_posix() for path in _walk_notes(base))
    return {"root": root, "count": len(notes), "files": notes}


@router.get("/read")
def read_note(root: str, path: str):
    """Return the UTF-8 text of one note, byte-exact (CRLF is preserved)."""
    base = _root_dir(root)
    parts = _note_parts(path)
    try:
        fd = os.open(base.joinpath(*parts), _OPEN_FLAGS)
    except OSError:
        raise HTTPException(status_code=404, detail="note not found")
    try:
        try:
            real = _opened_path(fd)
        except OSError:
            # Fail closed: without the opened file's real path nothing can be proven.
            raise HTTPException(status_code=501, detail="cannot verify the location of the opened file")
        if _visible_relative(real, base) is None:
            raise HTTPException(status_code=403, detail="path resolves outside root or into an ignored folder")
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HTTPException(status_code=404, detail="note not found")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read()
    except OSError:
        raise HTTPException(status_code=404, detail="note could not be read")
    finally:
        os.close(fd)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="note is not valid UTF-8")
    return {"path": "/".join(parts), "size": len(raw), "content": content}
