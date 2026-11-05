"""GET /api/template-designs/{doc_type} -- the template editor's Designs
gallery (app/template_designs.py): print-aligned styles, the standard
layout for the document type, and sample values (with the caller's own
company name / logo) for the previews. Read-only."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, template_designs
from ..database import get_db
from ..deps import get_current_user

router = APIRouter(prefix="/api", tags=["template-designs"])


@router.get("/template-designs/{doc_type}")
def get_template_designs(
    doc_type: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if doc_type not in template_designs.DOC_TYPES:
        raise HTTPException(status_code=404, detail="No designs for this template type.")
    company = db.get(models.Company, current_user.company_id)
    return template_designs.designs_for(db, company, doc_type)
