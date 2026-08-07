from datetime import date

import pytest

from tests.fhir_fakes import FakeFhirStore, FakeNppesClient
from voice_agent.scheduling.errors import (
    AppointmentNotFoundError,
    DoctorNotFoundError,
    SchedulingError,
    SlotUnavailableError,
)
from voice_agent.scheduling.models import AvailabilityQuery, DoctorSearchQuery
from voice_agent.scheduling.nppes_client import NppesApiError
from voice_agent.scheduling.sandbox_backend import (
    SandboxSchedulingBackend,
    _hash_npi_to_schedule_index,
)

_NPI = "1234567890"
_SAMPLE_NPPES_DOCTOR = {
    "number": _NPI,
    "enumeration_type": "NPI-1",
    "basic": {"first_name": "ALICE", "last_name": "NGUYEN"},
    "taxonomies": [{"desc": "Family Medicine", "primary": True}],
    "addresses": [{"city": "AUSTIN", "state": "TX", "address_purpose": "LOCATION"}],
}


def test_hash_npi_to_schedule_index_is_deterministic_and_in_range():
    assert _hash_npi_to_schedule_index("1234567890", 167) == int("1234567890") % 167
    assert 0 <= _hash_npi_to_schedule_index("1234567890", 167) < 167
    # same NPI, same count -> same index every time
    assert _hash_npi_to_schedule_index("1234567890", 50) == _hash_npi_to_schedule_index(
        "1234567890", 50
    )


@pytest.mark.asyncio
async def test_search_doctors_happy_path():
    nppes = FakeNppesClient(search_results=[_SAMPLE_NPPES_DOCTOR])
    backend = SandboxSchedulingBackend(nppes_client=nppes, fhir_client=FakeFhirStore())

    doctors = await backend.search_doctors(DoctorSearchQuery(specialty="Family Medicine", city="Austin"))

    assert len(doctors) == 1
    doctor = doctors[0]
    assert doctor.npi == _NPI
    assert doctor.name == "Dr. ALICE NGUYEN"
    assert doctor.specialty == "Family Medicine"
    assert doctor.city == "AUSTIN"
    assert doctor.state == "TX"
    assert doctor.languages == []  # NPPES has no language field
    assert nppes.search_calls[0]["taxonomy_description"] == "Family Medicine"
    assert nppes.search_calls[0]["enumeration_type"] == "NPI-1"


@pytest.mark.asyncio
async def test_search_doctors_benign_query_error_returns_empty():
    nppes = FakeNppesClient(raise_error=NppesApiError("No valid search criteria provided"))
    backend = SandboxSchedulingBackend(nppes_client=nppes, fhir_client=FakeFhirStore())

    doctors = await backend.search_doctors(DoctorSearchQuery())

    assert doctors == []


@pytest.mark.asyncio
async def test_search_doctors_real_error_becomes_scheduling_error():
    nppes = FakeNppesClient(raise_error=NppesApiError("connection reset"))
    backend = SandboxSchedulingBackend(nppes_client=nppes, fhir_client=FakeFhirStore())

    with pytest.raises(SchedulingError):
        await backend.search_doctors(DoctorSearchQuery(city="Austin"))


@pytest.mark.asyncio
async def test_check_availability_unknown_doctor_raises():
    backend = SandboxSchedulingBackend(
        nppes_client=FakeNppesClient(by_number={}), fhir_client=FakeFhirStore()
    )

    with pytest.raises(DoctorNotFoundError):
        await backend.check_availability(AvailabilityQuery(doctor_npi="9999999999"))


