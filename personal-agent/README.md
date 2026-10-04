# Personal Morning Digest — Multi-Agent System

A learning project: build a multi-agent system (orchestrator + sub-agents) that
runs every morning, gathers a few pieces of daily-life info in parallel, and
sends one email with everything in it.

The primary goal is learning multi-agent system design — parallel dispatch,
tool-scoped sub-agents, structured outputs, failure/retry handling, and
agentic vs. plain-code orchestration — not the specific data sources.

## Phase 1 vs Phase 2

**Phase 1 (current):** every sub-agent — including weather and BART — is built
as an LLM + tool-use loop, where the model itself decides which tools to call.

This is deliberate, but worth being honest about: weather and BART don't
actually need agency. Every run always calls the same fixed tools in the same
order (e.g. `get_forecast` for both cities, every time) — there's no
content-dependent branching, which is the actual justification for an agent
loop. Email and school *will* need it (what to search next depends on what's
found), but weather/BART are agentic in form without needing to be in
substance. Phase 1 keeps them as loops anyway, for consistent practice with
the pattern while there's little at stake.

**Phase 2:** replace the weather and BART sub-agents with a deterministic
version — plain code calls the fixed API(s) directly (no LLM in the loop
deciding whether/what to call), then a single LLM call (no tools) synthesizes
the numbers into the same `submit_*_report`-shaped structured output.

**The point of doing both:** compare them head-to-head on latency, LLM call
count, token usage, and estimated cost, using the observability fields already
built into `AgentResult` (`mode`, `llm_calls`, `input_tokens`, `output_tokens`,
`estimated_cost_usd`). Phase 1 costs at least 2 LLM calls per agent (one to
decide + call tools, one to submit); Phase 2 costs exactly 1. The expectation
is Phase 2 is faster and cheaper for zero loss in correctness here — the
comparison is meant to make that trade-off legible with real numbers, not just
argued in the abstract.

## What it does

Every morning (~6:30am), the system:

