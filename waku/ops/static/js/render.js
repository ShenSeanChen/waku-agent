// waku dashboard — formatters + chat card renderers + chatlog + streaming + send.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

const money = n => "$" + (n < 0.01 ? n.toFixed(4) : n.toFixed(2));
const secs = ms => ms==null ? "—" : (ms/1000).toFixed(1)+"s";

const gateBadge = g => !g ? "" :
  uiBadge("gate · " + esc(g.decision), g.decision === "retrieve" ? "live" : "neutral")
  + `<span class="meta" style="margin:0">${esc(g.reason||"")}</span>`;

// Spec 009 C: what a tool call cost and who served it, when its result says
// so. treg's call answers with cost_usd and endpoint_id, through the hosted
// relay too; a named provider wins over the endpoint's first segment
// ("tomba.email.find" -> "tomba"). null for a call that names no cost.
function toolCost(x){
  if (!x) return null;
  if (typeof x.cost_usd === "number")
    return x.cost_usd >= 0 ? {usd: x.cost_usd, provider: x.provider || ""} : null;
  let out = null;
  try { out = JSON.parse(x.output); } catch(e){ return null; }
  if (!out || typeof out !== "object" || typeof out.cost_usd !== "number" || !(out.cost_usd >= 0))
    return null;
  const args = x.args || {};
  const endpoint = out.endpoint_id || out.endpoint || args.endpoint_id || args.endpoint || "";
  const provider = (typeof out.provider === "string" && out.provider)
    || String(endpoint).split(".")[0];
  return {usd: out.cost_usd, provider};
}
// What a turn's tools cost together, or 0.
const toolsCost = tools => (tools || []).reduce((sum, x) => sum + ((toolCost(x) || {}).usd || 0), 0);

// A Waku Memory search answers with JSON, whose first "sentence" is no
// summary; say how many memories it found instead.
function toolSummary(x){
  try {
    const out = JSON.parse(x.output);
    if (out && Array.isArray(out.entries))
      return `${out.entries.length} ${out.entries.length === 1 ? "memory" : "memories"} found`
        + (x.read_first ? ", read before the turn" : "");
  } catch(e){ /* not JSON: the first sentence below */ }
  return x.summary;
}

// A tool call renders as a status row (dot + one-line summary); the raw output
// hides behind a disclosure so an ugly osascript error never floods the page.
const toolRow = x => {
  const cost = toolCost(x), summary = toolSummary(x);
  return `<div class="tool ${x.status||"ok"}">
  <div class="tool-head"><span class="dot ${x.status||"ok"}"></span><code>${esc(x.tool)}</code>
    ${summary?`<span style="color:var(--text-muted)">${esc(summary)}</span>`:""}
    ${cost?`<span class="tool-cost">${money(cost.usd)}${cost.provider?` · ${esc(cost.provider)}`:""}</span>`:""}</div>
  ${x.output!==undefined?`<details><summary>args &amp; raw output</summary>
    <pre>${esc(x.tool)}(${esc(JSON.stringify(x.args,null,1))})\n\n${esc(x.output)}</pre>
  </details>`:""}
</div>`;
};

