// waku dashboard — Model arena: race ONE message through several models at once.
// Split out of app.js: classic <script>, shared global scope (no build step,
// no modules). Loads after views.js so it can hang a page onto VIEWS.
//
// Each contestant runs server-side in its own throwaway home (see
// compare_models in dashboard.py) — this is a benchmark, not a conversation, so
// nothing here touches your real memory or calendar.

// State survives the 5s refresh redraw (the view rebuilds from here) AND — via
// localStorage — tab switches and full reloads, so a finished race isn't lost.
// Kept out of the chat log on purpose: a benchmark isn't a conversation.
let compareState = { message: "Build a Kanto team around Pikachu — search current picks, remember it, and schedule two training sessions this week.",
                     picked: null, running: false, results: null, order: null, sortBy: "latency",
                     // grade every race by default, with a neutral (non-racing) referee
                     judge: true, judgeModel: "openai:gpt-5.6-sol" };
try {
  const saved = JSON.parse(localStorage.getItem("waku_compare") || "null");
  if (saved){ compareState.message = saved.message ?? compareState.message;
              compareState.results = saved.results || null;
              // only restore columns that actually finished (drop any stale racing… ones)
              compareState.order = (saved.order || []).filter(s => saved.results && saved.results[s]); }
} catch(e){}
function saveCompare(){
  try { localStorage.setItem("waku_compare", JSON.stringify({
    message: compareState.message, order: compareState.order, results: compareState.results})); } catch(e){}
}

// --- Compare history: past races + a cumulative per-model scoreboard, from the
// server's own compare/history.jsonl (never the agent's real state). Loaded once
// when the tab opens and refreshed after each race.
async function loadCompareHistory(){
  try {
    const h = await (await fetch("/api/compare/history")).json();
    compareState.history = h.runs || [];
    compareState.aggregate = h.aggregate || [];
  } catch(e){ compareState.history = []; compareState.aggregate = []; }
  editing = false;   // ensure the scoreboard redraw isn't skipped by the edit-guard
  render();
}
// Sort the scoreboard by a column: same column flips asc<->desc, a new column
// starts ascending (lowest/best first for time/tokens/cost).
function setBoardSort(key){
  const b = compareState.boardSort || {key: "total_cost_usd", dir: "asc"};
  compareState.boardSort = (b.key === key) ? {key, dir: b.dir === "asc" ? "desc" : "asc"} : {key, dir: "asc"};
  editing = false; render();
}
async function clearCompareHistory(){
  if (!confirm("Clear the compare scoreboard and race history? (Only the arena's own log — your real data is untouched.)")) return;
  const r = await postJSON("/api/compare/clear", {});
  compareState.history = r.runs || []; compareState.aggregate = r.aggregate || [];
  editing = false; render();
}
// Re-run the referee on the most recent race for any models it skipped (429'd).
// Updates the stored history + the visible cards. only_missing keeps already-
// graded models untouched.
async function regradeCompare(){
  if (compareState.regrading) return;
  compareState.regrading = true; editing = false; render();
  try {
    const r = await postJSON("/api/compare/regrade",
      {judge_model: compareState.judgeModel || "openai:gpt-5.6-sol", only_missing: false});
    compareState.history = r.runs || []; compareState.aggregate = r.aggregate || [];
    const last = (r.runs || [])[0];
    if (last && compareState.results){
      last.results.forEach(x => { if (compareState.results[x.spec]) compareState.results[x.spec].quality = x.quality; });
    }
  } catch(e){ compareState.raceError = "re-grade failed: " + e; }
  compareState.regrading = false; editing = false; render();
}
// Grade ONE card — the referee sometimes 429-skips a single model. Grades just
// this spec in the latest run, updates its badge + the scoreboard.
async function gradeCard(spec){
  const R = compareState.results || {};
  if (!R[spec] || R[spec]._grading) return;
  R[spec]._grading = true; editing = false; render();
  try {
    const r = await postJSON("/api/compare/regrade",
      {spec, judge_model: compareState.judgeModel || "openai:gpt-5.6-sol"});
    compareState.history = r.runs || []; compareState.aggregate = r.aggregate || [];
    const row = ((r.runs || [])[0] || {}).results?.find(x => x.spec === spec);
    if (row && R[spec]) R[spec].quality = row.quality;
  } catch(e){ compareState.raceError = "grade failed: " + e; }
  if (R[spec]) R[spec]._grading = false;
  editing = false; render();
}
// Delete ONE race from the scoreboard (its models leave the totals), leaving
// every other race intact — vs "clear all" which wipes the whole history.
async function deleteCompareRun(ts){
  if (!ts || !confirm("Delete just this run from the scoreboard? (Other races stay.)")) return;
  try {
    const r = await postJSON("/api/compare/delete_run", {ts});
    compareState.history = r.runs || []; compareState.aggregate = r.aggregate || [];
  } catch(e){ compareState.raceError = "delete failed: " + e; }
  editing = false; render();
}
// Dismiss the race CARDS (the per-model columns) only — the cumulative
// scoreboard/history is left alone. Handy for a clean slate before the next race.
function clearCards(){
  if (compareState.running) return;   // don't yank cards mid-race
  compareState.order = []; compareState.results = {}; compareState.raceError = null;
  saveCompare(); editing = false; render();
}
// A stored (slimmed) result -> the shape compareCol expects (gate object, tool
// objects), so a past race renders identically to a live one.
function adaptHistResult(r){
  return {...r, gate: r.gate ? {decision: r.gate} : null,
          tools: (r.tools || []).map(t => ({tool: t}))};
}
// Reopen a past race into the columns (read-only view of that run).
function openCompareRun(idx){
  const run = (compareState.history || [])[idx];
  if (!run) return;
  compareState.order = run.results.map(r => r.spec);
  compareState.results = {}; run.results.forEach(r => { compareState.results[r.spec] = adaptHistResult(r); });
  compareState.message = run.message;
  render();
}

