# Phase 2 — Real voice channel

## Goal

Replace the mocked `VoiceChannel` (`ConsoleVoiceChannel`, stdin/stdout) with a real-time audio
pipeline: real caller audio in, real speech-to-text, a real Claude-backed agent, real
text-to-speech, real audio out — ending in a working phone call over Twilio. `SchedulingBackend`
is an independent swap-point from this specific document's scope — this doc (2.1/2.2) defaults to
`MockSchedulingBackend`, same as always, but a real implementation now exists too
(`SandboxSchedulingBackend`, real NPPES + real FHIR sandbox — see
[`docs/PHASE2_3_SCHEDULING_BACKEND.md`](PHASE2_3_SCHEDULING_BACKEND.md)) and applies to these same
voice bots via the `SCHEDULING_BACKEND` env var, since it's a drop-in swap by design.

Phase 2 is split into two sub-phases, run in order, against the same audio stack:

- **Phase 2.1** — plug the existing, tested `ClaudeToolAgent` directly into Pipecat. The point is
  to validate the audio plumbing (Deepgram STT/TTS, Silero VAD, Pipecat wiring) against an agent
  whose behavior is already known-good (17 passing tests, proven via
  `scripts/run_console_call.py`), before trusting a different LLM-calling mechanism.
- **Phase 2.2** — swap in Pipecat's native `AnthropicLLMService` once 2.1's plumbing is validated,
  trading `ClaudeToolAgent`'s simplicity for per-token streaming to TTS and native barge-in
  handling — the things that matter most once you're actually judging call quality rather than
  just "does audio flow end to end."

## Stack for this phase

- **Orchestration**: Pipecat — defines the pipeline (VAD -> STT -> LLM -> TTS) and the
  `STTService`/`TTSService` interfaces that vendor adapters plug into, the same swappable-interface
  pattern this repo already uses for `AgentOrchestrator` / `SchedulingBackend` / `VoiceChannel`.
- **VAD**: Silero (via `pipecat-ai[silero]`, already a dependency) — detects when the caller starts
  and stops talking.
- **STT**: Deepgram Nova-3 (`DeepgramSTTService`).
- **TTS**: Deepgram Aura-2 (`DeepgramTTSService`) — **same vendor as STT**, one account and one API
  key (`DEEPGRAM_API_KEY`) instead of two, and Pipecat has first-class native support for it
  (WebSocket streaming, low latency, handles interruptions). Cartesia Sonic — the original TTS
  pick — isn't dropped, it moves to a Phase 3 A/B candidate (`docs/PHASE3_EVAL.md`) instead of the
  Phase 2 default.
- **Telephony**: Twilio Voice + Media Streams, added only after the pipeline is proven locally
  (neither 2.1 nor 2.2 touches Twilio).
- **Local test client**: `pipecat-ai[webrtc]` (adds `aiortc`) plus `pipecat-ai-prebuilt` (the
  actual package Pipecat 1.7's dev runner imports for the `/client` UI — not
  `pipecat-ai-small-webrtc-prebuilt`, a similarly-named but different package).

## Phase 2.1 — ClaudeToolAgent in Pipecat (`pipeline/voice_bot_claude_agent.py`)

**Status**: ✅ live-tested end to end, including observability (`scripts/trace_dashboard.py`) —
audio plumbing, tool dispatch, and turn-taking all confirmed working over a real spoken call.

