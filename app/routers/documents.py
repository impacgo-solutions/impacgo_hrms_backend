import datetime
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from .. import crud, models, schemas
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled, require_permission
from ..storage import save_uploaded_file

router = APIRouter(
    prefix="/api", tags=["documents"],
    dependencies=[Depends(require_module_enabled("documents"))],
)


def _policy_doc_out(d: models.PolicyDocument) -> schemas.PolicyDocOut:
    return schemas.PolicyDocOut(
        id=d.id,
        name=d.name,
        version=d.version or "",
        effective=d.effective_date.isoformat() if d.effective_date else "—",
        ack=f"{d.acknowledgement_pct:g}%" if d.acknowledgement_pct is not None else "—",
        file_url=d.file_url,
    )


@router.get("/policy-documents", response_model=list[schemas.PolicyDocOut])
def list_policy_documents(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [_policy_doc_out(d) for d in crud.list_policy_documents(db, company.id, "policy")]


@router.post(
    "/policy-documents",
    response_model=schemas.PolicyDocOut,
    status_code=201,
)
def upload_policy_document(  # API-06: sync def -> threadpool (blocking file/DB I/O)
    name: str = Form(...),
    version: str | None = Form(None),
    effective_date: datetime.date | None = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    company = db.get(models.Company, current_user.company_id)
    file_url, _size_bytes = save_uploaded_file(file, entity_type="policy_document", entity_id=company.id)
    try:
        document = crud.create_policy_document(
            db, company.id, "policy", name, version, effective_date, None, file_url=file_url
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "policy_document", document.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(document)
    return _policy_doc_out(document)


@router.get("/document-templates", response_model=list[schemas.PolicyDocOut])
def list_document_templates(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [_policy_doc_out(d) for d in crud.list_policy_documents(db, company.id, "template")]


@router.post(
    "/document-templates",
    response_model=schemas.PolicyDocOut,
    status_code=201,
)
def upload_document_template(  # API-06: sync def -> threadpool (blocking file/DB I/O)
    name: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    company = db.get(models.Company, current_user.company_id)
    file_url, _size_bytes = save_uploaded_file(file, entity_type="document_template", entity_id=company.id)
    try:
        document = crud.create_policy_document(
            db, company.id, "template", name, None, None, None, file_url=file_url
        )
        crud.create_audit_log(db, company.id, current_user.id, "create", "document_template", document.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(document)
    return _policy_doc_out(document)


@router.delete("/document-templates/{document_id}", status_code=204)
def delete_document_template(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("benefits_admin")),
):
    """Sibling of upload_document_template above -- same permission gate,
    for removing a template uploaded in error or no longer needed."""
    company = db.get(models.Company, current_user.company_id)
    document = crud.get_policy_document(db, company.id, document_id, "template")
    if document is None:
        raise HTTPException(status_code=404, detail="Template not found")
    crud.create_audit_log(db, company.id, current_user.id, "delete", "document_template", document_id)
    crud.delete_policy_document(db, document_id)
    db.commit()


@router.get("/document-records", response_model=list[schemas.DocumentRecordOut])
def list_document_records(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    # Role-scoped: admins get all, HR gets their departments, everyone else
    # gets their full downward reporting subtree + self (IC employees get
    # self-only).
    visible_ids = crud.get_visible_employee_ids_for_docs(db, current_user)
    return crud.list_document_records(
        db, company.id, employee_ids=visible_ids, limit=limit, offset=offset,
    )
