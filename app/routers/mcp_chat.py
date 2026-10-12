"""Separate MCP chatbot API. Existing /api/v1/ai/chat is intentionally untouched.

Security policy: authenticated users with ai:chat permission can use only read-only
public hospital tools. No patient records or write operations are exposed here.
If MCP/database data cannot be verified, return DATA_UNAVAILABLE and do not call the LLM.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..deps import CurrentUser, require_permission

log = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp-chat", tags=["MCP Chatbot"])
# Temporary conversation state, isolated by authenticated user.
# For production, use a persistent store with expiry and access controls.
_CONVERSATIONS: dict[str, dict[str, Any]] = {}
_MAX_CONVERSATIONS = 1000


def _new_conversation(user_id: str) -> str:
    conversation_id = secrets.token_urlsafe(24)
    _CONVERSATIONS[conversation_id] = {
        "user_id": user_id,
        "stage": "idle",
        "appointment_date": None,
        "options": [],
    }
    return conversation_id

class MCPChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1500)
    conversation_id: str | None = Field(default=None, max_length=100)

class MCPChatResponse(BaseModel):
    status: str
    answer: str
    tool: str | None = None
    data: Any = None
    conversation_id: str | None = None

def _extract_tool_data(result: Any) -> Any:
    """Normalize FastMCP/MCP SDK results without treating errors as empty data."""
    if getattr(result, "is_error", False):
        raise RuntimeError("MCP tool reported an error")
    data = getattr(result, "data", None)
    if data is not None:
        return data
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if content:
        for block in content:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}
    raise RuntimeError("MCP result contained no readable data")


async def _call_mcp(tool_name: str, arguments: dict[str, Any]) -> Any:
    # Imported only when this endpoint is called so the main hospital app can
    # still start without the optional MCP package installed.
    from fastmcp import Client

    endpoint = getattr(settings, "mcp_hospital_url", "http://127.0.0.1:8010/mcp")
    async with Client(endpoint) as client:
        result = await client.call_tool(tool_name, arguments)
    return _extract_tool_data(result)


def _select_tool(message: str) -> tuple[str, dict[str, Any]]:
    """Small transparent router for the learning demo; no LLM decides access."""
    text = message.strip().lower()
    if any(word in text for word in ("slot", "available", "availability", "appointment", "book")):
        import re
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        if not match:
            # Do not guess a date. Ask the user for it without querying or inventing availability.
            return "NEED_DATE", {}
        return "find_available_slots", {"appointment_date": match.group(1), "limit": 20}
    if any(word in text for word in ("specialty", "specialties", "department", "departments")):
        return "list_specialties", {}
    if any(word in text for word in ("stats", "statistics", "how many doctors", "hospital summary")):
        return "get_public_hospital_stats", {}
    # Default to directory search. Use the original message as a specialty query
    # only when the user explicitly mentions a known search intent.
    specialty = ""
    for term in ("dermatology", "cardiology", "neurology", "orthopedics", "orthopaedics", "pediatrics", "paediatrics"):
        if term in text:
            specialty = term
            break
    return "search_doctors", {"specialty": specialty, "limit": 10}


def _verified_fallback(tool: str, data: Any) -> str:
    """Deterministic response generated only from successfully retrieved tool data."""
    if tool == "list_specialties":
        rows = data.get("specialties", []) if isinstance(data, dict) else []
        if not rows:
            return "The database query succeeded, but no active specialties were found."
        return "Active specialties returned by the hospital database:\n" + "\n".join(
            f"- {x.get('name', 'Unnamed specialty')}: consultation fee {x.get('consultation_fee', 'not listed')}"
            for x in rows
        )
    if tool == "search_doctors":
        rows = data.get("doctors", []) if isinstance(data, dict) else []
        if not rows:
            return "No matching active doctors were found in the hospital database."
        return "Doctors found in the hospital database:\n" + "\n".join(
            f"- {x.get('name', 'Name not listed')} | {x.get('specialty', 'Specialty not listed')} | fee: {x.get('consultation_fee', 'not listed')}"
            for x in rows
        )
    if tool == "find_available_slots":
        rows = data.get("slots", []) if isinstance(data, dict) else []
        if not rows:
            return f"No available slots were returned for {data.get('date', 'the requested date')}. This is not a booking."
        return "Slots currently returned by the database (not reserved):\n" + "\n".join(
            f"- {x.get('date', data.get('date', ''))} {x.get('start_time', '')}-{x.get('end_time', '')}: {x.get('doctor_name', 'Doctor not listed')} ({x.get('specialty', 'specialty not listed')})"
            for x in rows
        )
    if tool == "get_public_hospital_stats":
        return "Verified aggregate hospital statistics:\n" + "\n".join(
            f"- {k.replace('_', ' ')}: {v}" for k, v in data.items()
        )
    return "The hospital database returned verified information."

def _get_conversation(
    conversation_id: str | None,
    user_id: str,
) -> tuple[str, dict[str, Any]]:
    """Return a conversation owned by this user, or create a new one."""
    if conversation_id:
        state = _CONVERSATIONS.get(conversation_id)
        if state is None or state.get("user_id") != user_id:
            raise HTTPException(
                status_code=404,
                detail="Conversation not found. Start a new conversation.",
            )
        return conversation_id, state

    # Bound memory use for this demo.
    if len(_CONVERSATIONS) >= _MAX_CONVERSATIONS:
        _CONVERSATIONS.clear()

    new_id = _new_conversation(user_id)
    return new_id, _CONVERSATIONS[new_id]
async def _llm_polish(message: str, tool: str, data: Any, fallback: str) -> str:
    """Optional Groq wording layer; receives only a successful, read-only tool result."""
    if not settings.groq_api_key:
        return fallback
    try:
        from groq import Groq
        payload = json.dumps(data, ensure_ascii=False, default=str)
        def call() -> str:
            client = Groq(api_key=settings.groq_api_key, timeout=settings.groq_timeout_seconds)
            response = client.chat.completions.create(
                model=settings.groq_model,
                temperature=0,
                max_tokens=500,
                messages=[
                    {"role": "system", "content": (
                        "You are the wording layer for a hospital MCP chatbot. Use ONLY the supplied tool result. "
                        "Never infer missing facts, invent doctors, prices, appointment slots, medical advice, or database status. "
                        "If a field is absent, say it was not provided. Do not claim an appointment was booked. "
                        "Answer briefly in the user's language."
                    )},
                    {"role": "user", "content": f"Question: {message}\nTool: {tool}\nVerified tool result JSON: {payload}"},
                ],
            )
            text = response.choices[0].message.content
            return text.strip() if text else fallback
        answer = await asyncio.to_thread(call)
        return answer or fallback
    except Exception:
        log.exception("MCP chatbot LLM wording failed; using deterministic verified response")
        return fallback


@router.post("/chat", response_model=MCPChatResponse)
async def mcp_chat(
    payload: MCPChatRequest,
    user: CurrentUser = Depends(require_permission("ai:chat")),
):
    if user.role not in {
        "PATIENT", "DOCTOR", "RECEPTIONIST",
        "ACCOUNTANT", "ADMIN", "SUPER_ADMIN",
    }:
        raise HTTPException(
            status_code=403,
            detail="Role is not allowed to use the MCP chatbot",
        )

    conversation_id, state = _get_conversation(
        payload.conversation_id, str(user.id)
    )
    message = payload.message.strip()
    text = message.lower()

    def reply(status: str, answer: str, tool=None, data=None):
        return MCPChatResponse(
            status=status,
            answer=answer,
            tool=tool,
            data=data,
            conversation_id=conversation_id,
        )

    # A date supplied after the chatbot requested one.
    date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)

    if state["stage"] in {"need_date", "need_doctor_date"} and date_match:
        try:
            requested_date = date.fromisoformat(date_match.group(1))
        except ValueError:
            return reply(
                "NEED_MORE_INFORMATION",
                "Please enter a valid date in YYYY-MM-DD format.",
            )

        if requested_date < date.today():
            return reply(
                "NEED_MORE_INFORMATION",
                "Please choose today or a future date in YYYY-MM-DD format.",
            )

        arguments = {
            "appointment_date": requested_date.isoformat(),
            "limit": 20,
        }

        if state["stage"] == "need_doctor_date" and state.get("doctor_id"):
            arguments["doctor_id"] = state["doctor_id"]

        tool = "find_available_slots"
        state["appointment_date"] = requested_date.isoformat()

    # Interpret numeric replies against the last displayed options.
    elif message.isdigit() and state.get("options"):
        index = int(message) - 1
        options = state["options"]

        if index < 0 or index >= len(options):
            return reply(
                "NEED_MORE_INFORMATION",
                f"Please choose a number between 1 and {len(options)}.",
            )

        selected = options[index]

        if state["stage"] == "select_doctor":
            state["doctor_id"] = selected["doctor_id"]
            state["stage"] = "need_doctor_date"
            state["options"] = []
            return reply(
                "NEED_MORE_INFORMATION",
                f"You selected {selected.get('name', 'this doctor')} "
                f"({selected.get('specialty', 'specialty not listed')}). "
                "Which date would you prefer? Use YYYY-MM-DD. "
                "This does not book an appointment.",
                data={"selected_doctor": selected},
            )

        if state["stage"] == "select_slot":
            state["selected_slot"] = selected
            state["stage"] = "slot_selected"
            return reply(
                "SUCCESS",
                "You selected this available slot:\n"
                f"{selected.get('date', state.get('appointment_date', ''))} "
                f"{selected.get('start_time', '')}-{selected.get('end_time', '')}, "
                f"{selected.get('doctor_name', 'Doctor not listed')}.\n"
                "This slot has NOT been reserved and no appointment has been booked.",
                tool="find_available_slots",
                data={"selected_slot": selected, "booked": False},
            )

        return reply(
            "NEED_MORE_INFORMATION",
            "Please tell me what you would like to do next.",
        )

    else:
        # A booking request without a date starts a date-selection step.
        if any(word in text for word in ("book appointment", "book an appointment")):
            state["stage"] = "need_date"
            state["options"] = []
            return reply(
                "NEED_MORE_INFORMATION",
                "What date would you prefer? Please use YYYY-MM-DD. "
                "I can check availability, but booking is not enabled in this read-only demo.",
            )

        tool, arguments = _select_tool(message)

        if tool == "NEED_DATE":
            state["stage"] = "need_date"
            state["options"] = []
            return reply(
                "NEED_MORE_INFORMATION",
                "Please provide the date in YYYY-MM-DD format. "
                "I will check availability, not book an appointment.",
            )

    try:
        data = await _call_mcp(tool, arguments)
    except Exception:
        log.exception(
            "MCP tool/database call failed for tool=%s user_id=%s",
            tool, user.id,
        )
        return reply(
            "DATA_UNAVAILABLE",
            "I cannot verify this information because the hospital data service "
            "is unavailable. Please try again later.",
            tool=tool,
        )

    if not isinstance(data, dict) or data.get("success") is False:
        return reply(
            "DATA_UNAVAILABLE",
            "The hospital data service did not return a valid result. Please try again.",
            tool=tool,
        )

    if tool == "find_available_slots":
        state["appointment_date"] = arguments["appointment_date"]
        state["options"] = data.get("slots", [])
        state["stage"] = "select_slot" if state["options"] else "idle"

    elif tool == "search_doctors":
        state["options"] = data.get("doctors", [])
        state["stage"] = "select_doctor" if state["options"] else "idle"

    else:
        state["options"] = []
        state["stage"] = "idle"

    fallback = _verified_fallback(tool, data)
    answer = await _llm_polish(message, tool, data, fallback)

    return reply("SUCCESS", answer, tool=tool, data=data)
