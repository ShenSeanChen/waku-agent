// waku dashboard — formatters + chat card renderers + chatlog + streaming + send.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

const money = n => "$" + (n < 0.01 ? n.toFixed(4) : n.toFixed(2));
const secs = ms => ms==null ? "—" : (ms/1000).toFixed(1)+"s";

const gateBadge = g => !g ? "" :
  `<span class="badge ${g.decision==="retrieve"?"retrieve":""}">gate · ${esc(g.decision)}</span><span class="meta" style="margin:0">${esc(g.reason||"")}</span>`;

// A tool call renders as a status row (dot + one-line summary); the raw output
// hides behind a disclosure so an ugly osascript error never floods the page.
const toolRow = x => `<div class="tool ${x.status||"ok"}">
  <div class="tool-head"><span class="dot ${x.status||"ok"}"></span><code>${esc(x.tool)}</code>
    ${x.summary?`<span style="color:var(--ink2)">${esc(x.summary)}</span>`:""}</div>
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

const turnCard = t => `<div class="card">
  <div class="u">${esc(t.user_message)}</div>
  <div class="meta" style="margin-top:4px">${gateBadge(t.gate)}</div>
  ${(t.tools||[]).map(toolRow).join("")}
  <div class="r">${renderMarkdown(t.reply)}</div>
  <div class="meta">${esc((t.ts||"").replace("T"," ").slice(0,19))} · ${secs(t.latency_ms)} · ${t.iterations??"?"} iter · ${money(t.cost||0)}${t.consolidation?` · consolidated ${t.consolidation.new_facts} fact(s)`:""}</div>
</div>`;

const table = (heads, rows) => rows.length
  ? `<div class="card" style="padding:4px 8px"><table><tr>${heads.map(h=>`<th>${h}</th>`).join("")}</tr>${rows.join("")}</table></div>`
  : `<div class="card empty">nothing here yet</div>`;

const gateSplit = s => {
  if (!(s.gate_skips + s.gate_retrieves))
    return `<div class="splitbar"><div class="seg-skip" style="width:100%;opacity:.35"></div></div>
      <div class="meta" style="margin-top:6px">no turns yet — send a message and the gate starts deciding</div>`;
  const tot = s.gate_skips + s.gate_retrieves;
  const skipPct = Math.round(s.gate_skips/tot*100), retPct = 100-skipPct;
  // only label a segment when it's wide enough to fit the text — otherwise a
  // 0%/tiny segment spills its label past the bar (the "0 retri" bug).
  const seg = (cls, n, label, pct) =>
    `<div class="${cls}" style="width:${pct}%">${pct>=14?`${n} ${label}`:""}</div>`;
  return `<div class="splitbar">
    ${seg("seg-skip", s.gate_skips, "skipped", skipPct)}
    ${seg("seg-ret", s.gate_retrieves, "retrieved", retPct)}
  </div><div class="meta" style="margin-top:6px">the retrieval gate skipped memory on ${skipPct}% of turns — that's latency and bias saved</div>`;
};

// --- Chat gateway: type here, watch the harness run (turns kept in memory)
const CHAT = [];
// The gate → tools → reply stage strip, shared by the live card and the
// completed/replayed card so the markup can't drift. `live` lights stages up
// (gate flips to done once decided, reply "on" once text streams); otherwise
// every stage is done and the strip carries the .tele class (hidden by the
// stats toggle). (.stages is flexbox, so inter-span whitespace is irrelevant.)
function stagesRow(t, live){
  const gateCls = live ? (t.gate ? "done" : "on") : "done";
  const replyCls = live ? (t.stream ? "on" : "") : "done";
  const tools = (t.tools||[]).map(x => toolChip(x.tool)).join("");
  // graph chip first — the front door. A quick graph turn has NO gate stage
  // (memory retrieval never ran), so the gate chip is honest and disappears.
  const graph = (t.graph && t.graph.route)
    ? `<span class="stage done">graph · ${esc(t.graph.route)}</span>` : "";
  const gate = (t.graph && t.graph.route === "quick") ? ""
    : `<span class="stage ${gateCls}">gate${t.gate?` · ${esc(t.gate.decision)}`:""}</span>`;
  return `<div class="stages${live?"":" tele"}">`
    + graph + gate + tools + `<span class="stage ${replyCls}">reply</span></div>`;
}
// The per-turn telemetry footer: seconds · iterations · model · consolidation.
const teleFooter = t => `<div class="meta tele">${secs(t.latency_ms)} · ${t.iterations??"?"} iter${
  t.model?` · ${esc(t.model)}`:""}${t.consolidation?` · consolidated ${t.consolidation.new_facts} fact(s)`:""}</div>`;

