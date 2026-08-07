#!/usr/bin/env python3
"""Live dashboard for the Phase 2 bots' structured trace — works for both voice_bot_claude_agent.py
(2.1) and voice_bot_native_llm.py (2.2), since both write the same event shape via
pipeline/trace.py.

Reads the JSONL file voice_agent.pipeline.trace writes to and serves a simple auto-refreshing
page grouping events into per-turn cards (caller's line, each LLM round and tool call with its
own latency, the reply, and a total-time breakdown bar). Doesn't import Pipecat, so — unlike the
bot scripts — it's not subject to the nltk/cwd gotcha in docs/PHASE2_VOICE.md; run it from
anywhere, including inside the repo.

Usage: run this in a separate terminal from the bot, then open http://localhost:8901 in a
browser tab alongside the Pipecat Playground (localhost:7860/client) while you test a call.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from voice_agent.pipeline.trace import TRACE_FILE

app = FastAPI()


@app.get("/events")
def events() -> JSONResponse:
    if not TRACE_FILE.exists():
        return JSONResponse([])
    lines = TRACE_FILE.read_text().strip().splitlines()
    return JSONResponse([json.loads(line) for line in lines if line])


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return _PAGE


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Voice Bot Trace</title>
<style>
  :root {
    --bg: #14171A; --surface: #1B1F23; --ink: #E7E9EA; --ink-muted: #9BA1A8;
    --accent: #D9A34A; --border: #2A2F35; --llm: #5B8DEF; --tool: #D9A34A; --overhead: #4A5058;
    --error: #E05D5D; --font: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--font); font-size: 13px; }
  .page { max-width: 900px; margin: 0 auto; padding: 1.5rem 1.25rem 4rem; }
  header { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1.5rem; }
  h1 { font-size: 1rem; font-weight: 600; margin: 0; }
  .badge { font-size: 0.72rem; padding: 0.2em 0.6em; border-radius: 4px; border: 1px solid var(--border); }
  .badge.connected { color: #6FCF97; border-color: #6FCF97; }
  .badge.disconnected { color: var(--ink-muted); }
  .badge.pending { color: var(--accent); border-color: var(--accent); }
  .turn { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
          padding: 0.9rem 1rem; margin-bottom: 0.75rem; }
  .turn-header { display: flex; justify-content: space-between; align-items: baseline;
                 margin-bottom: 0.5rem; color: var(--ink-muted); font-size: 0.72rem; text-transform: uppercase;
                 letter-spacing: 0.05em; }
  .row { margin: 0.35rem 0; line-height: 1.5; }
  .label { color: var(--ink-muted); }
  .tool-line, .llm-line { margin-left: 1rem; color: var(--ink-muted); font-size: 0.85em; }
  .tool-line .lat, .llm-line .lat { color: var(--accent); }
  .bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; margin: 0.6rem 0 0.3rem; }
  .bar div { height: 100%; }
  .bar .llm-seg { background: var(--llm); }
  .bar .tool-seg { background: var(--tool); }
  .bar .overhead-seg { background: var(--overhead); }
  .legend { display: flex; gap: 1rem; font-size: 0.72rem; color: var(--ink-muted); }
  .legend span { display: inline-flex; align-items: center; gap: 0.35rem; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .end-call { color: var(--error); font-weight: 600; }
  .empty { color: var(--ink-muted); padding: 2rem 0; text-align: center; }
  .error-line { color: var(--error); }
</style>
</head>
<body>
<div class="page">
  <header>
    <h1>Voice Bot Trace</h1>
    <span id="status" class="badge pending">waiting…</span>
  </header>
  <div id="turns"><div class="empty">No events yet — connect and talk to the bot.</div></div>
</div>
<script>
function esc(s) { return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

function render(events) {
  const statusEl = document.getElementById("status");
  const connected = events.filter(e => e.stage === "call_connected").length;
  const disconnected = events.filter(e => e.stage === "call_disconnected").length;
  if (connected > disconnected) { statusEl.textContent = "connected"; statusEl.className = "badge connected"; }
  else if (connected > 0) { statusEl.textContent = "disconnected"; statusEl.className = "badge disconnected"; }
  else { statusEl.textContent = "waiting…"; statusEl.className = "badge pending"; }

  // group events into turns, split at each turn_summary (or error)
  const turns = [];
  let current = [];
  for (const e of events) {
    if (e.stage === "call_connected" || e.stage === "call_disconnected") continue;
    current.push(e);
    if (e.stage === "turn_summary" || e.stage === "error") { turns.push(current); current = []; }
  }
  if (current.length) turns.push(current);

  const turnsEl = document.getElementById("turns");
  if (turns.length === 0) { turnsEl.innerHTML = '<div class="empty">No events yet — connect and talk to the bot.</div>'; return; }

  turnsEl.innerHTML = turns.map((turn, i) => {
    const said = turn.find(e => e.stage === "stt_final");
    const summary = turn.find(e => e.stage === "turn_summary");
    const error = turn.find(e => e.stage === "error");
    const llmCalls = turn.filter(e => e.stage === "llm_call");
    const toolCalls = turn.filter(e => e.stage === "tool_call");

    if (error) {
      return `<div class="turn"><div class="turn-header"><span>Turn ${i + 1}</span></div>
        <div class="row error-line">Error: ${esc(error.message)}</div></div>`;
    }
    if (!summary) {
      return `<div class="turn"><div class="turn-header"><span>Turn ${i + 1} (in progress…)</span></div>
        ${said ? `<div class="row"><span class="label">Caller:</span> "${esc(said.text)}"</div>` : ""}</div>`;
    }

    const total = summary.total_s || 0.0001;
    const llmPct = (summary.llm_s / total * 100).toFixed(1);
    const toolPct = (summary.tool_s / total * 100).toFixed(1);
    const overheadPct = (100 - llmPct - toolPct).toFixed(1);

    return `<div class="turn">
      <div class="turn-header">
        <span>Turn ${i + 1}${summary.end_call ? ' <span class="end-call">· END CALL</span>' : ""}</span>
        <span>${summary.total_s.toFixed(2)}s</span>
      </div>
      ${said ? `<div class="row"><span class="label">Caller:</span> "${esc(said.text)}"</div>` : `<div class="row"><span class="label">(greeting)</span></div>`}
      ${llmCalls.map(c => `<div class="llm-line">LLM round ${c.round} <span class="lat">${c.latency_s.toFixed(2)}s</span></div>`).join("")}
      ${toolCalls.map(c => `<div class="tool-line">${esc(c.name)}(${esc(JSON.stringify(c.input))}) <span class="lat">${c.latency_s.toFixed(2)}s</span></div>`).join("")}
      <div class="row"><span class="label">Reply:</span> "${esc(summary.reply)}"</div>
      <div class="bar">
        <div class="llm-seg" style="width:${llmPct}%"></div>
        <div class="tool-seg" style="width:${toolPct}%"></div>
        <div class="overhead-seg" style="width:${overheadPct}%"></div>
      </div>
      <div class="legend">
        <span><span class="dot" style="background:var(--llm)"></span>LLM ${summary.llm_s.toFixed(2)}s (${summary.llm_call_count})</span>
        <span><span class="dot" style="background:var(--tool)"></span>Tools ${summary.tool_s.toFixed(2)}s (${summary.tool_call_count})</span>
        <span><span class="dot" style="background:var(--overhead)"></span>Overhead ${summary.overhead_s.toFixed(2)}s</span>
      </div>
    </div>`;
  }).reverse().join("");
}

async function poll() {
  try {
    const res = await fetch("/events");
    render(await res.json());
  } catch (e) { /* server not up yet or bot not started — keep trying */ }
}
poll();
setInterval(poll, 1000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8901)
