# Phase 3 — Evaluation-driven improvement

Phase 3 does not start from a pre-committed list of upgrades. It starts from the Langfuse traces
and structured logs Phase 2 already produces from real test calls.

## Process

1. **Latency breakdown** (per-stage spans from Pipecat/Langfuse): is the bottleneck STT TTFB, LLM
   TTFT, or TTS TTFA? This determines which component to touch first.
   - LLM TTFT dominates -> benchmark Claude Haiku 4.5 against Sonnet 5 on real scheduling
     transcripts; verify prompt caching is actually hitting.
   - STT dominates -> benchmark Deepgram Flux, self-hosted Parakeet / faster-whisper.
   - TTS dominates -> A/B ElevenLabs / Cartesia Sonic / Kokoro-82M against Deepgram Aura-2 (the
     Phase 2 default).
2. **Turn-taking quality**: add Pipecat's Smart Turn v3 semantic VAD on top of Silero VAD — a
   config/dependency addition, not a rearchitecture.
3. **ASR error analysis**: review real-call transcripts for misrecognitions (names, dates); may
   justify a Deepgram keyword-boost list or a different STT vendor.
4. **Voice naturalness**: listening tests across TTS candidates — healthcare callers likely want
   calm, unhurried delivery, not just low latency.
5. **Guardrails / RAG**: small retrieval layer for clinic-specific facts (insurance, hours, what
   to bring); harden the emergency-redirect and PHI-minimization instructions.
6. **Real backend**: swap the FHIR sandbox for a real hospital's FHIR endpoint via SMART OAuth — a
   new `SchedulingBackend` implementation; `scheduling/service.py`, `ClaudeToolAgent`, and the
   pipeline don't change.
7. **Compliance path** (only once real PHI is in scope): BAAs with Twilio/Deepgram/Cartesia/
   Anthropic, self-hosted Langfuse or PHI redaction, encryption-at-rest, retention policy.

## Multi-agent decomposition

Only pursued if evaluation shows the single-agent `ClaudeToolAgent` genuinely fails at something a
second agent would fix (e.g. a separate safety/triage check). Implemented as a new
`AgentOrchestrator` implementation (see `agent/orchestrator.py`) — tools, DB, and voice layer stay
unchanged.

## Optional portfolio track (MS coursework context)

This project doubles as a portfolio piece. If Phase 1/2 real-call evaluation shows a concrete,
measurable weakness that a trained/fine-tuned model would fix (e.g. specialty-routing accuracy
from patient-described symptoms, or medical-term ASR errors), a scoped fine-tuning experiment is a
reasonable Phase 4 addition — done *because the data justified it*, not by default. Candidates
if/when that's warranted: LoRA fine-tune on MedDialog/ChatDoctor for intent extraction, Whisper
vocabulary biasing for medical terms. Each would ship with a before/after eval, not replace the
production baseline unless it measurably wins.
