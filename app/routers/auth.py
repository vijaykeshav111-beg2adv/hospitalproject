"""Authentication: login, logout, registration, forgot/reset password, sessions, activity."""

from __future__ import annotations



import uuid

from datetime import date



from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from sqlalchemy import func, select

from sqlalchemy.orm import Session



from .. import models, schemas

from ..config import settings

from ..database import get_db

from ..deps import CurrentUser, audit, client_ip, get_current_user, get_optional_user, require_permission

from ..rbac import permissions_for_role

from ..security import (

    create_access_token, create_refresh_token, hash_password, hash_token, new_opaque_token,

    utcnow, validate_password_strength, verify_password,

)

from ..services import notify, patients as patient_service



router = APIRouter(prefix="/auth", tags=["Authentication"])





def _user_out(user: models.User) -> dict:

    return {

        "id": user.id, "uuid": user.uuid, "full_name": user.full_name, "email": user.email,

        "phone": user.phone, "role_name": user.role_name, "is_active": user.is_active,

        "is_verified": user.is_verified, "last_login_at": user.last_login_at,

        "created_at": user.created_at,

    }





def _issue_session(db: Session, user: models.User, request: Request) -> dict:

    session_key = uuid.uuid4().hex

    access_token, jti, expires_at = create_access_token(user.id, user.role_name, user.email)

    refresh_token, refresh_expires = create_refresh_token(user.id, session_key)

    session = models.UserSession(

        session_key=session_key,

        user_id=user.id,

        refresh_token_hash=hash_token(refresh_token),

        access_jti=jti,

        ip_address=client_ip(request),

        user_agent=request.headers.get("user-agent", "")[:255],

        device=request.headers.get("x-device", "web")[:120],

        expires_at=refresh_expires,

        last_seen_at=utcnow(),

    )

    db.add(session)

    db.commit()

    return {

        "access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer",

        "expires_at": expires_at, "session_key": session_key,

    }





def _set_ui_access_cookie(response: Response, access_token: str, expires_at) -> None:
    """Set the access token cookie used by server-rendered /ui pages.

    The cookie is HttpOnly so JavaScript cannot read the JWT. The existing
    localStorage token continues to support the browser API client.
    """
    max_age = max(0, int((expires_at - utcnow()).total_seconds()))

    response.set_cookie(
        key="vvh_access_token",
        value=access_token,
        max_age=max_age,
        expires=max_age,
        httponly=True,
        secure=False,       # localhost development; use True behind HTTPS in production
        samesite="lax",
        path="/",
    )


def _login_activity(db: Session, email: str, status_value: str, request: Request, reason: str | None = None,

                    user_id: int | None = None) -> None:

    db.add(models.LoginActivity(

        user_id=user_id, email_attempted=email[:150], status=status_value, reason=reason,

        ip_address=client_ip(request), user_agent=request.headers.get("user-agent", "")[:255],

    ))

    db.commit()





@router.post("/register", response_model=schemas.TokenResponse, status_code=201,

             summary="Patient / staff registration")

def register(
    payload: schemas.RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):

    email = payload.email.lower()

    if db.scalar(select(models.User).where(models.User.email == email)):

        raise HTTPException(status_code=400, detail="An account with this email already exists")



    problems = validate_password_strength(payload.password)

    if problems:

        raise HTTPException(status_code=422, detail="Password " + ", ".join(problems))



    role = db.scalar(select(models.Role).where(models.Role.name == payload.role))

    if not role:

        raise HTTPException(status_code=400, detail=f"Unknown role {payload.role}")



    user = models.User(

        uuid=uuid.uuid4().hex,

        full_name=payload.full_name.strip(),

        email=email,

        phone=payload.phone,

        password_hash=hash_password(payload.password),

        role_id=role.id,

        is_active=True,

        is_verified=payload.role == "PATIENT",

    )

    db.add(user)

    db.commit()

    db.refresh(user)



    if payload.role == "PATIENT":

        patient, _ = patient_service.create_patient_with_login(

            db, password=payload.password, full_name=payload.full_name, phone=payload.phone or "",

            email=email, date_of_birth=payload.date_of_birth, gender=payload.gender,

            blood_group=payload.blood_group, address_line=payload.address_line, city=payload.city,

            state=payload.state, pincode=payload.pincode,

            emergency_contact_name=payload.emergency_contact_name,

            emergency_contact_phone=payload.emergency_contact_phone,

            emergency_contact_relation=payload.emergency_contact_relation,

            created_by=user.id, source="SELF_REGISTRATION",

        )



    tokens = _issue_session(db, user, request)

    _login_activity(db, email, "SUCCESS", request, "registration", user.id)

    audit(db, action="USER_REGISTER", resource="user", resource_id=user.id, user=user, request=request,

          new_value={"email": email, "role": payload.role})

    _set_ui_access_cookie(
        response,
        tokens["access_token"],
        tokens["expires_at"],
    )

    return {
        **tokens,
        "user": _user_out(user),
        "permissions": permissions_for_role(user.role_name),
    }





