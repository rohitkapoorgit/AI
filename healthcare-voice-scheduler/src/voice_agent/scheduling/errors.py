"""Errors raised by any SchedulingBackend implementation (mock or real)."""


class SchedulingError(Exception):
    """Base class for scheduling backend errors."""


class DoctorNotFoundError(SchedulingError):
    def __init__(self, npi: str):
        super().__init__(f"No doctor with NPI {npi!r}")
        self.npi = npi


class SlotUnavailableError(SchedulingError):
    def __init__(self, slot_id: str):
        super().__init__(f"Slot {slot_id!r} is not open")
        self.slot_id = slot_id


class AppointmentNotFoundError(SchedulingError):
    def __init__(self, appointment_id: str):
        super().__init__(f"No appointment with id {appointment_id!r}")
        self.appointment_id = appointment_id
