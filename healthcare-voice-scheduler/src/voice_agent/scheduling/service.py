"""Tool functions the dialogue agent calls, plus their Claude tool-use schemas.

Thin JSON-in/JSON-out translation layer over a SchedulingBackend. Method
names match tool names 1:1 so an AgentOrchestrator can dispatch a Claude tool
call with `getattr(service, tool_name)(**tool_input)`. Signatures stay stable
even once the backend swaps from MockSchedulingBackend to real NPPES/FHIR.
"""

from datetime import date

from voice_agent.scheduling.backend import SchedulingBackend
from voice_agent.scheduling.errors import SchedulingError
from voice_agent.scheduling.models import (
    Appointment,
    AvailabilityQuery,
    Doctor,
    DoctorSearchQuery,
    Slot,
)


class SchedulingService:
    def __init__(self, backend: SchedulingBackend):
        self._backend = backend

    async def search_doctors(
        self,
        specialty: str | None = None,
        city: str | None = None,
        state: str | None = None,
        name: str | None = None,
    ) -> dict:
        try:
            doctors = await self._backend.search_doctors(
                DoctorSearchQuery(specialty=specialty, city=city, state=state, name=name)
            )
        except SchedulingError as exc:
            return {"error": str(exc)}
        return {"doctors": [_doctor_to_dict(d) for d in doctors]}

    async def check_availability(self, doctor_npi: str, on_or_after: str | None = None) -> dict:
        try:
            parsed_date = date.fromisoformat(on_or_after) if on_or_after else None
            slots = await self._backend.check_availability(
                AvailabilityQuery(doctor_npi=doctor_npi, on_or_after=parsed_date)
            )
        except SchedulingError as exc:
            return {"error": str(exc)}
        return {"slots": [_slot_to_dict(s) for s in slots]}

    async def book_appointment(self, slot_id: str, patient_name: str) -> dict:
        try:
            appointment = await self._backend.book_appointment(slot_id, patient_name)
        except SchedulingError as exc:
            return {"error": str(exc)}
        return _appointment_to_dict(appointment)

    async def cancel_appointment(self, appointment_id: str) -> dict:
        try:
            appointment = await self._backend.cancel_appointment(appointment_id)
        except SchedulingError as exc:
            return {"error": str(exc)}
        return _appointment_to_dict(appointment)

    async def confirm_appointment(self, appointment_id: str) -> dict:
        try:
            appointment = await self._backend.confirm_appointment(appointment_id)
        except SchedulingError as exc:
            return {"error": str(exc)}
        return _appointment_to_dict(appointment)


def _doctor_to_dict(doctor: Doctor) -> dict:
    return {
        "npi": doctor.npi,
        "name": doctor.name,
        "specialty": doctor.specialty,
        "city": doctor.city,
        "state": doctor.state,
        "languages": doctor.languages,
    }


def _slot_to_dict(slot: Slot) -> dict:
    return {
        "slot_id": slot.slot_id,
        "doctor_npi": slot.doctor_npi,
        "start": slot.start.isoformat(),
        "end": slot.end.isoformat(),
    }


def _appointment_to_dict(appointment: Appointment) -> dict:
    return {
        "appointment_id": appointment.appointment_id,
        "doctor_npi": appointment.doctor_npi,
        "patient_name": appointment.patient_name,
        "slot": _slot_to_dict(appointment.slot),
        "status": appointment.status,
    }


# Anthropic tool-use schemas — one per SchedulingService method, name-matched
# so a ClaudeToolAgent can dispatch `getattr(service, tool_call.name)`.
TOOL_SCHEMAS: list[dict] = [
    {
        "name": "search_doctors",
        "description": "Find doctors by specialty, city, state, and/or name. All filters are optional and combine with AND.",
        "input_schema": {
            "type": "object",
            "properties": {
                "specialty": {"type": "string", "description": "e.g. 'Family Medicine', 'Pediatrics'"},
                "city": {"type": "string"},
                "state": {"type": "string", "description": "Two-letter state code, e.g. 'TX'"},
                "name": {"type": "string", "description": "Doctor's name or partial name"},
            },
        },
    },
    {
        "name": "check_availability",
        "description": "List open appointment slots for a given doctor, optionally starting from a given date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doctor_npi": {"type": "string", "description": "The doctor's NPI, from search_doctors"},
                "on_or_after": {"type": "string", "description": "ISO date (YYYY-MM-DD); defaults to today"},
            },
            "required": ["doctor_npi"],
        },
    },
    {
        "name": "book_appointment",
        "description": "Book a specific open slot for a patient. Returns the created appointment, or an error if the slot is no longer open.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slot_id": {"type": "string", "description": "A slot_id from check_availability"},
                "patient_name": {"type": "string"},
            },
            "required": ["slot_id", "patient_name"],
        },
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel a previously booked appointment by its appointment_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
            },
            "required": ["appointment_id"],
        },
    },
    {
        "name": "confirm_appointment",
        "description": "Mark a booked appointment as confirmed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
            },
            "required": ["appointment_id"],
        },
    },
]
