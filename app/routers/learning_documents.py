"""Learning & Development > Learning Documents (db/add_learning_documents.sql).

Who may do what (all inside the caller's own company -- tenant schema +
company_id; another company's document id is indistinguishable from a
missing one, 404):

  view / search / preview / download   every employee of the company
  upload                                anyone with Learning access
                                        (learning:view, or Owner)
  edit (metadata / replace file)        the uploader, or learning:edit (HR/Admin)
  delete                                the uploader, or learning:delete (HR/Admin)

Files go through the existing upload storage (storage.save_uploaded_file ->
uploads/learning_document/<id>/) and are read back only through /media
(media_access: same company, document not deleted) or GET /{id}/file.
Validation: allowed extensions, 25 MB cap, no empty files, the content must
match its extension (magic bytes), and the same file (SHA-256) can't be
uploaded twice in a company. Every upload / edit / replace / delete is
recorded in the upload history.
"""

import datetime
import hashlib
import json
import mimetypes
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pydantic import Field as PydField
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crud, form_config, models
from ..database import get_db
from ..deps import _OWNER_ROLE_NAME, get_current_user, require_module_enabled
from ..storage import (OPTIONAL_EXTENSIONS, _ALLOWED_EXTENSIONS, _MAX_UPLOAD_BYTES, delete_uploaded_file,
                       save_uploaded_file, uploads_root, winlong_path)

router = APIRouter(
    prefix="/api/learning-documents", tags=["learning"],
    dependencies=[Depends(require_module_enabled("learning"))],
)

ENTITY_TYPE = "learning_document"
# The categories are a Form Configuration list ("learning_document.category");
# admins and users can add their own ("Add custom...").
_TITLE_MAX = 200
_DESCRIPTION_MAX = 2000

