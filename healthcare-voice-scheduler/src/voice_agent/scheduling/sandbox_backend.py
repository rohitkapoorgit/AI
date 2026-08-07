"""Real SchedulingBackend: NPPES (real doctor search) + the public SMART Health IT FHIR
*sandbox* (availability/booking/confirmation/cancellation) — never a real hospital system. See
docs/PHASE2_3_SCHEDULING_BACKEND.md for the full design rationale.

The one thing worth understanding before reading the rest of this file: there is no real-world
link between an NPPES-registered doctor and any public FHIR sandbox's synthetic scheduling data
(confirmed live — the sandbox's own Practitioner resources mostly carry fake NPIs). No public
system can honestly provide "this specific real doctor's real calendar" without a BAA, which is
out of scope until much later (docs/PHASE3_EVAL.md, item 6). So `check_availability` maps each
NPPES doctor to one of the sandbox's ~167 *existing* Schedules via a stable hash of their NPI —
an honest, consistent stand-in, not a claim of genuine per-doctor linkage.
"""

import contextlib
import os
from datetime import date, datetime

from fhirpy import AsyncFHIRClient
from fhirpy.base.exceptions import BaseFHIRError, ResourceNotFound

from voice_agent.scheduling.backend import SchedulingBackend
from voice_agent.scheduling.errors import (
    AppointmentNotFoundError,
    DoctorNotFoundError,
    SchedulingError,
    SlotUnavailableError,
)
from voice_agent.scheduling.models import (
    Appointment,
    AvailabilityQuery,
    Doctor,
    DoctorSearchQuery,
    Slot,
)
from voice_agent.scheduling.nppes_client import NppesApiError, NppesClient

_DEFAULT_FHIR_BASE_URL = "https://launch.smarthealthit.org/v/r4/fhir"
_ID_PREFIX = "sandbox"

# FHIR Appointment.status has no value literally called "confirmed" -- pending -> booked is the
# idiomatic "this got confirmed" transition; booked -> fulfilled means the visit already happened,
# which is not what our confirm_appointment means. See docs/PHASE2_3_SCHEDULING_BACKEND.md.
_FHIR_STATUS_ON_BOOK = "pending"
_FHIR_STATUS_ON_CONFIRM = "booked"
_FHIR_STATUS_ON_CANCEL = "cancelled"

_MAX_SCHEDULE_PROBES = 5
_MAX_FALLBACK_SLOTS = 5
_NAME_PREFIXES = {"dr", "doctor"}
_BENIGN_QUERY_ERROR_SNIPPETS = ("requires additional search criteria", "No valid search criteria")


def _hash_npi_to_schedule_index(npi: str, schedule_count: int) -> int:
    return int(npi) % schedule_count


def _parse_fhir_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=None)


def _parse_prefixed_id(value: str) -> tuple[str, str] | None:
    """Splits "sandbox:{doctor_npi}:{fhir_id}" -> (doctor_npi, fhir_id), or None if malformed."""
    parts = value.split(":", 2)
    if len(parts) != 3 or parts[0] != _ID_PREFIX:
        return None
    return parts[1], parts[2]


def _build_nppes_search_params(query: DoctorSearchQuery) -> dict:
    params: dict = {"enumeration_type": "NPI-1", "limit": 10}
    if query.specialty:
        params["taxonomy_description"] = query.specialty
    if query.city:
        params["city"] = query.city
    if query.state:
        params["state"] = query.state
    if query.name:
        tokens = [t for t in query.name.strip().split() if t.lower().rstrip(".") not in _NAME_PREFIXES]
        if len(tokens) >= 2:
            params["first_name"] = tokens[0]
            params["last_name"] = tokens[-1]
        elif len(tokens) == 1:
            params["last_name"] = tokens[0]
    return params


def _is_benign_query_error(exc: NppesApiError) -> bool:
    text = str(exc)
    return any(snippet in text for snippet in _BENIGN_QUERY_ERROR_SNIPPETS)


