// waku dashboard — the Observability page and the Evals page (spec 012).
// Classic <script>, shared global scope; load order and rules: static/README.md.
//
// Observability shows what happened: four cards over four tabs, in one order
// with one set of names (Turns, Tools, Memory, Spend), each card opening its
// tab. Evals judge whether it was good, so they have a page of their own,
// #evals; #observability/evals is its old address and render() in main.js
// sends it there. Both pages read GET /api/observability, which
// waku/ops/observability.py builds from this home's traces, usage.jsonl and
// receipts. #ops is the old name of Observability; render() maps it here.

const OBS_TABS = [["turns","Turns"],["tools","Tools"],["memory","Memory"],["spend","Spend"]];
const OBS_CAPTION = {
  page: "Observability answers three questions from your traces and spend ledger: what did it do, what did it cost, and what did it remember.",
  turns: "A trace is the record of one turn: its steps in order, each with its input, output, time and cost. Open a turn to see them.",
  tools: "Every tool call in your traces, grouped by where it went: treg, Waku Memory, or this machine.",
  memory: "Each turn's memory: whether the retrieval gate looked, how many memories went into the prompt, and how many facts it kept afterwards.",
  spend: "What the turns cost. Total is charged plus estimated: charged is what the platform billed for the turns it priced exactly, estimated is tokens × list price for the rest.",
  tokens: "Tokens are what model calls are charged by; tools and memory are charged per call.",
  evals: "Evals judge whether a turn or a release was good: tests, an AI judge, a human.",
};
const OBS_WINDOWS = [["today","Today"],["7d","7 days"],["all","All"]];
// The page's own state: the window the Tools tab reads, the last answer, and
// which turns are open. Kept here so the 5s refresh redraws without closing
// a waterfall someone is reading.
const OBS = {window: "7d", data: null, at: 0, loading: false, open: new Set(), drawn: new Set()};

async function loadObservability(background = false){
  if (OBS.loading) return;
  OBS.loading = true;
  try {
    const res = await fetch(`/api/observability?window=${encodeURIComponent(OBS.window)}`, background ? {headers: BG} : undefined);
    if (res.ok){ OBS.data = await res.json(); OBS.at = Date.now(); }
  } catch(e){ /* server restarting: keep the last answer */ }
  finally { OBS.loading = false; }
  if (activeView === "observability" || activeView === "evals") render();
}
function obsWindow(w){
  OBS.window = w; OBS.at = 0;
  loadObservability(false);
}
function obsToggle(key){
  if (OBS.open.has(key)) OBS.open.delete(key); else OBS.open.add(key);
  render();
}

const obsCap = key => `<div class="obs-cap">${esc(OBS_CAPTION[key])}</div>`;
const obsUsd = n => n == null ? "—" : Number(n) === 0 ? "$0" : money(Number(n));
const obsWhen = ts => esc((ts||"").replace("T"," ").slice(0,19));
const obsNum = n => n == null ? "—" : Number(n).toLocaleString();
const obsTok = n => n == null ? "—" : n >= 1e6 ? (n/1e6).toFixed(1) + "M" : n >= 1e3 ? (n/1e3).toFixed(1) + "k" : String(n);
// A bar with no chart library: segments [[value, chartIndex, label]], each
// sized by its share of `max` (default: their own sum).
function obsBar(segments, max){
  const total = max || segments.reduce((a, [v]) => a + (v || 0), 0);
  if (!total) return `<span class="obs-bar seg"></span>`;
  return `<span class="obs-bar seg">${segments.filter(([v]) => v > 0).map(([v, c, label]) =>
    `<i class="obs-seg obs-c${c}" style="width:${(v / total * 100).toFixed(2)}%" title="${esc(label || "")}"></i>`).join("")}</span>`;
}
const SOURCE_LABEL = {treg: "treg", waku_memory: "Waku Memory", local: "local"};
const obsSource = s => esc(SOURCE_LABEL[s] || s || "local");