@router.post("/login", response_model=schemas.TokenResponse, summary="Login (JWT + session)")

def login(
    payload: schemas.LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):

    email = payload.email.lower()

    user = db.scalar(select(models.User).where(models.User.email == email))

    if not user:

        _login_activity(db, email, "FAILED", request, "unknown email")

        raise HTTPException(status_code=401, detail="Invalid email or password")



    if user.is_locked:

        _login_activity(db, email, "LOCKED", request, "account locked", user.id)

        raise HTTPException(status_code=423,

                            detail=f"Account locked until {user.locked_until:%Y-%m-%d %H:%M} (UTC)")



    if not verify_password(payload.password, user.password_hash):

        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1

        if user.failed_login_attempts >= settings.max_login_attempts:

            from datetime import timedelta

            user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)

            db.commit()

            _login_activity(db, email, "LOCKED", request,

                            f"{user.failed_login_attempts} failed attempts", user.id)

            raise HTTPException(status_code=423,

                                detail=f"Too many failed attempts. Locked for {settings.lockout_minutes} minutes.")

        db.commit()

        _login_activity(db, email, "FAILED", request, "wrong password", user.id)

        raise HTTPException(status_code=401, detail="Invalid email or password")



    if not user.is_active:

        _login_activity(db, email, "FAILED", request, "account disabled", user.id)

        raise HTTPException(status_code=403, detail="Account is disabled. Contact the administrator.")



    user.failed_login_attempts = 0

    user.locked_until = None

    user.last_login_at = utcnow()

    user.last_login_ip = client_ip(request)

    db.commit()



    tokens = _issue_session(db, user, request)

    _login_activity(db, email, "SUCCESS", request, "login", user.id)

    audit(db, action="LOGIN", resource="user", resource_id=user.id, user=user, request=request,

          new_value={"ip": client_ip(request)})

    _set_ui_access_cookie(
        response,
        tokens["access_token"],
        tokens["expires_at"],
    )

    return {
        **tokens,
        "user": _user_out(user),
        "permissions": permissions_for_role(user.role_name),
    }





@router.post("/logout", summary="Logout (revoke session)")

def logout(
    request: Request,
    response: Response,
    everywhere: bool = False,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):

    revoked = 0

    query = select(models.UserSession).where(models.UserSession.user_id == user.id,

                                             models.UserSession.is_revoked.is_(False))

    if not everywhere and user.session:

        query = query.where(models.UserSession.id == user.session.id)

    for session in db.scalars(query):

        session.is_revoked = True

        session.revoked_at = utcnow()

        session.revoked_reason = "logout all" if everywhere else "logout"

        revoked += 1

    db.commit()

    _login_activity(db, user.email, "LOGOUT", request, "all devices" if everywhere else "this device", user.id)

    audit(db, action="LOGOUT", resource="session", user=user, request=request,

          new_value={"revoked": revoked, "everywhere": everywhere})

    response.delete_cookie(
        key="vvh_access_token",
        path="/",
    )

    return {"success": True, "sessions_revoked": revoked}





@router.post("/refresh", response_model=schemas.TokenResponse, summary="Rotate access token")

def refresh(
    payload: schemas.RefreshRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):

    from ..security import decode_token



    try:

        data = decode_token(payload.refresh_token)

    except Exception:  # noqa: BLE001

        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    if data.get("type") != "refresh":

        raise HTTPException(status_code=401, detail="Wrong token type")



    session = db.scalar(select(models.UserSession).where(

        models.UserSession.refresh_token_hash == hash_token(payload.refresh_token),

        models.UserSession.is_revoked.is_(False),

    ))

    if not session or session.expires_at < utcnow():

        raise HTTPException(status_code=401, detail="Session expired or revoked")



    user = db.get(models.User, int(data["sub"]))

    if not user or not user.is_active:

        raise HTTPException(status_code=401, detail="User unavailable")



    access_token, jti, expires_at = create_access_token(user.id, user.role_name, user.email)

    new_refresh, refresh_expires = create_refresh_token(user.id, session.session_key)

    session.refresh_token_hash = hash_token(new_refresh)

    session.access_jti = jti

    session.expires_at = refresh_expires

    session.last_seen_at = utcnow()

    db.commit()

    _set_ui_access_cookie(
        response,
        access_token,
        expires_at,
    )

    return {
        "access_token": access_token,
        "refresh_token": new_refresh,
        "token_type": "bearer",
        "expires_at": expires_at,
        "user": _user_out(user),
        "permissions": permissions_for_role(user.role_name),
    }