// Which models are offered: your pinned shortlist (models.json). Default-pick
// ALL of them, so a fresh Compare tab races the whole field (uncheck to narrow).
function compareModels(d){
  const pinned = ((d.settings && d.settings.pinned) || []);
  if (compareState.picked === null){
    compareState.picked = new Set(pinned.map(p => `${p.provider}:${p.model}`));
  }
  return pinned;
}

function setCompareSort(key){
  compareState.sortBy = key;
  editing = false;   // release the textarea lock so the re-sort redraw shows
  render();
}
function toggleCompareModel(spec){
  const s = compareState.picked;
  s.has(spec) ? s.delete(spec) : s.add(spec);
  editing = false;   // release the textarea edit-lock so this redraw isn't
  render();          // skipped by the guard (else the count/chips go stale)
}
// Grade-with-K3 toggle: when on, each column's reply is judged 0-10 by kimi-k3
// (an extra API call per column, so it's opt-in).
function toggleJudge(){
  compareState.judge = !compareState.judge;
  editing = false;
  render();
}
// Coding-mode toggle: register the delegate_task tool for the race, so the loop
// can hand real coding work to a pi sub-agent (running on each card's own model)
// — the FULL harness runs (gate, memory, tools), delegate_task is just one tool.
function toggleCoding(){
  compareState.coding = !compareState.coding;
  editing = false;
  render();
}
// Write to the real Apple Calendar ('Waku' calendar), opt-in. OFF by default so
// a race doesn't spam duplicates — when ON, EVERY racing model writes its own
// event (one per model). Use with 1-2 models to demo the real integration.
function toggleApple(){
  compareState.apple = !compareState.apple;
  editing = false;
  render();
}
// Who grades quality. Deliberately NOT a racing model by default — a contestant
// can't fairly judge its own round. gpt-5.6-sol is a strong text judge that
// makes a poor tool-calling contestant, so it's the natural neutral referee.
const JUDGES = [
  {spec:"openai:gpt-5.6-sol",            label:"GPT-5.6 Sol"},
  {spec:"anthropic:claude-opus-4-8",     label:"Claude Opus 4.8"},
  {spec:"gemini:gemini-3.1-pro-preview", label:"Gemini 3.1 Pro"},
  {spec:"kimi:kimi-k3",                  label:"Kimi K3 (contestant)"},
];
function setJudgeModel(spec){ compareState.judgeModel = spec; editing = false; render(); }

