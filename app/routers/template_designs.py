"""GET /api/template-designs/{doc_type} -- the template editor's Designs
gallery (app/template_designs.py): print-aligned styles, the standard
layout for the document type, and sample values (with the caller's own
company name / logo) for the previews. Read-only."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import email_service, models, template_designs, template_designs_more
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/api", tags=["template-designs"])


@router.get("/template-designs/{doc_type}")
def get_template_designs(
    doc_type: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    if doc_type in template_designs_more.MORE_DOC_TYPES:
        return template_designs_more.designs_for_more(db, company, doc_type)
    if doc_type not in template_designs.DOC_TYPES:
        raise HTTPException(status_code=404, detail="No designs for this template type.")
    return template_designs.designs_for(db, company, doc_type)


@router.get("/template-designs/email/{email_kind}")
def get_email_template_designs(
    email_kind: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Designs for one email kind, built from that kind's own tokens."""
    from .email_templates import EMAIL_KIND_TOKENS
    if email_kind not in email_service.EMAIL_KINDS:
        raise HTTPException(status_code=404, detail="No designs for this email.")
    company = db.get(models.Company, current_user.company_id)
    return template_designs_more.designs_for_email(db, company, email_kind, EMAIL_KIND_TOKENS[email_kind])
