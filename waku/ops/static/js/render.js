// waku dashboard — formatters + chat card renderers + chatlog + streaming + send.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

const money = n => "$" + (n < 0.01 ? n.toFixed(4) : n.toFixed(2));
const secs = ms => ms==null ? "—" : (ms/1000).toFixed(1)+"s";

const gateBadge = g => !g ? "" :
  uiBadge("gate · " + esc(g.decision), g.decision === "retrieve" ? "live" : "neutral")
  + `<span class="meta" style="margin:0">${esc(g.reason||"")}</span>`;

// A tool call renders as a status row (dot + one-line summary); the raw output
// hides behind a disclosure so an ugly osascript error never floods the page.
const toolRow = x => `<div class="tool ${x.status||"ok"}">
  <div class="tool-head"><span class="dot ${x.status||"ok"}"></span><code>${esc(x.tool)}</code>
    ${x.summary?`<span style="color:var(--text-muted)">${esc(x.summary)}</span>`:""}</div>
  ${x.output!==undefined?`<details><summary>args &amp; raw output</summary>
    <pre>${esc(x.tool)}(${esc(JSON.stringify(x.args,null,1))})\n\n${esc(x.output)}</pre>
  </details>`:""}
</div>`;

// A stored history row -> a CHAT item. Assistant rows with saved telemetry
// (meta: gate/latency/iterations/tools) render as the FULL turn card, so a
// reopened thread looks just like when it was live. Rows without meta (from
// before this was saved, or another gateway) fall back to a plain card.
function histItem(m){
  if (m.role === "user") return {role:"user", text:m.content};
  if (m.meta) return {role:"waku", reply:m.content, gate:m.meta.gate,
                      graph:m.meta.graph,
                      tools:m.meta.tools, iterations:m.meta.iterations,
                      latency_ms:m.meta.latency_ms, model:m.meta.model};
  return {role:"waku", reply:m.content, historical:true};
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
// The per-turn telemetry footer: seconds · iterations · model · consolidation.
const teleFooter = t => `<div class="meta tele">${secs(t.latency_ms)} · ${t.iterations??"?"} iter${
  t.model?` · ${esc(t.model)}`:""}${t.consolidation?` · consolidated ${t.consolidation.new_facts} fact(s)`:""}</div>`;

const chatTurnCard = t => uiCard(`
  ${msgCopy(t.reply)}
  ${(t.gate||t.graph)?`${stagesRow(t, false)}
    <div class="meta tele" style="margin:0 0 calc(var(--spacing) * 1.5)">${esc((t.gate&&t.gate.reason)||(t.graph&&t.graph.reason)||"")}</div>`:""}
  ${nodesRow(t)}
  ${(t.tools||[]).length?`<div class="tele">${(t.tools||[]).map(toolRow).join("")}</div>`:""}
  <div class="r" style="margin-top:var(--space-2)">${renderMarkdown(t.reply)}</div>
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
  ${m.stream
     ? `<div class="r" style="margin-top:var(--space-2)">${renderMarkdown(m.stream)}<span class="caret"></span></div>`
     : `<div class="meta" style="margin:0">thinking&hellip;${m.started?` ${Math.round((Date.now()-m.started)/1000)}s`:""}${
         m.started && Date.now()-m.started > 20000
         ? `<br>still waiting: slow models (free tiers especially) can queue for a while; this errors out at the WAKU_LLM_TIMEOUT limit instead of hanging forever`
         : ""}</div>`}`, {cls: "reply"});

// Messages loaded from history (a switched/opened conversation) have no live
// latency/iteration data, and their stored form carries an internal
// "[tools used: ...]" annotation — strip both so the thread reads cleanly.
const stripTools = t => (t || "").replace(/\s*\[tools used:[\s\S]*\]\s*$/, "").trim();
const historicalCard = m => uiCard(`
  ${msgCopy(stripTools(m.reply))}
  <div class="r">${renderMarkdown(stripTools(m.reply))}</div>`, {cls: "reply"});

function renderChatLog(){
  if (!CHAT.length)
    return `<div class="empty" style="padding:calc(var(--spacing) * 1.5) calc(var(--spacing) * 0.5)">Message Waku here from any tab. Open Overview to watch it flow through the harness, or the Gateway tab to see every channel's messages together.</div>`;
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
  if (ev.kind === "gate") pending.gate = {decision: ev.decision, reason: ev.reason};
  else if (ev.kind === "route")
    pending.graph = {route: ev.target === "quick_reply" ? "quick" : "full",
                     reason: (pending.graph || {}).reason};
  else if (ev.kind === "triage") (pending.graph = pending.graph || {}).reason = ev.reason;
  else if (ev.kind === "text") pending.stream = (pending.stream || "") + (ev.delta || "");
  else if (ev.kind === "tool"){
    (pending.tools = pending.tools || []).push({
      tool: ev.tool, args: ev.args, output: ev.output,
      status: (ev.output||"").toLowerCase().startsWith("error") ? "error" : "ok",
      summary: (ev.output || "").split(". ")[0].slice(0,120)});
    pending.stream = "";   // a new assistant turn begins after the tool result
  } else if (ev.kind === "done"){
    pending.pending = false; pending.stream = "";
    if (ev.error) pending.reply = "Error: " + ev.error;
    else Object.assign(pending, ev);   // reply, tools, gate, iterations, latency_ms, consolidation
  }
}

async function sendChat(fromInput){
  const input = fromInput || document.getElementById("msg") || document.getElementById("dmsg");
  const text = (input && input.value || "").trim();
  if (!text) return;
  input.value = "";
  CHAT.push({role:"user", text});
  const pending = {role:"waku", pending:true, stream:"", started: Date.now()};
  CHAT.push(pending);
  syncChatLogs();
  // tick the elapsed counter while we wait for the first token
  const ticker = setInterval(() => { if (pending.pending && !pending.stream) syncChatLogs(); }, 1000);
  try {
    const res = await fetch("/api/chat/stream", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({message:text})});
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
        try { applyStreamEvent(pending, JSON.parse(line.slice(5).trim())); } catch(e){}
        syncChatLogs();
      }
    }
  } catch(e){ Object.assign(pending, {pending:false, reply:"Error: "+e}); }
  clearInterval(ticker);
  if (pending.pending) pending.pending = false;   // stream ended without a 'done'
  syncChatLogs();
  input.focus();
}
function wireDock(){
  const b = document.getElementById("dsend"), i = document.getElementById("dmsg");
  if (b) b.onclick = () => sendChat(i);
  if (i) i.onkeydown = e => { if (e.key==="Enter") sendChat(i); };
  const close = document.getElementById("dock-close"), reopen = document.getElementById("dock-reopen");
  const setClosed = v => { document.body.classList.toggle("dock-closed", v); localStorage.setItem("dockClosed", v?"1":"0"); };
  if (close) close.onclick = () => setClosed(true);
  if (reopen) reopen.onclick = () => setClosed(false);
  const saved = localStorage.getItem("dockClosed");
  setClosed(saved === null ? window.innerWidth < 1180 : saved === "1");
  syncChatLogs();
}