// Race over SSE so each column fills the MOMENT its model finishes — a slow or
// broken contestant (e.g. a keyless provider) never blocks the others. Results
// are keyed by spec into compareState.results; the grid redraws per event.
async function runCompare(){
  const specs = [...compareState.picked];
  if (!compareState.message.trim() || !specs.length || compareState.running) return;
  editing = false;   // release the typing lock so the racing/results redraws show
  compareState.running = true;
  compareState.order = specs;      // columns to show, in picked order
  compareState.results = {};       // spec -> result, filled as they land
  compareState.raceError = null;
  compareState.grading = null;      // set during the post-race referee pass
  render();
  const R = compareState.results;
  try {
    const res = await fetch("/api/compare/stream", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({message: compareState.message, models: specs, judge: !!compareState.judge,
        judge_model: compareState.judgeModel || "openai:gpt-5.6-sol", coding: !!compareState.coding,
        apple: !!compareState.apple})});
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for(;;){
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0){
        const line = buf.slice(0, i); buf = buf.slice(i+2);
        if (!line.startsWith("data:")) continue;
        let ev; try { ev = JSON.parse(line.slice(5).trim()); } catch(e){ continue; }
        const s = ev.spec;
        // The harness plays out live: start -> gate -> tools, then the final
        // result with receipts. (We don't token-stream the reply — see
        // compare_stream in dashboard.py for why.)
        if (ev.kind === "start"){ R[s] = {spec:s, provider:ev.provider, model:ev.model, streaming:true, tools:[], gate:null}; render(); }
        else if (ev.kind === "gate" && R[s]){ R[s].gate = {decision:ev.decision, reason:ev.reason}; render(); }
        else if (ev.kind === "tool" && R[s]){ (R[s].tools = R[s].tools||[]).push({tool:ev.tool}); render(); }
        // The sub-agent (pi) streams its own events through delegate_task —
        // accumulate them per card so the contractor's work is watchable live
        // instead of a black box that returns a summary.
        else if (ev.kind === "subagent" && R[s]){
          const sub = R[s].sub = R[s].sub || {text:"", tools:[], tokens_in:0, tokens_out:0};
          if (ev.type === "text" && ev.delta) sub.text = (sub.text + ev.delta).slice(-4000);
          else if (ev.type === "tool") sub.tools.push(ev.tool);
          else if (ev.type === "turn_end"){ sub.tokens_in = ev.tokens_in||0; sub.tokens_out = ev.tokens_out||0; }
          render();
        }
        else if (ev.kind === "result" && s){ const sub = R[s] && R[s].sub; R[s] = ev; if (sub) R[s].sub = sub; saveCompare(); render(); }
        else if (ev.kind === "grading"){ compareState.grading = ev; render(); }   // post-race referee pass begins
        else if (ev.kind === "grade" && R[s]){ R[s].quality = ev.quality; if (compareState.grading) compareState.grading.done = (compareState.grading.done||0)+1; saveCompare(); render(); }
        else if (ev.kind === "done"){ compareState.grading = null; if (ev.error) compareState.raceError = ev.error; }
      }
    }
  } catch(e){ compareState.raceError = String(e); }
  compareState.running = false; saveCompare();
  // The server just logged this race; loadCompareHistory re-renders with the
  // fresh (race-inclusive) totals. No intermediate render(), so the live-folded
  // rows hand off to the server totals without a flicker.
  loadCompareHistory();
}

