from __future__ import annotations

from fastmcp import FastMCP

from .database import get_doctor, get_patient

mcp = FastMCP("Vijay Vargiya Hospital Medical MCP Server")


@mcp.tool()
def doctor_details(name: str):
    """Look up a doctor by exact name in the hospital MySQL database."""
    doctor = get_doctor(name)
    if not doctor:
        return {"success": False, "message": f"{name} not found"}
    return {
        "success": True,
        "doctor": {
            "id": doctor[0],
            "name": doctor[1],
            "specialization": doctor[2],
            "timing": f"{doctor[3]} minute slots",
        },
    }


@mcp.tool()
def patient_details(patient_id: int):
    """Look up a patient by ID in the hospital MySQL database."""
    patient = get_patient(patient_id)
    if not patient:
        return {"success": False, "message": f"Patient {patient_id} not found"}
    return {
        "success": True,
        "patient": {
            "id": patient[0],
            "name": patient[1],
            "age": patient[2],
            "city": patient[3],
            "blood_group": patient[4],
        },
    }


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8001)
