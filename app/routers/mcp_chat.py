
"""Separate MCP chatbot API. Existing /api/v1/ai/chat is intentionally untouched.

Security policy:
- Authenticated users with ai:chat permission can use read-only public hospital tools.
- No patient records or write operations are exposed here.
- If MCP/database data cannot be verified, return DATA_UNAVAILABLE.
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
        "doctor_id": None,
        "options": [],
        "selected_slot": None,
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
    """Normalize MCP SDK results without treating errors as empty data."""
    if getattr(result, "is_error", False):
        raise RuntimeError("MCP tool reported an error")

    data = getattr(result, "data", None)
    if data is not None:
        return data

    structured = (
        getattr(result, "structuredContent", None)
        or getattr(result, "structured_content", None)
    )
    if structured is not None:
        return structured

    content = getattr(result, "content", None)
    if content:
        for block in content:
            block_text = getattr(block, "text", None)
            if block_text:
                try:
                    return json.loads(block_text)
                except json.JSONDecodeError:
                    return {"text": block_text}

    raise RuntimeError("MCP result contained no readable data")


async def _call_mcp(tool_name: str, arguments: dict[str, Any]) -> Any:
    # Import only when the endpoint is called so the main app can start
    # without the optional MCP package installed.
    from fastmcp import Client

    endpoint = getattr(
        settings,
        "mcp_hospital_url",
        "http://127.0.0.1:8010/mcp",
    )

    async with Client(endpoint) as client:
        result = await client.call_tool(tool_name, arguments)

    return _extract_tool_data(result)


def _select_tool(message: str) -> tuple[str, dict[str, Any]]:
    """Choose a read-only tool deterministically; no LLM decides access."""
    text = message.strip().lower()

    # Extract an explicit doctor ID, if provided.
    doctor_match = re.search(
        r"\bdoctor\s*(?:id|number)\s*[:#]?\s*(\d+)\b"
        r"|\bdoctor\s*#\s*(\d+)\b",
        text,
    )
    doctor_id = None

    if doctor_match:
        doctor_id = int(
            next(group for group in doctor_match.groups() if group is not None)
        )

    # Doctor working hours / weekly schedule lookup.
    working_hours_phrases = (
        "working hours",
        "work hours",
        "doctor schedule",
        "doctor's schedule",
        "doctors schedule",
        "duty hours",
        "clinic timings",
        "doctor timings",
        "working time",
        "schedule of",
        "schedule for",
    )

    if any(phrase in text for phrase in working_hours_phrases):
        if doctor_id is not None:
            return "get_doctor_working_hours", {"doctor_id": doctor_id}

        # Extract a name after "of" or "for", optionally preceded by "Dr".
        name_match = re.search(
            r"\b(?:of|for)\s+(?:dr\.?\s*)?"
            r"([a-z][a-z .'-]{1,60}?)"
            r"(?=\s+(?:on|for date|doctor id|doctor number)\b|[?.!,]|$)",
            text,
        )

        if name_match:
            doctor_name = name_match.group(1).strip(" .,'-")
            if doctor_name and doctor_name not in {
                "the doctor",
                "a doctor",
                "doctor",
            }:
                return "get_doctor_working_hours", {
                    "doctor_name": doctor_name,
                }

        return "NEED_DOCTOR_NAME", {}

    # Availability lookup. This checks slots only; it does not book.
    availability_words = (
        "slot",
        "available",
        "availability",
        "appointment",
        "book",
    )

    if any(word in text for word in availability_words):
        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)

        if not date_match:
            arguments = {}
            if doctor_id is not None:
                arguments["doctor_id"] = doctor_id
            return "NEED_DATE", arguments

        try:
            requested_date = date.fromisoformat(date_match.group(1))
        except ValueError:
            return "NEED_DATE", {}

        if requested_date < date.today():
            return "INVALID_DATE", {}

        arguments = {
            "appointment_date": requested_date.isoformat(),
            "limit": 20,
        }

        if doctor_id is not None:
            arguments["doctor_id"] = doctor_id

        return "find_available_slots", arguments

    # Specialty directory.
    if any(
        word in text
        for word in (
            "specialty",
            "specialties",
            "speciality",
            "specialities",
            "department",
            "departments",
        )
    ):
        return "list_specialties", {}

    # Public aggregate statistics.
    if any(
        phrase in text
        for phrase in (
            "stats",
            "statistics",
            "how many doctors",
            "hospital summary",
        )
    ):
        return "get_public_hospital_stats", {}

    # Default: read-only doctor directory search.
    specialty = ""
    for term in (
        "dermatology",
        "cardiology",
        "neurology",
        "orthopedics",
        "orthopaedics",
        "pediatrics",
        "paediatrics",
    ):
        if term in text:
            specialty = term
            break

    return "search_doctors", {
        "specialty": specialty,
        "limit": 10,
    }


def _verified_fallback(tool: str, data: Any) -> str:
    """Build a deterministic answer from successfully retrieved tool data."""
    if not isinstance(data, dict):
        return "The hospital data service returned an unexpected result."

    if tool == "list_specialties":
        rows = data.get("specialties", [])
        if not rows:
            return (
                "The database query succeeded, but no active "
                "specialties were found."
            )

        return "Active specialties returned by the hospital database:\n" + "\n".join(
            f"- {item.get('name', 'Unnamed specialty')}: "
            f"consultation fee "
            f"{item.get('consultation_fee', 'not listed')}"
            for item in rows
        )

    if tool == "search_doctors":
        rows = data.get("doctors", [])
        if not rows:
            return "No matching active doctors were found in the hospital database."

        return "Doctors found in the hospital database:\n" + "\n".join(
            f"- {item.get('name', 'Name not listed')} | "
            f"{item.get('specialty', 'Specialty not listed')} | "
            f"fee: {item.get('consultation_fee', 'not listed')}"
            for item in rows
        )

    if tool == "find_available_slots":
        rows = data.get("slots", [])
        if not rows:
            return (
                f"No available slots were returned for "
                f"{data.get('date', 'the requested date')}. "
                "This is not a booking."
            )

        return (
            "Slots currently returned by the database (not reserved):\n"
            + "\n".join(
                f"- {item.get('date', data.get('date', ''))} "
                f"{item.get('start_time', '')}-{item.get('end_time', '')}: "
                f"{item.get('doctor_name', 'Doctor not listed')} "
                f"({item.get('specialty', 'specialty not listed')})"
                for item in rows
            )
        )

    if tool == "get_doctor_working_hours":
        rows = data.get("schedules", [])
        if not rows:
            return "No active working hours were found for this doctor."

        lines = []
        for item in rows:
            start = item.get("start_time", "Not listed")
            end = item.get("end_time", "Not listed")
            break_start = item.get("break_start")
            break_end = item.get("break_end")

            line = (
                f"- {item.get('doctor_name', 'Doctor')}: "
                f"{item.get('day', 'Day not listed')}, "
                f"{start}-{end}"
            )

            if break_start and break_end:
                line += f", break {break_start}-{break_end}"

            line += (
                f", slot duration "
                f"{item.get('slot_duration_minutes', 'not listed')} minutes"
                f", capacity "
                f"{item.get('capacity_per_slot', 'not listed')}"
            )
            lines.append(line)

        return "Verified doctor working hours:\n" + "\n".join(lines)

    if tool == "get_public_hospital_stats":
        return "Verified aggregate hospital statistics:\n" + "\n".join(
            f"- {key.replace('_', ' ')}: {value}"
            for key, value in data.items()
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


async def _llm_polish(
    message: str,
    tool: str,
    data: Any,
    fallback: str,
) -> str:
    """Optional Groq wording layer; receives only a successful read-only result."""
    if not settings.groq_api_key:
        return fallback

    try:
        from groq import Groq

        payload = json.dumps(data, ensure_ascii=False, default=str)

        def call() -> str:
            client = Groq(
                api_key=settings.groq_api_key,
                timeout=settings.groq_timeout_seconds,
            )
            response = client.chat.completions.create(
                model=settings.groq_model,
                temperature=0,
                max_tokens=500,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are the wording layer for a hospital MCP chatbot. "
                            "Use ONLY the supplied tool result. Never infer missing "
                            "facts, invent doctors, prices, appointment slots, medical "
                            "advice, or database status. If a field is absent, say "
                            "it was not provided. Do not claim an appointment was "
                            "booked. Answer briefly in the user's language."
                            "Do not use Markdown formatting such as **bold**, "
                            "`code`, or Markdown headings. Return plain text only. "
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Question: {message}\n"
                            f"Tool: {tool}\n"
                            f"Verified tool result JSON: {payload}"
                        ),
                    },
                ],
            )
            answer = response.choices[0].message.content
            return answer.strip() if answer else fallback

        answer = await asyncio.to_thread(call)
        return answer or fallback

    except Exception:
        log.exception(
            "MCP chatbot LLM wording failed; using verified deterministic response"
        )
        return fallback


@router.post("/chat", response_model=MCPChatResponse)
async def mcp_chat(
    payload: MCPChatRequest,
    user: CurrentUser = Depends(require_permission("ai:chat")),
):
    if user.role not in {
        "PATIENT",
        "DOCTOR",
        "RECEPTIONIST",
        "ACCOUNTANT",
        "ADMIN",
        "SUPER_ADMIN",
    }:
        raise HTTPException(
            status_code=403,
            detail="Role is not allowed to use the MCP chatbot",
        )

    conversation_id, state = _get_conversation(
        payload.conversation_id,
        str(user.id),
    )

    message = payload.message.strip()
    text = message.lower()

    def reply(
        status: str,
        answer: str,
        tool: str | None = None,
        data: Any = None,
    ) -> MCPChatResponse:
        return MCPChatResponse(
            status=status,
            answer=answer,
            tool=tool,
            data=data,
            conversation_id=conversation_id,
        )

    # Handle the date requested in a previous turn.
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

        arguments: dict[str, Any] = {
            "appointment_date": requested_date.isoformat(),
            "limit": 20,
        }

        if state.get("doctor_id") is not None:
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
                f"{selected.get('start_time', '')}-"
                f"{selected.get('end_time', '')}, "
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
        # Handle an explicit booking request without a date.
        if any(
            phrase in text
            for phrase in ("book appointment", "book an appointment")
        ) and not date_match:
            state["stage"] = "need_date"
            state["options"] = []
            state["doctor_id"] = None

            return reply(
                "NEED_MORE_INFORMATION",
                "What date would you prefer? Please use YYYY-MM-DD. "
                "I can check availability, but booking is not enabled "
                "in this read-only demo.",
            )

        tool, arguments = _select_tool(message)

        if tool == "NEED_DOCTOR_NAME":
            state["stage"] = "idle"
            state["options"] = []
            return reply(
                "NEED_MORE_INFORMATION",
                "Please provide the doctor's name or ID. "
                "Example: Show working hours of doctor ID 1",
            )

        if tool == "NEED_DATE":
            state["stage"] = "need_date"
            state["options"] = []
            state["doctor_id"] = arguments.get("doctor_id")

            return reply(
                "NEED_MORE_INFORMATION",
                "Please provide the date in YYYY-MM-DD format. "
                "I will check availability, not book an appointment.",
            )

        if tool == "INVALID_DATE":
            return reply(
                "NEED_MORE_INFORMATION",
                "Please choose today or a future date in YYYY-MM-DD format.",
            )

    try:
        data = await _call_mcp(tool, arguments)
    except Exception:
        log.exception(
            "MCP tool/database call failed for tool=%s user_id=%s",
            tool,
            user.id,
        )
        return reply(
            "DATA_UNAVAILABLE",
            "I cannot verify this information because the hospital data "
            "service is unavailable. Please try again later.",
            tool=tool,
        )

    if not isinstance(data, dict) or data.get("success") is False:
        return reply(
            "DATA_UNAVAILABLE",
            "The hospital data service did not return a valid result. "
            "Please try again.",
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
