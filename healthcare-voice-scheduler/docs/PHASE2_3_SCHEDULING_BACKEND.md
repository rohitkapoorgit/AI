# Phase 2.3 — Real NPPES/FHIR-sandbox scheduling backend

**Status:** ✅ done, live-tested end to end — real NPPES search, real FHIR sandbox
availability/booking/confirmation/cancellation, verified against the actual live services
(`scripts/fhir_smoke_test.py`, plus a manual run through `SchedulingService` itself).

## Goal

Replace `MockSchedulingBackend` with a real implementation: real doctor search against the public
NPPES NPI registry, and real availability/booking/confirmation/cancellation against the public
SMART Health IT FHIR **sandbox** — explicitly never a real hospital system. That stays a much
later, compliance-gated step (`docs/PHASE3_EVAL.md`, item 6 — swapping the sandbox for a real
hospital's FHIR endpoint via SMART OAuth, once BAAs exist).

This was originally scoped as an unscheduled "Phase 1, step 4." It's Phase 2.3 now, and Phase 1 is
fully done with nothing outstanding (see `docs/ARCHITECTURE.md`).

## The one thing worth understanding first: there's no real link between NPPES and any FHIR sandbox

NPPES (real doctors) and the SMART Health IT sandbox (synthetic scheduling data) are two
independent public systems with no connection to each other — confirmed live: the sandbox's own
Practitioner resources mostly carry fake NPIs (e.g. `"310"`, not a real 10-digit NPI). No public
system can honestly provide "this specific real doctor's real calendar" without a BAA.

So `check_availability` maps each NPPES-found doctor to one of the sandbox's *existing* Schedules
via a stable hash of their NPI, rather than creating new Practitioner/Schedule/Slot resources per
doctor — an honest, consistent stand-in, not a claim of genuine per-doctor linkage. The important
refinement made during implementation: **hash into the schedules that actually have free slots**,
not the full schedule list. Only 58 of the sandbox's 167 Schedules have any free Slot at all — the
rest are entirely booked. Hashing into the raw list of 167 deterministically stranded some NPIs on
a run of consistently-empty schedules (this happened, live, while building this). Hashing into the
curated "has ≥1 free slot" list instead guarantees every doctor gets *something*.

## The sandbox's data is stale — a second real fix

The sandbox's synthetic Slot data is static and apparently never refreshed. Confirmed live: across
**all** 167 schedules, free slots range from 2022-08-15 to 2026-06-28 — and by the time this was
implemented (2026-08-04), that's already entirely in the past. A strict "give me slots on or after
today" search can legitimately return **zero results**, everywhere, forever, until this specific
public sandbox's data changes.

`check_availability` handles this with two passes:
1. **Primary**: search the probed schedules for free slots on/after the requested date.
2. **Fallback**: if that comes up empty across every probed schedule, search again without the
   date filter and return the soonest free slots regardless of date, rather than reporting no
   availability at all for what's still a demo/sandbox backend.

## Status mapping

FHIR `Appointment.status` has no value literally called "confirmed" (the value set is
`proposed|pending|booked|arrived|fulfilled|cancelled|noshow|entered-in-error|checked-in|waitlist`;
`fulfilled` means the visit already happened, not "confirmed for the future" — don't conflate):

| Our call | FHIR status set | Our domain status |
|---|---|---|
| `book_appointment` | `pending` | `"booked"` |
| `confirm_appointment` | `pending` → `booked` | `"confirmed"` |
| `cancel_appointment` | → `cancelled` (+ Slot back to `free`) | `"cancelled"` |

Booking a slot does **not** auto-flip `Slot.status` on this server — done as an explicit second
write after creating the Appointment. That's two separate REST calls with no transaction
guarantee; if the Slot-flip write fails after the Appointment was created, `book_appointment`
makes a best-effort attempt to cancel the just-created Appointment and raises, rather than leaving
a half-committed state silently.

## A real bug this project's own testing caught: `.patch()` doesn't work against this server

fhirpy's convenience `.reference(...).patch(status=...)` method sends a request this server
rejects outright:

```
OperationOutcome: "Invalid Content-Type for PATCH operation: application/json"
```

`sandbox_backend.py` uses fetch → mutate the field → `.save()` (a full PUT) everywhere instead —
confirmed live to work correctly. If you're extending this file, don't reach for `.patch()`.

## A second real finding: search-index lag after a write

Confirmed live: a direct fetch-by-id (`reference(...).to_resource()`) reflects a write
immediately, but a *search* (`resources("Slot").search(status="free")`) can still list a
just-booked slot as free for a few seconds afterward — this server's search index catches up
asynchronously. `scripts/fhir_smoke_test.py` verifies writes via direct fetch, not by immediately
re-running `check_availability`, for exactly this reason.

This doesn't threaten data integrity: `book_appointment`'s own double-booking guard
(`slot.get("status") != "free"`) uses a direct fetch, not search, so it can't be fooled by the
lag — a caller can't actually double-book a slot that looks stale-free in a search result, the
booking attempt would correctly fail on the direct-fetch check. The only user-visible effect is
that `check_availability` might briefly still list (or briefly still omit) a slot for a few
seconds around a write.

## `SCHEDULING_BACKEND` — one toggle, all four entry points

`src/voice_agent/scheduling/factory.py`'s `build_backend()` reads `SCHEDULING_BACKEND` (`"mock"`,
the default — no external calls; or `"sandbox"`) and is used by every composition root:
`scripts/run_console_call.py`, `pipeline/voice_bot_claude_agent.py`, and
`pipeline/voice_bot_native_llm.py`. No separate sandbox-specific scripts — set the env var, run
the exact same command you already use:

```bash
SCHEDULING_BACKEND=sandbox uv run python scripts/run_console_call.py
```

or, in `.env`, `SCHEDULING_BACKEND=sandbox`, then run either voice bot exactly as before (from
`/tmp`, per the existing `nltk`/cwd gotcha in `docs/PHASE2_VOICE.md`).

## Accounts/keys needed

None. Both `NPPES_API_BASE_URL` and `FHIR_SANDBOX_BASE_URL` are public, unauthenticated endpoints
— already filled in with real values in `.env.example`, no signup required.

## `scripts/fhir_smoke_test.py`

One-time, manually-run script (not part of `pytest`/CI — it makes real writes to shared public
infrastructure) that exercises `SandboxSchedulingBackend` itself end to end: search → availability
→ book → confirm → cancel, verifying each write via direct fetch. Run it after touching
`sandbox_backend.py` or `nppes_client.py`:

```bash
uv run python scripts/fhir_smoke_test.py
```

**This sandbox is shared public infrastructure with no documented reset cadence.** Every booking
through `SandboxSchedulingBackend` — dev runs, this smoke test, demos — is a real write other
people using the same public sandbox can see, and there's no guaranteed cleanup. Treat it as
dev/demo-only, and don't be surprised by other people's leftover test data when you search it.

## What's out of scope

- A real hospital's FHIR endpoint (production, real PHI, BAAs required) — `docs/PHASE3_EVAL.md`,
  item 6, gated on compliance readiness, unrelated to this sandbox swap.
- Multi-agent decomposition, RAG, fine-tuning — Phase 3+, evaluation-gated.
