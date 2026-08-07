"""Plain data types shared by every SchedulingBackend implementation."""

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class Doctor:
    npi: str  # National Provider Identifier
    name: str
    specialty: str
    city: str
    state: str
    languages: list[str]


@dataclass
class Slot:
    slot_id: str
    doctor_npi: str
    start: datetime
    end: datetime


@dataclass
class Appointment:
    appointment_id: str
    doctor_npi: str
    patient_name: str
    slot: Slot
    status: str  # "booked" | "confirmed" | "cancelled"


@dataclass
class DoctorSearchQuery:
    specialty: str | None = None
    city: str | None = None
    state: str | None = None
    name: str | None = None


@dataclass
class AvailabilityQuery:
    doctor_npi: str
    on_or_after: date | None = None
