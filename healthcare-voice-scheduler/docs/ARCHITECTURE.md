# Architecture

## Goals

1. Production-grade: use the same class of APIs real voice AI / healthcare scheduling systems use.
2. Minimize rewrite across phases: every pipeline stage is swappable behind a stable interface.
3. Open source where viable; hosted APIs only where latency/quality genuinely requires it.
4. Testable end-to-end via a real inbound phone call.
5. Real backend data (NPPES, SMART FHIR) with synthetic patient identities — no real PHI.

## Components

| # | Component | Goal | Libraries / APIs | Data | Future swap point |
|---|---|---|---|---|---|
| 1 | Orchestration pipeline | Wire audio in -> STT -> agent -> TTS -> audio out, low latency | Pipecat (Python), asyncio | n/a | Add Smart Turn v3 semantic VAD; swap any node below |
| 2 | Telephony | Real inbound phone number over WebSocket audio | Twilio Voice + raw Media Streams (not ConversationRelay), FastAPI `/twiml` + `/ws` | Real | None expected |
| 3 | STT | Caller audio -> text, streaming, low latency | Deepgram Nova-3 via Pipecat `DeepgramSTTService` | n/a | Benchmark faster-whisper / Parakeet if STT is the bottleneck |
| 4 | TTS | Agent text -> natural audio, streaming | Deepgram Aura-2 via Pipecat `DeepgramTTSService` — same vendor as STT, one API key | n/a | A/B ElevenLabs / Cartesia Sonic / Kokoro-82M (`docs/PHASE3_EVAL.md`) |
| 5 | Dialogue / agent orchestrator | Intent extraction, tool-calling, conversation state | Claude Sonnet 5 (Anthropic SDK, native tool-calling, prompt caching) behind a custom `AgentOrchestrator` ABC. No LangChain. | n/a | Swap the `AgentOrchestrator` implementation (e.g. multi-agent) only if evaluation shows single-agent Claude is inadequate |
| 6 | Scheduling tools | `search_doctors`, `check_availability`, `book_appointment`, `cancel_appointment`, `confirm_appointment` | Plain Python functions, `httpx` | Real backend, synthetic patients | Tool signatures stay stable even if backend changes |
| 7 | Provider directory | Real doctor lookup (name, specialty, location, languages) | NPPES API (CMS NPI registry), free, no auth ✅ done, Phase 2.3 | Real | Enrich with CMS quality data / NUCC taxonomy later |
| 8 | Scheduling backend | Real scheduling standard, not a mock DB | SMART Health IT sandbox (public FHIR) ✅ done, Phase 2.3 — `docs/PHASE2_3_SCHEDULING_BACKEND.md` | Real API / synthetic patients | Point at a real hospital's FHIR endpoint via SMART OAuth once BAAs exist (`docs/PHASE3_EVAL.md`) |
| 9 | Observability | Per-stage latency, transcripts, tool-call success/failure — the Phase 3 evaluation dataset | Pipecat native OpenTelemetry -> Langfuse Cloud (free tier) | n/a | Self-host Langfuse / redact PHI once real patient data is in scope |
| 10 | Deployment | Publicly reachable phone number, anytime | ngrok (local dev) -> Fly.io (stable `wss://`), Docker | n/a | Revisit only if scaling past one concurrent call |
| 11 | Secrets & config | Safe to publish on GitHub | `pydantic-settings`, `.env.example`, `.gitignore`, Fly.io secrets | n/a | n/a |

## Explicitly out of scope for Phase 1 and 2

PubMed/RxNorm/CMS-quality RAG enrichment, insurance network data, caller phone-number validation,
appointment reminders, cross-call memory (Mem0), and any model fine-tuning/DAPT. These are
Phase 3+ candidates, evaluation-gated — not defaults.

## Why no LangChain

The tool schemas (JSON schema, name/params/description) are already provider-agnostic and live in
plain Python (`scheduling/service.py`), independent of which LLM calls them. The only
Anthropic-specific code is the `ClaudeToolAgent` implementation of `AgentOrchestrator`
(`agent/claude_agent.py`). Swapping providers later means replacing that one class, not adopting a
framework whose abstraction overhead costs latency in the real-time voice hot path. If Phase 3
evaluation genuinely shows a need for multi-agent decomposition, a LangGraph-based implementation
can be dropped in behind the same `AgentOrchestrator` interface — evaluation-driven, not assumed
upfront.

## Build order

### Phase 1 — mock-first pipeline + real agent (done, nothing outstanding)

1. Repo scaffold. ✅
2. **Mock-first pipeline scaffold** (no external accounts required): `SchedulingBackend` /
   `AgentOrchestrator` / `VoiceChannel` interfaces, each with a mock implementation, wired together
   by `pipeline/runner.py`. This validated the tool-calling shape and conversation flow before any
   real API was touched. ✅
3. **Real dialogue agent** (requires only `ANTHROPIC_API_KEY`): `ClaudeToolAgent` (Claude Sonnet 5,
   native tool-calling, the schemas defined in `scheduling/service.py`) replaces the scripted mock
   behind `AgentOrchestrator`, while `SchedulingBackend` stays mocked (`MockSchedulingBackend`) and
   `VoiceChannel` stays a console (`ConsoleVoiceChannel`). Runnable today via
   `scripts/run_console_call.py`; unit tests use a fake Anthropic client, no real calls. ✅

### Phase 2 — real voice channel + real scheduling backend (done)

- **2.1 / 2.2 — real `VoiceChannel`**: Pipecat + Deepgram (STT and TTS) over WebRTC, `ClaudeToolAgent`
  (2.1) then Pipecat's native `AnthropicLLMService` (2.2). Twilio telephony not yet started. Full
  plan, build order, and scope in **[`docs/PHASE2_VOICE.md`](PHASE2_VOICE.md)**. ✅
- **2.3 — real `SchedulingBackend`**: `SandboxSchedulingBackend` (real NPPES doctor search + real
  SMART Health IT FHIR sandbox availability/booking/confirmation/cancellation — never a real
  hospital system). Toggled via `SCHEDULING_BACKEND`, applies uniformly to every entry point. Full
  plan in **[`docs/PHASE2_3_SCHEDULING_BACKEND.md`](PHASE2_3_SCHEDULING_BACKEND.md)**. ✅

### Phase 3 — evaluation-driven improvement (planned)

Starts now that Phase 2 can produce real call traces. Full plan in
**[`docs/PHASE3_EVAL.md`](PHASE3_EVAL.md)**.