@pytest.mark.asyncio
async def test_check_availability_returns_free_slots_for_hashed_schedule():
    store = FakeFhirStore()
    schedule_count = 3
    schedules = [str(100 + i) for i in range(schedule_count)]
    for sid in schedules:
        store.seed("Schedule", sid, actor=[{"reference": "Practitioner/p1"}])

    target_schedule = schedules[_hash_npi_to_schedule_index(_NPI, schedule_count)]
    store.seed(
        "Slot",
        "s1",
        schedule={"reference": f"Schedule/{target_schedule}"},
        status="free",
        start="2026-08-10T09:00:00",
        end="2026-08-10T09:30:00",
    )
    store.seed(  # busy -- shouldn't show up
        "Slot",
        "s2",
        schedule={"reference": f"Schedule/{target_schedule}"},
        status="busy",
        start="2026-08-10T10:00:00",
        end="2026-08-10T10:30:00",
    )

    backend = SandboxSchedulingBackend(
        nppes_client=FakeNppesClient(by_number={_NPI: _SAMPLE_NPPES_DOCTOR}), fhir_client=store
    )
    slots = await backend.check_availability(AvailabilityQuery(doctor_npi=_NPI, on_or_after=date(2026, 8, 1)))

    assert len(slots) == 1
    assert slots[0].slot_id == f"sandbox:{_NPI}:s1"
    assert slots[0].doctor_npi == _NPI


@pytest.mark.asyncio
async def test_check_availability_probes_next_schedule_for_a_date_match():
    """The hash-selected schedule (from the curated "has >=1 free slot" list -- see
    _schedule_ids_with_free_slots) has a free slot, but it's before the requested date; the next
    schedule (wrapping) has one that matches. Confirms the primary date-filtered pass probes
    forward rather than stopping at the first schedule that merely has *some* free slot."""
    store = FakeFhirStore()
    schedule_ids = ["100", "101", "102"]
    for sid in schedule_ids:
        store.seed("Schedule", sid, actor=[{"reference": "Practitioner/p1"}])

    index = _hash_npi_to_schedule_index(_NPI, len(schedule_ids))
    hashed_schedule = schedule_ids[index]
    next_schedule = schedule_ids[(index + 1) % len(schedule_ids)]

    store.seed(  # free, but before the requested on_or_after -- shouldn't match
        "Slot",
        "old-slot",
        schedule={"reference": f"Schedule/{hashed_schedule}"},
        status="free",
        start="2026-01-01T09:00:00",
        end="2026-01-01T09:30:00",
    )
    store.seed(  # free and on/after the requested date -- should match
        "Slot",
        "new-slot",
        schedule={"reference": f"Schedule/{next_schedule}"},
        status="free",
        start="2026-08-10T09:00:00",
        end="2026-08-10T09:30:00",
    )

    backend = SandboxSchedulingBackend(
        nppes_client=FakeNppesClient(by_number={_NPI: _SAMPLE_NPPES_DOCTOR}), fhir_client=store
    )
    slots = await backend.check_availability(AvailabilityQuery(doctor_npi=_NPI, on_or_after=date(2026, 8, 1)))

    assert len(slots) == 1
    assert slots[0].slot_id == f"sandbox:{_NPI}:new-slot"


@pytest.mark.asyncio
async def test_check_availability_falls_back_to_soonest_slot_when_none_match_requested_date():
    """Mirrors a real bug caught live: this sandbox's synthetic Slot data is static and its free
    slots (confirmed live, across all schedules) top out around 2026-06-28 -- already behind
    real-world "now" by the time this runs. Rather than reporting no availability at all, the
    fallback pass returns the soonest free slot regardless of date."""
    store = FakeFhirStore()
    store.seed("Schedule", "100", actor=[{"reference": "Practitioner/p1"}])
    store.seed(
        "Slot",
        "old-slot",
        schedule={"reference": "Schedule/100"},
        status="free",
        start="2026-01-01T09:00:00",
        end="2026-01-01T09:30:00",
    )

    backend = SandboxSchedulingBackend(
        nppes_client=FakeNppesClient(by_number={_NPI: _SAMPLE_NPPES_DOCTOR}), fhir_client=store
    )
    slots = await backend.check_availability(AvailabilityQuery(doctor_npi=_NPI, on_or_after=date(2026, 8, 1)))

    assert len(slots) == 1
    assert slots[0].slot_id == f"sandbox:{_NPI}:old-slot"


