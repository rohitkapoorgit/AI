from datetime import datetime

import pytest

from voice_agent.scheduling.backend import SchedulingBackend
from voice_agent.scheduling.errors import SchedulingError
from voice_agent.scheduling.mock_backend import MockSchedulingBackend
from voice_agent.scheduling.models import (
    Appointment,
    AvailabilityQuery,
    Doctor,
    DoctorSearchQuery,
    Slot,
)
from voice_agent.scheduling.service import SchedulingService

FIXED_NOW = datetime(2026, 8, 3, 8, 0)  # a Monday  # noqa: DTZ001 (naive, matches domain model)


@pytest.fixture
def service() -> SchedulingService:
    backend = MockSchedulingBackend(clock=lambda: FIXED_NOW)
    return SchedulingService(backend)


@pytest.mark.asyncio
async def test_search_doctors_by_specialty(service: SchedulingService):
    result = await service.search_doctors(specialty="family medicine")
    names = {d["name"] for d in result["doctors"]}
    assert names == {"Dr. Alice Nguyen", "Dr. Sofia Alvarez"}


@pytest.mark.asyncio
async def test_search_doctors_no_match(service: SchedulingService):
    result = await service.search_doctors(specialty="neurosurgery")
    assert result["doctors"] == []


@pytest.mark.asyncio
async def test_check_availability_unknown_doctor(service: SchedulingService):
    result = await service.check_availability(doctor_npi="does-not-exist")
    assert "error" in result


@pytest.mark.asyncio
async def test_book_then_slot_disappears_from_availability(service: SchedulingService):
    availability = await service.check_availability(doctor_npi="1000000001")
    slot_id = availability["slots"][0]["slot_id"]

    booked = await service.book_appointment(slot_id=slot_id, patient_name="Jane Doe")
    assert booked["status"] == "booked"
    assert booked["patient_name"] == "Jane Doe"

    availability_after = await service.check_availability(doctor_npi="1000000001")
    remaining_ids = {s["slot_id"] for s in availability_after["slots"]}
    assert slot_id not in remaining_ids


@pytest.mark.asyncio
async def test_double_booking_same_slot_errors(service: SchedulingService):
    availability = await service.check_availability(doctor_npi="1000000002")
    slot_id = availability["slots"][0]["slot_id"]

    await service.book_appointment(slot_id=slot_id, patient_name="Jane Doe")
    result = await service.book_appointment(slot_id=slot_id, patient_name="John Smith")
    assert "error" in result


@pytest.mark.asyncio
async def test_confirm_and_cancel_appointment(service: SchedulingService):
    availability = await service.check_availability(doctor_npi="1000000003")
    slot_id = availability["slots"][0]["slot_id"]
    booked = await service.book_appointment(slot_id=slot_id, patient_name="Jane Doe")

    confirmed = await service.confirm_appointment(appointment_id=booked["appointment_id"])
    assert confirmed["status"] == "confirmed"

    cancelled = await service.cancel_appointment(appointment_id=booked["appointment_id"])
    assert cancelled["status"] == "cancelled"

    # slot should be bookable again after cancellation
    availability_after = await service.check_availability(doctor_npi="1000000003")
    remaining_ids = {s["slot_id"] for s in availability_after["slots"]}
    assert slot_id in remaining_ids


@pytest.mark.asyncio
async def test_confirm_unknown_appointment_errors(service: SchedulingService):
    result = await service.confirm_appointment(appointment_id="nope")
    assert "error" in result


class _RaisingBackend(SchedulingBackend):
    """Minimal stub proving search_doctors degrades to {"error": ...} instead of propagating --
    MockSchedulingBackend.search_doctors never raises, so it never exercised this path (the gap
    that shipped with Phase 2.3's real backend, see docs/PHASE2_3_SCHEDULING_BACKEND.md)."""

    async def search_doctors(self, query: DoctorSearchQuery) -> list[Doctor]:
        raise SchedulingError("boom")

    async def check_availability(self, query: AvailabilityQuery) -> list[Slot]:
        raise NotImplementedError

    async def book_appointment(self, slot_id: str, patient_name: str) -> Appointment:
        raise NotImplementedError

    async def cancel_appointment(self, appointment_id: str) -> Appointment:
        raise NotImplementedError

    async def confirm_appointment(self, appointment_id: str) -> Appointment:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_search_doctors_backend_error_degrades_gracefully():
    service = SchedulingService(_RaisingBackend())
    result = await service.search_doctors(specialty="anything")
    assert result == {"error": "boom"}