// ---------- Turns: one row per turn; open it for the waterfall (spec 016)
// The waterfall is one grid with one shared time axis. Its head says what
// the turn did in one line (the story, built by observability.story()), then
// lists any failures, then the rows: what ran before the loop, one summary
// row per loop (closed until clicked, unless it holds a failure), and what
// ran after the reply. A step's arguments and output sit behind "details".
// Open loops and open details live in OBS.open beside the open turns, keyed
// "<turn>|l<loop>" and "<turn>|s<step>", so the 5s refresh keeps them.
const obsTurnKey = (t, i) => String(t.turn_id || t.ts || i).replace(/[^A-Za-z0-9_:.+-]/g, "");
const obsRowId = (key, part) => "wf-" + key.replace(/[^A-Za-z0-9_-]/g, "") + "-" + part;
function obsLoopToggle(key, loop){ obsToggle(`${key}|l${loop}`); }
function obsDetailToggle(key, i){ obsToggle(`${key}|s${i}`); }
function obsAllLoops(key, loops, open){
  for (let n = 1; n <= loops; n++){ if (open) OBS.open.add(`${key}|l${n}`); else OBS.open.delete(`${key}|l${n}`); }
  render();
}
// Open a row's loop (and its details, for a failure), redraw, and scroll to it.
function obsJump(key, loop, step, details){
  if (loop) OBS.open.add(`${key}|l${loop}`);
  if (details && step != null) OBS.open.add(`${key}|s${step}`);
  render();
  const el = document.getElementById(obsRowId(key, step != null ? "s" + step : "l" + loop));
  if (el) el.scrollIntoView({block: "center"});
}
// What one step is, in the row's name cell, and what it keeps behind "details".
function obsStepName(s){
  let name = "", detail = [];
  if (s.kind === "gate"){
    name = esc(s.decision || "?");
    if (s.reason) detail.push(esc(s.reason));
  } else if (s.kind === "llm"){
    name = `${s.call && s.call !== "loop" ? esc(s.call) + " · " : ""}<code>${esc(s.model || "")}</code> · ${obsTok(s.in)} in / ${obsTok(s.out)} out`;
  } else if (s.kind === "tool" || s.kind === "memory"){
    name = `<code>${esc(s.tool)}</code>${s.endpoint_id ? ` <code>${esc(s.endpoint_id)}</code>` : ""}`;
    if (s.source === "waku_memory" && s.span === "retrieval" && s.results != null) name += ` · ${s.results} came back`;
    if (!s.ok) name += " " + uiBadge("error", "bad");
    detail.push(`<span class="obs-io">source</span> ${obsSource(s.source)}`);
    if (s.source === "waku_memory" && s.span === "retrieval"){
      if (s.query) detail.push(`<span class="obs-io">query</span> “${esc(s.query)}”`);
      detail.push(uiLink("see the match on waku.one", matchesUrl(s.retrieval_trace_id)));
    }
    if (s.args) detail.push(`<span class="obs-io">in</span> <code>${esc(s.args)}</code>`);
    if (s.error) detail.push(`<span class="obs-io">error</span> ${esc(s.error)}`);
    else if (s.output) detail.push(`<span class="obs-io">out</span> <code>${esc(s.output)}</code>`);
  } else if (s.kind === "consolidation"){
    name = `consolidation kept ${s.new_facts}${s.sent ? ` · ${s.sent} sent` : ""}${s.local_only ? ` · ${s.local_only} local only` : ""}`;
    (s.facts || []).forEach(f => detail.push(esc(f)));
  } else if (s.kind === "report"){
    name = `report saved${s.title ? " · " + esc(s.title) : ""}`;
  } else {
    name = esc(s.detail || s.kind || "");
  }
  return {name, detail};
}
const obsHot = labels => (labels || []).map(l => uiBadge(esc(l), "warn")).join(" ");
// One bar on the turn's axis: from..to in ms, drawn segmented. A sub-second
// step keeps a 0.6% sliver so its place on the axis stays visible.
function obsAxisBar(from, ms, total, kind){
  if (!total || from == null || ms == null) return "";
  const left = Math.max(0, from / total * 100), width = Math.max(0.6, ms / total * 100);
  return `<span class="wf-track seg"><span class="wf-fill wf-${kind}" style="left:${left.toFixed(2)}%;width:${Math.min(width, 100 - left).toFixed(2)}%"></span></span>`;
}
// One grid row: kind, name, bar, hotspot labels, time, dollars, and the details toggle.
const obsGridRow = (cls, id, cells) => `<div class="wf-row ${cls}"${id ? ` id="${id}"` : ""}>${
  ["kind", "name", "bar", "hot", "time", "usd", "more"].map((c, i) => `<span class="wf-c-${c}">${cells[i] || ""}</span>`).join("")}</div>`;