const chatTurnCard = t => `<div class="card">
  <button class="msg-copy" onclick="copyMsg(this)" data-text="${esc(t.reply)}" title="Copy reply">Copy</button>
  ${(t.gate||t.graph)?`${stagesRow(t, false)}
    <div class="meta tele" style="margin:0 0 6px">${esc((t.gate&&t.gate.reason)||(t.graph&&t.graph.reason)||"")}</div>`:""}
  ${nodesRow(t)}
  ${(t.tools||[]).length?`<div class="tele">${(t.tools||[]).map(toolRow).join("")}</div>`:""}
  <div class="r" style="margin-top:8px">${renderMarkdown(t.reply)}</div>
  ${teleFooter(t)}
</div>`;

// While a turn runs we stream it live: stages light up as the harness reaches
// them, and the reply text appears token by token (with a blinking caret).
// Graph nodes as chips: lit while running, with their measured time once done.
// Several lit at once IS the fan-out, which no amount of "thinking…" conveys.
const nodesRow = m => {
  const names = Object.keys(m.nodes || {});
  if (!names.length) return "";
  return `<div class="cmp-stats" style="margin:0 0 6px">` + names.map(n => {
    const s = m.nodes[n];
    const cls = s.status === "running" ? "chip live" : s.status === "error" ? "chip err" : "chip";
    const suffix = s.status === "running" ? "" : s.ms != null ? ` ${s.ms}ms` : "";
    return `<span class="${cls}">${esc(n)}${suffix}</span>`;
  }).join("") + `</div>`;
};

const streamingCard = m => `<div class="card">
  ${stagesRow(m, true)}
  ${nodesRow(m)}
  ${m.gate&&m.gate.reason?`<div class="meta" style="margin:0 0 6px">${esc(m.gate.reason)}</div>`:""}
  ${(m.tools||[]).map(toolRow).join("")}
  ${m.stream
     ? `<div class="r" style="margin-top:8px">${renderMarkdown(m.stream)}<span class="caret"></span></div>`
     : `<div class="meta" style="margin:0">thinking&hellip;${m.started?` ${Math.round((Date.now()-m.started)/1000)}s`:""}${
         m.started && Date.now()-m.started > 20000
         ? `<br>still waiting: slow models (free tiers especially) can queue for a while; this errors out at the WAKU_LLM_TIMEOUT limit instead of hanging forever`
         : ""}</div>`}
</div>`;

// Messages loaded from history (a switched/opened conversation) have no live
// latency/iteration data, and their stored form carries an internal
// "[tools used: ...]" annotation — strip both so the thread reads cleanly.
const stripTools = t => (t || "").replace(/\s*\[tools used:[\s\S]*\]\s*$/, "").trim();
const historicalCard = m => `<div class="card">
  <button class="msg-copy" onclick="copyMsg(this)" data-text="${esc(stripTools(m.reply))}" title="Copy reply">Copy</button>
  <div class="r">${renderMarkdown(stripTools(m.reply))}</div>
</div>`;

function renderChatLog(){
  if (!CHAT.length)
    return `<div class="empty" style="padding:6px 2px">Message Waku here from any tab. Open Overview to watch it flow through the harness, or the Gateway tab to see every channel's messages together.</div>`;
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

