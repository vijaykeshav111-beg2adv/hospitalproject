"""User + role/permission administration (RBAC matrix)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, require_permission
from ..rbac import ALL_PERMISSIONS, PERMISSIONS, ROLE_PERMISSIONS, permissions_for_role
from ..security import hash_password, utcnow, validate_password_strength

router = APIRouter(tags=["Users & RBAC"])


def _out(u: models.User) -> dict:
    return {"id": u.id, "uuid": u.uuid, "full_name": u.full_name, "email": u.email, "phone": u.phone,
            "role_name": u.role_name, "is_active": u.is_active, "is_verified": u.is_verified,
            "last_login_at": u.last_login_at, "created_at": u.created_at}


@router.get("/users", response_model=list[schemas.UserOut], summary="List users")
def list_users(db: Session = Depends(get_db), search: str | None = None, role: str | None = None,
               active_only: bool = False, limit: int = Query(100, le=500),
               _: CurrentUser = Depends(require_permission("users:read"))):
    stmt = select(models.User)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(models.User.full_name.like(like), models.User.email.like(like),
                              models.User.phone.like(like)))
    if role:
        stmt = stmt.join(models.Role).where(models.Role.name == role.upper())
    if active_only:
        stmt = stmt.where(models.User.is_active.is_(True))
    return list(db.scalars(stmt.order_by(models.User.created_at.desc()).limit(limit)))


@router.post("/users", response_model=schemas.UserOut, status_code=201, summary="Create a user")
def create_user(payload: schemas.UserCreate, request: Request, db: Session = Depends(get_db),
                admin: CurrentUser = Depends(require_permission("users:write"))):
    if payload.role == "SUPER_ADMIN" and admin.role != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Only a SUPER_ADMIN can create SUPER_ADMIN users")
    if db.scalar(select(models.User).where(models.User.email == payload.email.lower())):
        raise HTTPException(status_code=400, detail="Email already registered")
    problems = validate_password_strength(payload.password)
    if problems:
        raise HTTPException(status_code=422, detail="Password " + ", ".join(problems))
    role = db.scalar(select(models.Role).where(models.Role.name == payload.role))
    if not role:
        raise HTTPException(status_code=400, detail="Unknown role")
    user = models.User(
        uuid=uuid.uuid4().hex, full_name=payload.full_name, email=payload.email.lower(),
        phone=payload.phone, password_hash=hash_password(payload.password), role_id=role.id,
        is_active=True, is_verified=True, must_change_password=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    audit(db, action="USER_CREATE", resource="user", resource_id=user.id, user=admin, request=request,
          new_value=_out(user))
    return _out(user)


@router.get("/users/{user_id}", response_model=schemas.UserOut, summary="User detail")
def get_user(user_id: int, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    if user_id != user.id and not user.has_permission("users:read"):
        raise HTTPException(status_code=403, detail="Not allowed")
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return _out(target)


@router.patch("/users/{user_id}", response_model=schemas.UserOut, summary="Update a user")
def update_user(user_id: int, payload: schemas.UserUpdate, request: Request, db: Session = Depends(get_db),
                admin: CurrentUser = Depends(require_permission("users:write"))):
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    before = _out(target)
    data = payload.model_dump(exclude_unset=True)
    if "role" in data and data["role"]:
        if data["role"] == "SUPER_ADMIN" and admin.role != "SUPER_ADMIN":
            raise HTTPException(status_code=403, detail="Only a SUPER_ADMIN can grant SUPER_ADMIN")
        role = db.scalar(select(models.Role).where(models.Role.name == data.pop("role").upper()))
        if not role:
            raise HTTPException(status_code=400, detail="Unknown role")
        target.role_id = role.id
    for field, value in data.items():
        setattr(target, field, value)
    db.commit()
    db.refresh(target)
    audit(db, action="USER_UPDATE", resource="user", resource_id=user_id, user=admin, request=request,
          previous_value=before, new_value=_out(target))
    return _out(target)


@router.post("/users/{user_id}/reset-password", summary="Admin password reset")
def admin_reset_password(user_id: int, new_password: str, request: Request, db: Session = Depends(get_db),
                         admin: CurrentUser = Depends(require_permission("users:write"))):
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    problems = validate_password_strength(new_password)
    if problems:
        raise HTTPException(status_code=422, detail="Password " + ", ".join(problems))
    target.password_hash = hash_password(new_password)
    target.must_change_password = True
    target.failed_login_attempts = 0
    target.locked_until = None
    target.password_changed_at = utcnow()
    for session in db.scalars(select(models.UserSession).where(
            models.UserSession.user_id == user_id, models.UserSession.is_revoked.is_(False))):
        session.is_revoked = True
        session.revoked_at = utcnow()
        session.revoked_reason = "admin password reset"
    db.commit()
    audit(db, action="USER_PASSWORD_RESET", resource="user", resource_id=user_id, user=admin, request=request)
    return {"success": True, "user_id": user_id, "must_change_password": True}


@router.post("/users/{user_id}/unlock", summary="Unlock a locked account")
def unlock_user(user_id: int, request: Request, db: Session = Depends(get_db),
                admin: CurrentUser = Depends(require_permission("users:write"))):
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.failed_login_attempts = 0
    target.locked_until = None
    db.commit()
    audit(db, action="USER_UNLOCK", resource="user", resource_id=user_id, user=admin, request=request)
    return {"success": True, "user_id": user_id}


# --------------------------------------------------------------------------
# roles + permissions
# --------------------------------------------------------------------------
@router.get("/roles", summary="List roles with permission counts")
def list_roles(db: Session = Depends(get_db), _: CurrentUser = Depends(require_permission("users:read"))):
    roles = db.scalars(select(models.Role)).all()
    return [{
        "id": r.id, "name": r.name, "description": r.description, "is_system": r.is_system,
        "user_count": db.scalar(select(func.count(models.User.id)).where(models.User.role_id == r.id)) or 0,
        "permission_count": len(permissions_for_role(r.name)),
        "permissions": sorted(permissions_for_role(r.name)),
    } for r in roles]


@router.get("/roles/{role_name}", summary="Role detail")
def role_detail(role_name: str, db: Session = Depends(get_db),
                _: CurrentUser = Depends(require_permission("users:read"))):
    role = db.scalar(select(models.Role).where(models.Role.name == role_name.upper()))
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return {"id": role.id, "name": role.name, "description": role.description,
            "permissions": sorted(permissions_for_role(role.name))}


@router.get("/permissions", summary="Permission catalogue")
def list_permissions(_: CurrentUser = Depends(require_permission("users:read"))):
    modules: dict[str, list[dict]] = {}
    for code, (module, description) in PERMISSIONS.items():
        modules.setdefault(module, []).append({"code": code, "description": description})
    return {"total": len(PERMISSIONS), "modules": modules}


@router.get("/permissions/matrix", summary="Role x permission matrix")
def permission_matrix(_: CurrentUser = Depends(require_permission("roles:manage", "users:read",
                                                                     any_of=True))):
    return {
        "roles": list(ROLE_PERMISSIONS.keys()),
        "permissions": ALL_PERMISSIONS,
        "matrix": {role: sorted(permissions_for_role(role)) for role in ROLE_PERMISSIONS},
        "note": "SUPER_ADMIN holds every permission implicitly (wildcard).",
    }


@router.post("/users/{user_id}/roles", summary="Assign a role (SUPER_ADMIN / ADMIN)")
def assign_role(user_id: int, role_name: str, request: Request, db: Session = Depends(get_db),
                admin: CurrentUser = Depends(require_permission("roles:manage"))):
    target = db.get(models.User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    role = db.scalar(select(models.Role).where(models.Role.name == role_name.upper()))
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.name == "SUPER_ADMIN" and admin.role != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Only a SUPER_ADMIN can grant SUPER_ADMIN")
    before = {"role": target.role_name}
    target.role_id = role.id
    db.commit()
    audit(db, action="ROLE_ASSIGN", resource="user", resource_id=user_id, user=admin, request=request,
          previous_value=before, new_value={"role": role.name})
    return {"success": True, "user_id": user_id, "role": role.name,
            "permissions": sorted(permissions_for_role(role.name))}