// A stored history row -> a CHAT item. Assistant rows with saved telemetry
// (meta: gate/latency/iterations/tools) render as the FULL turn card, so a
// reopened thread looks just like when it was live. Rows without meta (from
// before this was saved, or another gateway) fall back to a plain card.
//
// A stored assistant row is the model's history record: the reply, then an
// internal "[tools used: ...]" note so the model remembers it already acted
// (waku/memory/tool_note.py). Rows from before that note was compact carry a
// tool's FULL output, tens of kilobytes of search JSON drawn as a block
// thousands of lines tall. stripToolNote is the one place the note comes off,
// and histItem is the one door every stored row comes through (dock.js's
// loadThreadInto: switch, history, the "__all__" timeline, the embedded chat).
const stripToolNote = t => (t || "").replace(/\s*\[tools used: [\s\S]*$/, "").trim();
function histItem(m){
  if (m.role === "user") return {role:"user", text:m.content};
  const reply = stripToolNote(m.content);
  if (m.meta) return {role:"waku", reply, gate:m.meta.gate, slot:m.meta.slot,
                      graph:m.meta.graph, report:m.meta.report, used:m.meta.used,
                      tools:m.meta.tools, iterations:m.meta.iterations,
                      latency_ms:m.meta.latency_ms, model:m.meta.model};
  return {role:"waku", reply, historical:true};
}

const turnCard = t => uiCard(`
  <div class="u">${esc(t.user_message)}</div>
  <div class="meta" style="margin-top:var(--spacing)">${gateBadge(t.gate)}</div>
  ${(t.tools||[]).map(toolRow).join("")}
  <div class="r">${renderMarkdown(t.reply)}</div>
  <div class="meta">${esc((t.ts||"").replace("T"," ").slice(0,19))} · ${secs(t.latency_ms)} · ${t.iterations??"?"} iter · ${money(t.cost||0)}${t.consolidation?` · consolidated ${t.consolidation.new_facts} fact(s)`:""}</div>`);

// The reply's copy button. It sits inside the card, and CSS shows it only
// while the card is hovered (.card:hover .msg-copy).
const msgCopy = text => uiButton("Copy", {level: "tertiary", size: "sm", cls: "msg-copy",
  onclick: "copyMsg(this)", title: "Copy reply", attrs: `data-text="${esc(text)}"`});

// uiTable takes rows as arrays of cell HTML. A row may still arrive as a
// "<tr><td>…</td></tr>" string; parse it into its cells (a <td class> is kept
// as a span) so either shape renders the same table.
const rowCells = r => {
  if (typeof r !== "string") return r;
  const t = document.createElement("template");
  t.innerHTML = `<table><tbody>${r}</tbody></table>`;
  return [...t.content.querySelectorAll("td")].map(td =>
    td.className ? `<span class="${td.className}">${td.innerHTML}</span>` : td.innerHTML);
};
const table = (heads, rows) => uiTable(heads, rows.map(rowCells), {empty: "nothing here yet"});

// Two figures over a thin bar in the chart ramp. Shared by the retrieval gate
// (skip / retrieve) and the graph panel (quick / full); a and b are counts.
// With no turns yet the figures read "—" over an empty bar.
function splitFigures(aLabel, a, bLabel, b, ofText){
  const tot = a + b;
  const aPct = tot ? Math.round(a / tot * 100) : 0, bPct = tot ? 100 - aPct : 0;
  const fig = (label, pct) =>
    `<div><span class="stat-label">${label}</span><b class="gate-fig">${tot ? pct + "%" : "—"}</b></div>`;
  return `<div class="gate-figs">${fig(aLabel, aPct)}${fig(bLabel, bPct)}${
      tot && ofText ? `<span class="gate-of">${ofText}</span>` : ""}</div>`
    + `<div class="thinbar">${tot
      ? `<i style="flex:${aPct};background:var(--chart-1)"></i><i style="flex:${bPct};background:var(--chart-3)"></i>`
      : ""}</div>`;
}

const gateSplit = s => {
  const tot = s.gate_skips + s.gate_retrieves;
  const figs = splitFigures("Skip", s.gate_skips, "Retrieve", s.gate_retrieves, `of ${tot} turns`);
  if (!tot)
    return figs + `<div class="meta" style="margin-top:calc(var(--spacing) * 1.5)">no turns yet — send a message and the gate starts deciding</div>`;
  const skipPct = Math.round(s.gate_skips/tot*100);
  return figs + `<div class="meta" style="margin-top:calc(var(--spacing) * 1.5)">the retrieval gate skipped memory on ${skipPct}% of turns — that's latency and bias saved</div>`;
};

// --- Chat gateway: type here, watch the harness run (turns kept in memory)
const CHAT = [];
// The gate → tools → reply stage strip, shared by the live card and the
// completed/replayed card so the markup can't drift. `live` lights stages up
// (gate flips to done once decided, reply "on" once text streams); otherwise
// every stage is done and the strip carries the .tele class (hidden by the
// stats toggle). (.stages is flexbox, so inter-span whitespace is irrelevant.)
function stagesRow(t, live){
  // A stage that is running is "live", a finished one "ok", one not reached yet "neutral".
  const gateVariant = live && !t.gate ? "live" : "ok";
  const replyVariant = live ? (t.stream ? "live" : "neutral") : "ok";
  const tools = (t.tools||[]).map(x => toolChip(x.tool)).join("");
  // graph chip first — the front door. A quick graph turn has NO gate stage
  // (memory retrieval never ran), so the gate chip is honest and disappears.
  const graph = (t.graph && t.graph.route)
    ? uiBadge("graph · " + esc(t.graph.route), "ok") : "";
  const gate = (t.graph && t.graph.route === "quick") ? ""
    : uiBadge(`gate${t.gate?` · ${esc(t.gate.decision)}`:""}`, gateVariant);
  return `<div class="stages${live?"":" tele"}">`
    + graph + gate + tools + uiBadge("reply", replyVariant) + `</div>`;
}
// The per-turn telemetry footer: seconds · iterations · model · what the
// turn's tools cost (spec 009 C, when any said) · consolidation.
const teleFooter = t => {
  const spent = toolsCost(t.tools);
  return `<div class="meta tele">${secs(t.latency_ms)} · ${t.iterations??"?"} iter${
    t.model?` · ${esc(t.model)}`:""}${spent > 0 ? ` · tools ${money(spent)}` : ""}${
    t.consolidation?` · consolidated ${t.consolidation.new_facts} fact(s)`:""}</div>`;
};

// --- Spec 008 B: what a turn saved, drawn in the chat itself.
//
// Where "Open report" and a kept fact's link go. A report lives in Waku
// Memory and waku.one renders it at /memories/<id>. Framed inside waku.one,
// the link goes to the site that framed us (embed.js knows which, and only
// answers an origin on its allowlist); everywhere else, to www.waku.one.
const WAKU_ONE = "https://www.waku.one";
function memoryUrl(id){
  const framed = typeof embedParentOrigin === "function" ? embedParentOrigin() : null;
  return (framed || WAKU_ONE) + "/memories/" + encodeURIComponent(id);
}
// The `report` event (spec 007 C): {title, memory_id, scope, summary}. The
// summary is the report's own bullets, three at most.
const reportCard = r => !r ? "" : `<div class="report-card">
  <div class="report-kicker">Report saved</div>
  <div class="report-title">${esc(r.title || "Research report")}</div>
  ${(r.summary||[]).length ? `<ul class="mdlist">${r.summary.slice(0, 3).map(b => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}
  ${r.memory_id ? `<a class="btn btn-secondary btn-sm report-open" href="${esc(memoryUrl(r.memory_id))}" target="_blank" rel="noopener noreferrer"
     data-memory-id="${esc(r.memory_id)}" data-title="${esc(r.title || "")}" onclick="return openReport(this)">Open report</a>` : ""}
</div>`;
// Spec 040 M3 (waku-memory), the agent's half: framed inside waku.one,
// "Open report" asks the page around us to open the report in place, with
// embed.js's tellParent, which posts only to the allowlisted origin that
// framed us (spec 008 F), never "*". Not framed, or framed by an origin off
// the list: the link opens a new tab, as before.
function openReport(link){
  const framed = typeof embedParentOrigin === "function" && embedParentOrigin();
  if (!framed || typeof tellParent !== "function") return true;
  tellParent({type: "open-report", memory_id: link.dataset.memoryId || "",
              title: link.dataset.title || ""});
  return false;
}
// Spec 009 A: what the company brain already knew, read before a research
// turn: each memory with the date it was saved, linked to waku.one.
const usedList = used => !(used || []).length ? "" : `<div class="kept">
  <div class="report-kicker">Used from memory</div>
  <ul class="mdlist">${used.map(u => {
    const label = (u.report ? `Report${u.title ? ` "${u.title}"` : ""}: ` : "") + (u.text || "");
    const short = label.length > 160 ? label.slice(0, 159) + "\u2026" : label;
    return `<li>${u.created_at ? `<span class="meta">${esc(u.created_at)}</span> ` : ""}${u.id
      ? `<a href="${esc(memoryUrl(u.id))}" target="_blank" rel="noopener noreferrer">${esc(short)}</a>`
      : esc(short)}</li>`;
  }).join("")}</ul>
</div>`;
// A `consolidation` event's `kept` (spec 006): each fact this turn put in
// memory, linked when Waku Memory gave it an id.
const keptList = c => !(c && (c.kept||[]).length) ? "" : `<div class="kept">
  <div class="report-kicker">Kept in memory</div>
  <ul class="mdlist">${c.kept.map(k => `<li>${k.memory_id
    ? `<a href="${esc(memoryUrl(k.memory_id))}" target="_blank" rel="noopener noreferrer">${esc(k.content)}</a>`
    : esc(k.content)}</li>`).join("")}</ul>
</div>`;

const chatTurnCard = t => uiCard(`
  ${msgCopy(t.reply)}
  ${(t.gate||t.graph)?`${stagesRow(t, false)}
    <div class="meta tele" style="margin:0 0 calc(var(--spacing) * 1.5)">${esc((t.gate&&t.gate.reason)||(t.graph&&t.graph.reason)||"")}</div>`:""}
  ${t.slot?`<div class="meta tele" style="margin:0 0 calc(var(--spacing) * 1.5)">Jev kept ${esc(String(t.slot.kept))} of ${esc(String(t.slot.total))} memories</div>`:""}
  ${nodesRow(t)}
  ${(t.tools||[]).length?`<div class="tele">${(t.tools||[]).map(toolRow).join("")}</div>`:""}
  <div class="r" style="margin-top:var(--space-2)">${renderMarkdown(t.reply)}</div>
  ${reportCard(t.report)}
  ${usedList(t.used)}
  ${keptList(t.consolidation)}
  ${teleFooter(t)}`, {cls: "reply"});

// While a turn runs we stream it live: stages light up as the harness reaches
// them, and the reply text appears token by token (with a blinking caret).
// Graph nodes as chips: lit while running, with their measured time once done.
// Several lit at once IS the fan-out, which no amount of "thinking…" conveys.
const nodesRow = m => {
  const names = Object.keys(m.nodes || {});
  if (!names.length) return "";
  return `<div class="cmp-stats" style="margin:0 0 calc(var(--spacing) * 1.5)">` + names.map(n => {
    const s = m.nodes[n];
    const variant = s.status === "running" ? "live" : s.status === "error" ? "bad" : "value";
    const suffix = s.status === "running" ? "" : s.ms != null ? ` ${s.ms}ms` : "";
    return uiBadge(esc(n) + suffix, variant);
  }).join("") + `</div>`;
};

const streamingCard = m => uiCard(`
  ${stagesRow(m, true)}
  ${nodesRow(m)}
  ${m.gate&&m.gate.reason?`<div class="meta" style="margin:0 0 calc(var(--spacing) * 1.5)">${esc(m.gate.reason)}</div>`:""}
  ${(m.tools||[]).map(toolRow).join("")}
  ${reportCard(m.report)}
  ${m.stream
     ? `<div class="r" style="margin-top:var(--space-2)">${renderMarkdown(m.stream)}<span class="caret"></span></div>`
     : `<div class="meta" style="margin:0">thinking&hellip;${m.started?` ${Math.round((Date.now()-m.started)/1000)}s`:""}${
         m.started && Date.now()-m.started > 20000
         ? `<br>still waiting: slow models (free tiers especially) can queue for a while; this errors out at the WAKU_LLM_TIMEOUT limit instead of hanging forever`
         : ""}</div>`}`, {cls: "reply"});

// Messages loaded from history (a switched/opened conversation) have no live
// latency/iteration data.
const historicalCard = m => uiCard(`
  ${msgCopy(m.reply)}
  <div class="r">${renderMarkdown(m.reply)}</div>`, {cls: "reply"});

function renderChatLog(){
  if (!CHAT.length)
    return `<div class="empty" style="padding:calc(var(--spacing) * 1.5) calc(var(--spacing) * 0.5)">${
      document.body.classList.contains("embed")   // the embedded chat has no tabs to point at
      ? "Message Waku here. Every tool call and reply shows as it runs."
      : "Message Waku here from any tab. Open Overview to watch it flow through the harness, or the Gateway tab to see every channel's messages together."}</div>`;
  return CHAT.map(m => m.role==="user"
      ? `<div class="bubble">${esc(m.text)}</div>`
      : m.pending ? streamingCard(m)
      : m.historical ? historicalCard(m)
      : chatTurnCard(m)).join("");
}

function syncChatLogs(){
  // one conversation, two surfaces: the Chat & watch tab and the side dock
  document.querySelectorAll(".chatlog").forEach(el => {
    el.innerHTML = renderChatLog();
    el.scrollTop = el.scrollHeight;      // dock scrolls its own container
  });
}

// One streamed harness event updates the live card in place.
function applyStreamEvent(pending, ev){
  // Graph events arrive here too when a workflow is called from the chat box.
  // The trace poller animates the chart either way, but it runs every 450ms
  // and stages play on a 620ms stagger — going straight to graphLive() means
  // the Overview panel swaps to the running workflow the moment you hit send.
  if (ev.kind === "graph_start" && typeof graphLive === "function") graphLive(ev.workflow);
  else if (ev.kind === "graph_end" && typeof graphLive === "function") graphLive(null);
  // Graph nodes are not ToolRegistry tools, so none of them ever reached the
  // tool chips — a /gather ran for thirteen seconds showing nothing but
  // "thinking…". Track them separately and render them the same way, because
  // "which four things are happening right now" is the entire point of a wave.
  if (ev.kind === "node_start"){
    (pending.nodes = pending.nodes || {})[ev.node] = {status: "running"};
  } else if (ev.kind === "node_end"){
    (pending.nodes = pending.nodes || {})[ev.node] =
      {status: ev.error ? "error" : "done", ms: ev.ms};
  }
  if (ev.kind === "report") pending.report = ev;
  else if (ev.kind === "consolidation") pending.consolidation = ev;
  else if (ev.kind === "gate") pending.gate = {decision: ev.decision, reason: ev.reason};
  else if (ev.kind === "route")
    pending.graph = {route: ev.target === "quick_reply" ? "quick" : "full",
                     reason: (pending.graph || {}).reason};
  else if (ev.kind === "triage") (pending.graph = pending.graph || {}).reason = ev.reason;
  else if (ev.kind === "text") pending.stream = (pending.stream || "") + (ev.delta || "");
  else if (ev.kind === "tool"){
    (pending.tools = pending.tools || []).push({
      tool: ev.tool, args: ev.args, output: ev.output, read_first: ev.read_first,
      status: (ev.output||"").toLowerCase().startsWith("error") ? "error" : "ok",
      summary: (ev.output || "").split(". ")[0].slice(0,120)});
    pending.stream = "";   // a new assistant turn begins after the tool result
  } else if (ev.kind === "done"){
    pending.pending = false; pending.stream = "";
    if (ev.error) pending.reply = "Error: " + ev.error;
    else Object.assign(pending, ev, {report: ev.report || pending.report,
                                     consolidation: ev.consolidation || pending.consolidation});
    // reply, tools, gate, iterations, latency_ms, consolidation, report
  }
}

// The composer is a <textarea> (index.html) so a long message wraps instead of
// scrolling sideways. Size it to its content on every change, up to the
// max-height in style.css. Call this anywhere .value is set from code too -
// a textarea does not resize itself.
function autogrow(el){
  if (!el || el.tagName !== "TEXTAREA") return;
  el.style.height = "auto";
  // box-sizing is border-box globally (style.css:1) but scrollHeight excludes
  // the border, so height alone lands 2px short and grows a scrollbar on every
  // keystroke. Measure the border back on.
  const cs = getComputedStyle(el);
  const border = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
  el.style.height = (el.scrollHeight + border) + "px";
}

// Who else hears a turn: every streamed event, then {kind: "turn_end"} once
// the stream is over, whatever way it ended. Empty on the dashboard; the
// embedded chat (embed.js) adds the one that tells waku.one.
const turnWatchers = [];
const tellWatchers = ev => turnWatchers.forEach(w => { try { w(ev); } catch(e){} });

async function sendChat(fromInput){
  const input = fromInput || document.getElementById("msg") || document.getElementById("dmsg");
  const text = (input && input.value || "").trim();
  if (!text) return;
  input.value = "";
  autogrow(input);          // an emptied box must shrink back to one row
  CHAT.push({role:"user", text});
  const pending = {role:"waku", pending:true, stream:"", started: Date.now()};
  CHAT.push(pending);
  syncChatLogs();
  // tick the elapsed counter while we wait for the first token
  const ticker = setInterval(() => { if (pending.pending && !pending.stream) syncChatLogs(); }, 1000);
  try {
    const res = await fetch("/api/chat/stream", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({message:text})});
    noteStatus(res.status);
    // A refusal that is not a stream (an ended session answers 401 with a
    // JSON body) has no data: frames, so read its sentence instead of
    // leaving an empty card.
    if (!res.ok && !(res.headers.get("Content-Type") || "").startsWith("text/event-stream")){
      let body = null;
      try { body = await res.json(); } catch(e){ /* not JSON */ }
      throw (body && body.error) || ("HTTP " + res.status);
    }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;){
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0){
        const line = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!line.startsWith("data:")) continue;
        try {
          const ev = JSON.parse(line.slice(5).trim());
          applyStreamEvent(pending, ev);
          tellWatchers(ev);
        } catch(e){}
        syncChatLogs();
      }
    }
  } catch(e){ Object.assign(pending, {pending:false, reply:"Error: "+e}); }
  clearInterval(ticker);
  if (pending.pending) pending.pending = false;   // stream ended without a 'done'
  syncChatLogs();
  tellWatchers({kind: "turn_end"});
  input.focus();
  // "Paused. Send a message to wake it." — this IS that message, and the turn
  // above already woke the container. Nothing else pulls /api/data again, so
  // without this the banner keeps saying "paused" while the reply sits in the
  // dock and every card on the page stays frozen on pre-pause data for the
  // life of the page. User-driven on purpose: no background header.
  if (paused) await refresh();
}
// The composer alone: Send, Enter, autogrow. Shared by the dashboard's dock
// (wireDock, below) and the embedded chat (embed.js), which has no dock to
// open or close.
function wireComposer(){
  const b = document.getElementById("dsend"), i = document.getElementById("dmsg");
  if (b) b.onclick = () => sendChat(i);
  // Enter sends; Shift+Enter is a real newline now that this is a textarea.
  // preventDefault stops the sending keystroke also inserting that newline.
  if (i) i.onkeydown = e => {
    if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); sendChat(i); }
  };
  if (i) i.oninput = () => autogrow(i);
}
function wireDock(){
  wireComposer();
  const close = document.getElementById("dock-close"), reopen = document.getElementById("dock-reopen");
  const setClosed = v => { document.body.classList.toggle("dock-closed", v); localStorage.setItem("dockClosed", v?"1":"0"); };
  if (close) close.onclick = () => setClosed(true);
  if (reopen) reopen.onclick = () => setClosed(false);
  const saved = localStorage.getItem("dockClosed");
  setClosed(saved === null ? window.innerWidth < 1180 : saved === "1");
  syncChatLogs();
}

