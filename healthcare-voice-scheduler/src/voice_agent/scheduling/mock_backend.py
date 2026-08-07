"""In-memory SchedulingBackend for local dev and tests.

Stands in for the NPPES provider registry + SMART on FHIR sandbox until
those real integrations are wired up. Canned data, deterministic behavior
(given a fixed `clock`), no network calls.
"""

import itertools
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from voice_agent.scheduling.backend import SchedulingBackend
from voice_agent.scheduling.errors import (
    AppointmentNotFoundError,
    DoctorNotFoundError,
    SlotUnavailableError,
)
from voice_agent.scheduling.models import (
    Appointment,
    AvailabilityQuery,
    Doctor,
    DoctorSearchQuery,
    Slot,
)

_DOCTORS = [
    Doctor(
        npi="1000000001",
        name="Dr. Alice Nguyen",
        specialty="Family Medicine",
        city="Austin",
        state="TX",
        languages=["English", "Vietnamese"],
    ),
    Doctor(
        npi="1000000002",
        name="Dr. Marcus Webb",
        specialty="Pediatrics",
        city="Austin",
        state="TX",
        languages=["English"],
    ),
    Doctor(
        npi="1000000003",
        name="Dr. Priya Raman",
        specialty="Dermatology",
        city="Round Rock",
        state="TX",
        languages=["English", "Hindi"],
    ),
    Doctor(
        npi="1000000004",
        name="Dr. Sofia Alvarez",
        specialty="Family Medicine",
        city="Austin",
        state="TX",
        languages=["English", "Spanish"],
    ),
]

_SLOT_HOURS = (9, 11, 14, 15, 16)
_DAYS_AHEAD = 5


class MockSchedulingBackend(SchedulingBackend):
    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        self._clock = clock or datetime.now
        self._doctors: dict[str, Doctor] = {d.npi: d for d in _DOCTORS}
        self._booked_slot_ids: set[str] = set()
        self._appointments: dict[str, Appointment] = {}
        self._appointment_ids = itertools.count(1)

    async def search_doctors(self, query: DoctorSearchQuery) -> list[Doctor]:
        results = list(self._doctors.values())
        if query.specialty:
            needle = query.specialty.lower()
            results = [d for d in results if needle in d.specialty.lower()]
        if query.city:
            needle = query.city.lower()
            results = [d for d in results if needle == d.city.lower()]
        if query.state:
            needle = query.state.lower()
            results = [d for d in results if needle == d.state.lower()]
        if query.name:
            needle = query.name.lower()
            results = [d for d in results if needle in d.name.lower()]
        return results

    async def check_availability(self, query: AvailabilityQuery) -> list[Slot]:
        if query.doctor_npi not in self._doctors:
            raise DoctorNotFoundError(query.doctor_npi)

        start_date = query.on_or_after or self._clock().date()
        slots = []
        for day_offset in range(_DAYS_AHEAD):
            day = start_date + timedelta(days=day_offset)
            if day.weekday() >= 5:  # skip weekends
                continue
            for hour in _SLOT_HOURS:
                slot_id = self._slot_id(query.doctor_npi, day, hour)
                if slot_id in self._booked_slot_ids:
                    continue
                start = datetime.combine(day, time(hour=hour))
                slots.append(
                    Slot(
                        slot_id=slot_id,
                        doctor_npi=query.doctor_npi,
                        start=start,
                        end=start + timedelta(minutes=30),
                    )
                )
        return slots

    async def book_appointment(self, slot_id: str, patient_name: str) -> Appointment:
        doctor_npi, day, hour = self._parse_slot_id(slot_id)
        if doctor_npi not in self._doctors:
            raise DoctorNotFoundError(doctor_npi)
        if slot_id in self._booked_slot_ids:
            raise SlotUnavailableError(slot_id)

        self._booked_slot_ids.add(slot_id)
        start = datetime.combine(day, time(hour=hour))
        appointment = Appointment(
            appointment_id=f"appt-{next(self._appointment_ids)}",
            doctor_npi=doctor_npi,
            patient_name=patient_name,
            slot=Slot(
                slot_id=slot_id,
                doctor_npi=doctor_npi,
                start=start,
                end=start + timedelta(minutes=30),
            ),
            status="booked",
        )
        self._appointments[appointment.appointment_id] = appointment
        return appointment

    async def cancel_appointment(self, appointment_id: str) -> Appointment:
        appointment = self._require_appointment(appointment_id)
        appointment.status = "cancelled"
        self._booked_slot_ids.discard(appointment.slot.slot_id)
        return appointment

    async def confirm_appointment(self, appointment_id: str) -> Appointment:
        appointment = self._require_appointment(appointment_id)
        appointment.status = "confirmed"
        return appointment

    def _require_appointment(self, appointment_id: str) -> Appointment:
        appointment = self._appointments.get(appointment_id)
        if appointment is None:
            raise AppointmentNotFoundError(appointment_id)
        return appointment

    @staticmethod
    def _slot_id(doctor_npi: str, day: date, hour: int) -> str:
        return f"{doctor_npi}:{day.isoformat()}:{hour:02d}"

    @staticmethod
    def _parse_slot_id(slot_id: str) -> tuple[str, date, int]:
        doctor_npi, day_str, hour_str = slot_id.split(":")
        return doctor_npi, date.fromisoformat(day_str), int(hour_str)