// The kind column's word: the span kind, with memory_write said as "write".
const obsKindLabel = kind => kind === "memory_write" ? "write" : kind;
function obsStepRow(t, key, s, i, total, child){
  const kind = s.span || s.kind;
  const {name, detail} = obsStepName(s);
  const time = s.duration_ms != null ? secs(s.duration_ms) : (s.span_ms != null ? "~" + secs(s.span_ms) : "");
  const usd = s.kind === "llm" || s.usd != null ? obsUsd(s.usd) : "—";
  const open = OBS.open.has(`${key}|s${i}`);
  const more = detail.length ? uiButton(open ? "details &#9662;" : "details", {level: "tertiary", size: "sm", onclick: `obsDetailToggle('${key}',${i})`}) : "";
  const at = s.at_ms != null && s.span_ms != null ? s.at_ms - s.span_ms : null;
  const row = obsGridRow(`wf-step${child ? " wf-child" : ""}${s.ok === false ? " wf-failed" : ""}`, obsRowId(key, "s" + i),
    [obsKindLabel(kind), name, obsAxisBar(at, s.span_ms, total, kind), obsHot(s.hot), time, usd, more]);
  return row + (open ? `<div class="wf-detail${child ? " wf-child" : ""}"><div class="wf-detail-body">${detail.map(x => `<div>${x}</div>`).join("")}</div></div>` : "");
}
// A loop's summary row: its model, how many tools it called, its failures,
// its span on the axis, its time and dollars, and its steps' hotspot labels.
function obsLoopRow(t, key, n, items, total){
  const open = OBS.open.has(`${key}|l${n}`);
  const steps = items.map(([s]) => s);
  const llm = steps.find(s => s.kind === "llm") || {};
  const tools = steps.filter(s => s.kind === "tool" || s.kind === "memory").length;
  const errors = steps.filter(s => s.ok === false).length;
  const starts = steps.filter(s => s.at_ms != null && s.span_ms != null).map(s => s.at_ms - s.span_ms);
  const ends = steps.filter(s => s.at_ms != null).map(s => s.at_ms);
  const from = starts.length ? Math.min(...starts) : null, to = ends.length ? Math.max(...ends) : null;
  const ms = from != null && to != null ? to - from : null;
  const usd = steps.reduce((a, s) => a + (s.usd || 0), 0);
  const hot = [...new Set(steps.flatMap(s => s.hot || []))];
  const name = [`<code>${esc(llm.model || "")}</code>`, tools ? `${tools} tool${tools === 1 ? "" : "s"}` : "reply",
                errors ? `${errors} error${errors === 1 ? "" : "s"}` : ""]
    .filter(Boolean).join(" · ");
  const head = uiButton(`${open ? "&#9662;" : "&#9656;"} loop ${n}`, {level: "tertiary", size: "sm", onclick: `obsLoopToggle('${key}',${n})`,
    attrs: `aria-expanded="${open}"`});
  const row = obsGridRow(`wf-loop${open ? " on" : ""}`, obsRowId(key, "l" + n),
    [head, name, obsAxisBar(from, ms, total, "llm"), obsHot(hot), ms != null ? "~" + secs(ms) : "", obsUsd(usd), ""]);
  return row + (open ? items.map(([s, i]) => obsStepRow(t, key, s, i, total, true)).join("") : "");
}
function obsWaterfall(t, key, total){
  // the receipt is bookkeeping: the total row carries what it says
  const rows = [];
  t.steps.forEach((s, i) => {
    if (s.kind === "receipt") return;
    const last = rows[rows.length - 1];
    if (s.phase === "loop"){
      if (last && last.loop === s.loop) last.items.push([s, i]);
      else rows.push({loop: s.loop, items: [[s, i]]});
    } else rows.push({step: s, i});
  });
  const axis = obsGridRow("wf-axis", "", ["kind", "name",
    `<span class="wf-axis-ends"><span>0s</span><span>${total ? secs(total) : ""}</span></span>`, "", "time", "$", ""]);
  const receipt = [...t.steps].reverse().find(s => s.kind === "receipt");
  const foot = obsGridRow("wf-total", "", ["", `total${receipt && receipt.credits != null ? ` · ${obsNum(receipt.credits)} credits` : ""}`,
    "", "", secs(t.latency_ms), obsUsd(t.usd) + (t.has_receipt && receipt && receipt.estimate === false ? "" : " est"), ""]);
  const body = rows.map(r => r.items ? obsLoopRow(t, key, r.loop, r.items, total) : obsStepRow(t, key, r.step, r.i, total, false)).join("");
  return `<div class="wf" role="table">${axis}${body}${foot}</div>`;
}
// The one slot for a turn's badges, under the story line: today the scores
// spec 012 reads from `score` events; spec 015's pass/fail chips go here too.
function obsTurnBadges(t){
  const scores = (t.scores || []).map(x => uiBadge(`${esc(x.source)}${x.name ? " · " + esc(x.name) : ""} · ${esc(String(x.value))}`, "value", x.note || ""));
  return scores.length ? `<div class="obs-scores">${scores.join(" ")}</div>` : "";
}
// The waterfall's head: the story line (each part opens its row), the badges
// slot, one notice per failure, and Open all / Close all.
function obsTurnHead(t, key){
  const steps = t.steps;
  const parts = (t.story || []).map(p => {
    const text = esc(p.text) + (p.part === "treg" ? " " + obsUsd(p.usd) : "");
    if (p.step == null && p.loop == null) return `<span>${text}</span>`;
    const s = p.step != null ? steps[p.step] : null;
    const loop = s ? (s.phase === "loop" ? s.loop : 0) : p.loop;
    return uiButton(text, {level: "tertiary", size: "sm", cls: "wf-part", onclick: `obsJump('${key}',${loop},${p.step != null ? p.step : "null"},false)`});
  });
  const tail = [secs(t.latency_ms), obsUsd(t.usd) + (t.has_receipt ? "" : " est")].join(" · ");
  const story = `<div class="wf-story">${parts.join(`<span class="wf-arrow">&rarr;</span>`)}<span class="wf-tail">· ${tail}</span></div>`;
  const errors = (t.errors || []).map(e => uiNotice("failed",
    `<code>${esc(e.tool)}</code>${e.endpoint_id ? ` <code>${esc(e.endpoint_id)}</code>` : ""} ${esc(e.error)}`,
    `<span class="meta">${e.phase === "loop" ? "loop " + e.loop : e.phase === "after" ? "after the reply" : "before the loop"}</span> `
      + uiButton("show", {level: "tertiary", size: "sm", onclick: `obsJump('${key}',${e.phase === "loop" ? e.loop : 0},${e.step},true)`}))).join("");
  const graph = t.graph ? `graph ${esc(t.graph.workflow)}: ${t.graph.path.map(esc).join(" &rarr; ")}${t.graph.ms != null ? " · " + secs(t.graph.ms) : ""} · ` : "";
  const where = t.turn_id ? `turn <code>${esc(t.turn_id)}</code> · ` : "older trace: no turn id or tool durations; times are measured between lines · ";
  const tools = t.loops ? `<span class="wf-tools">${uiButton("Open all", {level: "tertiary", size: "sm", onclick: `obsAllLoops('${key}',${t.loops},true)`})}${
    uiButton("Close all", {level: "tertiary", size: "sm", onclick: `obsAllLoops('${key}',${t.loops},false)`})}</span>` : "";
  return story + obsTurnBadges(t) + errors + `<div class="wf-meta"><span class="meta">${graph}${where}${obsWhen(t.ts)}</span>${tools}</div>`;
}
function obsTurns(d){
  const turns = d.turns || [];
  let h = obsCap("turns");
  if ((d.trace_errors||[]).length){
    h += d.trace_errors.map(e => uiNotice("failed", `${uiBadge("trace encoding error", "bad")}
      <code>${esc(e.file)}</code>: ${esc(e.error)}`)).join("");
  }
  if (!turns.length) return h + uiCard(`<span class="empty">no turns traced yet. Send Waku a message and it appears here.</span>`);
  h += turns.map((t, i) => {
    const key = obsTurnKey(t, i);
    const open = OBS.open.has(key);
    const meta = [secs(t.latency_ms), ...(t.scores||[]).map(x => `${esc(x.source)} score ${esc(String(x.value))}`),
                  t.loops ? `${t.loops} loop${t.loops === 1 ? "" : "s"}` : "", `${t.tool_calls} tool${t.tool_calls === 1 ? "" : "s"}`,
                  t.tokens_in || t.tokens_out ? `${obsTok(t.tokens_in)} in / ${obsTok(t.tokens_out)} out` : "",
                  obsUsd(t.usd) + (t.has_receipt ? "" : " est"),
                  t.gate ? `gate ${esc(t.gate)}` : "", t.unfinished ? "never finished" : ""].filter(Boolean).join(" · ");
    const row = uiRow(`<span class="meta">${obsWhen(t.ts).slice(5,16)}</span>`,
      `${open ? "&#9662;" : "&#9656;"} ${esc(t.user_message || "(no message)")}`, meta,
      {onclick: `obsToggle('${key}')`, cls: open ? "on" : ""});
    if (!open) return row;
    // A failed step opens its loop the first time the turn is drawn open, so
    // a failure is never behind a closed row; after that, the reader decides.
    if (!OBS.drawn.has(key)){
      OBS.drawn.add(key);
      (t.errors || []).forEach(e => { if (e.phase === "loop") OBS.open.add(`${key}|l${e.loop}`); });
    }
    const total = Math.max(t.latency_ms || 0, ...t.steps.map(s => s.at_ms || 0)) || null;
    return row + uiCard(obsTurnHead(t, key) + obsWaterfall(t, key, total), {size: "sm", cls: "obs-wf"});
  }).join("");
  return h;
}

