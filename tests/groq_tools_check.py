"""Static contract check for the AI tool layer.

The hospital exposes 18 controlled tools (see the platform spec):
  patient context   4 -> profile, summary, concerns, appointments
  more patient ctx  2 -> prescriptions, files
  routing           3 -> specialties, doctors, doctor availability
  booking           6 -> slots, hold, release, book, cancel, reschedule
  billing           2 -> invoice, payment status
  engagement        1 -> notification

Every tool must exist in BOTH registries, in the same order, otherwise the
  * /api/v1/ai/tools  endpoint and the deterministic engine disagree with the
  * Groq function-calling schema, and a tool the assistant is told about cannot
  * actually be invoked (the old 404 "Unknown tool" bug).
"""
from app.services.ai_tools import TOOLS
from app.services.groq_engine import TOOL_DEFINITIONS

EXPECTED = [
    "get_patient_profile", "get_patient_summary", "get_patient_concerns",
    "get_patient_appointments", "get_patient_prescriptions", "get_patient_files",
    "search_specialties", "search_doctors", "get_doctor_availability",
    "get_available_slots", "hold_slot", "release_slot", "book_appointment",
    "cancel_appointment", "reschedule_appointment", "get_invoice",
    "get_payment_status", "send_notification",
]

assert len(EXPECTED) == 18, f"Spec requires 18 tools, listed {len(EXPECTED)}"
assert list(TOOLS) == EXPECTED, f"Unexpected backend tool registry: {list(TOOLS)}"
assert [x["function"]["name"] for x in TOOL_DEFINITIONS] == EXPECTED, (
    "Groq TOOL_DEFINITIONS drifted from ai_tools.TOOLS: "
    f"{[x['function']['name'] for x in TOOL_DEFINITIONS]}"
)
assert all(x["type"] == "function" for x in TOOL_DEFINITIONS)
print(f"AI tool contract OK: {len(EXPECTED)} tools in both registries")