# extension -> (file-type label used for display + filtering, MIME type)
FILE_TYPES: dict[str, tuple[str, str]] = {
    ".pdf": ("PDF", "application/pdf"),
    ".doc": ("Word", "application/msword"),
    ".docx": ("Word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xls": ("Excel", "application/vnd.ms-excel"),
    ".xlsx": ("Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".ppt": ("PowerPoint", "application/vnd.ms-powerpoint"),
    ".pptx": ("PowerPoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".png": ("Image", "image/png"),
    ".jpg": ("Image", "image/jpeg"),
    ".jpeg": ("Image", "image/jpeg"),
    ".txt": ("Text", "text/plain"),
    ".csv": ("CSV", "text/csv"),
    ".json": ("JSON", "application/json"),
}
_EXTRA_EXTENSIONS = frozenset({".json"})  # opted into storage (not accepted by other uploads)
assert set(FILE_TYPES) <= _ALLOWED_EXTENSIONS | _EXTRA_EXTENSIONS <= _ALLOWED_EXTENSIONS | OPTIONAL_EXTENSIONS

_ZIP = b"PK\x03\x04"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF",), ".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
    ".docx": (_ZIP,), ".xlsx": (_ZIP,), ".pptx": (_ZIP,),
    ".doc": (_OLE,), ".xls": (_OLE,), ".ppt": (_OLE,),
}

_SORTS = {"uploaded_at", "title", "size", "file_type", "uploader", "category"}


# ── permissions ─────────────────────────────────────────────────────────────

def _is_owner(db: Session, user: models.User) -> bool:
    return any(r.name == _OWNER_ROLE_NAME for r in crud.get_user_roles(db, user.id))


def _has(db: Session, user: models.User, action: str) -> bool:
    return _is_owner(db, user) or crud.user_has_action(db, user.id, "learning", action)


def _perms(db: Session, user: models.User) -> dict:
    return {"can_upload": _has(db, user, "view"), "can_edit_any": _has(db, user, "edit"),
            "can_delete_any": _has(db, user, "delete")}


def _get(db: Session, user: models.User, doc_id: uuid.UUID, *, lock: bool = False) -> models.LearningDocument:
    q = select(models.LearningDocument).where(models.LearningDocument.id == doc_id)
    doc = db.scalar(q.with_for_update() if lock else q)
    if doc is None or doc.company_id != user.company_id or doc.is_deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


# ── validation ──────────────────────────────────────────────────────────────

def _read_validated(upload: UploadFile) -> tuple[bytes, str, str]:
    """(content, extension, sha256) of an acceptable upload, else 4xx."""
    name = Path(upload.filename or "").name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Choose a file to upload.")
    ext = Path(name).suffix.lower()
    if ext not in FILE_TYPES:
        raise HTTPException(status_code=400, detail=f"File type '{ext or 'unknown'}' is not supported. Allowed: "
                                                    + ", ".join(e.lstrip('.').upper() for e in FILE_TYPES))
    data = upload.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
    if not data:
        raise HTTPException(status_code=422, detail="The file is empty.")
    sigs = _SIGNATURES.get(ext)
    if sigs and not any(data.startswith(s) for s in sigs):
        raise HTTPException(status_code=400, detail=f"'{name}' is not a valid {FILE_TYPES[ext][0]} file "
                                                    "(its content doesn't match the extension).")
    if ext in (".txt", ".csv", ".json") and b"\x00" in data[:8192]:
        raise HTTPException(status_code=400, detail=f"'{name}' is not a plain-text file.")
    if ext == ".json":
        try:
            json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(status_code=400, detail=f"'{name}' is not a valid JSON file.") from None
    upload.file.seek(0)
    return data, ext, hashlib.sha256(data).hexdigest()


def _clean_meta(title: str | None, description: str | None, category: str | None, *,
                db: Session, company_id: uuid.UUID, current_category: str | None = None) -> tuple:
    t = (title or "").strip() if title is not None else None
    if t is not None and not (3 <= len(t) <= _TITLE_MAX):
        raise HTTPException(status_code=422, detail=f"Title must be 3–{_TITLE_MAX} characters.")
    d = description.strip() if description is not None else None
    if d is not None and len(d) > _DESCRIPTION_MAX:
        raise HTTPException(status_code=422, detail=f"Description can be at most {_DESCRIPTION_MAX} characters.")
    c = None
    if category is not None and category.strip():  # blank = unchanged / the default
        # An unchanged (possibly since-hidden) category still saves.
        c = form_config.require_option(db, company_id, "learning_document.category", category,
                                       current=current_category, required=True)
    return t, d, c


def _duplicate(db: Session, company_id: uuid.UUID, sha: str, exclude: uuid.UUID | None = None) -> None:
    q = select(models.LearningDocument).where(
        models.LearningDocument.company_id == company_id, models.LearningDocument.content_sha256 == sha,
        models.LearningDocument.is_deleted.is_(False))
    if exclude is not None:
        q = q.where(models.LearningDocument.id != exclude)
    dup = db.scalar(q)
    if dup is not None:
        who = crud.employee_display_name(db, dup.uploaded_by_employee_id) if dup.uploaded_by_employee_id else "an admin"
        raise HTTPException(status_code=409, detail=(
            f"This file has already been uploaded as \"{dup.title}\" by {who} on "
            f"{dup.uploaded_at.astimezone(_IST).strftime('%d-%m-%Y')}."))


_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


# ── output ──────────────────────────────────────────────────────────────────

def _file_path(doc: models.LearningDocument) -> Path:
    return winlong_path(uploads_root() / (doc.file_url or "").removeprefix("/media/"))


def _uploader_names(db: Session, docs) -> dict:
    return crud.employee_display_names_bulk(db, {d.uploaded_by_employee_id for d in docs})


def _out(db: Session, user: models.User, doc: models.LearningDocument, names: dict | None = None,
         perms: dict | None = None, folders: dict | None = None) -> dict:
    names = names if names is not None else _uploader_names(db, [doc])
    perms = perms or _perms(db, user)
    mine = doc.uploaded_by_user_id == user.id
    if folders is None and doc.folder_id is not None:
        folders = _folder_index(db, doc.company_id)
    chain = _chain(doc.folder_id, folders or {})
    return {
        # False when the stored file isn't in this server's upload storage
        # (e.g. uploaded through another server sharing the database).
        "file_available": _file_path(doc).is_file(),
        "folder_id": doc.folder_id,
        "folder_path": " / ".join(f.name for f in chain),
        "id": doc.id, "title": doc.title, "description": doc.description, "category": doc.category,
        "file_name": doc.file_name, "file_ext": doc.file_ext.lstrip("."),
        "file_type": FILE_TYPES.get(doc.file_ext, ("File", ""))[0], "mime_type": doc.mime_type,
        "size_bytes": doc.size_bytes, "file_url": doc.file_url, "version": doc.version,
        "uploaded_by_employee_id": doc.uploaded_by_employee_id,
        "uploaded_by": names.get(doc.uploaded_by_employee_id, "Admin") if doc.uploaded_by_employee_id else "Admin",
        "uploaded_at": doc.uploaded_at, "updated_at": doc.updated_at, "is_mine": mine,
        "can_edit": mine or perms["can_edit_any"], "can_delete": mine or perms["can_delete_any"],
    }


# ── folders ─────────────────────────────────────────────────────────────────

_FOLDER_NAME_MAX = 255
_MAX_DEPTH = 20
_BULK_MAX_FILES = 100


def _folder_index(db: Session, company_id: uuid.UUID) -> dict:
    """Every live folder of the company, by id."""
    return {f.id: f for f in db.scalars(select(models.LearningFolder).where(
        models.LearningFolder.company_id == company_id, models.LearningFolder.is_deleted.is_(False)))}


def _chain(folder_id: uuid.UUID | None, index: dict) -> list:
    """Folders from the top level down to [folder_id] (inclusive)."""
    chain, seen = [], set()
    while folder_id is not None and folder_id in index and folder_id not in seen:
        seen.add(folder_id)
        chain.append(index[folder_id])
        folder_id = index[folder_id].parent_id
    return list(reversed(chain))


def _clean_folder_name(name: str | None) -> str:
    n = " ".join((name or "").split())
    if not n:
        raise HTTPException(status_code=422, detail="Folder name is required.")
    if len(n) > _FOLDER_NAME_MAX:
        raise HTTPException(status_code=422, detail=f"Folder name can be at most {_FOLDER_NAME_MAX} characters.")
    if n in (".", "..") or any(c in n for c in '/\\:*?"<>|'):
        raise HTTPException(status_code=422, detail='Folder name can\'t contain / \\ : * ? " < > |')
    return n


def _folder_or_404(db: Session, user: models.User, folder_id: uuid.UUID, *, lock: bool = False) -> models.LearningFolder:
    q = select(models.LearningFolder).where(models.LearningFolder.id == folder_id)
    f = db.scalar(q.with_for_update() if lock else q)
    if f is None or f.company_id != user.company_id or f.is_deleted:
        raise HTTPException(status_code=404, detail="Folder not found")
    return f


def _sibling(db: Session, company_id: uuid.UUID, parent_id: uuid.UUID | None, name: str,
             exclude: uuid.UUID | None = None) -> models.LearningFolder | None:
    F = models.LearningFolder
    q = select(F).where(F.company_id == company_id, F.is_deleted.is_(False), func.lower(F.name) == name.lower(),
                        F.parent_id.is_(None) if parent_id is None else F.parent_id == parent_id)
    if exclude is not None:
        q = q.where(F.id != exclude)
    return db.scalar(q.limit(1))


def _ensure_folder(db: Session, user: models.User, parent_id: uuid.UUID | None, name: str) -> tuple:
    """(folder, created) -- the live folder [name] in [parent_id], created if
    missing (the caller holds the company's folder lock)."""
    existing = _sibling(db, user.company_id, parent_id, name)
    if existing is not None:
        return existing, False
    f = models.LearningFolder(id=uuid.uuid4(), company_id=user.company_id, parent_id=parent_id, name=name,
                              created_by_user_id=user.id, created_by_employee_id=user.employee_id,
                              created_at=datetime.datetime.now(datetime.timezone.utc))
    db.add(f)
    db.flush()
    return f, True


def _folder_lock(db: Session, company_id: uuid.UUID) -> None:
    crud._advisory_lock(db, "learning_folders", str(company_id))


def _subtree(folder_id: uuid.UUID, index: dict) -> set:
    """[folder_id] and every live folder below it."""
    children: dict = {}
    for f in index.values():
        children.setdefault(f.parent_id, []).append(f.id)
    out, stack = set(), [folder_id]
    while stack:
        fid = stack.pop()
        if fid in out:
            continue
        out.add(fid)
        stack.extend(children.get(fid, []))
    return out


def _folder_out(user: models.User, f: models.LearningFolder, index: dict, stats: dict, names: dict,
                perms: dict) -> dict:
    sub = _subtree(f.id, index)
    files = sum(stats.get(fid, (0, 0))[0] for fid in sub)
    size = sum(stats.get(fid, (0, 0))[1] for fid in sub)
    mine = f.created_by_user_id == user.id
    return {
        "id": f.id, "name": f.name, "parent_id": f.parent_id,
        "folder_count": sum(1 for x in index.values() if x.parent_id == f.id),
        "file_count": stats.get(f.id, (0, 0))[0],      # directly inside
        "total_files": files, "total_size_bytes": size,  # including subfolders
        "created_by": names.get(f.created_by_employee_id, "Admin") if f.created_by_employee_id else "Admin",
        "created_at": f.created_at, "is_mine": mine,
        "can_edit": mine or perms["can_edit_any"], "can_delete": mine or perms["can_delete_any"],
    }


def _doc_stats(db: Session, company_id: uuid.UUID) -> dict:
    """folder_id -> (live documents directly in it, their total bytes)."""
    D = models.LearningDocument
    return {fid: (n, int(size or 0)) for fid, n, size in db.execute(
        select(D.folder_id, func.count(), func.sum(D.size_bytes))
        .where(D.company_id == company_id, D.is_deleted.is_(False)).group_by(D.folder_id))}


def _history(db: Session, user: models.User, doc: models.LearningDocument, action: str, details: str | None = None) -> None:
    db.add(models.LearningDocumentHistory(
        id=uuid.uuid4(), company_id=doc.company_id, document_id=doc.id, action=action, title=doc.title,
        file_name=doc.file_name, size_bytes=doc.size_bytes, details=details, actor_user_id=user.id,
        actor_employee_id=user.employee_id, created_at=datetime.datetime.now(datetime.timezone.utc)))
    crud.create_audit_log(db, doc.company_id, user.id, action[:20], ENTITY_TYPE, doc.id)


def _store(db: Session, doc: models.LearningDocument, upload: UploadFile, ext: str, sha: str, size: int) -> None:
    file_url, stored = save_uploaded_file(upload, entity_type=ENTITY_TYPE, entity_id=doc.id,
                                          extra_extensions=_EXTRA_EXTENSIONS)
    doc.file_url, doc.size_bytes = file_url, stored or size
    doc.file_name = Path(upload.filename).name[:255]
    doc.file_ext = ext
    doc.mime_type = FILE_TYPES[ext][1]
    doc.content_sha256 = sha


# ── endpoints ───────────────────────────────────────────────────────────────

@router.get("/meta")
def documents_meta(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    """What the caller may do + the upload rules (for the Upload dialog)."""
    return {**_perms(db, current_user),
            "categories": form_config.option_values(db, current_user.company_id, "learning_document.category"),
            "file_types": sorted({v[0] for v in FILE_TYPES.values()}),
            "allowed_extensions": [e.lstrip(".") for e in FILE_TYPES], "max_size_bytes": _MAX_UPLOAD_BYTES}


@router.get("")
def list_documents(
    q: str | None = Query(default=None, max_length=200),
    category: str | None = None,
    file_type: str | None = None,
    uploaded_by: list[uuid.UUID] | None = Query(default=None),
    mine: bool = False,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    folder_id: uuid.UUID | None = None,
    sort: str = "uploaded_at",
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Every live document of the caller's company, searchable (title,
    description, file name, uploader), filterable, sortable, paged.
    [folder_id] limits it to that folder and everything below it."""
    if sort not in _SORTS:
        raise HTTPException(status_code=422, detail="sort must be one of " + ", ".join(sorted(_SORTS)))
    D, E = models.LearningDocument, models.Employee
    uploader = func.trim(func.concat(E.first_name, " ", func.coalesce(E.last_name, "")))
    base = (select(D).outerjoin(E, E.id == D.uploaded_by_employee_id)
            .where(D.company_id == current_user.company_id, D.is_deleted.is_(False)))
    if q and q.strip():
        like = f"%{q.strip()}%"
        base = base.where(or_(D.title.ilike(like), D.description.ilike(like), D.file_name.ilike(like),
                              D.category.ilike(like), uploader.ilike(like)))
    if category:
        base = base.where(D.category == category)
    if file_type:
        exts = [e for e, (label, _) in FILE_TYPES.items() if label.lower() == file_type.lower()]
        base = base.where(D.file_ext.in_(exts or ["-"]))
    if uploaded_by:
        base = base.where(D.uploaded_by_employee_id.in_(uploaded_by))
    if mine:
        base = base.where(D.uploaded_by_user_id == current_user.id)
    if date_from:
        base = base.where(D.uploaded_at >= datetime.datetime.combine(date_from, datetime.time.min, _IST))
    if date_to:
        base = base.where(D.uploaded_at < datetime.datetime.combine(date_to + datetime.timedelta(days=1), datetime.time.min, _IST))
    folders = _folder_index(db, current_user.company_id)
    if folder_id is not None:
        if folder_id not in folders:
            raise HTTPException(status_code=404, detail="Folder not found")
        base = base.where(D.folder_id.in_(_subtree(folder_id, folders)))
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    key = {"uploaded_at": D.uploaded_at, "title": func.lower(D.title), "size": D.size_bytes,
           "file_type": D.file_ext, "uploader": func.lower(uploader), "category": D.category}[sort]
    rows = db.scalars(base.order_by(key.asc() if order == "asc" else key.desc(), D.id)
                      .limit(limit).offset(offset)).all()
    names = _uploader_names(db, rows)
    perms = _perms(db, current_user)
    uploaders = db.execute(
        select(D.uploaded_by_employee_id).where(D.company_id == current_user.company_id, D.is_deleted.is_(False),
                                                D.uploaded_by_employee_id.is_not(None)).distinct()).scalars().all()
    upl_names = crud.employee_display_names_bulk(db, set(uploaders))
    return {
        "items": [_out(db, current_user, d, names, perms, folders) for d in rows],
        "total": total,
        "uploaders": sorted(({"id": str(i), "name": upl_names.get(i, "—")} for i in uploaders), key=lambda x: x["name"]),
    }


@router.post("", status_code=201)
def upload_document(  # sync def -> threadpool (blocking file/DB I/O)
    title: str = Form(...),
    category: str = Form(default="Learning"),
    description: str | None = Form(default=None),
    folder_id: uuid.UUID | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not _has(db, current_user, "view"):
        raise HTTPException(status_code=403, detail="You don't have access to Learning & Development.")
    folder_id = folder_id if isinstance(folder_id, uuid.UUID) else None  # direct calls pass the Form default
    if folder_id is not None:
        _folder_or_404(db, current_user, folder_id)
    t, d, c = _clean_meta(title, description, (category or "").strip() or "Learning",
                          db=db, company_id=current_user.company_id)
    data, ext, sha = _read_validated(file)
    _duplicate(db, current_user.company_id, sha)
    now = datetime.datetime.now(datetime.timezone.utc)
    doc = models.LearningDocument(
        id=uuid.uuid4(), company_id=current_user.company_id, folder_id=folder_id,
        title=t, description=d or None, category=c,
        file_name="", file_url="", file_ext=ext, size_bytes=len(data), content_sha256=sha, version=1,
        uploaded_by_employee_id=current_user.employee_id, uploaded_by_user_id=current_user.id, uploaded_at=now)
    db.add(doc)
    _store(db, doc, file, ext, sha, len(data))
    try:
        db.flush()
    except IntegrityError:  # a concurrent upload of the same file won the race
        db.rollback()
        delete_uploaded_file(doc.file_url)
        _duplicate(db, current_user.company_id, sha)
        raise
    _history(db, current_user, doc, "uploaded", f"{FILE_TYPES[ext][0]} · {c}")
    db.commit()
    return _out(db, current_user, doc)


@router.get("/history")
def upload_history(
    document_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Upload history of the company's Learning Documents, newest first."""
    H = models.LearningDocumentHistory
    q = select(H).where(H.company_id == current_user.company_id)
    if document_id:
        q = q.where(H.document_id == document_id)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(H.created_at.desc()).limit(limit).offset(offset)).all()
    names = crud.employee_display_names_bulk(db, {h.actor_employee_id for h in rows})
    live = set(db.scalars(select(models.LearningDocument.id).where(
        models.LearningDocument.id.in_({h.document_id for h in rows}),
        models.LearningDocument.is_deleted.is_(False)))) if rows else set()
    return {"total": total, "items": [{
        "id": h.id, "document_id": h.document_id, "action": h.action, "title": h.title, "file_name": h.file_name,
        "size_bytes": h.size_bytes, "details": h.details,
        "actor": names.get(h.actor_employee_id, "Admin") if h.actor_employee_id else "Admin",
        "created_at": h.created_at, "document_available": h.document_id in live,
    } for h in rows]}


class FolderCreate(BaseModel):
    name: str = PydField(min_length=1, max_length=_FOLDER_NAME_MAX)
    parent_id: uuid.UUID | None = None


class FolderRename(BaseModel):
    name: str = PydField(min_length=1, max_length=_FOLDER_NAME_MAX)


_BROWSE_SORTS = {"title", "uploaded_at", "size", "file_type", "uploader", "category"}


@router.get("/browse")
def browse(
    folder_id: uuid.UUID | None = None,
    sort: str = "title",
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """One folder's contents (top level when [folder_id] is empty): its
    breadcrumb, sub-folders (with counts / sizes, incl. everything below)
    and the documents directly in it."""
    if sort not in _BROWSE_SORTS:
        raise HTTPException(status_code=422, detail="sort must be one of " + ", ".join(sorted(_BROWSE_SORTS)))
    company_id = current_user.company_id
    index = _folder_index(db, company_id)
    if folder_id is not None and folder_id not in index:
        raise HTTPException(status_code=404, detail="Folder not found")
    D, E = models.LearningDocument, models.Employee
    uploader = func.trim(func.concat(E.first_name, " ", func.coalesce(E.last_name, "")))
    key = {"uploaded_at": D.uploaded_at, "title": func.lower(D.title), "size": D.size_bytes,
           "file_type": D.file_ext, "uploader": func.lower(uploader), "category": D.category}[sort]
    docs = db.scalars(
        select(D).outerjoin(E, E.id == D.uploaded_by_employee_id)
        .where(D.company_id == company_id, D.is_deleted.is_(False),
               D.folder_id.is_(None) if folder_id is None else D.folder_id == folder_id)
        .order_by(key.asc() if order == "asc" else key.desc(), D.id).limit(1000)).all()
    children = sorted((f for f in index.values() if f.parent_id == folder_id), key=lambda f: f.name.lower())
    stats = _doc_stats(db, company_id)
    perms = _perms(db, current_user)
    names = crud.employee_display_names_bulk(
        db, {d.uploaded_by_employee_id for d in docs} | {f.created_by_employee_id for f in children}
        | ({index[folder_id].created_by_employee_id} if folder_id else set()))
    return {
        "folder": _folder_out(current_user, index[folder_id], index, stats, names, perms) if folder_id else None,
        "breadcrumb": [{"id": f.id, "name": f.name} for f in _chain(folder_id, index)],
        "folders": [_folder_out(current_user, f, index, stats, names, perms) for f in children],
        "documents": [_out(db, current_user, d, names, perms, index) for d in docs],
        **perms,
    }


@router.post("/folders", status_code=201)
def create_folder(payload: FolderCreate, db: Session = Depends(get_db),
                  current_user: models.User = Depends(get_current_user)):
    if not _has(db, current_user, "view"):
        raise HTTPException(status_code=403, detail="You don't have access to Learning & Development.")
    name = _clean_folder_name(payload.name)
    if payload.parent_id is not None:
        _folder_or_404(db, current_user, payload.parent_id)
    _folder_lock(db, current_user.company_id)
    if _sibling(db, current_user.company_id, payload.parent_id, name) is not None:
        raise HTTPException(status_code=409, detail=f'A folder named "{name}" already exists here.')
    index = _folder_index(db, current_user.company_id)
    if len(_chain(payload.parent_id, index)) >= _MAX_DEPTH:
        raise HTTPException(status_code=422, detail=f"Folders can be nested at most {_MAX_DEPTH} levels deep.")
    f, _ = _ensure_folder(db, current_user, payload.parent_id, name)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "create", "learning_folder", f.id)
    db.commit()
    index = _folder_index(db, current_user.company_id)
    names = crud.employee_display_names_bulk(db, {f.created_by_employee_id})
    return _folder_out(current_user, f, index, _doc_stats(db, current_user.company_id), names, _perms(db, current_user))


@router.patch("/folders/{folder_id}")
def rename_folder(folder_id: uuid.UUID, payload: FolderRename, db: Session = Depends(get_db),
                  current_user: models.User = Depends(get_current_user)):
    f = _folder_or_404(db, current_user, folder_id, lock=True)
    if f.created_by_user_id != current_user.id and not _has(db, current_user, "edit"):
        raise HTTPException(status_code=403, detail="Only the folder's creator or HR / Admin can rename it.")
    name = _clean_folder_name(payload.name)
    _folder_lock(db, current_user.company_id)
    if _sibling(db, current_user.company_id, f.parent_id, name, exclude=f.id) is not None:
        raise HTTPException(status_code=409, detail=f'A folder named "{name}" already exists here.')
    if name != f.name:
        f.name = name
        f.updated_at = datetime.datetime.now(datetime.timezone.utc)
        crud.create_audit_log(db, current_user.company_id, current_user.id, "update", "learning_folder", f.id)
        db.commit()
    index = _folder_index(db, current_user.company_id)
    names = crud.employee_display_names_bulk(db, {f.created_by_employee_id})
    return _folder_out(current_user, f, index, _doc_stats(db, current_user.company_id), names, _perms(db, current_user))


@router.delete("/folders/{folder_id}", status_code=204)
def delete_folder(folder_id: uuid.UUID, db: Session = Depends(get_db),
                  current_user: models.User = Depends(get_current_user)):
    """Deletes the folder with everything in it (sub-folders and documents;
    soft, like a document delete -- the upload history keeps them, the files
    leave storage). Needs the creator of the folder AND of everything in it,
    or learning:delete (HR / Admin)."""
    f = _folder_or_404(db, current_user, folder_id, lock=True)
    _folder_lock(db, current_user.company_id)
    index = _folder_index(db, current_user.company_id)
    sub = _subtree(f.id, index)
    D = models.LearningDocument
    docs = db.scalars(select(D).where(D.company_id == current_user.company_id, D.is_deleted.is_(False),
                                      D.folder_id.in_(sub)).with_for_update()).all()
    if not _has(db, current_user, "delete"):
        others = any(index[x].created_by_user_id != current_user.id for x in sub) or any(
            d.uploaded_by_user_id != current_user.id for d in docs)
        if others:
            raise HTTPException(status_code=403, detail=(
                "Only HR / Admin can delete this folder: it contains items uploaded by others."
                if f.created_by_user_id == current_user.id else
                "Only the folder's creator or HR / Admin can delete it."))
    now = datetime.datetime.now(datetime.timezone.utc)
    urls = []
    for d in docs:
        d.is_deleted, d.deleted_at, d.deleted_by_user_id = True, now, current_user.id
        urls.append(d.file_url)
        _history(db, current_user, d, "deleted", f'With folder "{f.name}"')
    for fid in sub:
        x = index[fid]
        x.is_deleted, x.deleted_at, x.deleted_by_user_id = True, now, current_user.id
    crud.create_audit_log(db, current_user.company_id, current_user.id, "delete", "learning_folder", f.id)
    db.commit()
    for url in urls:
        delete_uploaded_file(url)


def _title_from(file_name: str) -> str:
    stem = Path(file_name).stem
    t = " ".join(stem.replace("_", " ").replace("-", " ").split())
    return (t if len(t) >= 3 else file_name)[:_TITLE_MAX]


@router.post("/bulk", status_code=201)
def bulk_upload(  # sync def -> threadpool (blocking file/DB I/O)
    files: list[UploadFile] = File(...),
    paths: list[str] | None = Form(default=None),
    folder_id: uuid.UUID | None = Form(default=None),
    category: str = Form(default="Learning"),
    description: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Several files at once, optionally a whole folder: [paths][i] is file
    i's path relative to the upload (e.g. "Onboarding/Week 1/intro.pdf");
    its folders are created under [folder_id] (top level when empty) or
    reused when they already exist, so the uploaded structure is preserved
    -- also across several requests of one big upload. Each file is
    validated on its own; a rejected one (type, size, empty, content,
    duplicate) is listed under "skipped" and the rest is still uploaded.
    Title = the file name; category / description apply to every file."""
    if not _has(db, current_user, "view"):
        raise HTTPException(status_code=403, detail="You don't have access to Learning & Development.")
    if not files:
        raise HTTPException(status_code=422, detail="Choose at least one file.")
    if len(files) > _BULK_MAX_FILES:
        raise HTTPException(status_code=422, detail=f"Upload at most {_BULK_MAX_FILES} files per request.")
    if paths is not None and len(paths) != len(files):
        raise HTTPException(status_code=422, detail="Each file needs its path.")
    company_id = current_user.company_id
    if folder_id is not None:
        _folder_or_404(db, current_user, folder_id)
    _, d, c = _clean_meta(None, description, (category or "").strip() or "Learning", db=db, company_id=company_id)
    _folder_lock(db, company_id)
    base_depth = len(_chain(folder_id, _folder_index(db, company_id)))
    uploaded, skipped, stored = [], [], []
    seen: set[str] = set()
    created = 0
    try:
        for i, up in enumerate(files):
            raw = (paths[i] if paths is not None else None) or up.filename or ""
            parts = [p.strip() for p in raw.replace("\\", "/").split("/") if p.strip()]
            shown = "/".join(parts) or (up.filename or "file")
            doc = None
            made_here = 0
            try:
                if not parts or any(p in (".", "..") for p in parts):
                    raise HTTPException(status_code=422, detail="Invalid file path.")
                if base_depth + len(parts) - 1 > _MAX_DEPTH:
                    raise HTTPException(status_code=422, detail=f"Folders can be nested at most {_MAX_DEPTH} levels deep.")
                folder_names = [_clean_folder_name(p) for p in parts[:-1]]
                up.filename = parts[-1]
                data, ext, sha = _read_validated(up)
                if sha in seen:
                    raise HTTPException(status_code=409, detail="The same file is already in this upload.")
                _duplicate(db, company_id, sha)
                with db.begin_nested():
                    parent = folder_id
                    for name in folder_names:
                        fol, made = _ensure_folder(db, current_user, parent, name)
                        made_here += made
                        parent = fol.id
                    doc = models.LearningDocument(
                        id=uuid.uuid4(), company_id=company_id, folder_id=parent, title=_title_from(parts[-1]),
                        description=d or None, category=c, file_name="", file_url="", file_ext=ext,
                        size_bytes=len(data), content_sha256=sha, version=1,
                        uploaded_by_employee_id=current_user.employee_id, uploaded_by_user_id=current_user.id,
                        uploaded_at=datetime.datetime.now(datetime.timezone.utc))
                    db.add(doc)
                    _store(db, doc, up, ext, sha, len(data))
                    db.flush()
                    where = "/".join(folder_names)
                    _history(db, current_user, doc, "uploaded",
                             f"{FILE_TYPES[ext][0]} · {c}" + (f" · {where}" if where else ""))
                stored.append(doc.file_url)
                created += made_here
                seen.add(sha)
                uploaded.append(doc)
            except (HTTPException, IntegrityError) as e:
                if doc is not None and doc.file_url:  # stored, then the savepoint rolled back
                    delete_uploaded_file(doc.file_url)
                reason = (str(e.detail) if isinstance(e, HTTPException)
                          else "This file has just been uploaded by someone else.")
                skipped.append({"path": shown, "reason": reason})
        db.commit()
    except Exception:
        db.rollback()
        for url in stored:
            delete_uploaded_file(url)
        raise
    index = _folder_index(db, company_id)
    names = _uploader_names(db, uploaded)
    perms = _perms(db, current_user)
    return {"uploaded": [_out(db, current_user, x, names, perms, index) for x in uploaded],
            "skipped": skipped, "folders_created": created}


@router.get("/{doc_id}")
def get_document(doc_id: uuid.UUID, db: Session = Depends(get_db),
                 current_user: models.User = Depends(get_current_user)):
    return _out(db, current_user, _get(db, current_user, doc_id))


@router.get("/{doc_id}/file")
def download_document(
    doc_id: uuid.UUID,
    download: bool = True,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The file itself under its original name -- attachment (download) or
    inline (preview)."""
    doc = _get(db, current_user, doc_id)
    path = _file_path(doc)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=(
            f'The file "{doc.file_name}" is not available on this server — it may have been uploaded '
            "through another server. Ask the uploader (or HR) to replace it with Edit."))
    return FileResponse(path, media_type=doc.mime_type or mimetypes.guess_type(doc.file_name)[0] or "application/octet-stream",
                        filename=doc.file_name, content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, max-age=60"})


@router.patch("/{doc_id}")
def update_document(
    doc_id: uuid.UUID,
    title: str | None = Form(default=None),
    category: str | None = Form(default=None),
    description: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Edit title / category / description and/or replace the file
    (uploader or learning:edit)."""
    doc = _get(db, current_user, doc_id, lock=True)
    if doc.uploaded_by_user_id != current_user.id and not _has(db, current_user, "edit"):
        raise HTTPException(status_code=403, detail="Only the uploader or HR / Admin can edit this document.")
    t, d, c = _clean_meta(title, description, category, db=db, company_id=current_user.company_id,
                          current_category=doc.category)
    changes = []
    if t is not None and t != doc.title:
        changes.append("title"); doc.title = t
    if d is not None and (d or None) != doc.description:
        changes.append("description"); doc.description = d or None
    if c is not None and c != doc.category:
        changes.append("category"); doc.category = c
    replaced = False
    if file is not None and (file.filename or "").strip():
        data, ext, sha = _read_validated(file)
        if sha == doc.content_sha256:
            raise HTTPException(status_code=409, detail="That is the same file that is already uploaded.")
        _duplicate(db, doc.company_id, sha, exclude=doc.id)
        old_url, old_name = doc.file_url, doc.file_name
        _store(db, doc, file, ext, sha, len(data))
        doc.version = (doc.version or 1) + 1
        replaced = True
    if not changes and not replaced:
        raise HTTPException(status_code=400, detail="No changes to save.")
    if changes:
        _history(db, current_user, doc, "updated", "Changed " + ", ".join(changes))
    if replaced:
        _history(db, current_user, doc, "file_replaced", f"{old_name} → {doc.file_name} (version {doc.version})")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if replaced:
            delete_uploaded_file(doc.file_url)
        raise HTTPException(status_code=409, detail="This file has just been uploaded by someone else.")
    if replaced:
        delete_uploaded_file(old_url)
    return _out(db, current_user, doc)


@router.delete("/{doc_id}", status_code=204)
def delete_document(doc_id: uuid.UUID, db: Session = Depends(get_db),
                    current_user: models.User = Depends(get_current_user)):
    """Soft delete (the upload history keeps it) + the file leaves storage."""
    doc = _get(db, current_user, doc_id, lock=True)
    if doc.uploaded_by_user_id != current_user.id and not _has(db, current_user, "delete"):
        raise HTTPException(status_code=403, detail="Only the uploader or HR / Admin can delete this document.")
    doc.is_deleted = True
    doc.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    doc.deleted_by_user_id = current_user.id
    _history(db, current_user, doc, "deleted")
    db.commit()
    delete_uploaded_file(doc.file_url)