**Pipeline**: `transport.input() -> stt -> ClaudeAgentProcessor -> tts -> transport.output()` — 5
stages, notably *no* `LLMContext` or `LLMContextAggregatorPair`. `ClaudeToolAgent` already keeps
its own internal conversation state (see `agent/claude_agent.py`'s module docstring), so there's no
native LLM node here for Pipecat's context aggregators to feed — they'd be dead weight.

**`ClaudeAgentProcessor`** is a custom `FrameProcessor` that bridges the two worlds:

- It reacts only to finalized `TranscriptionFrame`s from `stt` (Deepgram's own endpointing decides
  when an utterance is "done" — there's no `SileroVADAnalyzer`-driven turn aggregator in this
  path, just a `vad_analyzer` on the transport itself for barge-in detection). Both
  `TranscriptionFrame` and `InterimTranscriptionFrame` are swallowed rather than forwarded — if
  raw transcript text reached `tts` downstream, the bot would echo the caller's own words back at
  them.
- On a finalized transcript, it calls `await self._agent.run(self._history, frame.text)` — the
  exact same `ClaudeToolAgent` method the console path calls — and pushes the reply as a
  `TTSSpeakFrame` (the frame that tells TTS "say this," independent of the normal streaming
  LLM -> TTS path).
- On `result.end_call`, it pushes an `EndTaskFrame` upstream, same pattern Phase 2.2's `end_call`
  tool uses.

**Known trade-off vs. 2.2**: `ClaudeToolAgent.run()` is one non-streaming Anthropic call — TTS
can't start speaking any of the reply until the *entire* response has been generated, and there's
no way to interrupt Claude mid-generation the way Pipecat's native LLM node supports. Acceptable
for validating plumbing; not the end state.

Run it (same `nltk`/cwd gotcha as 2.2 — see below; use `-v` for verbose/debug-level logs):

```bash
cd /tmp   # see the gotcha below for why this has to be /tmp, not just "anywhere outside the repo"
uv run --project /path/to/healthcare-voice-scheduler python \
  /path/to/healthcare-voice-scheduler/src/voice_agent/pipeline/voice_bot_claude_agent.py -t webrtc -v
```

Open **http://localhost:7860/client**, click Connect.

**Observability**: every stage logs to the terminal (what the caller said, each Claude API
round-trip and each tool call with its own latency, the reply, a per-turn total broken into
LLM time / tool time / overhead) — see `voice_bot_claude_agent.py`'s module docstring for the
full breakdown. Per-round LLM and per-call tool timing is measured inside `ClaudeToolAgent`
itself (`agent/claude_agent.py`, exposed via `AgentResult.llm_calls`/`tool_calls`) so it's
available to the console path too, not just voice. None of these timers start until a finalized
transcript arrives, so caller think-time is never counted.

For a visual view, run `scripts/trace_dashboard.py` in a separate terminal (it doesn't import
Pipecat, so — unlike the bot — it's not subject to the `nltk`/cwd gotcha; run it from anywhere,
including inside the repo):

```bash
uv run python scripts/trace_dashboard.py
```

Open **http://localhost:8901** alongside the Pipecat Playground. It polls a JSONL trace file
(`/tmp/voice_bot_trace.jsonl` by default, `VOICE_BOT_TRACE_FILE` to override) that the bot writes
to via `pipeline/trace.py`, and renders each turn as a card: what the caller said, each LLM/tool
call with its latency, the reply, and a stacked bar showing the LLM/tool/overhead split of that
turn's total time. Resets each time you click Connect.

## Phase 2.2 — Pipecat-native AnthropicLLMService (`pipeline/voice_bot_native_llm.py`)

**Status**: ✅ live-tested end to end, same as 2.1, including observability.

**Pipeline**: `transport.input() -> stt -> SttObserver -> user_aggregator -> llm ->
llm_span_observer -> tts -> transport.output() -> assistant_aggregator -> MetricsFrameLogger` —
using Pipecat's `LLMContext` + `LLMContextAggregatorPair` for VAD-driven turn detection and
transcript bookkeeping, plus the observability processors described below.

Originally the plan for *this* phase was a small custom Pipecat processor wrapping
`ClaudeToolAgent` directly — building Phase 2.1 first is what revealed that approach fights the
framework (see the trade-off above), which is why Phase 2.2 instead uses Pipecat's own
`AnthropicLLMService`, with the scheduling tools registered as Pipecat `FunctionSchema`s (thin
async handlers that call `SchedulingService` and forward the result via
`params.result_callback(...)`), plus an `end_call` function tool that pushes an `EndTaskFrame` to
hang up. `ClaudeToolAgent` isn't used by this path at all — but the exact same `SYSTEM_PROMPT` and
`TOOL_SCHEMAS` it uses (`agent/claude_agent.py`, `scheduling/service.py`) are reused here too, so
the two paths can't drift into different prompts/tool definitions even though the LLM-calling
mechanics differ.

Run it the same way, pointing at `voice_bot_native_llm.py` instead:

```bash
cd /tmp
uv run --project /path/to/healthcare-voice-scheduler python \
  /path/to/healthcare-voice-scheduler/src/voice_agent/pipeline/voice_bot_native_llm.py -t webrtc -v
```

**Observability**: same trace file, same `scripts/trace_dashboard.py`, same event names as 2.1 —
but measured differently, because the architecture genuinely differs and it'd be dishonest to fake
identical semantics. `ClaudeToolAgent`'s hand-rolled loop let 2.1 time each raw Claude API call
separately; `AnthropicLLMService` manages its own internal tool-calling loop, so from outside it we
only see one `LLMFullResponseStartFrame` -> `LLMFullResponseEndFrame` span per turn (tool calls
included) — that's what `LlmSpanObserver` (sitting between `llm` and `tts`) times and reports as a
single `llm_call` entry, reconstructing the reply text from the `TextFrame`s it accumulates in
between (native streaming doesn't hand back one atomic string the way `ClaudeToolAgent.run()`
does).

**Confirmed via live testing**: a turn with a tool call shows only **one** `llm_call` entry in the
dashboard, vs. two (`round 1`, `round 2`) for the equivalent turn in 2.1. This doesn't mean fewer
API calls actually happened — `AnthropicLLMService` almost certainly still makes the same two raw
Claude calls internally (one to get the tool request, one after the tool result to get the final
text), it just doesn't expose that internal boundary to the rest of the pipeline; from outside,
"generate a response" is one atomic span regardless of how many round-trips it took inside. So the
single `llm_call` entry's `latency_s` is really the *combined* duration of however many internal
API calls occurred, not one call's duration — you can't tell from the trace alone whether a given
span contains 1 or several internal round-trips. If that per-round-trip granularity is ever
needed, it would require finding whether Pipecat's Anthropic adapter exposes an internal hook for
it — not investigated, since turn-level timing is what actually matters for perceived call latency.

Tool call timing is exact, not approximated — measured directly inside each
`FunctionSchema` handler (`_scheduling_tool`/`_end_call_tool`), which report into the observer via
`record_tool_call()`/`mark_end_call()` since tool handlers fire *during* the LLM span, not after
it. The `llm_s` figure in the dashboard is therefore "span time minus tool time" — an
approximation of thinking/streaming time, not a raw API call duration like 2.1's. `SttObserver`
mirrors 2.1's STT logging but must forward every frame unchanged (`user_aggregator` still needs
them), unlike 2.1's `ClaudeAgentProcessor` which owns turn-taking and swallows them.
`MetricsFrameLogger` (shared with 2.1 via `pipeline/observability.py`) now also captures
`AnthropicLLMService`'s own native metrics, since — unlike `ClaudeToolAgent` — it's a real Pipecat
service Pipecat can instrument itself.

