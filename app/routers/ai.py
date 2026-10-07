"""AI endpoints: chat, routing, conversations, memory, tools, safety, escalations, monitor."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import CurrentUser, audit, get_current_user, get_optional_user, patient_for_user, require_permission
from ..security import utcnow
from ..services import ai_engine, ai_tools, notify
from ..services.ai_tools import ToolContext

router = APIRouter(prefix="/ai", tags=["AI Assistant"])


# ==========================================================================
# CHAT
# ==========================================================================
@router.post("/chat", response_model=schemas.AIChatResponse, summary="Talk to the AI front-desk assistant")
def chat(payload: schemas.AIChatRequest, request: Request, db: Session = Depends(get_db),
         user: CurrentUser | None = Depends(get_optional_user)):
    """Works for anonymous visitors (booking flow identifies them) and logged-in patients."""
    if user and user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if patient and not payload.phone:
            payload.phone = patient.phone
    result = ai_engine.handle_message(db, payload, user=user)
    if not user:
        audit(db, action="AI_CHAT_ANONYMOUS", resource="ai_conversation",
              resource_id=result.get("conversation_id"), request=request,
              new_value={"intent": result.get("intent"), "stage": result.get("stage")})
    return result


@router.get("/routing", summary="Stateless concern -> specialty -> doctor -> slots routing")
def routing(concern: str = Query(..., min_length=3), days_ahead: int = 7, limit: int = 5,
            db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    return ai_engine.quick_slot_search(db, concern_text=concern, days_ahead=days_ahead, limit=limit)


@router.get("/intents", summary="Supported intents and safety rules")
def intents(_: CurrentUser | None = Depends(get_optional_user)):
    return {
        "intents": [name for name, _ in ai_engine.INTENT_PATTERNS] + ["SELECT_OPTION", "PROVIDE_NAME",
                                                                     "PROVIDE_PHONE", "PROVIDE_CONCERN",
                                                                     "CONFIRM_BOOKING", "DECLINE"],
        "stages": ["IDLE", "AWAIT_IDENTITY_NAME", "AWAIT_IDENTITY_PHONE", "AWAIT_CONCERN",
                   "AWAIT_DOCTOR_CHOICE", "AWAIT_SLOT_CHOICE", "AWAIT_CONFIRMATION",
                   "AWAIT_APPOINTMENT_CHOICE", "POST_BOOK", "ESCALATED_RECEPTION", "ESCALATED_DOCTOR"],
        "safety": {
            "medical_advice_restriction": True,
            "diagnosis_restriction": True,
            "prescription_restriction": True,
            "emergency_detection": True,
            "urgent_escalation": True,
            "doctor_escalation": True,
            "reception_escalation": True,
            "uncertainty_handling": "the assistant admits gaps and routes to a human instead of guessing",
            "conversation_audit": "every message, tool call and routing decision is persisted",
        },
        "pipeline": ["patient message", "intent detection", "concern extraction", "structured concern",
                     "specialty matching", "doctor matching", "availability", "slots", "patient choice",
                     "booking"],
    }


# ==========================================================================
# CONVERSATIONS / MEMORY
# ==========================================================================
@router.get("/conversations", response_model=list[schemas.AIConversationOut], summary="List conversations")
def list_conversations(db: Session = Depends(get_db), patient_id: int | None = None,
                       status_filter: str | None = None, channel: str | None = None,
                       limit: int = Query(50, le=500), user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.AIConversation)
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.AIConversation.patient_id == (patient.id if patient else -1))
    elif not user.has_permission("ai:admin"):
        stmt = stmt.where(models.AIConversation.user_id == user.id)
    if patient_id:
        stmt = stmt.where(models.AIConversation.patient_id == patient_id)
    if status_filter:
        stmt = stmt.where(models.AIConversation.status == status_filter.upper())
    if channel:
        stmt = stmt.where(models.AIConversation.channel == channel.upper())
    rows = db.scalars(stmt.order_by(models.AIConversation.started_at.desc()).limit(limit)).all()
    return [{
        "id": c.id, "session_id": c.session_id, "patient_id": c.patient_id,
        "patient_name": c.patient.full_name if c.patient else None, "channel": c.channel,
        "status": c.status, "stage": c.stage, "patient_type": c.patient_type,
        "primary_intent": c.primary_intent, "concern_summary": c.concern_summary, "summary": c.summary,
        "message_count": c.message_count, "tool_call_count": c.tool_call_count,
        "booking_attempts": c.booking_attempts, "successful_bookings": c.successful_bookings,
        "failed_bookings": c.failed_bookings, "escalation_count": c.escalation_count,
        "error_count": c.error_count, "avg_response_ms": c.avg_response_ms,
        "started_at": c.started_at, "last_message_at": c.last_message_at,
    } for c in rows]


@router.get("/conversations/{conversation_id}", summary="Conversation detail + messages")
def conversation_detail(conversation_id: int, db: Session = Depends(get_db),
                        user: CurrentUser = Depends(get_current_user)):
    conv = db.get(models.AIConversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or conv.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    messages = db.scalars(select(models.AIMessage).where(
        models.AIMessage.conversation_id == conversation_id).order_by(models.AIMessage.id)).all()
    tool_calls = db.scalars(select(models.AIToolCall).where(
        models.AIToolCall.conversation_id == conversation_id).order_by(models.AIToolCall.id)).all()
    concerns = db.scalars(select(models.AIConcern).where(
        models.AIConcern.conversation_id == conversation_id)).all()
    return {
        "conversation": {
            "id": conv.id, "session_id": conv.session_id, "status": conv.status, "stage": conv.stage,
            "patient_id": conv.patient_id, "patient_type": conv.patient_type,
            "channel": conv.channel, "primary_intent": conv.primary_intent,
            "intent_history": conv.intent_history, "concern_summary": conv.concern_summary,
            "summary": conv.summary, "started_at": conv.started_at,
            "message_count": conv.message_count, "tool_call_count": conv.tool_call_count,
            "successful_bookings": conv.successful_bookings, "failed_bookings": conv.failed_bookings,
            "escalation_count": conv.escalation_count, "avg_response_ms": conv.avg_response_ms,
        },
        "messages": [{
            "id": m.id, "role": m.role, "content": m.content, "intent": m.intent,
            "confidence": float(m.confidence) if m.confidence is not None else None,
            "tool_calls": m.tool_calls, "structured": m.structured, "latency_ms": m.latency_ms,
            "safety_flag": m.safety_flag, "at": m.created_at.isoformat(),
        } for m in messages],
        "tool_calls": [{
            "id": t.id, "tool": t.tool_name, "arguments": t.arguments, "status": t.status,
            "error": t.error, "duration_ms": t.duration_ms, "at": t.created_at.isoformat(),
        } for t in tool_calls],
        "concerns": [{
            "id": c.id, "text": c.concern_text, "keywords": c.keywords, "urgency": c.urgency,
            "specialty_id": c.specialty_id, "status": c.status,
        } for c in concerns],
    }


@router.post("/conversations/{conversation_id}/close", summary="Close a conversation")
def close_conversation(conversation_id: int, request: Request, db: Session = Depends(get_db),
                       user: CurrentUser = Depends(require_permission("ai:admin"))):
    conv = db.get(models.AIConversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.status = "CLOSED"
    conv.stage = "CLOSED"
    conv.ended_at = utcnow()
    db.commit()
    summary = ai_engine.conversation_summary(db, conv)
    audit(db, action="AI_CONVERSATION_CLOSE", resource="ai_conversation", resource_id=conversation_id,
          user=user, request=request)
    return {"success": True, "summary": summary.summary if summary else None}


@router.post("/conversations/{conversation_id}/escalate", response_model=schemas.AIEscalationOut,
             summary="Escalate a conversation to reception / doctor")
def escalate_conversation(conversation_id: int, level: str = "RECEPTION", reason: str = "manual escalation",
                          request: Request = None, db: Session = Depends(get_db),
                          user: CurrentUser = Depends(require_permission("ai:admin"))):
    conv = db.get(models.AIConversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    patient = db.get(models.Patient, conv.patient_id) if conv.patient_id else None
    esc = ai_engine.escalate(db, conv, level.upper(), reason, patient=patient)
    audit(db, action="AI_CONVERSATION_ESCALATE", resource="ai_conversation", resource_id=conversation_id,
          user=user, request=request, new_value={"level": level, "reason": reason})
    return esc


@router.get("/conversations/{conversation_id}/messages", response_model=list[schemas.AIMessageOut],
            summary="Raw message list")
def conversation_messages(conversation_id: int, db: Session = Depends(get_db),
                          user: CurrentUser = Depends(get_current_user)):
    conv = db.get(models.AIConversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        if not patient or conv.patient_id != patient.id:
            raise HTTPException(status_code=403, detail="Not allowed")
    return list(db.scalars(select(models.AIMessage).where(
        models.AIMessage.conversation_id == conversation_id).order_by(models.AIMessage.id)))


# ==========================================================================
# CONCERNS / SUMMARIES / MEMORY
# ==========================================================================
@router.get("/concerns", summary="Concern history")
def list_concerns(db: Session = Depends(get_db), patient_id: int | None = None,
                  urgency: str | None = None, status_filter: str | None = None, limit: int = 100,
                  user: CurrentUser = Depends(get_current_user)):
    stmt = select(models.AIConcern)
    if user.role == "PATIENT":
        patient = patient_for_user(db, user.id)
        stmt = stmt.where(models.AIConcern.patient_id == (patient.id if patient else -1))
    elif not user.has_permission("ai:admin"):
        raise HTTPException(status_code=403, detail="Missing permission: ai:admin")
    if patient_id:
        stmt = stmt.where(models.AIConcern.patient_id == patient_id)
    if urgency:
        stmt = stmt.where(models.AIConcern.urgency == urgency.upper())
    if status_filter:
        stmt = stmt.where(models.AIConcern.status == status_filter.upper())
    rows = db.scalars(stmt.order_by(models.AIConcern.created_at.desc()).limit(limit)).all()
    return [{
        "id": c.id, "patient_id": c.patient_id, "conversation_id": c.conversation_id,
        "concern": c.concern_text, "keywords": c.keywords, "duration": c.duration_text,
        "severity": c.severity, "urgency": c.urgency, "status": c.status,
        "specialty_id": c.specialty_id, "appointment_id": c.appointment_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    } for c in rows]


@router.get("/summaries", response_model=list[schemas.AISummaryOut], summary="AI memory summaries")
def list_summaries(db: Session = Depends(get_db), patient_id: int | None = None, limit: int = 50,
                   _: CurrentUser = Depends(require_permission("ai:admin", "records:read", any_of=True))):
    stmt = select(models.AISummary)
    if patient_id:
        stmt = stmt.where(models.AISummary.patient_id == patient_id)
    return list(db.scalars(stmt.order_by(models.AISummary.created_at.desc()).limit(limit)))


@router.post("/summaries/patient/{patient_id}", response_model=schemas.AISummaryOut,
             summary="Generate a fresh patient context summary")
def generate_summary(patient_id: int, db: Session = Depends(get_db),
                     user: CurrentUser = Depends(require_permission("ai:admin", "records:read", any_of=True))):
    patient = db.get(models.Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    context = ai_engine.build_patient_context_summary(db, patient)
    summary = models.AISummary(
        patient_id=patient_id, summary_type="CONTEXT", summary=context["text_summary"],
        context_json=context, generated_by="AI",
        token_estimate=len(context["text_summary"]) // 4,
    )
    db.add(summary)
    db.commit()
    db.refresh(summary)
    return summary


@router.get("/context/{patient_id}", summary="AI context retrieval for a patient")
def context(db: Session = Depends(get_db), patient_id: int = 0,
            _: CurrentUser = Depends(require_permission("ai:admin", "records:read", any_of=True))):
    patient = db.get(models.Patient, patient_id)
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    latest = db.scalar(select(models.AISummary).where(
        models.AISummary.patient_id == patient_id).order_by(models.AISummary.created_at.desc()).limit(1))
    conv = db.scalar(select(models.AIConversation).where(
        models.AIConversation.patient_id == patient_id).order_by(
        models.AIConversation.started_at.desc()).limit(1))
    return {
        "context": ai_engine.build_patient_context_summary(db, patient),
        "latest_summary": latest.summary if latest else None,
        "last_conversation": {"id": conv.id, "stage": conv.stage, "intent": conv.primary_intent,
                              "summary": conv.summary} if conv else None,
    }


# ==========================================================================
# TOOLS
# ==========================================================================
@router.get("/tools", response_model=list[schemas.AIToolOut], summary="Available AI tools")
def tools(_: CurrentUser = Depends(require_permission("ai:tools", "ai:admin", "ai:chat", any_of=True))):
    return ai_tools.list_tools()


@router.post("/tools/invoke", summary="Invoke an AI tool directly (debug / admin)")
def invoke_tool(payload: schemas.AIToolInvoke, request: Request, db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_permission("ai:tools"))):
    ctx = ToolContext(db, patient_id=payload.arguments.get("patient_id"), user_id=user.id, caller=user.email)
    try:
        result = ai_tools.invoke_tool(db, ctx, payload.name, payload.arguments)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    audit(db, action="AI_TOOL_INVOKE", resource="ai_tool", resource_id=payload.name, user=user,
          request=request, new_value={"arguments": payload.arguments})
    return {"tool": payload.name, "result": result}


@router.get("/tools/calls", summary="Tool call log")
def tool_calls(db: Session = Depends(get_db), tool_name: str | None = None, status_filter: str | None = None,
               limit: int = Query(100, le=500), _: CurrentUser = Depends(require_permission("ai:admin"))):
    stmt = select(models.AIToolCall)
    if tool_name:
        stmt = stmt.where(models.AIToolCall.tool_name == tool_name)
    if status_filter:
        stmt = stmt.where(models.AIToolCall.status == status_filter.upper())
    rows = db.scalars(stmt.order_by(models.AIToolCall.created_at.desc()).limit(limit)).all()
    return [{
        "id": t.id, "conversation_id": t.conversation_id, "tool": t.tool_name, "arguments": t.arguments,
        "status": t.status, "error": t.error, "duration_ms": t.duration_ms,
        "result_summary": t.result_summary, "at": t.created_at.isoformat(),
    } for t in rows]


# ==========================================================================
# ESCALATIONS
# ==========================================================================
@router.get("/escalations", response_model=list[schemas.AIEscalationOut], summary="Escalation queue")
def escalations(db: Session = Depends(get_db), status_filter: str | None = "OPEN", level: str | None = None,
                limit: int = 100, _: CurrentUser = Depends(require_permission("ai:admin", "queue:manage",
                                                                               any_of=True))):
    stmt = select(models.AIEscalation)
    if status_filter and status_filter.upper() != "ALL":
        stmt = stmt.where(models.AIEscalation.status == status_filter.upper())
    if level:
        stmt = stmt.where(models.AIEscalation.level == level.upper())
    return list(db.scalars(stmt.order_by(models.AIEscalation.created_at.desc()).limit(limit)))


@router.post("/escalations/{escalation_id}/resolve", response_model=schemas.AIEscalationOut,
             summary="Resolve an escalation")
def resolve_escalation(escalation_id: int, notes: str = "handled", request: Request = None,
                       db: Session = Depends(get_db),
                       user: CurrentUser = Depends(require_permission("ai:admin", "queue:manage",
                                                                      any_of=True))):
    esc = db.get(models.AIEscalation, escalation_id)
    if not esc:
        raise HTTPException(status_code=404, detail="Escalation not found")
    esc.status = "RESOLVED"
    esc.assigned_to = user.id
    esc.resolution_notes = notes
    esc.resolved_at = utcnow()
    conv = db.get(models.AIConversation, esc.conversation_id)
    if conv and conv.status == "ESCALATED":
        conv.status = "ACTIVE"
    db.commit()
    audit(db, action="AI_ESCALATION_RESOLVE", resource="ai_escalation", resource_id=escalation_id,
          user=user, request=request, new_value={"notes": notes})
    db.refresh(esc)
    return esc


# ==========================================================================
# MONITOR
# ==========================================================================
@router.get("/monitor", summary="AI monitor: conversations, routing, tools, failures, escalations")
def monitor(db: Session = Depends(get_db), days: int = 14,
            _: CurrentUser = Depends(require_permission("ai:admin"))):
    from ..services import analytics

    return analytics.ai_monitor(db, days=days)


@router.get("/monitor/routing", summary="Routing decision log")
def routing_log(db: Session = Depends(get_db), limit: int = 100,
                _: CurrentUser = Depends(require_permission("ai:admin"))):
    rows = db.scalars(select(models.AIRoutingLog).order_by(
        models.AIRoutingLog.created_at.desc()).limit(limit)).all()
    return [{
        "id": r.id, "conversation_id": r.conversation_id,
        "selected_specialty_id": r.selected_specialty_id, "selected_doctor_id": r.selected_doctor_id,
        "candidates": r.candidate_specialties, "doctors": r.candidate_doctors,
        "match_score": float(r.match_score) if r.match_score is not None else None,
        "decision": r.decision, "at": r.created_at.isoformat(),
    } for r in rows]


@router.get("/monitor/failures", summary="Failed tool calls and AI errors")
def failures(db: Session = Depends(get_db), limit: int = 100,
             _: CurrentUser = Depends(require_permission("ai:admin"))):
    rows = db.scalars(select(models.AIToolCall).where(models.AIToolCall.status == "FAILED")
                      .order_by(models.AIToolCall.created_at.desc()).limit(limit)).all()
    conv_errors = db.scalars(select(models.AIConversation).where(models.AIConversation.error_count > 0)
                             .order_by(models.AIConversation.started_at.desc()).limit(25)).all()
    return {
        "failed_tool_calls": [{"tool": r.tool_name, "error": r.error, "arguments": r.arguments,
                               "conversation_id": r.conversation_id, "at": r.created_at.isoformat()}
                              for r in rows],
        "conversations_with_errors": [{"id": c.id, "errors": c.error_count, "intent": c.primary_intent,
                                       "stage": c.stage} for c in conv_errors],
    }