@pytest.mark.asyncio
async def test_book_appointment_happy_path_flips_slot_busy():
    store = FakeFhirStore()
    store.seed("Schedule", "100", actor=[{"reference": "Practitioner/p1"}])
    store.seed(
        "Slot",
        "s1",
        schedule={"reference": "Schedule/100"},
        status="free",
        start="2026-08-10T09:00:00",
        end="2026-08-10T09:30:00",
    )
    backend = SandboxSchedulingBackend(nppes_client=FakeNppesClient(), fhir_client=store)

    appointment = await backend.book_appointment(f"sandbox:{_NPI}:s1", "Jane Doe")

    assert appointment.status == "booked"
    assert appointment.patient_name == "Jane Doe"
    assert appointment.doctor_npi == _NPI
    assert store.data[("Slot", "s1")]["status"] == "busy"
    fhir_appt_id = appointment.appointment_id.split(":")[-1]
    assert store.data[("Appointment", fhir_appt_id)]["status"] == "pending"


@pytest.mark.asyncio
async def test_book_appointment_already_busy_slot_raises():
    store = FakeFhirStore()
    store.seed("Schedule", "100", actor=[{"reference": "Practitioner/p1"}])
    store.seed(
        "Slot",
        "s1",
        schedule={"reference": "Schedule/100"},
        status="busy",
        start="2026-08-10T09:00:00",
        end="2026-08-10T09:30:00",
    )
    backend = SandboxSchedulingBackend(nppes_client=FakeNppesClient(), fhir_client=store)

    with pytest.raises(SlotUnavailableError):
        await backend.book_appointment(f"sandbox:{_NPI}:s1", "Jane Doe")


@pytest.mark.asyncio
async def test_book_appointment_unknown_slot_raises():
    backend = SandboxSchedulingBackend(nppes_client=FakeNppesClient(), fhir_client=FakeFhirStore())

    with pytest.raises(SlotUnavailableError):
        await backend.book_appointment(f"sandbox:{_NPI}:does-not-exist", "Jane Doe")


@pytest.mark.asyncio
async def test_book_appointment_malformed_slot_id_raises():
    backend = SandboxSchedulingBackend(nppes_client=FakeNppesClient(), fhir_client=FakeFhirStore())

    with pytest.raises(SlotUnavailableError):
        await backend.book_appointment("not-a-sandbox-slot-id", "Jane Doe")


@pytest.mark.asyncio
async def test_confirm_and_cancel_round_trip_frees_slot():
    store = FakeFhirStore()
    store.seed("Schedule", "100", actor=[{"reference": "Practitioner/p1"}])
    store.seed(
        "Slot",
        "s1",
        schedule={"reference": "Schedule/100"},
        status="free",
        start="2026-08-10T09:00:00",
        end="2026-08-10T09:30:00",
    )
    backend = SandboxSchedulingBackend(nppes_client=FakeNppesClient(), fhir_client=store)

    booked = await backend.book_appointment(f"sandbox:{_NPI}:s1", "Jane Doe")
    confirmed = await backend.confirm_appointment(booked.appointment_id)
    assert confirmed.status == "confirmed"
    assert confirmed.patient_name == "Jane Doe"

    cancelled = await backend.cancel_appointment(booked.appointment_id)
    assert cancelled.status == "cancelled"
    assert store.data[("Slot", "s1")]["status"] == "free"


@pytest.mark.asyncio
async def test_confirm_unknown_appointment_raises():
    backend = SandboxSchedulingBackend(nppes_client=FakeNppesClient(), fhir_client=FakeFhirStore())

    with pytest.raises(AppointmentNotFoundError):
        await backend.confirm_appointment(f"sandbox:{_NPI}:does-not-exist")


@pytest.mark.asyncio
async def test_cancel_unknown_appointment_raises():
    backend = SandboxSchedulingBackend(nppes_client=FakeNppesClient(), fhir_client=FakeFhirStore())

    with pytest.raises(AppointmentNotFoundError):
        await backend.cancel_appointment(f"sandbox:{_NPI}:does-not-exist")