**The `nltk`/cwd gotcha** (applies to both 2.1 and 2.2): don't run either bot from inside the repo
directory — and **`cd ~` isn't a reliable fix either**, depending on your machine. `nltk` (a
transitive Pipecat dependency) ships a 2026 security hook that blocks any import it triggers whose
*resolved path* sits under `Path.cwd()` (CWE-427 mitigation), regardless of `sys.path` — `-P` and
`PYTHONSAFEPATH` don't help, since this isn't a `sys.path` search-order issue. Two things can
trigger it from the same process: our project's `.venv` (which lives inside the repo folder), and
the Python interpreter's own standard library. If your Python install lives inside your home
directory (e.g. a conda install at `~/miniconda3/...`, common on macOS), then `cd ~` makes even
stdlib imports look like "CWD imports" and still gets blocked — that's not hypothetical, it's what
happened during testing. **Use `/tmp`** — nothing either bot needs lives there. This only affects
the Pipecat-based bots — `scripts/run_console_call.py` never imports Pipecat's RTVI stack, so it's
unaffected.

## What neither 2.1 nor 2.2 replaces

`pipeline/runner.py`, `ConsoleVoiceChannel`, and `ClaudeToolAgent` aren't going away — they stay
the fast, free, no-audio-needed way to test the booking flow in isolation
(`scripts/run_console_call.py`). Both Pipecat bots are additional, parallel entry points onto the
real audio path, and both use `SchedulingBackend` purely through `SchedulingService` — they don't
know or care whether it's `MockSchedulingBackend` (the default) or `SandboxSchedulingBackend`
(`SCHEDULING_BACKEND=sandbox` — see `docs/PHASE2_3_SCHEDULING_BACKEND.md`) underneath.

## Build order

1. Phase 2.1 — `ClaudeToolAgent` in Pipecat, validate plumbing. ✅ done, live-tested
2. Phase 2.2 — swap in native `AnthropicLLMService`, validate streaming + barge-in. ✅ done, live-tested
3. Add a FastAPI server with `/twiml` and `/ws` endpoints, following Pipecat's Twilio phone-bot
   pattern.
4. Twilio account + a verified caller ID + a trial phone number (manual step — signup/purchases
   aren't something done on your behalf; the free trial credit covers this phase's testing).
5. Local test loop: run the server, `ngrok http 8000`, point Twilio's webhook at the ngrok URL,
   call the number from your own verified phone, iterate.
6. Wire OpenTelemetry -> Langfuse; verify transcripts and per-stage latency — this is the data
   `docs/PHASE3_EVAL.md` depends on.
7. Containerize, deploy to Fly.io, point the Twilio webhook at the stable Fly.io URL.
8. Tag the release, push to GitHub.

## Explicitly out of scope for this doc (2.1 / 2.2 — Twilio telephony steps 3-8 above)

- The real scheduling backend swap is covered in a sibling doc, not here —
  [`docs/PHASE2_3_SCHEDULING_BACKEND.md`](PHASE2_3_SCHEDULING_BACKEND.md).
- Multi-agent decomposition, RAG, fine-tuning — Phase 3+, evaluation-gated (`docs/PHASE3_EVAL.md`).

## Accounts/keys needed

| Need | Required for | Notes |
|---|---|---|
| `DEEPGRAM_API_KEY` | STT + TTS, both 2.1 and 2.2 | One key covers both services this phase |
| Pipecat local WebRTC client | Steps 1-2 | No account — runs on `localhost` |
| Twilio account + phone number | Steps 3-5 | Free trial credit covers local dev testing |
| `ngrok` | Step 5 | Free tier is sufficient |
| Langfuse account | Step 6 | Free tier (cloud) |