def _nppes_result_to_doctor(result: dict) -> Doctor:
    basic = result.get("basic", {})
    name = f"Dr. {basic.get('first_name', '')} {basic.get('last_name', '')}".strip()

    taxonomies = result.get("taxonomies") or []
    primary_taxonomy = next((t for t in taxonomies if t.get("primary")), None)
    specialty = (primary_taxonomy or (taxonomies[0] if taxonomies else {})).get("desc", "")

    addresses = result.get("addresses") or []
    location = next((a for a in addresses if a.get("address_purpose") == "LOCATION"), None)
    location = location or (addresses[0] if addresses else {})

    return Doctor(
        npi=result["number"],
        name=name,
        specialty=specialty,
        city=location.get("city", ""),
        state=location.get("state", ""),
        languages=[],  # NPPES has no language-spoken field -- confirmed, nothing populates this
    )


class SandboxSchedulingBackend(SchedulingBackend):
    def __init__(
        self,
        *,
        nppes_client: NppesClient | None = None,
        fhir_client: AsyncFHIRClient | None = None,
        fhir_base_url: str | None = None,
    ):
        self._nppes = nppes_client or NppesClient()
        self._fhir = fhir_client or AsyncFHIRClient(
            fhir_base_url or os.environ.get("FHIR_SANDBOX_BASE_URL", _DEFAULT_FHIR_BASE_URL)
        )
        self._schedule_ids_cache: list[str] | None = None

    async def search_doctors(self, query: DoctorSearchQuery) -> list[Doctor]:
        try:
            results = await self._nppes.search(**_build_nppes_search_params(query))
        except NppesApiError as exc:
            if _is_benign_query_error(exc):
                return []
            raise SchedulingError(f"Doctor search failed: {exc}") from exc
        return [_nppes_result_to_doctor(r) for r in results]

    async def check_availability(self, query: AvailabilityQuery) -> list[Slot]:
        try:
            doctor = await self._nppes.get_by_number(query.doctor_npi)
        except NppesApiError as exc:
            raise SchedulingError(f"Doctor lookup failed: {exc}") from exc
        if doctor is None:
            raise DoctorNotFoundError(query.doctor_npi)

        schedule_ids = await self._schedule_ids_with_free_slots()
        start_index = _hash_npi_to_schedule_index(query.doctor_npi, len(schedule_ids))
        start_filter = (query.on_or_after or date.today()).isoformat()  # noqa: DTZ011 (naive, matches domain model)
        probes = min(_MAX_SCHEDULE_PROBES, len(schedule_ids))
        probe_schedule_ids = [schedule_ids[(start_index + offset) % len(schedule_ids)] for offset in range(probes)]

        # The hashed schedule may have no free slots for this date range -- probe a few more
        # (deterministically, wrapping) before giving up, rather than failing on the first miss.
        for schedule_id in probe_schedule_ids:
            fhir_slots = await self._free_slots(schedule_id, start_filter=start_filter)
            if fhir_slots:
                return [_fhir_slot_to_domain(s, query.doctor_npi) for s in fhir_slots]

        # This sandbox's synthetic Slot data is static and never refreshed -- confirmed live that
        # its free slots (across all 167 schedules) top out around 2026-06-28, which is already
        # behind real-world "now" by the time you're reading this. Rather than reporting no
        # availability at all for what's still a demo/sandbox backend, fall back to the soonest
        # free slots regardless of date if the strict on-or-after search comes up empty
        # everywhere probed. See docs/PHASE2_3_SCHEDULING_BACKEND.md.
        for schedule_id in probe_schedule_ids:
            fhir_slots = await self._free_slots(schedule_id, start_filter=None)
            if fhir_slots:
                fhir_slots.sort(key=lambda s: s.get("start") or "")
                return [
                    _fhir_slot_to_domain(s, query.doctor_npi) for s in fhir_slots[:_MAX_FALLBACK_SLOTS]
                ]
        return []

    async def _free_slots(self, schedule_id: str, *, start_filter: str | None) -> list:
        params: dict[str, str] = {"schedule": f"Schedule/{schedule_id}", "status": "free"}
        if start_filter:
            params["start"] = f"ge{start_filter}"
        try:
            return await self._fhir.resources("Slot").search(**params).fetch_all()
        except BaseFHIRError as exc:
            raise SchedulingError(f"Availability lookup failed: {exc}") from exc

    async def book_appointment(self, slot_id: str, patient_name: str) -> Appointment:
        parsed = _parse_prefixed_id(slot_id)
        if parsed is None:
            raise SlotUnavailableError(slot_id)
        doctor_npi, fhir_slot_id = parsed

        try:
            slot = await self._fhir.reference("Slot", fhir_slot_id).to_resource()
        except ResourceNotFound as exc:
            raise SlotUnavailableError(slot_id) from exc
        except BaseFHIRError as exc:
            raise SchedulingError(f"Failed to fetch slot: {exc}") from exc

        if slot.get("status") != "free":
            raise SlotUnavailableError(slot_id)

        participants = [{"actor": {"display": patient_name}, "status": "accepted"}]
        practitioner_ref = await self._practitioner_ref_for_slot(slot)
        if practitioner_ref:
            participants.insert(0, {"actor": {"reference": practitioner_ref}, "status": "accepted"})

        appointment = self._fhir.resource(
            "Appointment",
            status=_FHIR_STATUS_ON_BOOK,
            start=slot.get("start"),
            end=slot.get("end"),
            slot=[{"reference": f"Slot/{fhir_slot_id}"}],
            participant=participants,
        )
        try:
            await appointment.save()
        except BaseFHIRError as exc:
            raise SchedulingError(f"Failed to create appointment: {exc}") from exc

        try:
            slot["status"] = "busy"
            await slot.save()
        except BaseFHIRError as exc:
            # Best-effort rollback so we don't leave a booked appointment against a slot that
            # still looks free to the next caller. Booking itself already succeeded, so this is
            # a "flag it, don't hide it" failure rather than a silent inconsistency.
            with contextlib.suppress(BaseFHIRError):
                await self._set_status("Appointment", appointment.id, "cancelled")
            raise SchedulingError(f"Booked but failed to reserve the slot: {exc}") from exc

        return Appointment(
            appointment_id=f"{_ID_PREFIX}:{doctor_npi}:{appointment.id}",
            doctor_npi=doctor_npi,
            patient_name=patient_name,
            slot=Slot(
                slot_id=slot_id,
                doctor_npi=doctor_npi,
                start=_parse_fhir_datetime(slot.get("start")),
                end=_parse_fhir_datetime(slot.get("end")),
            ),
            status="booked",
        )

    async def confirm_appointment(self, appointment_id: str) -> Appointment:
        doctor_npi, _fhir_id, appt = await self._fetch_appointment(appointment_id)
        try:
            appt["status"] = _FHIR_STATUS_ON_CONFIRM
            await appt.save()
        except BaseFHIRError as exc:
            raise SchedulingError(f"Failed to confirm appointment: {exc}") from exc
        return _appointment_from_fhir(appointment_id, doctor_npi, appt, domain_status="confirmed")

    async def cancel_appointment(self, appointment_id: str) -> Appointment:
        doctor_npi, _fhir_id, appt = await self._fetch_appointment(appointment_id)
        try:
            appt["status"] = _FHIR_STATUS_ON_CANCEL
            await appt.save()
        except BaseFHIRError as exc:
            raise SchedulingError(f"Failed to cancel appointment: {exc}") from exc

        slot_refs = appt.get("slot") or []
        if slot_refs:
            slot_fhir_id = slot_refs[0].reference.split("/")[-1]
            with contextlib.suppress(BaseFHIRError):
                await self._set_status("Slot", slot_fhir_id, "free")

        return _appointment_from_fhir(appointment_id, doctor_npi, appt, domain_status="cancelled")

    async def aclose(self) -> None:
        # fhirpy opens a fresh aiohttp session per request and closes it itself -- nothing to do
        # on the FHIR side. NppesClient's httpx.AsyncClient is a real persistent pool, though.
        await self._nppes.aclose()

    async def _set_status(self, resource_type: str, fhir_id: str, status: str) -> None:
        # A full fetch-mutate-PUT, not .patch() -- confirmed live that this server rejects
        # fhirpy's default PATCH content-type ("Invalid Content-Type for PATCH operation:
        # application/json"). It wants JSON Patch or a full resource replace; PUT is simpler and
        # universally supported, so that's what this uses instead.
        resource = await self._fhir.reference(resource_type, fhir_id).to_resource()
        resource["status"] = status
        await resource.save()

    async def _schedule_ids_with_free_slots(self) -> list[str]:
        """Confirmed live: only ~35% of this sandbox's Schedules (58 of 167) have any free Slot
        at all -- the rest are entirely booked. Hashing an NPI into the *full* Schedule list would
        deterministically strand some NPIs on a run of empty schedules (this happened during
        implementation). Hashing into this curated list instead guarantees the chosen schedule has
        at least one free slot, so the fallback pass in check_availability almost always succeeds
        on the very first probe."""
        if self._schedule_ids_cache is None:
            free_slots = await self._fhir.resources("Slot").search(status="free").fetch_all()
            schedule_ids = set()
            for slot in free_slots:
                ref = slot.get("schedule")
                reference = getattr(ref, "reference", None)  # some slots have an unresolvable,
                if reference:  # display-only schedule -- confirmed live, skip those
                    schedule_ids.add(reference.split("/")[-1])
            self._schedule_ids_cache = sorted(schedule_ids)
        return self._schedule_ids_cache

    async def _practitioner_ref_for_slot(self, slot) -> str | None:
        schedule_ref = slot.get("schedule")
        if schedule_ref is None:
            return None
        schedule_id = schedule_ref.reference.split("/")[-1]
        try:
            schedule = await self._fhir.reference("Schedule", schedule_id).to_resource()
        except BaseFHIRError:
            return None
        actors = schedule.get("actor") or []
        return actors[0].reference if actors else None

    async def _fetch_appointment(self, appointment_id: str) -> tuple[str, str, object]:
        parsed = _parse_prefixed_id(appointment_id)
        if parsed is None:
            raise AppointmentNotFoundError(appointment_id)
        doctor_npi, fhir_id = parsed
        try:
            appt = await self._fhir.reference("Appointment", fhir_id).to_resource()
        except ResourceNotFound as exc:
            raise AppointmentNotFoundError(appointment_id) from exc
        except BaseFHIRError as exc:
            raise SchedulingError(f"Failed to fetch appointment: {exc}") from exc
        return doctor_npi, fhir_id, appt