// ---------- Tools: grouped by source, treg endpoints first, then its actions
function obsToolTable(rows, {results = false, source = false, byUsd = false, provider = false, head = "tool"} = {}){
  // the bar is sized by dollars for treg, by calls everywhere else
  const max = Math.max(...rows.map(r => byUsd ? (r.usd || 0) : r.calls), 0);
  const heads = [head, "", ...(provider ? ["provider"] : []), ...(source ? ["source"] : []), "calls", "errors", "avg time", ...(results ? ["avg results"] : ["$"])];
  return table(heads, rows.map(r => `<tr><td><code>${esc(r.tool)}</code></td>
    <td class="obs-barcell">${obsBar([[byUsd ? (r.usd || 0) : r.calls, byUsd ? 2 : 3, byUsd ? obsUsd(r.usd) : r.calls + " calls"]], max)}</td>${provider ? `<td class="meta">${esc(r.provider || "")}</td>` : ""}${source ? `<td class="meta">${obsSource(r.source)}</td>` : ""}
    <td class="meta">${r.calls}</td><td>${r.errors ? uiBadge(String(r.errors), "bad") : `<span class="meta">0</span>`}</td>
    <td class="meta">${r.avg_ms != null ? secs(r.avg_ms) : "—"}</td>
    <td class="meta">${results ? (r.avg_results ?? "—") : obsUsd(r.usd)}</td></tr>`));
}
function obsTools(d){
  const t = d.tools || {treg:{tools:[],endpoints:[]}, waku_memory:{tools:[],recent_queries:[]}, other:{tools:[]}};
  let h = obsCap("tools");
  h += `<h2>treg <span class="meta obs-h-sub">${t.treg.calls} call(s) · ${obsUsd(t.treg.usd)}</span></h2>`;
  // the endpoints answer "which tools did it use"; the actions are how it got there
  h += `<h3>Endpoints called</h3>`;
  h += obsToolTable((t.treg.endpoints||[]).map(e => ({...e, tool: e.endpoint_id})), {byUsd: true, provider: true, head: "endpoint"});
  h += `<h3>treg actions</h3><div class="obs-cap">The steps around each call: search the catalog, read an endpoint's docs, call it.</div>`;
  h += obsToolTable(t.treg.tools, {byUsd: true});
  h += `<h2>Waku Memory <span class="meta obs-h-sub">${t.waku_memory.calls} call(s)</span></h2>`;
  const reads = t.waku_memory.tools.filter(r => r.span === "retrieval");
  const writes = t.waku_memory.tools.filter(r => r.span !== "retrieval");
  h += `<h3>retrieval: reading memory</h3>` + obsToolTable(reads, {results: true});
  if (writes.length) h += `<h3>memory_write: saving to memory</h3>` + obsToolTable(writes, {results: true});
  if ((t.waku_memory.recent_queries||[]).length){
    h += `<h3>Recent searches</h3>`;
    h += table(["query","tool","came back",""], t.waku_memory.recent_queries.map(q =>
      `<tr><td>${esc(q.query)}</td><td class="meta"><code>${esc(q.tool)}</code></td>
        <td class="meta">${q.results ?? "—"}</td>
        <td>${uiLink(q.retrieval_trace_id ? "match" : "matches", matchesUrl(q.retrieval_trace_id))}</td></tr>`));
  }
  h += `<h2>Local and MCP tools <span class="meta obs-h-sub">${t.other.calls} call(s)</span></h2>`;
  h += obsToolTable(t.other.tools, {source: true});
  h += `<div class="meta obs-foot">What each tool is: ${uiLink("Tools", "#tools")}.</div>`;
  return h;
}