@router.get("/me", summary="Current user + permissions")

def me(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):

    patient = db.scalar(select(models.Patient).where(models.Patient.user_id == user.id))

    return {

        "user": _user_out(user.user),

        "role": user.role,

        "permissions": sorted(user.permissions),

        "session": {

            "id": user.session.id if user.session else None,

            "ip": user.session.ip_address if user.session else None,

            "expires_at": user.session.expires_at.isoformat() if user.session else None,

        },

        "patient_profile": {"id": patient.id, "patient_code": patient.patient_code} if patient else None,

        "database": "mysql",

    }





@router.post("/change-password", summary="Change own password")

def change_password(payload: schemas.ChangePasswordRequest, request: Request,

                    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):

    if not verify_password(payload.current_password, user.user.password_hash):

        raise HTTPException(status_code=400, detail="Current password is incorrect")

    problems = validate_password_strength(payload.new_password)

    if problems:

        raise HTTPException(status_code=422, detail="Password " + ", ".join(problems))

    user.user.password_hash = hash_password(payload.new_password)

    user.user.password_changed_at = utcnow()

    user.user.must_change_password = False

    db.commit()

    audit(db, action="PASSWORD_CHANGE", resource="user", resource_id=user.id, user=user, request=request)

    return {"success": True, "message": "Password updated. Other sessions are still valid."}





@router.post("/forgot-password", summary="Request a password reset link")

def forgot_password(payload: schemas.ForgotPasswordRequest, request: Request, db: Session = Depends(get_db)):

    user = db.scalar(select(models.User).where(models.User.email == payload.email.lower()))

    token = None

    if user:

        token = new_opaque_token()

        db.add(models.PasswordResetToken(

            user_id=user.id, token_hash=hash_token(token), channel="EMAIL",

            expires_at=utcnow() + __import__("datetime").timedelta(minutes=30),

            requested_ip=client_ip(request),

        ))

        db.commit()

        reset_link = f"/ui/reset-password?token={token}"

        notify.queue_notification(

            db, template="CUSTOM", channel="EMAIL", user_id=user.id,

            subject=f"{settings.app_name} - password reset",

            context={"message": f"Hello {user.full_name}, use this link within 30 minutes to reset your "

                                f"password: {reset_link}"},

        )

        audit(db, action="PASSWORD_RESET_REQUEST", resource="user", resource_id=user.id,

              user=user, request=request)

    response = {"success": True,

                "message": "If that email exists, a reset link has been sent."}

    if settings.app_env != "production":

        response["reset_token"] = token  # dev convenience only

        response["note"] = "Token exposed because APP_ENV != production"

    return response





@router.post("/reset-password", summary="Reset password with a token")

def reset_password(payload: schemas.ResetPasswordRequest, request: Request, db: Session = Depends(get_db)):

    record = db.scalar(select(models.PasswordResetToken).where(

        models.PasswordResetToken.token_hash == hash_token(payload.token)))

    if not record or record.used_at or record.expires_at < utcnow():

        raise HTTPException(status_code=400, detail="Reset token is invalid or expired")

    problems = validate_password_strength(payload.new_password)

    if problems:

        raise HTTPException(status_code=422, detail="Password " + ", ".join(problems))



    user = db.get(models.User, record.user_id)

    if not user:

        raise HTTPException(status_code=400, detail="User not found")

    user.password_hash = hash_password(payload.new_password)

    user.password_changed_at = utcnow()

    user.failed_login_attempts = 0

    user.locked_until = None

    record.used_at = utcnow()

    for session in db.scalars(select(models.UserSession).where(

            models.UserSession.user_id == user.id, models.UserSession.is_revoked.is_(False))):

        session.is_revoked = True

        session.revoked_at = utcnow()

        session.revoked_reason = "password reset"

    db.commit()

    audit(db, action="PASSWORD_RESET", resource="user", resource_id=user.id, user=user, request=request)

    return {"success": True, "message": "Password reset. Please login with the new password."}





