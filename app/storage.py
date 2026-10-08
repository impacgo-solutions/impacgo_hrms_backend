import os
import shutil
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from .config import settings


def winlong_path(path: Path) -> Path:
    r"""On Windows, returns the `\\?\`-prefixed extended-length form of an
    absolute path, which lifts the legacy 260-character MAX_PATH limit to
    ~32,767 -- this project's own folder is unusually deeply nested
    (repeated `impacgo-people-frontEnd(N)` segments from zip
    re-extraction), so even a short uuid-based filename can push the full
    absolute path over that limit, making `open()`/`mkdir()` fail with
    FileNotFoundError even though every path segment is individually
    valid. No-op on non-Windows platforms or paths already prefixed."""
    if os.name != "nt":
        return path
    resolved = str(path.resolve())
    if resolved.startswith("\\\\?\\"):
        return path
    return Path(f"\\\\?\\{resolved}")


# Mirrors the Flutter client's own allowlist (lib/widgets/drag_drop_file_upload.dart),
# widened slightly to cover policy-document/template uploads (spreadsheets,
# slides, plain text) that aren't employee-document specific, so a direct API
# call can't bypass what the UI already restricts. 25MB matches that widget's
# higher cap; the server is the actual enforcement point.
_ALLOWED_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv",
}
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# L-07: content must match the extension (magic bytes), so an .exe renamed
# .pdf or an HTML page renamed .png is refused at upload time. Text types
# have no signature; they are refused if they contain NUL bytes (binary).
_ZIP = b"PK\x03\x04"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".docx": (_ZIP,), ".xlsx": (_ZIP,), ".pptx": (_ZIP,),
    ".doc": (_OLE,), ".xls": (_OLE,), ".ppt": (_OLE,),
}
_TEXT_EXTENSIONS = {".txt", ".csv"}
_TYPE_LABELS = {
    ".pdf": "PDF", ".png": "PNG image", ".jpg": "JPG image", ".jpeg": "JPG image",
    ".doc": "Word", ".docx": "Word", ".xls": "Excel", ".xlsx": "Excel",
    ".ppt": "PowerPoint", ".pptx": "PowerPoint", ".txt": "text", ".csv": "CSV",
}


def content_matches_extension(head: bytes, extension: str) -> bool:
    """True when the first bytes of a file are plausible for `extension`."""
    if extension == ".pdf":
        # Leading whitespace before the header is tolerated by every reader.
        return head[:1024].lstrip().startswith(b"%PDF-")
    signatures = _SIGNATURES.get(extension)
    if signatures is not None:
        return any(head.startswith(s) for s in signatures)
    if extension in _TEXT_EXTENSIONS:
        return b"\x00" not in head
    return True


def uploads_root() -> Path:
    root = Path(settings.uploads_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def delete_uploaded_file(file_url: str) -> None:
    """Removes a file previously written by save_uploaded_file, given the
    file_url it returned (e.g. '/media/employee_document/<id>/<name>').
    Best-effort: a missing file (already deleted, or a pre-existing record
    with no real file on disk) is not an error."""
    if not file_url.startswith("/media/"):
        return
    path = uploads_root() / file_url.removeprefix("/media/")
    winlong_path(path).unlink(missing_ok=True)


def save_uploaded_file(
    upload: UploadFile, *, entity_type: str, entity_id: uuid.UUID
) -> tuple[str, int]:
    """Writes an uploaded file to disk under uploads/<entity_type>/<entity_id>/,
    naming it with a fresh uuid (+ original extension) so two uploads never
    collide and the stored path stays short regardless of the original
    filename, and returns (file_url, size_bytes). file_url is a path
    servable via the /media static mount set up in main.py."""
    original_name = Path(upload.filename or "file").name
    extension = Path(original_name).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{extension or 'unknown'}' is not allowed. "
            f"Allowed types: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
        )
    # Store under a short, bounded name (uuid + extension only) rather than
    # appending the original filename -- the original can be long (spaces,
    # dates, etc.) and combined with a deeply nested project path can push
    # the full absolute path past Windows' 260-character MAX_PATH limit,
    # causing `dest.open()` to fail with FileNotFoundError even though the
    # parent directory was just created successfully. The original filename
    # isn't needed for uniqueness (the uuid already guarantees that) or for
    # display (no caller reads it back out of file_url).
    # L-07: refuse empty files and content that doesn't match the extension
    # BEFORE anything is written to disk.
    head = upload.file.read(8192)
    upload.file.seek(0)
    if not head:
        raise HTTPException(status_code=400, detail="The selected file is empty.")
    if not content_matches_extension(head, extension):
        raise HTTPException(
            status_code=400,
            detail=f"'{original_name}' is not a valid {_TYPE_LABELS.get(extension, extension)} file "
            "(its content doesn't match the extension).",
        )
    stored_name = f"{uuid.uuid4()}{extension}"
    entity_dir = uploads_root() / entity_type / str(entity_id)
    winlong_path(entity_dir).mkdir(parents=True, exist_ok=True)
    dest = entity_dir / stored_name
    long_dest = winlong_path(dest)
    size_bytes = 0
    with long_dest.open("wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            size_bytes += len(chunk)
            if size_bytes > _MAX_UPLOAD_BYTES:
                out.close()
                long_dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail=f"File exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit.",
                )
            out.write(chunk)
    file_url = f"/media/{entity_type}/{entity_id}/{stored_name}"
    return file_url, size_bytes
