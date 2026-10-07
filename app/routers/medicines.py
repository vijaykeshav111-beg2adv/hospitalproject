"""Medicine master."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, require_permission

router = APIRouter(prefix="/medicines", tags=["Medicines"])


@router.get("", response_model=list[schemas.MedicineOut], summary="List / search medicines")
def list_medicines(db: Session = Depends(get_db), search: str | None = None, active_only: bool = True,
                   limit: int = Query(100, le=500),
                   _: CurrentUser = Depends(require_permission("medicines:read", "prescriptions:read",
                                                               any_of=True))):
    stmt = select(models.Medicine)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(models.Medicine.name.like(like), models.Medicine.generic_name.like(like)))
    if active_only:
        stmt = stmt.where(models.Medicine.is_active.is_(True))
    return list(db.scalars(stmt.order_by(models.Medicine.name).limit(limit)))


@router.post("", response_model=schemas.MedicineOut, status_code=201, summary="Add a medicine")
def create_medicine(payload: schemas.MedicineBase, request: Request, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(require_permission("medicines:write"))):
    medicine = models.Medicine(**payload.model_dump())
    db.add(medicine)
    db.commit()
    db.refresh(medicine)
    audit(db, action="MEDICINE_CREATE", resource="medicine", resource_id=medicine.id, user=user,
          request=request, new_value=payload.model_dump())
    return medicine


@router.get("/{medicine_id}", response_model=schemas.MedicineOut, summary="Medicine detail")
def medicine_detail(medicine_id: int, db: Session = Depends(get_db),
                    _: CurrentUser = Depends(require_permission("medicines:read", "prescriptions:read",
                                                                any_of=True))):
    medicine = db.get(models.Medicine, medicine_id)
    if not medicine:
        raise HTTPException(status_code=404, detail="Medicine not found")
    return medicine


@router.patch("/{medicine_id}", response_model=schemas.MedicineOut, summary="Update medicine")
def update_medicine(medicine_id: int, payload: schemas.MedicineBase, request: Request,
                    db: Session = Depends(get_db),
                    user: CurrentUser = Depends(require_permission("medicines:write"))):
    medicine = db.get(models.Medicine, medicine_id)
    if not medicine:
        raise HTTPException(status_code=404, detail="Medicine not found")
    for field, value in payload.model_dump().items():
        setattr(medicine, field, value)
    db.commit()
    db.refresh(medicine)
    audit(db, action="MEDICINE_UPDATE", resource="medicine", resource_id=medicine_id, user=user,
          request=request, new_value=payload.model_dump())
    return medicine


@router.delete("/{medicine_id}", summary="Deactivate a medicine")
def deactivate_medicine(medicine_id: int, request: Request, db: Session = Depends(get_db),
                        user: CurrentUser = Depends(require_permission("medicines:write"))):
    medicine = db.get(models.Medicine, medicine_id)
    if not medicine:
        raise HTTPException(status_code=404, detail="Medicine not found")
    medicine.is_active = False
    db.commit()
    audit(db, action="MEDICINE_DEACTIVATE", resource="medicine", resource_id=medicine_id, user=user,
          request=request)
    return {"success": True, "medicine_id": medicine_id}