def _fhir_slot_to_domain(slot, doctor_npi: str) -> Slot:
    return Slot(
        slot_id=f"{_ID_PREFIX}:{doctor_npi}:{slot.id}",
        doctor_npi=doctor_npi,
        start=_parse_fhir_datetime(slot.get("start")),
        end=_parse_fhir_datetime(slot.get("end")),
    )


def _appointment_from_fhir(appointment_id: str, doctor_npi: str, appt, *, domain_status: str) -> Appointment:
    patient_name = ""
    for participant in appt.get("participant") or []:
        actor = participant.get("actor") or {}
        if actor.get("display") and not actor.get("reference"):
            patient_name = actor["display"]
            break

    slot_refs = appt.get("slot") or []
    slot_fhir_id = slot_refs[0].reference.split("/")[-1] if slot_refs else ""
    slot_id = f"{_ID_PREFIX}:{doctor_npi}:{slot_fhir_id}" if slot_fhir_id else ""

    return Appointment(
        appointment_id=appointment_id,
        doctor_npi=doctor_npi,
        patient_name=patient_name,
        slot=Slot(
            slot_id=slot_id,
            doctor_npi=doctor_npi,
            start=_parse_fhir_datetime(appt.get("start")),
            end=_parse_fhir_datetime(appt.get("end")),
        ),
        status=domain_status,
    )