// ---------- Memory: the gate, used and kept per turn
function obsMemory(d, D){
  let h = obsCap("memory");
  h += `<h2>Retrieval gate</h2>${gateSplit(D.stats)}`;
  const rows = (d.memory||[]).filter(m => m.gate || m.used != null || m.kept != null || m.searches);
  h += `<h2>Per turn</h2>`;
  h += table(["turn","gate","Waku Memory searches","used in prompt","kept"], rows.slice(0, 30).map(m =>
    `<tr><td>${esc((m.user_message||"").slice(0, 60))}</td>
      <td>${m.gate ? uiBadge(esc(m.gate), m.gate === "retrieve" ? "ok" : "neutral") : `<span class="meta">—</span>`}</td>
      <td class="meta">${m.searches || 0}</td><td class="meta">${m.used ?? "—"}</td>
      <td class="meta">${m.kept ?? "—"}${m.local_only ? ` · ${m.local_only} local only` : ""}</td></tr>`));
  h += `<div class="meta obs-foot">"Used" and "kept" come from each turn's receipt; a turn traced before receipts shows "—" for used.</div>`;
  return h;
}

// ---------- Spend: estimated vs charged
function obsSpend(d){
  const s = d.spend || {ledger: {by_day: [], by_provider: []}};
  const m = d.summary || {};
  const sp = m.spend || s, tok = m.tokens || {in: 0, out: 0, calls: 0};
  const win = (OBS_WINDOWS.find(([k]) => k === m.window) || [, "all"])[1].toLowerCase();
  let h = obsCap("spend") + obsCap("tokens");
  h += uiStatBand([
    {label: "total", value: obsUsd(sp.total_usd), sub: `${esc(win)}: ${obsSpendSplit(sp)}`},
    {label: "charged", value: sp.charged_usd == null ? "—" : obsUsd(sp.charged_usd),
     sub: sp.charged_usd == null ? "only the hosted platform bills" : `${sp.charged_turns} turn(s) priced exactly by the platform`, tone: sp.charged_usd == null ? "" : "ok"},
    {label: "estimated", value: obsUsd(sp.estimated_usd), sub: "every other turn, tokens × list price"},
    {label: "credits", value: sp.credits == null ? "—" : obsNum(sp.credits), sub: "Waku Memory credits"},
    {label: "model calls", value: obsNum(tok.calls), sub: `${obsNum(tok.in)} in / ${obsNum(tok.out)} out`},
  ]);
  h += uiCard(`<span class="r prose">Every model call's tokens are logged to <code>usage.jsonl</code>, which a demo
    reset never wipes. The estimate prices those tokens at list price. On agent.waku.one the metering proxy
    knows the exact charge, and each turn's receipt records it as charged; that figure replaces the
    turn's estimate, so no dollar is counted twice.</span>`,
    {footer: reveal("usage.jsonl","open usage.jsonl")});
  const models = s.by_model || [];
  if (models.length){
    h += `<h2>By model, all-time</h2>` + table(["model","provider","calls","tokens in","tokens out","at list price"], models.map(r =>
      `<tr><td><code>${esc(r.model)}</code></td><td class="meta">${esc(r.provider)}</td><td class="meta">${obsNum(r.calls)}</td>
        <td class="meta">${obsNum(r.in)}</td><td class="meta">${obsNum(r.out)}</td><td class="meta">${obsUsd(r.usd)}</td></tr>`));
  }
  const days = d.spend_by_day || [];
  if (days.length){
    const max = Math.max(...days.map(r => r.total), 0);
    h += `<h2>Per day</h2><div class="obs-legend">${[["model", 1], ["treg tools", 2], ["Waku Memory", 3]].map(([l, c]) =>
      `<span><i class="obs-swatch obs-c${c}"></i>${l}</span>`).join("")}</div>`;
    h += table(["day","","tokens in","tokens out","$"], days.map(r =>
      `<tr><td class="meta">${esc(r.date.slice(5))}</td>
        <td class="obs-barcell">${obsBar([[r.model, 1, "model " + obsUsd(r.model)], [r.treg, 2, "treg " + obsUsd(r.treg)], [r.memory + r.other, 3, "Waku Memory and other tools " + obsUsd(r.memory + r.other)]], max)}</td>
        <td class="meta">${obsTok(r.in)}</td><td class="meta">${obsTok(r.out)}</td><td class="meta">${obsUsd(r.total)}</td></tr>`));
    h += `<div class="meta obs-foot">Model dollars are charged for the turns the platform priced exactly and estimated from usage.jsonl for the rest; tool dollars are what each tool's answer said it cost.</div>`;
  }
  return h;
}

