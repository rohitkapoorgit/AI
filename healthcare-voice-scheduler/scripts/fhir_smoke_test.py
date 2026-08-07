#!/usr/bin/env python3
"""One-time, manual smoke test against the REAL SMART Health IT FHIR sandbox and REAL NPPES.

This makes real writes (POST/PUT) to a shared public sandbox with no documented reset cadence --
see docs/PHASE2_3_SCHEDULING_BACKEND.md. Not part of pytest/CI; run this by hand once after
touching sandbox_backend.py or nppes_client.py. Exercises SandboxSchedulingBackend itself end to
end (not a parallel reimplementation), so this is the first real proof the write path actually
works -- search/read behavior was live-verified during research, POST/PUT writes were not.

Post-write checks fetch the resource directly by id (`AsyncFHIRClient.reference(...).to_resource()`)
rather than re-running check_availability's *search* -- confirmed live that this sandbox's search
index lags a direct write by a few seconds (a direct fetch-by-id reflects a PUT immediately; a
Slot?status=free search can still list a just-booked slot for a few seconds after). This doesn't
threaten data integrity -- book_appointment's own double-booking guard already uses a direct fetch,
not search, so it can't be fooled by the lag -- but it does mean re-checking via search
immediately after a write is an unreliable thing to assert on, so this script doesn't.

Usage: python scripts/fhir_smoke_test.py
"""

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fhirpy import AsyncFHIRClient

from voice_agent.scheduling.models import AvailabilityQuery, DoctorSearchQuery
from voice_agent.scheduling.sandbox_backend import SandboxSchedulingBackend

_PASS = "PASS"
_FAIL = "FAIL"


def _report(step: str, ok: bool, detail: str = "") -> None:
    line = f"[{_PASS if ok else _FAIL}] {step}"
    if detail:
        line += f" -- {detail}"
    print(line)
    if not ok:
        raise SystemExit(1)


async def _slot_status(fhir: AsyncFHIRClient, fhir_slot_id: str) -> str:
    slot = await fhir.reference("Slot", fhir_slot_id).to_resource()
    return slot.get("status")


async def main() -> None:
    print(
        "Real NPPES + real FHIR SANDBOX smoke test -- this makes real writes to shared public\n"
        "infrastructure. Ctrl-C now if that's not what you want.\n"
    )

    today = date.today()  # noqa: DTZ011 (naive, matches domain model)
    backend = SandboxSchedulingBackend()
    verify_fhir = AsyncFHIRClient(backend._fhir.url)
    try:
        doctors = await backend.search_doctors(DoctorSearchQuery(specialty="Family Medicine", state="TX"))
        _report("NPPES search_doctors", len(doctors) > 0, f"{len(doctors)} doctor(s) found")
        doctor = doctors[0]
        print(f"       using {doctor.name} ({doctor.npi}), {doctor.specialty}, {doctor.city}, {doctor.state}")

        slots = await backend.check_availability(AvailabilityQuery(doctor_npi=doctor.npi, on_or_after=today))
        _report("FHIR check_availability", len(slots) > 0, f"{len(slots)} free slot(s) found")
        slot = slots[0]
        fhir_slot_id = slot.slot_id.split(":")[-1]
        print(f"       slot {slot.slot_id} at {slot.start}")

        appointment = await backend.book_appointment(slot.slot_id, "Smoke Test Patient")
        _report(
            "FHIR book_appointment (POST Appointment + PUT Slot -> busy)",
            appointment.status == "booked",
        )

        status_after_booking = await _slot_status(verify_fhir, fhir_slot_id)
        _report(
            "Slot status is 'busy' on direct fetch after booking",
            status_after_booking == "busy",
            f"status={status_after_booking!r}",
        )

        confirmed = await backend.confirm_appointment(appointment.appointment_id)
        _report(
            "FHIR confirm_appointment (PUT Appointment pending -> booked)",
            confirmed.status == "confirmed",
        )

        cancelled = await backend.cancel_appointment(appointment.appointment_id)
        _report(
            "FHIR cancel_appointment (PUT Appointment -> cancelled + PUT Slot -> free)",
            cancelled.status == "cancelled",
        )

        status_after_cancel = await _slot_status(verify_fhir, fhir_slot_id)
        _report(
            "Slot status is 'free' on direct fetch after cancellation",
            status_after_cancel == "free",
            f"status={status_after_cancel!r}",
        )

        print("\nAll steps passed -- real NPPES search and the real FHIR sandbox write path both work.")
        print(
            "Note: check_availability (search-based) may still list/omit this slot inconsistently\n"
            "for a few seconds after these writes -- that's this sandbox's search index catching up,\n"
            "confirmed live during implementation, not a bug in this codebase."
        )
    finally:
        await backend.aclose()


if __name__ == "__main__":
    asyncio.run(main())
