import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import crud, form_config, models, schemas
from ..database import get_db
from ..deps import DEFAULT_LIST_LIMIT, get_current_user, require_module_enabled, require_permission
from ..storage import delete_uploaded_file, save_uploaded_file

router = APIRouter(
    prefix="/api", tags=["assets"],
    dependencies=[Depends(require_module_enabled("assets"))],
)

_STATUS_DISPLAY = {
    "available": "Available",
    "assigned": "Assigned",
    "repair": "In Repair",
    "in_repair": "In Repair",
    "retired": "Retired",
}


def _format_inr(amount: float | None) -> str:
    """Indian digit grouping (last 3 digits, then pairs) -- e.g.
    189000 -> '1,89,000'. Reconstructs the seeded display strings closely,
    though a recurring-cost suffix like '/ yr' isn't preserved."""
    if amount is None:
        return "—"
    n = str(int(round(amount)))
    if len(n) <= 3:
        return f"₹{n}"
    last3, rest = n[-3:], n[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return f"₹{','.join(groups)},{last3}"


def _status_label(db, company_id, status: str) -> str:
    """Configured label of a stored status key (Form Configuration >
    Asset > Status); legacy keys ('repair') map to their option first."""
    key = "in_repair" if status == "repair" else status
    label = form_config.option_label(db, company_id, "asset.status", key)
    if label and label != key:
        return label
    return _STATUS_DISPLAY.get(status, status.title())


def _resolve_status(db, company_id, raw: str, current: str | None = None) -> str:
    """Maps what a client sent (the stored key, or -- from older clients --
    the label) to a configured status key, 422 when it isn't offered."""
    text = raw.strip()
    for value, label in form_config.options(db, company_id, "asset.status"):
        if text.lower() in (value.lower(), label.lower()):
            return value
    return form_config.require_option(
        db, company_id, "asset.status", text.lower().replace(" ", "_"), current=current, required=True)


# Asset attachments (images and documents -- invoice, warranty, manual...):
# images PNG / JPG up to 5MB, documents PDF / Word / Excel / PowerPoint /
# text up to 10MB; at most 20 per asset. Content is checked against the
# extension by storage.save_uploaded_file.
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv"}
_ATTACHMENT_EXTENSIONS = _IMAGE_EXTENSIONS | _DOCUMENT_EXTENSIONS
_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
_MAX_IMAGES_PER_ASSET = 20


def _attachment_kind(file_name: str) -> str:
    return "image" if Path(file_name or "").suffix.lower() in _IMAGE_EXTENSIONS else "document"


def _load_images(db, asset_ids: list) -> dict:
    out: dict = {}
    if not asset_ids:
        return out
    for img in db.scalars(select(models.AssetImage).where(models.AssetImage.asset_id.in_(asset_ids))
                          .order_by(models.AssetImage.sort_order, models.AssetImage.created_at)):
        out.setdefault(img.asset_id, []).append(
            schemas.AssetImageOut(id=img.id, url=img.file_url, name=img.file_name, size_bytes=img.size_bytes,
                                  kind=_attachment_kind(img.file_name),
                                  ext=Path(img.file_name or "").suffix.lower().lstrip(".")))
    return out


def _to_out(asset, db, custom: dict | None = None, images: dict | None = None) -> schemas.AssetInventoryItemOut:
    return schemas.AssetInventoryItemOut(
        id=asset.id,
        tag=asset.asset_tag,
        type=asset.asset_type,
        model=asset.model or "",
        status=_status_label(db, asset.company_id, asset.status),
        status_key="in_repair" if asset.status == "repair" else asset.status,
        value=_format_inr(asset.purchase_value),
        purchased=asset.purchased_on.isoformat() if asset.purchased_on else "—",
        category=schemas.ASSET_CATEGORIES.get(asset.asset_category or "", "—"),
        category_key=asset.asset_category,
        name=asset.asset_name,
        serial_number=asset.serial_number,
        description=asset.description,
        custom_fields=(custom or {}).get(asset.id, {}),
        images=(images if images is not None else _load_images(db, [asset.id])).get(asset.id, []),
    )


@router.get("/asset-inventory", response_model=list[schemas.AssetInventoryItemOut])
def list_asset_inventory(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    assets = crud.list_asset_inventory(db, company.id, limit=limit, offset=offset)
    custom = form_config.load_custom_values(db, company.id, "asset", [a.id for a in assets])
    images = _load_images(db, [a.id for a in assets])
    return [_to_out(a, db, custom, images) for a in assets]


def _asset_or_404(db: Session, user: models.User, item_id: uuid.UUID) -> models.AssetInventoryItem:
    asset = db.get(models.AssetInventoryItem, item_id)
    if asset is None or asset.company_id != user.company_id:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.post("/asset-inventory/{item_id}/images", response_model=schemas.AssetInventoryItemOut)
def upload_asset_images(  # sync def -> threadpool (blocking file/DB I/O)
    item_id: uuid.UUID,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    """Attaches one or more images (PNG/JPG) and/or documents (PDF, Word,
    Excel, PowerPoint, text) to an asset (multipart field `files`,
    repeatable). All-or-nothing: if any file is rejected nothing is kept.
    Files are stored under uploads/asset_image/<asset_id>/ and served only
    via the authenticated /media route (media_access)."""
    asset = _asset_or_404(db, current_user, item_id)
    if not files:
        raise HTTPException(status_code=400, detail="Select at least one file.")
    existing = db.scalar(select(func.count()).select_from(models.AssetImage)
                         .where(models.AssetImage.asset_id == asset.id)) or 0
    if existing + len(files) > _MAX_IMAGES_PER_ASSET:
        raise HTTPException(status_code=400, detail=(
            f"An asset can have at most {_MAX_IMAGES_PER_ASSET} images and documents "
            f"({existing} already attached)."))
    for f in files:
        if Path(f.filename or "").suffix.lower() not in _ATTACHMENT_EXTENSIONS:
            raise HTTPException(status_code=400, detail=(
                f"'{f.filename}': only images (PNG, JPG) and documents (PDF, Word, Excel, PowerPoint, "
                "TXT, CSV) are supported."))
    saved: list[str] = []
    try:
        for i, f in enumerate(files):
            # save_uploaded_file also checks the content really matches the type.
            file_url, size = save_uploaded_file(f, entity_type="asset_image", entity_id=asset.id)
            saved.append(file_url)
            is_image = _attachment_kind(f.filename or "") == "image"
            limit = _IMAGE_MAX_BYTES if is_image else _DOCUMENT_MAX_BYTES
            if size > limit:
                raise HTTPException(status_code=400, detail=(
                    f"'{f.filename}' is larger than {limit // (1024 * 1024)}MB "
                    f"(the limit for {'images' if is_image else 'documents'})."))
            db.add(models.AssetImage(
                id=uuid.uuid4(), company_id=asset.company_id, asset_id=asset.id, file_url=file_url,
                file_name=Path(f.filename or "image").name[:255], size_bytes=size,
                sort_order=existing + i, uploaded_by=current_user.employee_id))
        crud.create_audit_log(db, asset.company_id, current_user.id, "update", "asset_inventory_item", asset.id,
                              changes={"images_added": str(len(files))})
        db.commit()
    except Exception:
        db.rollback()
        for url in saved:
            delete_uploaded_file(url)
        raise
    db.refresh(asset)
    return _to_out(asset, db, form_config.load_custom_values(db, asset.company_id, "asset", [asset.id]))


@router.delete("/asset-inventory/{item_id}/images/{image_id}", response_model=schemas.AssetInventoryItemOut)
def delete_asset_image(
    item_id: uuid.UUID,
    image_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    asset = _asset_or_404(db, current_user, item_id)
    img = db.get(models.AssetImage, image_id)
    if img is None or img.asset_id != asset.id:
        raise HTTPException(status_code=404, detail="Image not found")
    file_url = img.file_url
    db.delete(img)
    crud.create_audit_log(db, asset.company_id, current_user.id, "update", "asset_inventory_item", asset.id,
                          changes={"image_removed": img.file_name})
    db.commit()
    delete_uploaded_file(file_url)
    return _to_out(asset, db, form_config.load_custom_values(db, asset.company_id, "asset", [asset.id]))


@router.post(
    "/asset-inventory",
    response_model=schemas.AssetInventoryItemOut,
    status_code=201,
)
def create_asset_inventory_item(
    payload: schemas.AssetInventoryItemCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    """Every field is optional (Status defaults to Available, the Tag comes
    from the Asset ID format)."""
    company = db.get(models.Company, current_user.company_id)
    asset_type = _resolve_type(db, company.id, payload.type)
    status = _resolve_status(db, company.id, payload.status) if (payload.status or "").strip() else "available"
    cleaned = form_config.validate_custom_values(db, company.id, "asset", payload.custom_fields,
                                                 enforce_required=False)
    amount = _parse_value(payload.value)
    purchased = _parse_purchased(db, company.id, payload.purchased)
    try:
        asset = crud.create_asset_inventory_item(
            db,
            company.id,
            payload.tag,
            asset_type,
            _text(payload.model),
            status,
            amount,
            purchased,
            asset_category=payload.category,
            asset_name=_text(payload.name),
            serial_number=_text(payload.serial_number),
            description=_text(payload.description, multiline=True),
        )
        form_config.save_custom_values(db, company.id, "asset", asset.id, cleaned)
        crud.create_audit_log(db, company.id, current_user.id, "create", "asset_inventory_item", asset.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(asset)
    return _to_out(asset, db, form_config.load_custom_values(db, company.id, "asset", [asset.id]))


def _text(raw: str | None, *, multiline: bool = False) -> str | None:
    """Trimmed text, None when blank (single-line fields collapse inner
    whitespace runs; multi-line ones keep their line breaks)."""
    if raw is None:
        return None
    t = raw.strip() if multiline else " ".join(raw.split())
    return t or None


def _resolve_type(db, company_id, raw: str | None, current: str | None = None) -> str:
    """Type is optional: blank = not set (stored ''); a given one must be a
    configured option (Form Configuration > Asset > Type)."""
    text = (raw or "").strip()
    if not text:
        return ""
    return form_config.require_option(db, company_id, "asset.type", text, current=current, required=True)


def _parse_value(raw: str | None) -> float | None:
    """M-04: a given value must be a non-negative amount (blank / "—" = not recorded)."""
    text = (raw or "").strip()
    if not text or text == "—":
        return None
    amount = crud.parse_inr_amount(text)
    if amount is None or text.lstrip("₹ ").startswith("-"):
        raise HTTPException(status_code=422, detail="Asset value must be a non-negative amount, e.g. 45000.")
    return amount


def _parse_purchased(db, company_id, raw: str | None):
    """A given purchase date must be a real YYYY-MM-DD date, not in the future."""
    text = (raw or "").strip()
    if not text or text == "—":
        return None
    purchased = crud.parse_date_safe(text)
    if purchased is None:
        raise HTTPException(status_code=422, detail="Purchase date must be a valid date in YYYY-MM-DD format.")
    if purchased > crud.company_today(db, company_id):
        raise HTTPException(status_code=422, detail="Purchase date can't be in the future.")
    return purchased


@router.patch("/asset-inventory/{item_id}", response_model=schemas.AssetInventoryItemOut)
def update_asset_inventory_item(
    item_id: uuid.UUID,
    payload: schemas.AssetInventoryItemUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    """Edit Asset: only the fields sent change; a blank / null optional
    field is cleared. Nothing is required. The Tag never changes."""
    company = db.get(models.Company, current_user.company_id)
    asset = crud.get_asset_inventory_item(db, company.id, item_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    sent = payload.model_fields_set
    changes: dict[str, str] = {}

    def put(attr: str, value) -> None:
        if getattr(asset, attr) != value:
            changes[attr] = "" if value is None else str(value)
            setattr(asset, attr, value)

    if "type" in sent:
        put("asset_type", _resolve_type(db, company.id, payload.type, current=asset.asset_type))
    if "category" in sent:
        put("asset_category", payload.category)
    if "name" in sent:
        put("asset_name", _text(payload.name))
    if "serial_number" in sent:
        put("serial_number", _text(payload.serial_number))
    if "description" in sent:
        put("description", _text(payload.description, multiline=True))
    if "model" in sent:
        put("model", _text(payload.model))
    if "status" in sent and (payload.status or "").strip():
        put("status", _resolve_status(db, company.id, payload.status, current=asset.status))
    if "value" in sent:
        put("purchase_value", _parse_value(payload.value))
    if "purchased" in sent:
        put("purchased_on", _parse_purchased(db, company.id, payload.purchased))
    if "custom_fields" in sent and payload.custom_fields is not None:
        cleaned = form_config.validate_custom_values(db, company.id, "asset", payload.custom_fields,
                                                     partial=True, enforce_required=False)
        form_config.save_custom_values(db, company.id, "asset", asset.id, cleaned)
        if cleaned:
            changes["custom_fields"] = ", ".join(sorted(cleaned))
    if changes:
        crud.create_audit_log(db, company.id, current_user.id, "update", "asset_inventory_item", asset.id,
                              changes=changes)
    db.commit()
    db.refresh(asset)
    return _to_out(asset, db, form_config.load_custom_values(db, company.id, "asset", [asset.id]))


@router.patch(
    "/asset-inventory/{item_id}/status",
    response_model=schemas.AssetInventoryItemOut,
)
def update_asset_inventory_status(
    item_id: uuid.UUID,
    payload: schemas.AssetInventoryItemStatusUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    company = db.get(models.Company, current_user.company_id)
    asset = crud.get_asset_inventory_item(db, company.id, item_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset.status = _resolve_status(db, company.id, payload.status, current=asset.status)
    crud.create_audit_log(db, company.id, current_user.id, "update", "asset_inventory_item", asset.id)
    db.commit()
    db.refresh(asset)
    return _to_out(asset, db, form_config.load_custom_values(db, company.id, "asset", [asset.id]))


@router.delete("/asset-inventory/{item_id}", status_code=204)
def delete_asset_inventory_item(
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    company = db.get(models.Company, current_user.company_id)
    asset = crud.get_asset_inventory_item(db, company.id, item_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    crud.create_audit_log(db, company.id, current_user.id, "delete", "asset_inventory_item", item_id)
    image_urls = list(db.scalars(select(models.AssetImage.file_url).where(models.AssetImage.asset_id == item_id)))
    crud.delete_asset_inventory_item(db, item_id)  # image rows go with it (ON DELETE CASCADE)
    form_config.delete_custom_values_for(db, company.id, item_id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This asset has assignment history and cannot be deleted.",
        ) from exc
    for url in image_urls:
        delete_uploaded_file(url)


def _assignment_out(a: models.AssetAssignment) -> schemas.AssetAssignmentOut:
    return schemas.AssetAssignmentOut(
        id=a.id,
        asset_tag=a.asset.asset_tag,
        asset_type=a.asset.asset_type,
        employee_id=a.employee_id,
        employee_name=crud._full_name(a.employee),
        assigned_on=a.assigned_on,
        returned_on=a.returned_on,
        return_condition=a.return_condition,
        return_notes=a.return_notes,
    )


@router.get("/asset-assignments", response_model=list[schemas.AssetAssignmentOut])
def list_asset_assignments(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    return [
        _assignment_out(a)
        for a in crud.list_asset_assignments(db, company.id, limit=limit, offset=offset)
    ]


@router.post(
    "/asset-assignments",
    response_model=schemas.AssetAssignmentOut,
    status_code=201,
)
def create_asset_assignment(
    payload: schemas.AssetAssignmentCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    company = db.get(models.Company, current_user.company_id)
    asset = crud.get_asset_inventory_item_by_tag(db, company.id, payload.asset_tag)
    if asset is None:
        raise HTTPException(status_code=409, detail=f"Asset tag '{payload.asset_tag}' not found")
    if asset.status == "assigned":
        raise HTTPException(
            status_code=409,
            detail=f"Asset tag '{payload.asset_tag}' is already assigned to another employee.",
        )
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != company.id:
        raise HTTPException(status_code=422, detail="Employee not found in your company.")
    assigned_on = payload.assigned_on or crud.company_today(db, company.id)
    if asset.purchased_on is not None and assigned_on < asset.purchased_on:
        raise HTTPException(status_code=422, detail="Assigned date can't be before the asset's purchase date.")
    assignment = crud.create_asset_assignment(db, asset.id, payload.employee_id, assigned_on)
    asset.status = "assigned"
    crud.create_audit_log(db, company.id, current_user.id, "create", "asset_assignment", assignment.id)
    db.commit()
    db.refresh(assignment)
    return _assignment_out(assignment)


@router.patch(
    "/asset-assignments/{assignment_id}/return",
    response_model=schemas.AssetAssignmentOut,
)
def process_asset_return(
    assignment_id: uuid.UUID,
    payload: schemas.AssetReturnUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    company = db.get(models.Company, current_user.company_id)
    assignment = crud.get_asset_assignment_for_update(db, assignment_id)
    if assignment is None or assignment.asset is None or assignment.asset.company_id != company.id:
        raise HTTPException(status_code=404, detail="Asset assignment not found")
    # M-04: one return per assignment; return date on/after assignment.
    if assignment.returned_on is not None:
        raise HTTPException(
            status_code=409,
            detail=f"This asset was already returned on {assignment.returned_on.isoformat()}.",
        )
    returned_on = payload.returned_on or crud.company_today(db, company.id)
    if returned_on < assignment.assigned_on:
        raise HTTPException(status_code=422, detail="Return date can't be before the assigned date.")
    if returned_on > crud.company_today(db, company.id):
        raise HTTPException(status_code=422, detail="Return date can't be in the future.")
    assignment.returned_on = returned_on
    assignment.return_condition = form_config.require_option(
        db, company.id, "asset.return_condition", payload.return_condition, current=assignment.return_condition)
    assignment.return_notes = payload.return_notes
    assignment.asset.status = "available"
    crud.create_audit_log(db, company.id, current_user.id, "update", "asset_assignment", assignment.id)
    db.commit()
    db.refresh(assignment)
    return _assignment_out(assignment)


def _asset_request_out(
    db: Session,
    r: models.AssetRequestModel,
    names: dict[uuid.UUID, str] | None = None,
) -> schemas.AssetRequestOut:
    def name(employee_id: uuid.UUID | None) -> str:
        if employee_id is None:
            return "—"
        if names is not None:
            return names.get(employee_id, "—")
        return crud.employee_display_name(db, employee_id)

    return schemas.AssetRequestOut(
        id=r.id,
        employee_id=r.employee_id,
        asset_type=r.asset_type,
        justification=r.justification,
        status=r.status,
        approver_id=r.approver_id,
        approver_name=name(r.approver_id),
        decision_notes=r.decision_notes,
        decided_at=r.decided_at,
        decided_by_manager_type=crud.decided_by_manager_type(
            db.get(models.Employee, r.employee_id), r.approver_id
        ),
    )


@router.get("/asset-requests", response_model=list[schemas.AssetRequestOut])
def list_asset_requests(
    limit: int | None = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    rows = crud.list_asset_requests(db, company.id, limit=limit, offset=offset)
    approver_ids = {r.approver_id for r in rows if r.approver_id}
    names = crud.employee_display_names_bulk(db, approver_ids)
    return [_asset_request_out(db, r, names=names) for r in rows]


@router.post(
    "/asset-requests",
    response_model=schemas.AssetRequestOut,
    status_code=201,
)
def create_asset_request(
    payload: schemas.AssetRequestCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if not crud.can_submit_self_service_request(
        db, current_user, payload.employee_id, "asset_management", "asset_management", "manage"
    ):
        raise HTTPException(status_code=403, detail="You can only submit this for yourself")
    # WF-03: a requester with no Reporting Manager (e.g. the CEO) is no
    # longer refused -- crud.notify_new_request routes the request to the
    # fallback approvers (Owner / System Settings RBAC holders) who can
    # decide it; self-approval stays blocked.
    request = crud.create_asset_request(db, payload.employee_id, " ".join((payload.asset_type or "").split()),
                                        payload.justification)
    crud.create_audit_log(db, company.id, current_user.id, "create", "asset_request", request.id)
    db.commit()
    db.refresh(request)
    crud.notify_new_request(db, company.id, employee, "Asset Request", "asset_request", request.id)
    db.commit()
    return _asset_request_out(db, request)


@router.patch(
    "/asset-requests/{request_id}",
    response_model=schemas.AssetRequestOut,
)
def update_asset_request(
    request_id: uuid.UUID,
    payload: schemas.AssetRequestUpdate,
    db: Session = Depends(get_db),
    # Reporting Manager Approval Workflow: see the identical comment on
    # update_leave_request in routers/leave.py.
    current_user: models.User = Depends(get_current_user),
):
    company = db.get(models.Company, current_user.company_id)
    request = crud.get_asset_request_for_update(db, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Asset request not found")
    # Two Reporting Managers: same atomic-with-lock guard as
    # update_work_entry/update_leave_request -- blocks a conflicting second
    # decision from the employee's other manager. Checked before the
    # permission gate so that manager sees a clear "already decided"
    # message instead of a confusing permission error once
    # can_decide_request_configurable (correctly) stops recognizing them
    # as the current decider.
    if request.status in ("approved", "rejected"):
        decider = crud.employee_display_name(db, request.approver_id)
        decider_type = crud.decided_by_manager_type(
            db.get(models.Employee, request.employee_id), request.approver_id
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"This request was already {request.status} by {decider}"
                f" ({decider_type}) and cannot be decided again"
                if decider_type
                else f"This request was already {request.status} and cannot be decided again"
            ),
        )
    if not crud.can_decide_request_configurable(
        db, current_user, request.employee_id, "asset_request", request.id
    ):
        raise HTTPException(
            status_code=403,
            detail="Only this employee's assigned Reporting Manager can act on this request",
        )
    effective_status = crud.decide_configurable_request(
        db, current_user, request.employee_id, "asset_request", request.id,
        payload.status, payload.decision_notes,
    )
    request.status = effective_status
    request.approver_id = current_user.employee_id
    request.decision_notes = payload.decision_notes
    if effective_status != "pending":
        request.decided_at = datetime.now(timezone.utc)
    crud.create_audit_log(db, company.id, current_user.id, payload.status, "asset_request", request.id)
    db.commit()
    db.refresh(request)
    if effective_status != "pending":
        crud.notify_decision(
            db, company.id, request.employee_id, "Asset Request",
            effective_status, payload.decision_notes, "asset_request", request.id,
        )
        db.commit()
    return _asset_request_out(db, request)


@router.get(
    "/asset-recovery-deductions",
    response_model=list[schemas.AssetRecoveryDeductionOut],
)
def list_asset_recovery_deductions(
    employee_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Assets > Asset Recovery Deduction audit trail -- every request
    regardless of status, each showing whether/when it was actually
    applied to a payslip (Payroll Settings > Deductions Configuration)."""
    return crud.list_asset_recovery_deductions(db, current_user.company_id, employee_id)


@router.post(
    "/asset-recovery-deductions",
    response_model=schemas.AssetRecoveryDeductionOut,
    status_code=201,
)
def create_asset_recovery_deduction(
    payload: schemas.AssetRecoveryDeductionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    """HR/Asset-management proposes a recovery amount for an employee
    (e.g. an unreturned/damaged asset), targeted at a specific payroll
    period -- starts 'pending', only picked up for payroll once approved
    (PATCH .../decision)."""
    employee = db.get(models.Employee, payload.employee_id)
    if employee is None or employee.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Employee not found")
    if payload.asset_assignment_id is not None:
        assignment = db.get(models.AssetAssignment, payload.asset_assignment_id)
        if assignment is None or assignment.employee_id != payload.employee_id:
            raise HTTPException(status_code=404, detail="Asset assignment not found for this employee")
    deduction = crud.create_asset_recovery_deduction(
        db, current_user.company_id, payload.employee_id, payload.asset_assignment_id,
        payload.amount, payload.reason, payload.target_period_month, payload.target_period_year,
        current_user.id,
    )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, "create", "asset_recovery_deduction", deduction.id,
    )
    db.commit()
    db.refresh(deduction)
    return crud.asset_recovery_deduction_out(db, deduction)


@router.patch(
    "/asset-recovery-deductions/{deduction_id}/decision",
    response_model=schemas.AssetRecoveryDeductionOut,
)
def decide_asset_recovery_deduction(
    deduction_id: uuid.UUID,
    payload: schemas.AssetRecoveryDeductionDecision,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_permission("asset_management")),
):
    deduction = crud.get_asset_recovery_deduction_for_update(db, deduction_id)
    if deduction is None or deduction.company_id != current_user.company_id:
        raise HTTPException(status_code=404, detail="Asset recovery deduction not found")
    if deduction.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"This deduction was already {deduction.status} and cannot be decided again",
        )
    crud.decide_asset_recovery_deduction(
        db, deduction, payload.status, payload.decision_notes, current_user.employee_id,
    )
    crud.create_audit_log(
        db, current_user.company_id, current_user.id, payload.status, "asset_recovery_deduction", deduction.id,
    )
    db.commit()
    db.refresh(deduction)
    return crud.asset_recovery_deduction_out(db, deduction)