// ---------- Evals (its own page): what exists, the last gate, where they run
function obsEvals(d){
  const e = d.evals || {};
  const verdict = v => v === "pass" ? "ok" : v === "fail" ? "bad" : "neutral";
  let h = "";
  const det = e.deterministic, run = e.last_run;
  const gate = run ? run.verdict : e.last;
  const open = !!gate && gate.deterministic === "pass" && gate.judge !== "fail";
  // The cards report the last make gate run's own counts. Without a run, they
  // say what is on disk and call it that: test functions and suite files,
  // which are not results.
  const runCard = (label, c) => c
    ? {label, value: `${obsNum(c.passed)} <span class="obs-of">passed</span>`,
       sub: `${obsNum(c.failed)} failed · last gate ${obsWhen(run.ran_at)}`, tone: c.failed ? "" : "ok"}
    : {label, value: "—", sub: `did not run in the last gate, ${obsWhen(run.ran_at)}`};
  // A hosted container has no gate run of its own, but its image carries the
  // record of the release it runs: the commit and the CI checks that passed
  // on it (hosted/deploy/autodeploy.sh). The counts are the ones CI reported.
  const rel = e.release;
  const relOk = !!rel && rel.checks.every(c => c.conclusion === "success");
  const relDet = rel && rel.checks.find(c => c.name === "skills-and-evals");
  const relTests = relDet && relDet.tests;
  h += uiStatBand(run ? [
    runCard("deterministic", run.deterministic),
    runCard("AI judge", run.judge),
    {label: "last gate", value: open ? "open" : "closed", sub: obsWhen(run.ran_at), tone: open ? "ok" : ""},
  ] : rel ? [
    {label: "deterministic", value: relTests ? `${obsNum(relTests.passed)} <span class="obs-of">passed</span>` : esc(relDet ? relDet.conclusion : "—"),
     sub: relTests ? `${obsNum(relTests.failed)} failed · ${obsNum(relTests.skipped)} skipped · in CI on ${esc(rel.short_sha)}`
                   : `in CI on ${esc(rel.short_sha)}; CI recorded no count`,
     tone: relTests && !relTests.failed ? "ok" : ""},
    {label: "AI judge", value: "not in CI", sub: "it needs an API key, so it runs in make gate on a maintainer's machine"},
    {label: "release", value: esc(rel.short_sha), sub: `deployed ${obsWhen(rel.deployed_at)}`, tone: relOk ? "ok" : ""},
  ] : [
    {label: "deterministic", value: det ? obsNum(det.tests) : "in CI",
     sub: det ? `test functions in ${det.files} files, counted on disk; no gate run recorded` : "not shipped in this container"},
    {label: "AI judge", value: e.judge_suites ? obsNum(e.judge_suites.length) : "in CI",
     sub: e.judge_suites ? "suite files, counted on disk; no gate run recorded" : "run by make gate"},
    {label: "last gate", value: e.last ? (open ? "open" : "closed") : "—",
     sub: e.last ? obsWhen(e.last.ran_at) : "make gate has not run here", tone: open ? "ok" : ""},
  ]);
  h += table(["kind","what it checks","where"], [
    `<tr><td>Deterministic</td><td class="meta">Pass or fail with no model: trace shape, redaction, routes, the receipt, the design system${det ? ` (${obsNum(det.tests)} test functions in ${det.files} files)` : ""}.</td><td class="meta"><code>evals/deterministic/</code>, every PR in CI</td></tr>`,
    `<tr><td>AI judge</td><td class="meta">A model scores answers against a rubric${e.judge_suites ? ", one suite file each: " + e.judge_suites.map(s => `<code>${esc(s)}</code>`).join(", ") : ""}.</td><td class="meta"><code>evals/judge/</code>, in <code>make gate</code></td></tr>`,
    `<tr><td>Human</td><td class="meta">You read a turn's waterfall and decide whether it did the right thing.</td><td class="meta">${uiLink("Turns", "#observability/turns")}</td></tr>`,
  ]);
  if (e.hosted){
    h += uiNotice("note", `<b>On agent.waku.one, evals run before a release, not in your container.</b>
      <span class="r">Every change to Waku Agent runs the deterministic evals in CI, and <code>make gate</code> adds the
      AI judge. agent.waku.one upgrades only to a commit on <code>main</code> whose <code>skills-and-evals</code> and
      <code>hosted-docker</code> checks passed, so only green commits ship.</span>`);
  }
  if (rel){
    const out = href => `<a class="link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">`;
    const tests = t => t ? `${esc(t.label)}: ${obsNum(t.passed)} passed, ${obsNum(t.failed)} failed, ${obsNum(t.skipped)} skipped`
                         : "no count recorded";
    h += `<h2>This release</h2>` + uiCard(
      `<div>Commit ${out(rel.commit_url)}<code>${esc(rel.short_sha)}</code></a>, deployed ${obsWhen(rel.deployed_at)}.
         It shipped because every required check below passed on that exact commit.</div>`)
      + table(["required check","result","tests","run"], rel.checks.map(c =>
        `<tr><td><code>${esc(c.name)}</code></td>
          <td>${uiBadge(esc(c.conclusion), c.conclusion === "success" ? "ok" : "bad")}</td>
          <td class="meta">${tests(c.tests)}</td>
          <td class="meta">${out(c.url)}view on GitHub</a></td></tr>`));
  }
  h += `<h2>Release gate</h2>`;
  h += e.last ? uiCard(`${uiBadge(`deterministic · ${esc(e.last.deterministic)}`, verdict(e.last.deterministic))}
        <span class="obs-gap">${uiBadge(`AI judge · ${esc(e.last.judge)}`, verdict(e.last.judge))}</span>
        <div class="meta">last run ${obsWhen(e.last.ran_at)}. Run it again with <code>make gate</code>.</div>`)
    : uiCard(`<span class="empty">${rel ? "make gate, which adds the AI judge, runs on a maintainer's machine, so this container keeps no record of it. The CI checks above are this release's record."
                                    : e.hosted ? "The release gate runs in CI before an upgrade, so this container keeps no record of it."
                                             : "never run here yet. Run <code>make gate</code> to fill this in."}</span>`);
  if ((e.history||[]).length){
    const cnt = s => s ? `${s.passed||0} pass · ${s.failed||0} fail` : "—";
    h += `<h2>Gate history</h2>` + table(["when","deterministic","AI judge","counts"], e.history.map(r =>
      `<tr><td class="meta">${obsWhen(r.ran_at)}</td><td>${uiBadge(esc(r.deterministic), verdict(r.deterministic))}</td>
        <td>${uiBadge(esc(r.judge), verdict(r.judge))}</td>
        <td class="meta">det ${cnt(r.suites&&r.suites.deterministic)} · judge ${cnt(r.suites&&r.suites.judge)}</td></tr>`));
  }
  return h;
}

