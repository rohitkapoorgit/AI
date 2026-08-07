"""Interface boundary between the scheduling tools and the data source.

Phase 1 implements this with an in-memory MockSchedulingBackend so the agent
and voice pipeline can be built and tested with zero external accounts. Phase
2.3's SandboxSchedulingBackend (NPPES provider lookup + SMART on FHIR sandbox)
drops in behind this same interface without touching the agent or the voice
pipeline; it did require one fix to scheduling/service.py (search_doctors was
missing the try/except SchedulingError every other method already had — see
docs/PHASE2_3_SCHEDULING_BACKEND.md), which MockSchedulingBackend never raises
from and so never exercised.
"""

from abc import ABC, abstractmethod

from voice_agent.scheduling.models import (
    Appointment,
    AvailabilityQuery,
    Doctor,
    DoctorSearchQuery,
    Slot,
)


class SchedulingBackend(ABC):
    @abstractmethod
    async def search_doctors(self, query: DoctorSearchQuery) -> list[Doctor]:
        """Find doctors matching specialty/location/name."""
        raise NotImplementedError

    @abstractmethod
    async def check_availability(self, query: AvailabilityQuery) -> list[Slot]:
        """List open slots for a given doctor."""
        raise NotImplementedError

    @abstractmethod
    async def book_appointment(self, slot_id: str, patient_name: str) -> Appointment:
        """Reserve a slot for a patient. Raises if the slot is no longer open."""
        raise NotImplementedError

    @abstractmethod
    async def cancel_appointment(self, appointment_id: str) -> Appointment:
        """Cancel a previously booked appointment."""
        raise NotImplementedError

    @abstractmethod
    async def confirm_appointment(self, appointment_id: str) -> Appointment:
        """Mark an appointment as confirmed (e.g. after a callback/reminder)."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any held resources (e.g. an HTTP session). No-op by default so every
        composition root can unconditionally `await backend.aclose()` on shutdown regardless
        of which implementation is in use."""
