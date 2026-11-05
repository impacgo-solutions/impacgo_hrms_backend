"""Leave > Holiday Calendar > Upload Document.

    POST /api/holiday-imports                 upload a PDF / DOCX / XLSX / CSV;
                                              stored, holidays extracted
                                              (app/holiday_extract.py)
    GET  /api/holiday-imports                 upload history
    GET  /api/holiday-imports/{id}            one upload + its extracted rows
    GET  /api/holiday-imports/{id}/file       the original document
    POST /api/holiday-imports/{id}/confirm    save the reviewed rows as holidays
    POST /api/holiday-imports/{id}/cancel     discard without importing

Same permission as Add Holiday (Leave Approval Edit/Admin, or the Owner).
Duplicates -- same date + holiday name + applicability (branch or all
branches) as a holiday already on the calendar, or repeated in the file --
are skipped; the same holiday for a different branch is imported.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud, holiday_extract, models, schemas
from ..database import get_db
from ..deps import require_permission
from ..storage import save_uploaded_file, uploads_root, winlong_path

router = APIRouter(prefix="/api", tags=["holidays"])

_require_holiday_admin = require_permission("leave_approval")
_ALLOWED = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".csv": "text/csv", ".txt": "text/plain"}
_MAX_BYTES = 10 * 1024 * 1024


def _actor_name(db: Session, user: models.User) -> str:
    name = crud.employee_display_name(db, user.employee_id) if user.employee_id else "—"
    return name if name != "—" else (getattr(user, "email", None) or "—")


def _branches(db: Session, company_id: uuid.UUID) -> list[models.Branch]:
    return db.scalars(select(models.Branch).where(models.Branch.company_id == company_id)
                      .order_by(models.Branch.name)).all()


def _out(db: Session, imp: models.HolidayImport, *, with_rows: bool = True) -> schemas.HolidayImportOut:
    rows = []
    if with_rows:
        for r in imp.extracted_rows or []:
            date = datetime.date.fromisoformat(r["date"])
            branch_id = uuid.UUID(r["branch_id"]) if r.get("branch_id") else None
            duplicate = crud.find_duplicate_holiday(db, imp.company_id, date, r["name"], branch_id) is not None
            rows.append(schemas.HolidayImportRowOut(**{**r, "date": date, "branch_id": branch_id, "duplicate": duplicate}))
    return schemas.HolidayImportOut(
        id=imp.id, original_filename=imp.original_filename, file_size=imp.file_size, status=imp.status,
        extracted_count=imp.extracted_count, imported_count=imp.imported_count, skipped_count=imp.skipped_count,
        error=imp.error, uploaded_by_name=imp.uploaded_by_name, uploaded_at=imp.uploaded_at,
        imported_by_name=imp.imported_by_name, imported_at=imp.imported_at, rows=rows, result=imp.result,
        branches=[{"id": str(b.id), "name": b.name} for b in _branches(db, imp.company_id)],
    )


def _get(db: Session, user: models.User, import_id: uuid.UUID) -> models.HolidayImport:
    imp = db.get(models.HolidayImport, import_id)
    if imp is None or imp.company_id != user.company_id:
        raise HTTPException(status_code=404, detail="Holiday upload not found")
    return imp


@router.post("/holiday-imports", response_model=schemas.HolidayImportOut, status_code=201)
def upload_holiday_document(  # sync def -> threadpool (file + DB I/O)
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_holiday_admin),
):
    name = Path(file.filename or "").name or "holidays"
    ext = Path(name).suffix.lower()
    if ext not in _ALLOWED:
        raise HTTPException(status_code=400, detail="Upload a PDF, Word (.docx), Excel (.xlsx) or CSV holiday calendar.")
    content = file.file.read(_MAX_BYTES + 1)
    if len(content) > _MAX_BYTES:
        raise HTTPException(status_code=400, detail="The document must be smaller than 10 MB.")
    if not content:
        raise HTTPException(status_code=400, detail="The file is empty.")
    if ext == ".pdf" and not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="This file is not a valid PDF.")
    file.file.seek(0)
    file_url, size = save_uploaded_file(file, entity_type="holiday_import", entity_id=current_user.company_id)
    imp = models.HolidayImport(
        id=uuid.uuid4(), company_id=current_user.company_id, original_filename=name[:255], file_url=file_url,
        content_type=_ALLOWED[ext], file_size=size, status="extracted", extracted_rows=[],
        uploaded_by=current_user.id, uploaded_by_name=_actor_name(db, current_user),
        uploaded_at=datetime.datetime.now(datetime.timezone.utc),
    )
    try:
        doc_rows = holiday_extract.document_rows(content, name)
        branches = [(str(b.id), b.name) for b in _branches(db, current_user.company_id)]
        extracted = holiday_extract.extract_holidays(
            doc_rows, branches, crud.company_today(db, current_user.company_id).year)
        if not extracted:
            raise holiday_extract.ExtractionError(
                "No holidays with a readable date were found in this document. Check that it lists a date and "
                "a holiday name on each line or row.")
        imp.extracted_rows = extracted
        imp.extracted_count = len(extracted)
    except holiday_extract.ExtractionError as exc:
        imp.status, imp.error = "failed", str(exc)
    db.add(imp)
    crud.create_audit_log(db, current_user.company_id, current_user.id, "upload", "holiday_import", imp.id,
                          changes={"file": imp.original_filename, "status": imp.status,
                                   "extracted": str(imp.extracted_count)})
    db.commit()
    if imp.status == "failed":
        raise HTTPException(status_code=422, detail={"message": imp.error, "import_id": str(imp.id)})
    return _out(db, imp)


@router.get("/holiday-imports", response_model=list[schemas.HolidayImportOut])
def list_holiday_imports(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_holiday_admin),
):
    rows = db.scalars(select(models.HolidayImport).where(models.HolidayImport.company_id == current_user.company_id)
                      .order_by(models.HolidayImport.uploaded_at.desc()).limit(limit)).all()
    return [_out(db, r, with_rows=False) for r in rows]


@router.get("/holiday-imports/{import_id}", response_model=schemas.HolidayImportOut)
def get_holiday_import(import_id: uuid.UUID, db: Session = Depends(get_db),
                       current_user: models.User = Depends(_require_holiday_admin)):
    return _out(db, _get(db, current_user, import_id))


@router.get("/holiday-imports/{import_id}/file")
def download_holiday_document(import_id: uuid.UUID, db: Session = Depends(get_db),
                              current_user: models.User = Depends(_require_holiday_admin)):
    imp = _get(db, current_user, import_id)
    path = uploads_root() / imp.file_url.removeprefix("/media/")
    try:
        data = winlong_path(path).read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=410, detail="The stored document is missing.") from exc
    return Response(content=data, media_type=imp.content_type or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{imp.original_filename}"'})


@router.post("/holiday-imports/{import_id}/confirm", response_model=schemas.HolidayImportOut)
def confirm_holiday_import(
    import_id: uuid.UUID,
    payload: schemas.HolidayImportConfirmIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(_require_holiday_admin),
):
    """Saves the rows HR reviewed (edited / added / removed) as holidays."""
    imp = _get(db, current_user, import_id)
    if imp.status != "extracted":
        raise HTTPException(status_code=409, detail=f"This upload is already {imp.status}.")
    # Serialise double submits of the same upload.
    db.execute(select(models.HolidayImport.id).where(models.HolidayImport.id == imp.id).with_for_update())
    branch_names = {b.id: b.name for b in _branches(db, current_user.company_id)}
    created, skipped = [], []
    seen: set[tuple] = set()
    for row in payload.rows:
        name = " ".join(row.name.split())
        if row.branch_id is not None and row.branch_id not in branch_names:
            raise HTTPException(status_code=422, detail=f"'{name}': choose one of your branches.")
        label = branch_names.get(row.branch_id, "All Branches")
        key = (row.date, name.lower(), row.branch_id)
        entry = {"date": row.date.isoformat(), "name": name, "branch": label}
        if key in seen:
            skipped.append({**entry, "reason": "Listed twice in this upload"})
            continue
        seen.add(key)
        if crud.find_duplicate_holiday(db, current_user.company_id, row.date, name, row.branch_id) is not None:
            skipped.append({**entry, "reason": "Already on the holiday calendar"})
            continue
        holiday = crud.create_holiday(db, current_user.company_id, row.date, name, branch_id=row.branch_id,
                                      is_optional=row.is_optional, source_import_id=imp.id)
        created.append({**entry, "id": str(holiday.id), "is_optional": row.is_optional})
    now = datetime.datetime.now(datetime.timezone.utc)
    imp.status = "imported"
    imp.imported_count, imp.skipped_count = len(created), len(skipped)
    imp.result = {"created": created, "skipped": skipped, "reviewed_rows": len(payload.rows)}
    imp.imported_by, imp.imported_by_name, imp.imported_at = current_user.id, _actor_name(db, current_user), now
    crud.create_audit_log(db, current_user.company_id, current_user.id, "import", "holiday_import", imp.id,
                          changes={"created": str(len(created)), "skipped": str(len(skipped))})
    db.commit()
    return _out(db, imp, with_rows=False)


@router.post("/holiday-imports/{import_id}/cancel", response_model=schemas.HolidayImportOut)
def cancel_holiday_import(import_id: uuid.UUID, db: Session = Depends(get_db),
                          current_user: models.User = Depends(_require_holiday_admin)):
    imp = _get(db, current_user, import_id)
    if imp.status != "extracted":
        raise HTTPException(status_code=409, detail=f"This upload is already {imp.status}.")
    imp.status = "cancelled"
    crud.create_audit_log(db, current_user.company_id, current_user.id, "cancel", "holiday_import", imp.id)
    db.commit()
    return _out(db, imp, with_rows=False)