# --------------------------------------------------------------------------

# session management

# --------------------------------------------------------------------------

@router.get("/sessions", response_model=list[schemas.SessionOut], summary="My active sessions")

def my_sessions(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),

                include_revoked: bool = False):

    stmt = select(models.UserSession).where(models.UserSession.user_id == user.id)

    if not include_revoked:

        stmt = stmt.where(models.UserSession.is_revoked.is_(False))

    return list(db.scalars(stmt.order_by(models.UserSession.last_seen_at.desc()).limit(50)))





@router.get("/sessions/all", response_model=list[schemas.SessionOut], summary="All sessions (admin)")

def all_sessions(db: Session = Depends(get_db), user_id: int | None = None,

                 _: CurrentUser = Depends(require_permission("session:manage"))):

    stmt = select(models.UserSession).where(models.UserSession.is_revoked.is_(False))

    if user_id:

        stmt = stmt.where(models.UserSession.user_id == user_id)

    return list(db.scalars(stmt.order_by(models.UserSession.last_seen_at.desc()).limit(200)))





@router.delete("/sessions/{session_id}", summary="Revoke a session")

def revoke_session(session_id: int, request: Request, db: Session = Depends(get_db),

                   user: CurrentUser = Depends(get_current_user)):

    session = db.get(models.UserSession, session_id)

    if not session:

        raise HTTPException(status_code=404, detail="Session not found")

    is_admin = user.has_permission("session:manage")

    if session.user_id != user.id and not is_admin:

        raise HTTPException(status_code=403, detail="Not allowed")

    session.is_revoked = True

    session.revoked_at = utcnow()

    session.revoked_reason = f"revoked by {user.email}"

    db.commit()

    audit(db, action="SESSION_REVOKE", resource="session", resource_id=session_id, user=user, request=request)

    return {"success": True, "session_id": session_id}





# --------------------------------------------------------------------------

# login activity + audit log

# --------------------------------------------------------------------------

@router.get("/login-activity", response_model=list[schemas.LoginActivityOut],

            summary="Login / logout activity")

def login_activity(db: Session = Depends(get_db), email: str | None = None, status_filter: str | None = None,

                   limit: int = Query(50, le=500), user: CurrentUser = Depends(get_current_user)):

    stmt = select(models.LoginActivity)

    if not user.has_permission("audit:read"):

        stmt = stmt.where(models.LoginActivity.email_attempted == user.email)

    elif email:

        stmt = stmt.where(models.LoginActivity.email_attempted == email.lower())

    if status_filter:

        stmt = stmt.where(models.LoginActivity.status == status_filter.upper())

    return list(db.scalars(stmt.order_by(models.LoginActivity.attempt_at.desc()).limit(limit)))





@router.get("/audit-logs", response_model=list[schemas.AuditLogOut], summary="Audit trail")

def audit_logs(db: Session = Depends(get_db), action: str | None = None, resource: str | None = None,

               user_id: int | None = None, limit: int = Query(100, le=500),

               _: CurrentUser = Depends(require_permission("audit:read"))):

    stmt = select(models.AuditLog)

    if action:

        stmt = stmt.where(models.AuditLog.action == action.upper())

    if resource:

        stmt = stmt.where(models.AuditLog.resource == resource)

    if user_id:

        stmt = stmt.where(models.AuditLog.user_id == user_id)

    return list(db.scalars(stmt.order_by(models.AuditLog.created_at.desc()).limit(limit)))





@router.get("/audit-logs/summary", summary="Audit summary counters")

def audit_summary(db: Session = Depends(get_db), _: CurrentUser = Depends(require_permission("audit:read"))):

    by_action = db.execute(

        select(models.AuditLog.action, func.count(models.AuditLog.id))

        .group_by(models.AuditLog.action).order_by(func.count(models.AuditLog.id).desc()).limit(20)

    ).all()

    logins = db.scalar(select(func.count(models.LoginActivity.id)).where(

        models.LoginActivity.status == "SUCCESS")) or 0

    failed = db.scalar(select(func.count(models.LoginActivity.id)).where(

        models.LoginActivity.status.in_(["FAILED", "LOCKED"]))) or 0

    return {

        "total_audit_entries": db.scalar(select(func.count(models.AuditLog.id))) or 0,

        "successful_logins": logins,

        "failed_logins": failed,

        "by_action": [{"action": a, "count": c} for a, c in by_action],

    }