// One contestant's column. While the model runs (res.streaming) it plays out
// live like the chat dock — gate badge, tool chips light up, reply types in with
// a caret. When it finishes (res.result) it flips to the full receipts card.
// Reuses the shared formatters (renderMarkdown/secs/money).
// Plain-English reason for the common, expected failure modes — so the arena
// reads honestly on camera (the raw error stays below, muted).
function compareErrorReason(err){
  const e = (err || "").toLowerCase();
  if (e.includes("reasoning_effort") || e.includes("/v1/responses")) return "can't call tools — reasoning model, needs the /v1/responses API";
  if (e.includes("not a chat model") || e.includes("v1/completions")) return "not a chat model — needs the completions/responses API, not chat";
  if (e.includes("thought_signature")) return "can't call tools — missing thought_signature echo";
  if (e.includes("credit") || e.includes("permission-denied") || e.includes("license")) return "no credits/licenses on this provider";
  if (e.includes("max_tokens")) return "token-parameter mismatch";
  if (e.includes("not found") || e.includes("no longer available")) return "model id not available";
  return null;
}
// "knows to 2026-01" next to the model name — each brain's knowledge cutoff,
// so a confidently-wrong answer about recent events reads as stale knowledge,
// not low capability (a model can't know about releases after its cutoff).
// Server-supplied (MODEL_CUTOFF in dashboard.py); absent = vendor unpublished.
function cutoffTag(cutoff){
  return cutoff ? ` <span class="meta" style="font-size:11px;white-space:nowrap"
    title="knowledge cutoff — this model's world knowledge ends here; it cannot know releases after this date">knows to ${esc(cutoff)}</span>` : "";
}
// The sub-agent's receipt: pi working inside this card. Live = a small
// terminal window (last lines, cleaned of markdown fences). Finished = one
// quiet summary row that expands on click — the reply below already shows the
// final code, so the pane must not repeat it. Full stream: pi-transcript-events.jsonl.
function subPane(sub, live, spec){
  const seq = [];   // collapse repeats: write, bash, bash -> write · bash ×2
  for (const t of sub.tools||[]){
    const last = seq[seq.length-1];
    if (last && last.t === t) last.n++; else seq.push({t, n:1});
  }
  const toolStr = seq.map(x => x.n>1 ? `${x.t} ×${x.n}` : x.t).join(" · ");
  const tok = sub.tokens_in ? `${(sub.tokens_in/1000).toFixed(1)}k tok` : "";
  const clean = (sub.text||"").replace(/```[a-zA-Z]*\n?/g, "").trim();
  const tail = clean.split("\n").filter(Boolean).slice(-4).map(esc).join("\n");
  const head = `<span class="mm-prov">pi</span><span class="subterm-t">${esc(toolStr)||"…"}</span>` +
    (tok ? `<span class="meta" title="the sub-agent's tokens — billed to this card's cost">${tok}</span>` : "");
  if (live) return `<div class="subterm live"><div class="subterm-h">${head}<span class="live-dot"></span></div><pre>${tail||"spawning…"}</pre></div>`;
  // remember open/closed across re-renders — render() rebuilds the DOM every
  // refresh tick, which would otherwise snap an expanded pane shut mid-read
  return `<details class="subterm"${sub.open?" open":""} ontoggle="const r=compareState.results['${esc(spec)}']; if(r&&r.sub) r.sub.open=this.open">
    <summary class="subterm-h">${head}</summary><pre>${tail}</pre></details>`;
}
// Long replies are the cards' vertical hog (a coding answer pastes whole
// programs). Clamp past a threshold with a fade + toggle; open state lives on
// the card's state so the refresh tick doesn't snap it shut.
function replyBlock(res){
  const r = res.reply||"";
  // height comes from LINES, not characters — 20 short code lines are taller
  // than a 400-char paragraph
  const long = r.length > 400 || (r.match(/\n/g)||[]).length > 8;
  if (!long) return `<div class="r cmp-reply">${renderMarkdown(res.reply||"")}</div>`;
  return `<div class="r cmp-reply ${res.replyOpen?"":"clamped"}">${renderMarkdown(res.reply||"")}</div>
    <a class="reveal cmp-more" onclick="const r=compareState.results['${esc(res.spec)}']; if(r){r.replyOpen=!r.replyOpen; render();}">${res.replyOpen?"show less":"show full reply"}</a>`;
}
function compareCol(res){
  if (res.error){
    const why = compareErrorReason(res.error);
    return `<div class="cmp-col err"><div class="cmp-h"><span class="mm-prov">${esc(res.provider)}</span> <code>${esc(res.model)}</code>${cutoffTag(res.cutoff)}
      <span class="srcpill apple">error</span></div>
      ${why?`<div class="meta" style="color:var(--bad)"><b>${esc(why)}</b></div>`:""}
      <div class="meta" style="opacity:.7">${esc(res.error)}</div></div>`;
  }
  const tools = (res.tools||[]).map(t => t.tool === "delegate_task"
    ? `<span class="stage done subagent" title="the loop spawned a pi sub-agent on ${esc(res.model)} to write &amp; run the code">delegate_task → pi · ${esc(res.model)}</span>`
    : toolChip(t.tool)).join("");
  const gateBadgeHtml = `<span class="badge ${res.gate&&res.gate.decision==="retrieve"?"retrieve":""}">gate · ${esc(res.gate?res.gate.decision:"…")}</span>`;
  if (res.streaming){
    return `<div class="cmp-col">
      <div class="cmp-h"><span class="mm-prov">${esc(res.provider)}</span> <code>${esc(res.model)}</code>${cutoffTag(res.cutoff)}
        <span class="live-dot"></span></div>
      <div class="cmp-stats">${gateBadgeHtml}</div>
      ${tools?`<div class="stages" style="flex-wrap:wrap">${tools}</div>`:""}
      ${res.sub?subPane(res.sub, true):""}
      <div class="meta">${(res.tools||[]).length?"running tools…":"thinking…"} <span class="caret"></span></div>
    </div>`;
  }
  const c = res.completion;
  const completionBadge = c ? `<span class="cmp-score ${c.passed?"pass":"fail"}" title="${esc(c.why||"")}">${c.passed?"solved":"failed"}${c.passed?"":" · "+esc(c.why||"")}</span>` : "";
  const q = res.quality;
  const qualityBadge = q && q.score!=null ? `<span class="cmp-q ${q.score>=7?"hi":q.score>=4?"mid":"lo"}" title="graded ${q.score}/10 by ${esc(q.judge||"referee")} — ${esc(q.reason||"")}">${q.score}/10</span>` : "";
  // per-card grade button — grade just this card if the referee skipped it (429)
  const gradeBtn = `<a class="reveal cmp-grade1" title="grade this card with the referee" onclick="gradeCard('${esc(res.spec)}')">${res._grading?"grading…":(q&&q.score!=null?"re-grade":"grade")}</a>`;
  return `<div class="cmp-col${c?(c.passed?" solved":" failed"):""}">
    <div class="cmp-h"><span class="mm-prov">${esc(res.provider)}</span> <code>${esc(res.model)}</code>${cutoffTag(res.cutoff)}${completionBadge}${qualityBadge}${gradeBtn}</div>
    <div class="cmp-stats">
      ${gateBadgeHtml}
      <span class="chip ${compareState.sortBy==="latency"?"sorted":""}">${secs(res.latency_ms)}</span>
      <span class="chip">${res.iterations??"?"} iter</span>
      <span class="chip ${compareState.sortBy==="cost"?"money":""}">${money(res.cost_usd||0)}</span>
      <span class="chip ${compareState.sortBy==="tokens"?"sorted":""}">${(res.tokens_in||0)+(res.tokens_out||0)} tok</span>
    </div>
    ${tools?`<div class="stages" style="flex-wrap:wrap">${tools}</div>`:""}
    ${res.sub?subPane(res.sub, false, res.spec):""}
    ${replyBlock(res)}
  </div>`;
}

VIEWS.compare = function(d){
  const pinned = compareModels(d);
  const chips = pinned.length ? pinned.map(p => {
    const spec = `${p.provider}:${p.model}`, on = compareState.picked.has(spec);
    return `<label class="cmp-pick ${on?"on":""}"><input type="checkbox" ${on?"checked":""}
      onchange="toggleCompareModel('${esc(spec)}')"> <span class="mm-prov">${esc(p.provider)}</span> ${esc(p.model)}</label>`;
  }).join("") : `<div class="meta">No models pinned yet — star some in <a class="reveal" onclick="location.hash='#models'">Models</a>.</div>`;
  const n = compareState.picked ? compareState.picked.size : 0;

  // One column per raced model, in order. Each shows "racing…" until its result
  // arrives over the stream, then flips to the receipts card.
  let grid = "";
  const order = compareState.order || [];
  if (order.length){
    const results = compareState.results || {};
    // "done" = finished successfully (not streaming, not errored) — only these
    // have latency/cost for the fastest/cheapest summary.
    const done = order.map(s => results[s]).filter(Boolean).filter(r => !r.error && !r.streaming);
    // Sort key: the finished cards rank ascending by the chosen metric (least is
    // best — fastest / cheapest / fewest tokens), winner to the top-left.
    const metric = { latency: r => r.latency_ms || 0,
                     cost:    r => r.cost_usd || 0,
                     tokens:  r => (r.tokens_in || 0) + (r.tokens_out || 0) };
    const key = metric[compareState.sortBy] || metric.latency;
    const sorters = [["latency", "seconds"], ["tokens", "tokens"], ["cost", "money"]];
    // Right of the sort tabs, above the cards: "re-grade run" re-runs the referee
    // on every model in THIS run (the cards below); "clear cards" just dismisses
    // the columns. Both act on the current run.
    const regradeBtn = (done.length && !compareState.running)
      ? `<a class="reveal" style="margin-left:auto;font-size:12px" title="Re-run the referee on every model in this run (fills a skipped/429'd grade, or re-scores)" onclick="regradeCompare()">${compareState.regrading?"re-grading…":"re-grade run"}</a>` : "";
    const clearBtn = (order.length && !compareState.running)
      ? `<a class="reveal" style="${regradeBtn?"":"margin-left:auto;"}font-size:12px" onclick="clearCards()">clear cards</a>` : "";
    // Prominent, tab-like sort buttons — the selected one is highlighted.
    const sortBar = (done.length || clearBtn) ? `<div class="cmp-sortbar">${done.length
      ? `sort by ${sorters.map(([k, label]) => `<button class="cmp-sortbtn ${compareState.sortBy === k ? "on" : ""}" onclick="setCompareSort('${k}')">${label}</button>`).join("")}`
      : ""}${regradeBtn}${clearBtn}</div>` : "";
    // Only a progress line while the race is still running; once every column is
    // in, the sort tabs + cards + scoreboard say it all (no redundant summary).
    const g = compareState.grading;
    const summary = done.length < order.length
      ? `Racing ${order.length} models — ${done.length}/${order.length} done`
      : (g ? `Referee ${esc(g.judge||"")} grading — ${g.done||0}/${g.n} scored` : "");
    // Rank finished models first (by the chosen metric), then still-running,
    // then errors — so as the race resolves, the best rises to the top-left.
    const rank = s => {
      const r = results[s];
      if (!r) return [2, 0];                       // not started
      if (r.error) return [3, 0];                  // failed -> end
      if (r.streaming) return [1, 0];              // running -> middle
      return [0, key(r)];                          // done -> front, best-of-metric first
    };
    const shown = [...order].sort((a, b) => { const ra = rank(a), rb = rank(b); return ra[0] - rb[0] || ra[1] - rb[1]; });
    const cols = shown.map(s => {
      const r = results[s];
      if (r) return compareCol(r);
      return `<div class="cmp-col"><div class="cmp-h"><span class="mm-prov">${esc(s.split(":")[0])}</span> <code>${esc(s.split(":").slice(1).join(":"))}</code></div>
        <div class="meta">racing… <span class="caret"></span></div></div>`;
    }).join("");
    grid = `${summary ? `<div class="meta" style="margin:2px 0 6px">${summary}</div>` : ""}${sortBar}<div class="cmp-grid">${cols}</div>`
      + (compareState.raceError ? `<div class="meta" style="color:var(--bad)">${esc(compareState.raceError)}</div>` : "");
  }

  // Load the history once when the tab first opens (setting [] first stops the
  // 5s refresh from re-triggering); loadCompareHistory re-renders when it lands.
  if (compareState.history === undefined){ compareState.history = []; setTimeout(loadCompareHistory, 0); }

  return `<div class="card">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
      <span class="meta">One message, every brain at once — same harness, isolated homes, real receipts (gate · latency · cost · tools). Compare, don't guess.</span>
      <label class="cmp-judge ${compareState.apple?"on":""}" style="margin-left:auto" title="Write create_event results to your REAL Apple Calendar (the 'Waku' calendar). Off by default so a race doesn't spam duplicates — when on, EACH model writes its own event (use 1-2 models).">
        <input type="checkbox" ${compareState.apple?"checked":""} onchange="toggleApple()"> write to calendar</label>
      <label class="cmp-judge ${compareState.coding?"on":""}" title="Coding task: enables the delegate_task tool so the loop can hand real coding work to a pi sub-agent on this card's own model — the full harness runs (gate, tools), delegate_task is one of them">
        <input type="checkbox" ${compareState.coding?"checked":""} onchange="toggleCoding()"> coding (pi)</label>
      <label class="cmp-judge ${compareState.judge?"on":""}" title="Grade each reply 0-10 for how well it serves the request (correctness, honesty, concision). One extra API call per column, by a referee that isn't racing.">
        <input type="checkbox" ${compareState.judge?"checked":""} onchange="toggleJudge()"> grade &mdash; referee
        <select onchange="setJudgeModel(this.value)" onclick="event.stopPropagation()" ${compareState.judge?"":"disabled"}>
          ${JUDGES.map(j=>`<option value="${j.spec}" ${(compareState.judgeModel||"openai:gpt-5.6-sol")===j.spec?"selected":""}>${esc(j.label)}</option>`).join("")}
        </select></label>
      <button class="save cmp-race" onclick="runCompare()" ${(!n||compareState.running)?"disabled":""}>
        ${compareState.running?"Racing…":`Race ${n} model${n===1?"":"s"}`}</button>
    </div>
    <textarea id="cmp-msg" class="cmp-input" rows="2" onfocus="markEditing()"
      oninput="compareState.message=this.value">${esc(compareState.message)}</textarea>
    <div class="cmp-picks">${chips}</div>
  </div>${grid}${compareHistoryHtml()}`;
};

// The cumulative view under the current race: a per-model scoreboard averaged
// across every logged race, then the list of recent races (click to reopen).
// Data comes from GET /api/compare/history (the arena's own JSONL, never the
// agent's real state).
// The scoreboard the board shows = the server's totals (finished races) PLUS,
// while a race is still running, its already-finished columns folded in — so a
// model's numbers land the moment ITS column finishes, instead of waiting for
// the slowest model in the race. No double count: the running race isn't in the
// server totals yet, and once it completes running=false so we stop folding it.
function boardAggregate(){
  const map = {};
  (compareState.aggregate || []).forEach(a => { map[a.spec] = {...a}; });
  if (compareState.running){
    (compareState.order || []).forEach(spec => {
      const r = (compareState.results || {})[spec];
      if (!r || r.streaming) return;   // column not finished yet
      const a = map[spec] || (map[spec] = {spec, provider: r.provider, model: r.model,
        cutoff: r.cutoff, runs: 0, ok: 0, total_latency_ms: 0, total_tokens_in: 0, total_tokens_out: 0,
        total_tokens: 0, total_cost_usd: 0, cases_passed: 0, cases_scored: 0,
        _qsum: 0, quality_n: 0, quality_avg: null});
      a.runs += 1;
      if (!r.error){
        a.ok += 1;
        a.total_latency_ms += r.latency_ms || 0;
        a.total_tokens_in += r.tokens_in || 0;
        a.total_tokens_out += r.tokens_out || 0;
        a.total_tokens = a.total_tokens_in + a.total_tokens_out;
        a.total_cost_usd = Math.round((a.total_cost_usd + (r.cost_usd || 0)) * 10000) / 10000;
      }
      if (r.completion){ a.cases_scored += 1; a.cases_passed += r.completion.passed ? 1 : 0; }
      if (r.quality && r.quality.score!=null){
        // reconstruct the running sum from the server row's avg on first fold
        if (a._qsum===undefined){ a._qsum = (a.quality_avg||0) * (a.quality_n||0); }
        a._qsum += r.quality.score; a.quality_n = (a.quality_n||0) + 1;
        a.quality_avg = Math.round(a._qsum / a.quality_n * 10) / 10;
      }
    });
  }
  return Object.values(map);
}
// A small styled tooltip for the scatter's hover zones (instant + reliable,
// unlike a native SVG <title>). One element, reused; follows the cursor.
function _scTip(){
  let el = document.getElementById("sc-tip");
  if (!el){ el = document.createElement("div"); el.id = "sc-tip"; el.className = "sc-tip"; document.body.appendChild(el); }
  return el;
}
function showScatterTip(e){ const el = _scTip(); el.textContent = e.currentTarget.getAttribute("data-tip") || ""; el.style.display = "block"; moveScatterTip(e); }
function moveScatterTip(e){ const el = _scTip(); el.style.left = (e.clientX + 14) + "px"; el.style.top = (e.clientY + 12) + "px"; }
function hideScatterTip(){ const el = document.getElementById("sc-tip"); if (el) el.style.display = "none"; }
// The reveal: total cost (x) vs how good (y). Y is K3's grade when we have it,
// else the completion pass-rate — so "cheap AND good" sits top-LEFT. This is the
// picture the whole arena is built to draw ("is opus 20x the price 20x better?").
function costQualityScatter(agg){
  const useQ = agg.some(a => a.quality_avg != null);
  const pts = agg.map(a => {
    let y = null;
    if (useQ && a.quality_avg != null) y = a.quality_avg;                       // 0-10
    else if (!useQ && a.cases_scored) y = a.cases_passed / a.cases_scored * 10; // 0-10
    return {a, x: a.total_cost_usd || 0, y};
  }).filter(p => p.y != null && p.x > 0);
  if (pts.length < 2) return "";
  const W = 640, H = 300, L = 46, R = 150, T = 16, B = 36;
  const xmax = Math.max(...pts.map(p => p.x)) * 1.08 || 1;
  const px = x => L + (x / xmax) * (W - L - R);
  const py = y => H - B - (y / 10) * (H - T - B);
  const gr = [0,2,4,6,8,10].map(v => `<line x1="${L}" y1="${py(v)}" x2="${W-R}" y2="${py(v)}" class="sc-grid"/>
    <text x="${L-6}" y="${py(v)+3}" class="sc-tick" text-anchor="end">${v}</text>`).join("");
  const dots = pts.sort((a,b)=>a.x-b.x).map(p => {
    const good = p.y >= 7, mid = p.y >= 4;
    const cls = good ? "hi" : mid ? "mid" : "lo";
    return `<circle cx="${px(p.x).toFixed(1)}" cy="${py(p.y).toFixed(1)}" r="5" class="sc-dot ${cls}"/>
      <text x="${(px(p.x)+9).toFixed(1)}" y="${(py(p.y)+3).toFixed(1)}" class="sc-lbl">${esc(p.a.model)} · ${money(p.x)}</text>`;
  }).join("");
  // Hover the y-axis label to read the criteria (native SVG <title> tooltip).
  const yCriteria = useQ
    ? "Referee grade — 0-10, scored by a model that isn't racing, given the tools that actually fired:\n"
      + "9-10  fully addresses the request — correct, concise, honest\n"
      + "5-8   mostly there — minor gaps, padding, or small errors\n"
      + "1-4   partial, vague, or partly wrong\n"
      + "0     ignores it, or claims an action it didn't take"
    : "Completion — fraction of the task's checklist met (right tool, right args, enough calls). Deterministic, no judge.";
  const yLabel = useQ ? "referee grade" : "completion";
  return `<div class="card" style="padding:12px 14px;margin-top:14px">
    <div class="meta" style="margin-bottom:4px">Cost vs ${useQ?"quality (referee grade)":"completion"} — cheap &amp; good is top-left</div>
    <svg viewBox="0 0 ${W} ${H}" class="scatter" preserveAspectRatio="xMidYMid meet">
      <line x1="${L}" y1="${T}" x2="${L}" y2="${H-B}" class="sc-axis"/>
      <line x1="${L}" y1="${H-B}" x2="${W-R}" y2="${H-B}" class="sc-axis"/>
      ${gr}${dots}
      <text x="${(L+(W-R-L)/2).toFixed(0)}" y="${H-6}" class="sc-tick" text-anchor="middle">total cost →</text>
      <text x="14" y="${(T+(H-B-T)/2).toFixed(0)}" class="sc-tick sc-ylabel" text-anchor="middle" transform="rotate(-90 14 ${(T+(H-B-T)/2).toFixed(0)})">${yLabel} →</text>
      <rect class="sc-yhit" x="0" y="${T}" width="26" height="${H-B-T}" data-tip="${esc(yCriteria)}"
        onmouseenter="showScatterTip(event)" onmousemove="moveScatterTip(event)" onmouseleave="hideScatterTip()"/>
    </svg></div>`;
}
function compareHistoryHtml(){
  const agg = boardAggregate();
  const hist = compareState.history || [];
  const raceCount = hist.length + (compareState.running ? 1 : 0);
  if (!agg.length && !hist.length) return "";
  // Cumulative totals across all races. Click a column header to sort by it —
  // ascending first, click again to flip (arrow shows the active column + dir).
  const bs = compareState.boardSort || (compareState.boardSort = {key: "total_cost_usd", dir: "asc"});
  const arrow = k => bs.key === k ? (bs.dir === "asc" ? " ▲" : " ▼") : "";
  const th = (k, label) => `<th class="cmp-th ${bs.key===k?"on":""}" onclick="setBoardSort('${k}')">${label}${arrow(k)}</th>`;
  const rows = [...agg].sort((x, y) => ((x[bs.key] ?? 0) - (y[bs.key] ?? 0)) * (bs.dir === "asc" ? 1 : -1));
  const scoreboard = agg.length ? `
    <h2 style="margin-top:22px;display:flex;align-items:center;gap:10px">Scoreboard
      <span class="meta" style="font-weight:400">— totals across ${raceCount} race${raceCount===1?"":"s"}</span>
      <a class="reveal" style="margin-left:auto;font-size:12px" onclick="clearCompareHistory()">clear all</a></h2>
    ${costQualityScatter(agg)}
    <div class="card" style="padding:4px 8px"><table>
      <tr><th>model</th><th title="knowledge cutoff — when each model's world knowledge ends; it cannot know releases after this date">cutoff</th>${th("cases_passed","solved")}<th class="cmp-th ${bs.key==="quality_avg"?"on":""}" onclick="setBoardSort('quality_avg')" title="referee's mean 0-10 grade on the replies (correctness, honesty, concision) — referee is not a racing model">grade${arrow("quality_avg")}</th>${th("runs","races")}<th>ok</th>${th("total_latency_ms","total time")}${th("total_tokens_in","in tok")}${th("total_tokens_out","out tok")}${th("total_tokens","total tok")}<th title="list price per million tokens, input / output">rate $/M</th>${th("total_cost_usd","total cost")}</tr>
      ${rows.map(a=>`<tr>
        <td><span class="mm-prov">${esc(a.provider)}</span> <code>${esc(a.model)}</code></td>
        <td class="meta">${a.cutoff?esc(a.cutoff):"—"}</td>
        <td>${a.cases_scored?`<span class="cmp-score ${a.cases_passed===a.cases_scored?"pass":(a.cases_passed?"part":"fail")}">${a.cases_passed}/${a.cases_scored}</span>`:'<span class="meta">—</span>'}</td>
        <td>${a.quality_avg!=null?`<span class="cmp-q ${a.quality_avg>=7?"hi":a.quality_avg>=4?"mid":"lo"}">${a.quality_avg}</span>`:'<span class="meta">—</span>'}</td>
        <td class="meta">${a.runs}</td><td class="meta">${a.ok}/${a.runs}</td>
        <td class="meta">${secs(a.total_latency_ms)}</td>
        <td class="meta">${a.total_tokens_in}</td><td class="meta">${a.total_tokens_out}</td>
        <td class="meta">${a.total_tokens}</td>
        <td class="meta">${a.rate_in!=null?`$${a.rate_in}/$${a.rate_out}`:"—"}</td>
        <td class="meta" style="color:var(--good)">${money(a.total_cost_usd)}</td></tr>`).join("")}
    </table></div>` : "";
  const recent = hist.length ? `
    <h2 style="margin-top:18px">Recent races <span class="meta" style="font-weight:400">— click to reopen</span></h2>
    <div class="card">${hist.map((run,i)=>`
      <div class="pinrow" style="cursor:pointer" onclick="openCompareRun(${i})">
        <code style="flex:1;word-break:break-all">${esc((run.message||"").slice(0,90))}</code>
        <span class="meta" style="white-space:nowrap">${(run.results||[]).length} models · ${esc((run.ts||"").slice(0,16).replace("T"," "))}</span>
        <a class="reveal del" style="margin-left:8px;font-size:14px" title="delete just this run" onclick="event.stopPropagation(); deleteCompareRun('${esc(run.ts||"")}')">×</a>
      </div>`).join("")}</div>` : "";
  return scoreboard + recent;
}
