"""Administration > Form Configuration API (see app/form_config.py).

  * GET  /api/form-config                      every signed-in user: the active
                                               options per field + the active
                                               custom fields per form (what the
                                               forms render)
  * everything under /api/form-config/...      System Settings / RBAC at Edit or
                                               Admin (same gate as custom roles)

Tenant isolation: every row is scoped by the caller's company_id.
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import crud, form_config as fc, models
from ..database import get_db
from ..deps import get_current_user, require_permission

router = APIRouter(prefix="/api/form-config", tags=["form-config"])
_ADMIN = require_permission("system_settings_rbac")
MAX_CUSTOM_FIELDS_PER_FORM = 30
MAX_OPTIONS_PER_FIELD = 200


# ── schemas ───────────────────────────────────────────────────────────────

class OptionOut(BaseModel):
    value: str
    label: str


class AdminOptionOut(BaseModel):
    id: uuid.UUID
    value: str
    label: str
    sort_order: int
    is_active: bool
    is_system: bool


class CustomFieldOut(BaseModel):
    id: uuid.UUID
    form_key: str
    key: str
    label: str
    type: str
    required: bool
    help_text: str | None = None
    sort_order: int
    is_active: bool = True
    options: list[OptionOut] = []


class FormConfigOut(BaseModel):
    options: dict[str, list[OptionOut]]
    custom_fields: dict[str, list[CustomFieldOut]]
    # field keys the caller may add an option to from a form ("Add custom...")
    quick_add: list[str] = []


class QuickAddIn(BaseModel):
    field_key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)


class QuickAddOut(BaseModel):
    value: str
    label: str
    created: bool


class FieldSettingIn(BaseModel):
    field_key: str = Field(min_length=1, max_length=80)
    allow_user_add: bool


class AdminFieldOut(BaseModel):
    key: str
    form_key: str
    form_label: str
    module: str
    label: str
    allow_custom: bool
    allow_user_add: bool = True
    options: list[AdminOptionOut]


class AdminCustomFieldOut(CustomFieldOut):
    admin_options: list[AdminOptionOut] = []
    allow_user_add: bool = True


class AdminFormOut(BaseModel):
    key: str
    label: str
    module: str


class AdminConfigOut(BaseModel):
    forms: list[AdminFormOut]
    fields: list[AdminFieldOut]
    custom_fields: list[AdminCustomFieldOut]
    externally_managed: dict[str, str]


class OptionCreate(BaseModel):
    field_key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)


class OptionUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None


class OptionOrder(BaseModel):
    field_key: str = Field(min_length=1, max_length=80)
    option_ids: list[uuid.UUID]


class CustomFieldCreate(BaseModel):
    form_key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=120)
    field_type: str
    is_required: bool = False
    help_text: str | None = Field(default=None, max_length=200)
    options: list[str] = Field(default_factory=list, max_length=MAX_OPTIONS_PER_FIELD)


class CustomFieldUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    is_required: bool | None = None
    help_text: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


# ── helpers ───────────────────────────────────────────────────────────────

def _require_tables(db: Session) -> None:
    if not fc._tables_ready(db):
        raise HTTPException(
            status_code=409,
            detail="Form Configuration isn't set up for this organization yet "
                   "(database migration add_form_configuration.sql has not been applied).")


def _opts(rows) -> list[OptionOut]:
    return [OptionOut(value=r.value, label=r.label) for r in rows if r.is_active]


def _admin_opts(rows) -> list[AdminOptionOut]:
    return [AdminOptionOut(id=r.id, value=r.value, label=r.label, sort_order=r.sort_order,
                           is_active=r.is_active, is_system=r.is_system) for r in rows]


def _custom_out(d: models.CustomFieldDef, rows: list[models.FormFieldOption]) -> CustomFieldOut:
    return CustomFieldOut(id=d.id, form_key=d.form_key, key=d.field_key, label=d.label, type=d.field_type,
                          required=d.is_required, help_text=d.help_text, sort_order=d.sort_order,
                          is_active=d.is_active, options=_opts(rows))


def _own_custom_field(db: Session, company_id: uuid.UUID, field_id: uuid.UUID) -> models.CustomFieldDef:
    d = db.get(models.CustomFieldDef, field_id)
    if d is None or d.company_id != company_id:
        raise HTTPException(status_code=404, detail="Custom field not found")
    return d


def _field_rules(db: Session, company_id: uuid.UUID, field_key: str) -> bool:
    """Validates [field_key] belongs to this company's configuration and
    returns whether admins may add options to it."""
    if field_key in fc.FIELDS:
        return fc.FIELDS[field_key].allow_custom
    if field_key.startswith("custom."):
        try:
            fid = uuid.UUID(field_key.split(".", 1)[1])
        except ValueError:
            raise HTTPException(status_code=404, detail="Unknown field") from None
        d = _own_custom_field(db, company_id, fid)
        if d.field_type != "dropdown":
            raise HTTPException(status_code=409, detail="Only dropdown fields have options.")
        return True
    raise HTTPException(status_code=404, detail="Unknown field")


def _own_option(db: Session, company_id: uuid.UUID, option_id: uuid.UUID) -> models.FormFieldOption:
    o = db.get(models.FormFieldOption, option_id)
    if o is None or o.company_id != company_id:
        raise HTTPException(status_code=404, detail="Option not found")
    return o


def _new_value(label: str, field_key: str) -> str:
    style = fc.FIELDS[field_key].value_style if field_key in fc.FIELDS else "label"
    value = fc.slugify(label) if style == "key" else label.strip()
    if not value:
        raise HTTPException(status_code=422, detail="Enter a name with letters or numbers.")
    return value[:120]


def _audit(db: Session, user: models.User, action: str, entity: str, entity_id, changes=None) -> None:
    crud.create_audit_log(db, user.company_id, user.id, action, entity, entity_id, changes=changes)


# ── read (every signed-in user) ───────────────────────────────────────────

@router.get("", response_model=FormConfigOut)
def get_form_config(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    cid = current_user.company_id
    out_options = {key: [OptionOut(value=v, label=l) for v, l in fc.options(db, cid, key)] for key in fc.FIELDS}
    custom: dict[str, list[CustomFieldOut]] = {}
    if fc._tables_ready(db):
        rows = fc._rows(db, cid)
        for form_key in fc.FORMS:
            for d in fc.custom_field_defs(db, cid, form_key):
                custom.setdefault(form_key, []).append(_custom_out(d, rows.get(f"custom.{d.id}", [])))
        quick = fc.quick_add_keys(db, cid)
        db.commit()  # persists the first-use seeding of defaults
    else:
        quick = []
    return FormConfigOut(options=out_options, custom_fields=custom, quick_add=quick)


@router.post("/options/quick-add", response_model=QuickAddOut, status_code=201)
def quick_add_option(payload: QuickAddIn, db: Session = Depends(get_db),
                     current_user: models.User = Depends(get_current_user)):
    """The "Add custom..." row of a dropdown: any signed-in user may add an
    option to a list that accepts them (and the admin hasn't switched off).
    An existing name is reused; the new option is shared by the whole
    organization and an administrator can rename / hide / remove it."""
    cid = current_user.company_id
    row, created = fc.quick_add_option(db, cid, current_user.id, payload.field_key, payload.label)
    if created:
        _audit(db, current_user, "create", "form_option", row.id,
               {"field": payload.field_key, "label": row.label, "via": "quick_add"})
    db.commit()
    fc.invalidate(db)
    return QuickAddOut(value=row.value, label=row.label, created=created)


# ── admin ─────────────────────────────────────────────────────────────────

@router.get("/admin", response_model=AdminConfigOut)
def get_admin_config(db: Session = Depends(get_db), current_user: models.User = Depends(_ADMIN)):
    cid = current_user.company_id
    rows = fc._rows(db, cid)
    sm = fc.user_add_settings(db, cid)
    fields = []
    for key, f in fc.FIELDS.items():
        form = fc.FORMS[f.form]
        fields.append(AdminFieldOut(
            key=key, form_key=f.form, form_label=form.label, module=form.module, label=f.label,
            allow_custom=f.allow_custom, allow_user_add=sm.get(key, True), options=_admin_opts(rows.get(key, []))))
    custom = []
    for form_key in fc.FORMS:
        for d in fc.custom_field_defs(db, cid, form_key, include_inactive=True):
            opt_rows = rows.get(f"custom.{d.id}", [])
            base = _custom_out(d, opt_rows)
            custom.append(AdminCustomFieldOut(
                **base.model_dump(), admin_options=_admin_opts(opt_rows), allow_user_add=sm.get(f"custom.{d.id}", True)))
    db.commit()
    return AdminConfigOut(
        forms=[AdminFormOut(key=f.key, label=f.label, module=f.module) for f in fc.FORMS.values()],
        fields=fields, custom_fields=custom, externally_managed=fc.EXTERNALLY_MANAGED)


@router.post("/options", response_model=AdminOptionOut, status_code=201)
def create_option(payload: OptionCreate, db: Session = Depends(get_db),
                  current_user: models.User = Depends(_ADMIN)):
    _require_tables(db)
    cid = current_user.company_id
    if not _field_rules(db, cid, payload.field_key):
        raise HTTPException(status_code=409, detail="This list drives built-in behaviour, so options can only be "
                                                    "renamed, hidden or re-ordered -- not added.")
    label = payload.label.strip()
    value = _new_value(label, payload.field_key)
    rows = fc._rows(db, cid).get(payload.field_key, [])
    if len(rows) >= MAX_OPTIONS_PER_FIELD:
        raise HTTPException(status_code=409, detail=f"A list can have at most {MAX_OPTIONS_PER_FIELD} options.")
    if any(r.value.lower() == value.lower() or r.label.lower() == label.lower() for r in rows):
        raise HTTPException(status_code=409, detail=f"'{label}' is already in this list.")
    row = models.FormFieldOption(
        id=uuid.uuid4(), company_id=cid, field_key=payload.field_key, value=value, label=label,
        sort_order=(max((r.sort_order for r in rows), default=-1) + 1), is_active=True, is_system=False,
        created_by=current_user.id)
    db.add(row)
    db.flush()
    _audit(db, current_user, "create", "form_option", row.id, {"field": payload.field_key, "label": label})
    db.commit()
    fc.invalidate(db)
    return _admin_opts([row])[0]


@router.patch("/options/{option_id}", response_model=AdminOptionOut)
def update_option(option_id: uuid.UUID, payload: OptionUpdate, db: Session = Depends(get_db),
                  current_user: models.User = Depends(_ADMIN)):
    _require_tables(db)
    cid = current_user.company_id
    row = _own_option(db, cid, option_id)
    siblings = fc._rows(db, cid).get(row.field_key, [])
    if payload.label is not None:
        label = payload.label.strip()
        if not label:
            raise HTTPException(status_code=422, detail="The name can't be blank.")
        if any(s.id != row.id and s.label.lower() == label.lower() for s in siblings):
            raise HTTPException(status_code=409, detail=f"'{label}' is already in this list.")
        row.label = label
    if payload.is_active is not None and payload.is_active != row.is_active:
        if not payload.is_active and sum(1 for s in siblings if s.is_active and s.id != row.id) == 0:
            raise HTTPException(status_code=409, detail="Keep at least one option available in each list.")
        row.is_active = payload.is_active
    row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    _audit(db, current_user, "update", "form_option", row.id,
           {"field": row.field_key, "label": row.label, "active": row.is_active})
    db.commit()
    fc.invalidate(db)
    return _admin_opts([row])[0]


@router.delete("/options/{option_id}")
def delete_option(option_id: uuid.UUID, db: Session = Depends(get_db),
                  current_user: models.User = Depends(_ADMIN)):
    """An admin-added option is deleted; a built-in one is hidden instead
    (records keep their stored value either way)."""
    _require_tables(db)
    cid = current_user.company_id
    row = _own_option(db, cid, option_id)
    siblings = fc._rows(db, cid).get(row.field_key, [])
    if row.is_active and sum(1 for s in siblings if s.is_active and s.id != row.id) == 0:
        raise HTTPException(status_code=409, detail="Keep at least one option available in each list.")
    if row.is_system:
        row.is_active = False
        row.updated_at = datetime.datetime.now(datetime.timezone.utc)
        result = {"deleted": False, "hidden": True}
    else:
        db.delete(row)
        result = {"deleted": True, "hidden": False}
    _audit(db, current_user, "delete", "form_option", option_id, {"field": row.field_key, "label": row.label})
    db.commit()
    fc.invalidate(db)
    return result


@router.put("/options/order", response_model=list[AdminOptionOut])
def reorder_options(payload: OptionOrder, db: Session = Depends(get_db),
                    current_user: models.User = Depends(_ADMIN)):
    _require_tables(db)
    cid = current_user.company_id
    _field_rules(db, cid, payload.field_key)
    rows = fc._rows(db, cid).get(payload.field_key, [])
    if {r.id for r in rows} != set(payload.option_ids) or len(payload.option_ids) != len(rows):
        raise HTTPException(status_code=422, detail="Send every option of the list exactly once.")
    by_id = {r.id: r for r in rows}
    for i, oid in enumerate(payload.option_ids):
        by_id[oid].sort_order = i
    _audit(db, current_user, "update", "form_option", None, {"field": payload.field_key, "reordered": True})
    db.commit()
    fc.invalidate(db)
    return _admin_opts(sorted(rows, key=lambda r: r.sort_order))


@router.post("/custom-fields", response_model=AdminCustomFieldOut, status_code=201)
def create_custom_field(payload: CustomFieldCreate, db: Session = Depends(get_db),
                        current_user: models.User = Depends(_ADMIN)):
    _require_tables(db)
    cid = current_user.company_id
    if payload.form_key not in fc.FORMS:
        raise HTTPException(status_code=422, detail="Unknown form.")
    if payload.field_type not in fc.CUSTOM_FIELD_TYPES:
        raise HTTPException(status_code=422, detail=f"Field type must be one of: {', '.join(fc.CUSTOM_FIELD_TYPES)}.")
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=422, detail="The label can't be blank.")
    existing = fc.custom_field_defs(db, cid, payload.form_key, include_inactive=True)
    if len(existing) >= MAX_CUSTOM_FIELDS_PER_FORM:
        raise HTTPException(status_code=409, detail=f"A form can have at most {MAX_CUSTOM_FIELDS_PER_FORM} custom fields.")
    if any(d.label.lower() == label.lower() for d in existing):
        raise HTTPException(status_code=409, detail=f"This form already has a field named '{label}'.")
    taken = {d.field_key for d in existing}
    base = fc.slugify(label) or "field"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    choices = [o.strip() for o in payload.options if o and o.strip()]
    if payload.field_type == "dropdown":
        if not choices:
            raise HTTPException(status_code=422, detail="A dropdown field needs at least one option.")
        if len({c.lower() for c in choices}) != len(choices):
            raise HTTPException(status_code=422, detail="Dropdown options must be different from each other.")
    d = models.CustomFieldDef(
        id=uuid.uuid4(), company_id=cid, form_key=payload.form_key, field_key=key[:60], label=label,
        field_type=payload.field_type, is_required=payload.is_required,
        help_text=(payload.help_text or "").strip() or None,
        sort_order=max((x.sort_order for x in existing), default=-1) + 1, is_active=True, created_by=current_user.id)
    db.add(d)
    db.flush()
    opt_rows = []
    if payload.field_type == "dropdown":
        for i, c in enumerate(choices):
            opt = models.FormFieldOption(
                id=uuid.uuid4(), company_id=cid, field_key=f"custom.{d.id}", value=c[:120], label=c[:120],
                sort_order=i, is_active=True, is_system=False, created_by=current_user.id)
            db.add(opt)
            opt_rows.append(opt)
        db.flush()
    _audit(db, current_user, "create", "custom_field", d.id, {"form": d.form_key, "label": label, "type": d.field_type})
    db.commit()
    fc.invalidate(db)
    return AdminCustomFieldOut(**_custom_out(d, opt_rows).model_dump(), admin_options=_admin_opts(opt_rows))


@router.patch("/custom-fields/{field_id}", response_model=CustomFieldOut)
def update_custom_field(field_id: uuid.UUID, payload: CustomFieldUpdate, db: Session = Depends(get_db),
                        current_user: models.User = Depends(_ADMIN)):
    _require_tables(db)
    cid = current_user.company_id
    d = _own_custom_field(db, cid, field_id)
    if payload.label is not None:
        label = payload.label.strip()
        if not label:
            raise HTTPException(status_code=422, detail="The label can't be blank.")
        clash = db.scalar(select(func.count()).select_from(models.CustomFieldDef).where(
            models.CustomFieldDef.company_id == cid, models.CustomFieldDef.form_key == d.form_key,
            models.CustomFieldDef.id != d.id, func.lower(models.CustomFieldDef.label) == label.lower()))
        if clash:
            raise HTTPException(status_code=409, detail=f"This form already has a field named '{label}'.")
        d.label = label
    if payload.is_required is not None:
        d.is_required = payload.is_required
    if "help_text" in payload.model_fields_set:
        d.help_text = (payload.help_text or "").strip() or None
    if payload.is_active is not None:
        d.is_active = payload.is_active
    d.updated_at = datetime.datetime.now(datetime.timezone.utc)
    _audit(db, current_user, "update", "custom_field", d.id, {"label": d.label, "required": d.is_required, "active": d.is_active})
    db.commit()
    fc.invalidate(db)
    return _custom_out(d, fc._rows(db, cid).get(f"custom.{d.id}", []))


@router.delete("/custom-fields/{field_id}", status_code=204)
def delete_custom_field(field_id: uuid.UUID, db: Session = Depends(get_db),
                        current_user: models.User = Depends(_ADMIN)):
    """Removes the field, its dropdown options and every saved value."""
    _require_tables(db)
    cid = current_user.company_id
    d = _own_custom_field(db, cid, field_id)
    for o in db.scalars(select(models.FormFieldOption).where(
            models.FormFieldOption.company_id == cid, models.FormFieldOption.field_key == f"custom.{d.id}")):
        db.delete(o)
    _audit(db, current_user, "delete", "custom_field", d.id, {"form": d.form_key, "label": d.label})
    db.delete(d)  # values go with it (ON DELETE CASCADE)
    db.commit()
    fc.invalidate(db)


@router.put("/field-settings", response_model=FieldSettingIn)
def set_field_setting(payload: FieldSettingIn, db: Session = Depends(get_db),
                      current_user: models.User = Depends(_ADMIN)):
    """Admin switch: may users add their own option to this list from forms?"""
    _require_tables(db)
    if not fc._settings_ready(db):
        raise HTTPException(status_code=409, detail="The database migration add_form_field_settings.sql has not been applied.")
    cid = current_user.company_id
    _field_rules(db, cid, payload.field_key)  # 404 for an unknown / foreign field
    row = db.get(models.FormFieldSetting, (cid, payload.field_key))
    if row is None:
        db.add(models.FormFieldSetting(company_id=cid, field_key=payload.field_key,
                                       allow_user_add=payload.allow_user_add, updated_by=current_user.id))
    else:
        row.allow_user_add = payload.allow_user_add
        row.updated_by = current_user.id
        row.updated_at = datetime.datetime.now(datetime.timezone.utc)
    _audit(db, current_user, "update", "form_field_setting", None,
           {"field": payload.field_key, "allow_user_add": payload.allow_user_add})
    db.commit()
    return payload