// The four cards, one per tab and in the tabs' order, for the chosen window.
// Each card opens its tab, and the open tab's card is drawn selected.
function obsCardBody(k, m){
  const of = t => `<span class="obs-of">${t}</span>`;
  const sub = lines => lines.filter(Boolean).map(l => `<span class="stat-sub">${l}</span>`).join("");
  if (k === "turns"){
    return {value: obsNum(m.turns.count),
      sub: sub([m.turns.avg_loops != null ? `avg ${m.turns.avg_loops} loops per turn` : "no loop calls traced"])};
  }
  if (k === "tools"){
    const tl = m.tools, total = tl.treg.calls + tl.waku_memory.calls + tl.other.calls;
    const errors = tl.treg.errors + tl.waku_memory.errors + tl.other.errors;
    return {value: `${obsNum(total)} ${of("calls")}`,
      bar: obsBar([[tl.treg.calls, 2, "treg"], [tl.waku_memory.calls, 3, "Waku Memory"], [tl.other.calls, 4, "local and MCP"]]),
      sub: sub([`treg ${tl.treg.calls} · Waku Memory ${tl.waku_memory.calls} · local ${tl.other.calls}`,
                `${errors} error${errors === 1 ? "" : "s"}`])};
  }
  if (k === "memory"){
    const me = m.memory || {retrievals: 0, writes: 0, kept: 0, gate_retrieve: 0, gate_skip: 0};
    return {value: `${obsNum(me.retrievals)} ${of(me.retrievals === 1 ? "retrieval" : "retrievals")}`,
      bar: obsBar([[me.gate_retrieve, 1, `gate retrieve ${me.gate_retrieve}`], [me.gate_skip, 3, `gate skip ${me.gate_skip}`]]),
      sub: sub([me.avg_results != null ? `avg ${me.avg_results} results per retrieval` : "no results counted",
                `${obsNum(me.writes)} write${me.writes === 1 ? "" : "s"} · ${obsNum(me.kept)} fact${me.kept === 1 ? "" : "s"} kept`,
                `gate: retrieve ${me.gate_retrieve} · skip ${me.gate_skip}`])};
  }
  const sp = m.spend;
  return {value: `${obsUsd(sp.total_usd)} ${of("total")}`,
    bar: obsBar([[sp.model_usd, 1, "model"], [sp.treg_usd, 2, "treg"], [sp.memory_usd + sp.other_usd, 3, "Waku Memory and other tools"]]),
    sub: sub([obsSpendSplit(sp), obsChargedLine(sp),
              `${obsTok(m.tokens.in)} in / ${obsTok(m.tokens.out)} out · ${obsNum(m.tokens.calls)} model calls`])};
}
// The two ways one total splits (observability.spend_totals): by where the
// dollars went, and by charged (a turn the platform priced exactly) plus
// estimated (tokens × list price for every other turn). Both add up to it.
const obsSpendSplit = sp => `model ${obsUsd(sp.model_usd)} · treg ${obsUsd(sp.treg_usd)} · memory ${obsUsd(sp.memory_usd)}`
  + (sp.other_usd ? ` · other ${obsUsd(sp.other_usd)}` : "");