1. Checks weather for San Francisco and Dublin, CA
2. Checks BART status for the Dublin/Pleasanton → Embarcadero/Montgomery commute
3. Reads and summarizes recent email (excluding Procare mail)
4. Reads Procare (son's school) email for what he needs to bring/know today
5. Composes all of the above into one digest
6. Sends a single email with the full digest

## Architecture

```
orchestrator (plain Python, not an LLM)
 ├─ weather agent   (LLM + weather tool)      ─┐
 ├─ bart agent      (LLM + BART tool)          ├─ run concurrently,
 ├─ email agent     (LLM + Gmail read tools)   │  each returns {status, data}
 └─ school agent    (LLM + Gmail read tools)  ─┘
        │
        ▼
 composer agent (LLM only, no tools — single-shot synthesis)
        │
        ▼
 delivery (plain code, Gmail send — no LLM)
```

### Orchestrator: plain code, not an LLM

The orchestrator's job — run 4 agents in parallel, apply timeout/retry, collect
results, decide when to give up — is mechanical control flow, not judgment.
Plain code is deterministic, free, and has no failure mode of its own.

An LLM-driven orchestrator would trade that determinism for adaptiveness (e.g.
re-running the school agent because the email agent spotted a closure notice).
Worth trying later as an experiment, but not the starting point — a system
that runs unattended every morning should have legible, testable failure
behavior first.

### Sub-agents: LLM + a scoped tool list, in a loop

Each sub-agent is its own `messages.create` tool-use loop (call model → maybe
call a tool → feed result back → repeat until done), isolated from the others:

| Agent | Tools | Notes |
|---|---|---|
| Weather | NWS API (api.weather.gov), by lat/lon | SF ≈ 37.77,-122.42; Dublin, CA ≈ 37.70,-121.94 — use lat/lon, not city name, to avoid Dublin/Ireland ambiguity |
| BART | api.bart.gov (ETD + advisories) | Dublin/Pleasanton (`DUBL`) → Embarcadero (`EMBR`)/Montgomery (`MONT`), direct ride, no transfer |
| Email | Gmail, read-only, via a local MCP server | Genuinely agentic — how much to search/read depends on inbox content. 24h window, promo/marketing exclusion (`category:promotions`), and Procare exclusion all enforced server-side, not left to the prompt |
| School | Same mailbox, dedicated `search_procare_emails` tool (`from:online.procaresoftware.com`, hardcoded server-side) | Procare has no parent API, so email is the integration point. Tool-scoped in code: this agent never sees the general `search_emails` tool, and vice versa — no message is reported by both agents. Procare's own notification email was pointed at this Gmail account (previously went elsewhere) |

Tool scoping is enforced in code (e.g. filtering `list_tools()` before Claude
sees them), not by prompting alone — the email/school agents never see a
"send" tool; only delivery does.

### Composer: LLM, no tools, single call

Takes the 4 structured results, writes the final digest prose. This is where
cross-agent reasoning happens (e.g. "rain in SF + a Blue Line delay + son
needs a change of clothes → leave 15 min early") — confirmed working in
testing (a quiet day's weather/commute/school info got folded into one lead
sentence, not just concatenated). Skips missing sections and calls out
failures rather than hiding them. Runs through `run_with_retry` too (mode
`"synthesis"`, distinct from a sub-agent's `"agentic"`) — a transient failure
composing is worth retrying just like a sub-agent's. Orchestrated by
`orchestrator.run_full_digest()`, which skips composing entirely if the kill
switch trips (see below).

### Delivery: plain code

Sends the composed digest via Gmail send (separate, narrower OAuth scope —
`gmail.send` only, its own cached token — from the read-only fetch the
sub-agents use). No LLM, no MCP, no tool-use loop: `delivery.send_digest_run`
just takes a `DigestRun` and decides what to send — the real digest, a
kill-switch alert, or a composer-failure alert — verified working end to end
(`scripts/send_digest.py`), a real email landed in the inbox.

## Failure & retry design

- **Per-agent timeout** (~30s) so one slow/stuck agent doesn't block the run.
- **Retry only on transient errors** (rate limits, network, 5xx) with backoff,
  2–3 attempts. Don't retry auth/schema errors — retrying won't fix those.
- **Isolation**: agents run concurrently with exceptions captured per-agent
  (not raised into the orchestrator), so one failure can't take down the rest.
- **Malformed reports are retried, not just failed**: `MalformedReportError`
  (agents/base.py) is in the retryable set. Observed in testing that a
  `submit_*_report` tool call occasionally comes back malformed or truncated —
  a one-off model quirk, not a structural bug — so a fresh attempt is worth
  it. Each agent validates its own report shape (`_validate_report`) before
  returning success; see "Structured output reliability" below for why this
  is done with plain code instead of the API's `strict: true`.
- **Structured status, not silent gaps**: every agent returns
  `{status: "ok" | "error", data, error}`. The composer is built from the
  start to handle partial results ("couldn't fetch email — check manually"),
  not to hallucinate filler for a missing section.
- **Kill switch**: if fewer than `MIN_SUCCESSFUL_AGENTS` (currently 3 of 4)
  succeed, `run_full_digest()` skips composing and `send_digest_run` sends a
  minimal alert email instead of a broken/thin digest. If all 4 agents
  succeed but the composer itself fails, a different alert is sent instead —
  see `delivery.send_digest_run` for both paths.
- **Observability**: every `AgentResult` carries `mode`, `attempts`,
  `latency_s`, `llm_calls`, `input_tokens`, `output_tokens`, and
  `estimated_cost_usd` (see `observability.py` for the pricing table). This is
  what makes the Phase 1 vs Phase 2 comparison a measurement instead of a guess.

## Structured output reliability: why no `strict: true`

The Anthropic API supports `strict: true` on a tool schema to have the API
validate `tool_use.input` server-side. Tried it on all three `submit_*_report`
tools — reverted after testing showed it made things *less* reliable here:

- Without `strict`: >10 test runs of the weather agent's submit call
  consistently completed in 100-350 output tokens.
- With `strict`: the same call intermittently hit `max_tokens` (reproduced at
  both 1024 and 2048) with the report missing required fields entirely (e.g.
  only `headline` present, `locations` never started) — yet a separate run
  with `strict` still on and `max_tokens=8192` completed fine using only 250
  tokens. That combination (sometimes fine, sometimes far over budget, not a
  runaway loop) points to inconsistent constrained-decoding overhead on
  Sonnet 5 for this schema, not a sizing problem.

Fix used instead: no `strict`, a generous-but-not-huge `max_tokens` per agent,
a guard that rejects a submit block if `stop_reason == "max_tokens"` (a
tool_use block can still be present even when generation was cut off
mid-JSON), and a plain-code `_validate_report` per agent that checks the
shape by hand before accepting it as success. `MalformedReportError` (raised
by both the truncation guard and `_validate_report`) is retryable — see
"Failure & retry design" above.

## Configuration loading: two `.env` bugs worth knowing about

Both surfaced only once delivery was built and actually needed a config value
with no safe fallback — worth documenting since the same mistake is easy to
reintroduce:

1. **`load_dotenv()` must run before importing any `personal_agent` module.**
   Several modules read config from `os.environ` at *import time* (e.g.
   `delivery.py`'s `DIGEST_RECIPIENT`, `tools/bart.py`'s API key). Every
   script used to call `load_dotenv()` inside `main()`, which runs *after*
   its top-level `from personal_agent...` imports already executed — so
   `.env` hadn't been loaded yet when those modules read their config. It
   went unnoticed because most of those values have working fallback
   defaults; `DIGEST_RECIPIENT` has none, so it was the first to fail loudly
   (`ValueError: No recipient`) instead of silently using the wrong value.
   Fixed by moving `load_dotenv()` above the `personal_agent` imports in
   every script.
2. **`os.environ.get(key, default)`'s default only applies when the key is
   entirely absent — not when it's present but empty.** `.env.example` ships
   `BART_API_KEY=` blank on purpose, meaning "fall back to BART's public test
   key." Once bug 1 was fixed and `.env` actually loaded at the right time,
   that blank value got read as `os.environ["BART_API_KEY"] = ""`, which
   *is* present (just falsy), so the fallback never kicked in — a real
   `400 Bad Request` from BART's API. Fixed in `tools/bart.py` with
   `os.environ.get("BART_API_KEY") or "MW9S-E7SL-26DU-VV8V"` instead of the
   two-arg form. Worth checking any future `os.environ.get(key, default)`
   against an intentionally-blank `.env.example` line the same way.

## Tech choices

- **Plain Anthropic API** (`anthropic` Python SDK), not the Claude Agent SDK —
  writing the tool-use loop, dispatch, and retry logic by hand is the point of
  this project. (Compare: `healthcare-voice-scheduler/` in this repo already
  does this — its `claude_agent.py` hand-rolled loop is the pattern this
  project's sub-agents follow.)
- **Gmail via MCP** — a local MCP server we wrote (`mcp_servers/gmail_server.py`,
  using the official `mcp` SDK's `FastMCP`), not the hosted MCP connector and
  not a third-party package. Runs as a subprocess over stdio (launched fresh
  per agent run, not a persistent daemon), so the Gmail OAuth token never
  leaves this machine. Read-only scope (`gmail.readonly`) — can't send,
  delete, or modify mail. `mcp_client.py` is the generic client-side helper
  (launch, list tools, parse results) any future local MCP server can reuse.
- **Weather and BART as plain function tools** — no MCP needed; they're single
  stateless HTTP calls.

## Status

The full pipeline is live end-to-end: all four Phase 1 sub-agents (weather,
BART, email, school) run concurrently, the composer synthesizes their reports,
and delivery sends the result by email — a real digest has landed in the
inbox via `scripts/send_digest.py`. Full run costs ~$0.05. A launchd plist
(paired with `pmset` to wake the laptop beforehand) is built and validated
for daily 6:30am runs — see "Scheduling" below for the install commands
(yours to run, not run on your behalf).

```
src/personal_agent/
  observability.py        model pricing table + estimate_cost_usd
  orchestrator.py          concurrent dispatch, kill-switch check, run_full_digest()
  delivery.py              plain-code Gmail send (own gmail.send-scoped token)
  mcp_client.py            generic MCP stdio client helper (any future local MCP server)
  tools/weather.py         NWS API client (real tool)
  tools/bart.py            BART API client (real tools: departures, advisories)
  mcp_servers/gmail_server.py  Gmail MCP server (read-only, local subprocess over stdio;
                                search_emails, search_procare_emails, get_email)
  agents/base.py           AgentResult + run_with_retry + MalformedReportError
  agents/weather_agent.py  tool-use loop + submit_weather_report (fake "return" tool)
  agents/bart_agent.py     tool-use loop + submit_bart_report (fake "return" tool)
  agents/email_agent.py    tool-use loop over Gmail MCP tools + submit_email_report
  agents/school_agent.py   tool-use loop over Procare-only MCP tool + submit_school_report
  agents/composer.py       single-call synthesis of all 4 reports into the final digest
scripts/
  run_weather_agent.py     standalone smoke test
  run_bart_agent.py        standalone smoke test
  run_email_agent.py       standalone smoke test
  run_school_agent.py      standalone smoke test
  run_orchestrator.py      runs all agents concurrently, prints a summary table
  run_digest.py            all agents + composer, prints the digest (no send)
  send_digest.py           full pipeline including delivery — what launchd calls daily
launchd/
  com.rohitkapoor.personal-agent.digest.plist  daily 6:30am job (see "Scheduling")
logs/                      launchd's StandardOutPath/StandardErrorPath land here (gitignored)
```

## Scheduling

launchd (macOS-native; cron is legacy here) runs `scripts/send_digest.py`
daily at 6:30am. Since the laptop sleeps overnight, this is paired with
`pmset` to wake it 5 minutes early — launchd itself never wakes a sleeping
Mac.

`launchd/com.rohitkapoor.personal-agent.digest.plist` is built and
`plutil`-validated. Two things about it matter beyond the obvious:
- **`WorkingDirectory` is required, not cosmetic.** `gmail_server.py` and
  `delivery.py` both resolve default credential/token paths via
  `os.getcwd()` — without this key, launchd's default working directory
  (not this project folder) would break those lookups.
- **Absolute paths only** (`ProgramArguments` points straight at the conda
  `python3` binary) — launchd runs with a minimal environment, no shell
  `PATH` to resolve a bare `python3` against.
- **The plist has machine-specific paths hardcoded.** `ProgramArguments`,
  `WorkingDirectory`, `StandardOutPath` and `StandardErrorPath` all point at
  `/Users/rohitkapoor/...` (including the miniconda `python3`). If you clone
  this repo to another machine or user account, edit every path in
  `launchd/com.rohitkapoor.personal-agent.digest.plist` (and the `Label`, if
  you want your own name) before installing it, or the job will fail silently.

Install (run these yourself — they start a persistent daily job and `pmset`
needs your sudo password, so this isn't something to run unattended on your
behalf). Use `bootstrap`/`bootout`/`kickstart`/`print` (the modern launchd
subcommands, targeting your GUI session domain), not the legacy
`load`/`unload`/`start`/`list` — those are deprecated on current macOS and can
fail with a cryptic `Input/output error` for things that aren't actually
broken (e.g. calling `load` twice on the same job — confirmed harmless in
testing; `print` showed the job correctly registered either way, just
noisier and less reliable than the modern commands):

```sh
cp launchd/com.rohitkapoor.personal-agent.digest.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rohitkapoor.personal-agent.digest.plist
launchctl print gui/$(id -u)/com.rohitkapoor.personal-agent.digest   # confirm it's loaded

sudo pmset repeat wakeorpoweron MTWRFSU 06:25:00
```

Test without waiting for 6:30am: `launchctl kickstart gui/$(id -u)/com.rohitkapoor.personal-agent.digest`
(fires immediately), then check `logs/digest.out.log` / `logs/digest.err.log`.

### Stopping the schedule

**Pause temporarily** (job definition stays installed, easy to resume with
`bootstrap` again):

```sh
launchctl bootout gui/$(id -u)/com.rohitkapoor.personal-agent.digest
```

This alone stops the daily digest. The wake schedule is separate — if the
laptop waking up early at 6:25am for no reason (no job left to run) bothers
you, also run:

```sh
sudo pmset repeat cancel
```

**Note:** `pmset` only supports *one* repeating wake/poweron event system-wide
— `pmset repeat cancel` clears it entirely, not just this project's. Harmless
if this is the only repeating wake schedule on the machine (true by default);
worth checking `pmset -g sched` first if you've set up others since.

**Remove entirely** (pause first, then also delete the installed copy —
the plist under this repo's `launchd/` dir is untouched either way, so
reinstalling later is just re-running the install steps above):

```sh
launchctl bootout gui/$(id -u)/com.rohitkapoor.personal-agent.digest
rm ~/Library/LaunchAgents/com.rohitkapoor.personal-agent.digest.plist
sudo pmset repeat cancel
```

## Build order

1. ~~Weather agent (thin end-to-end slice)~~ ✅
2. ~~BART agent~~ ✅
3. ~~Orchestrator with concurrent dispatch, timeout/retry, partial-failure handling~~ ✅
4. ~~Email agent (Gmail MCP, read-only)~~ ✅
5. ~~School agent (Procare via filtered email)~~ ✅
6. ~~Composer with cross-agent synthesis~~ ✅
7. ~~Real delivery (Gmail send)~~ ✅ — ~~scheduling~~ ✅ (see below); structured
   logging still open
8. Phase 2: deterministic weather/BART agents + head-to-head comparison
   (latency, LLM calls, tokens, cost) against their Phase 1 versions

## Open questions / setup needed

- [x] Google Cloud project + OAuth creds (Gmail API) — both scopes done and
      working: read-only (`.secrets/gmail_credentials.json` +
      `gmail_token.json`) and send (`gmail_send_token.json`), all gitignored
- [x] Scheduling — launchd (see "Scheduling" below); plist is built and
      validated, install commands are yours to run (they start a persistent
      job + need sudo, see below for why)
- [x] BART API key — using BART's shared public test key for now; register a
      free one at api.bart.gov when moving past development
- [x] Procare sender address — `online.procaresoftware.com`; notification
      email repointed at the Gmail account this project already has access to
- [ ] OAuth consent screen is still in "Testing" publishing status, which
      caps refresh tokens at 7 days — fine for now, but scheduling won't stay
      unattended past a week until this is published (a one-time Cloud
      Console setting, not a code change; no Google verification needed for
      a single personal user)
- [ ] Partial-failure threshold for the kill switch — currently 3 of 4 agents
      must succeed (`MIN_SUCCESSFUL_AGENTS` in orchestrator.py)

## Possible future additions

- Calendar agent (today's meetings, travel time, conflicts)
- Kid-logistics synthesis (weather + school → "PE day + rain, pack indoor shoes")
- Tasks/deadlines agent (bills, deliveries, birthdays — parsed from email)
- Memory (remember what's been marked important/ignored, improve over time)
- Feedback loop (reply "more/less of X" to adjust the digest)
- Evening counterpart (tomorrow's prep)
- Agentic-orchestrator experiment (an LLM-driven orchestrator instead of plain
  code, compared the same way Phase 1 vs Phase 2 compares sub-agents)
