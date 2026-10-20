"""PairDown backend routes, mounted by Hermes at /api/plugins/pairdown/.

Filesystem access is pure pathlib/os: no subprocess, no shell. Every
caller-supplied relative path is checked lexically and again after symlink
resolution, so nothing outside the vault root is ever read.
"""

import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

IGNORED_DIRS = frozenset({".obsidian", ".trash", "node_modules"})
NOTE_SUFFIX = ".md"

# Obsidian refuses these characters in file names on every platform, so a
# note path never legitimately contains them. ":" also blocks NTFS alternate
# data streams and drive-letter paths; "\\" is normalised to "/" first.
_FORBIDDEN_PATH_CHARS = re.compile(r'[:\x00*?"<>|]')


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


def _resolves_inside(path: Path, base: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


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
    if any(part in IGNORED_DIRS for part in parts[:-1]):
        raise HTTPException(status_code=403, detail="path is inside an ignored folder")
    if not parts[-1].lower().endswith(NOTE_SUFFIX):
        raise HTTPException(status_code=400, detail="only .md notes can be read")
    return parts


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
    """List Markdown notes under root as sorted, POSIX-style relative paths."""
    base = _root_dir(root)
    notes: list[str] = []
    for current, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in filenames:
            if not name.lower().endswith(NOTE_SUFFIX):
                continue
            full = Path(current, name)
            if full.is_symlink() and not _resolves_inside(full, base):
                continue
            notes.append(full.relative_to(base).as_posix())
    notes.sort()
    return {"root": root, "count": len(notes), "files": notes}


@router.get("/read")
def read_note(root: str, path: str):
    """Return the UTF-8 text of one note, byte-exact (CRLF is preserved)."""
    base = _root_dir(root)
    parts = _note_parts(path)
    target = base.joinpath(*parts)
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="note not found")
    try:
        resolved.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403, detail="path resolves outside root")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="note not found")
    try:
        raw = resolved.read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="note could not be read")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="note is not valid UTF-8")
    return {"path": "/".join(parts), "size": len(raw), "content": content}
