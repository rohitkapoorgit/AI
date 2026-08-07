# Healthcare Voice Scheduler

A production-grade voice AI that helps patients schedule doctor appointments over a real phone call.

**Status:** Phase 1 complete. Phase 2: 2.1 (`ClaudeToolAgent` in Pipecat) and 2.2 (Pipecat's native
streaming LLM node) are both live-tested over WebRTC — Twilio telephony not started yet. 2.3 (real
NPPES doctor search + real SMART FHIR sandbox booking) is done and live-tested end to end. See
[`docs/PHASE2_VOICE.md`](docs/PHASE2_VOICE.md) and
[`docs/PHASE2_3_SCHEDULING_BACKEND.md`](docs/PHASE2_3_SCHEDULING_BACKEND.md).

## What this is

Call a real phone number, talk to an AI agent, and book a real appointment slot against a real
healthcare scheduling standard (SMART on FHIR). Built with production APIs and open-source
orchestration rather than a from-scratch or toy pipeline.

- **Real doctors** via the NPPES NPI registry (federal, public, no auth).
- **Real scheduling standard** via a SMART on FHIR sandbox (the same interoperability standard
  Epic/Cerner expose) — not a Google Calendar stand-in.
- **Synthetic patients only.** This system has no BAA with any vendor and is not HIPAA-compliant
  as configured. Do not use real patient data.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full component breakdown,
[`docs/PIPELINE_FLOW.html`](docs/PIPELINE_FLOW.html) for a diagram of one caller turn end to end,
[`docs/PHASE2_VOICE.md`](docs/PHASE2_VOICE.md) for the real voice channel plan,
[`docs/PHASE2_3_SCHEDULING_BACKEND.md`](docs/PHASE2_3_SCHEDULING_BACKEND.md) for the real
scheduling backend, and [`docs/PHASE3_EVAL.md`](docs/PHASE3_EVAL.md) for how later improvements
are evaluated and selected.

## Stack

Pipecat (orchestration) · Twilio Voice + Media Streams (telephony) · Deepgram Nova-3 (STT) ·
Deepgram Aura-2 (TTS — same vendor as STT, one API key; Cartesia Sonic is a Phase 3 A/B candidate)
· Claude Sonnet 5 with native tool-calling (dialogue agent, no LangChain) · SMART FHIR sandbox +
NPPES (scheduling/provider data) · Pipecat OpenTelemetry → Langfuse (observability) · Fly.io
(deploy).

## Setup

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY — that's the only one Phase 1 needs so far
uv sync --extra dev     # or: pip install -e ".[dev]"
```

Deepgram and Twilio credentials aren't needed yet — those land with Phase 2 (`docs/PHASE2_VOICE.md`).

## Quickstart

The scheduling backend, dialogue agent, and voice channel are each built behind a small interface
(`SchedulingBackend`, `AgentOrchestrator`, `VoiceChannel`) so real implementations drop in without
touching callers. The dialogue agent is already real — `ClaudeToolAgent` (Claude Sonnet 5, native
tool-calling) — talking to `MockSchedulingBackend` (canned doctors/slots, in memory, the default)
over a `ConsoleVoiceChannel` (stdin/stdout standing in for phone audio).

```bash
uv run python scripts/run_console_call.py
```

This makes real, billed Anthropic API calls. Talk to it naturally, e.g. "I need a family medicine
doctor in Austin" — the agent will ask follow-up questions, look up doctors and availability, and
book against the mock backend. Type `bye` to hang up early.

Want real doctors and a real (sandbox) booking instead of canned data? Set
`SCHEDULING_BACKEND=sandbox` (in `.env`, or inline) — no extra signup, both NPPES and the FHIR
sandbox are public and unauthenticated:

```bash
SCHEDULING_BACKEND=sandbox uv run python scripts/run_console_call.py
```

Same toggle works for the voice bots too — see
[`docs/PHASE2_3_SCHEDULING_BACKEND.md`](docs/PHASE2_3_SCHEDULING_BACKEND.md).

Run the tests (these use a fake Anthropic client — no API calls, no cost):

```bash
uv run pytest
```

The real scheduling backend (NPPES/FHIR, see
[`docs/PHASE2_3_SCHEDULING_BACKEND.md`](docs/PHASE2_3_SCHEDULING_BACKEND.md)) is done; the real
voice channel (Pipecat + Deepgram, Twilio still pending — see
[`docs/PHASE2_VOICE.md`](docs/PHASE2_VOICE.md)) is partway there. Both drop in behind their
existing interfaces — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the swap points.

## Disclaimer

This project uses synthetic patient data for development and testing. It is not configured for
HIPAA compliance and must not be used with real patient health information.