const obsChargedLine = sp => sp.charged_usd == null ? "all estimated: tokens × list price"
  : `${obsUsd(sp.charged_usd)} charged + ${obsUsd(sp.estimated_usd)} estimated`;
function obsCards(m, active){
  return `<div class="obs-strip">${OBS_TABS.map(([k, label]) => {
    const b = m ? obsCardBody(k, m) : {value: "…", sub: ""};
    const on = k === active;
    return `<a class="obs-tile${on ? " on" : ""}" href="#observability/${k}" data-card="${k}"${on ? ' aria-current="page"' : ""}>
      <span class="stat-label">${label}</span><b class="stat-value">${b.value}</b>${b.bar || ""}${b.sub}</a>`;
  }).join("")}</div>`;
}
const obsWindowBar = () => `<div class="obs-windows">${OBS_WINDOWS.map(([k,l]) =>
    uiButton(l, {level: k === OBS.window ? "primary" : "secondary", size: "sm", onclick: `obsWindow('${k}')`})).join("")}
    ${OBS.loading ? `<span class="meta">loading…</span>` : ""}</div>`;
VIEWS.observability = function(D, sub){
  sub = OBS_TABS.some(([k]) => k === sub) ? sub : "turns";
  if (!OBS.loading && Date.now() - OBS.at > 5000) deferBg(loadObservability);
  const d = OBS.data;
  let h = `<div class="obs-cap obs-page-cap">${esc(OBS_CAPTION.page)}</div>`;
  h += obsWindowBar();
  h += obsCards(d && d.summary, sub);
  // no counts on the tabs: the card above each tab carries its numbers
  h += subtabBar("observability", OBS_TABS, sub);
  if (!d) return h + uiCard(`<span class="empty">reading traces…</span>`);
  if (sub === "tools") return h + obsTools(d);
  if (sub === "memory") return h + obsMemory(d, D);
  if (sub === "spend") return h + obsSpend(d);
  return h + obsTurns(d);
};
VIEWS.evals = function(){
  if (!OBS.loading && Date.now() - OBS.at > 5000) deferBg(loadObservability);
  const h = `<div class="obs-cap obs-page-cap">${esc(OBS_CAPTION.evals)}</div>`;
  if (!OBS.data) return h + uiCard(`<span class="empty">reading the eval record…</span>`);
  return h + obsEvals(OBS.data);
};
