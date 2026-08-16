// waku dashboard — graph workflows: the topology chart + its live animation.
// Split out: classic <script>, shared global scope. Load order: static/README.md.
//
// The chart is DATA-DRIVEN: it renders Graph.describe() served in /api/data
// (d.graph.workflows), so the picture is provably the topology the engine
// runs — the anti-drift lesson learned from archSVG's byte-freeze. Never
// hand-edit a workflow's shape here; change the workflow and this follows.
// Ids are namespaced "g-" so they can never collide with archSVG's ids.

// --- layered layout: START at the left, each node one column after its
// furthest predecessor. Small graphs only (triage is 5 nodes) — no library.
function graphLayout(wf){
  const names = ["START", ...wf.nodes.map(n => n.name), "END"];
  const layer = {START: 0};
  for (let pass = 0; pass < names.length; pass++)     // relax until stable
    wf.edges.forEach(e => {
      const src = layer[e.src] ?? 0;
      layer[e.dst] = Math.max(layer[e.dst] ?? 0, src + 1);
    });
  const cols = [];
  names.forEach(n => {
    if (layer[n] == null) layer[n] = 0;
    (cols[layer[n]] = cols[layer[n]] || []).push(n);
  });
  return {layer, cols: cols.filter(c => c && c.length)};
}

function graphSVG(wf, opts = {}){
  const {cols} = graphLayout(wf);
  const kinds = Object.fromEntries(wf.nodes.map(n => [n.name, n.kind]));
  const W = 168, H = 52, GX = 74, GY = 22, PAD = 14;
  const height = Math.max(...cols.map(c => c.length)) * (H + GY) - GY + PAD * 2;
  const width = cols.length * (W + GX) - GX + PAD * 2;
  const pos = {};
  cols.forEach((col, ci) => col.forEach((n, ri) => {
    const colH = col.length * (H + GY) - GY;
    pos[n] = {x: PAD + ci * (W + GX), y: PAD + (height - PAD * 2 - colH) / 2 + ri * (H + GY)};
  }));
  // Ids carry the workflow name. START and END exist in EVERY workflow, so on a
  // page showing two charts an un-namespaced `g-START` lit both of them at once
  // — and hot() deliberately hits every match, so the bug looked like a feature.
  const nid = n => `g-${wf.name}-${n}`;
  // Both labels used to overclaim. "local read" was true for triage's calendar
  // peek and false for gather's scan_github (a subprocess) and scan_web (the
  // network) — what tool nodes share is that NO MODEL RUNS. And "small model"
  // was true of triage's classify and false of gather's synthesize, which uses
  // the main one; the kind alone does not know which, so do not claim.
  const SUB = {llm: "one model call", agent: "THE loop, as a node", tool: "code, no model", fn: ""};
  const nodeBox = n => {
    const p = pos[n];
    if (n === "START" || n === "END")
      return `<g class="node" data-node="${nid(n)}">
        <rect class="bx" x="${p.x + W/2 - 34}" y="${p.y + H/2 - 15}" width="68" height="30" rx="15"/>
        <text class="nt" x="${p.x + W/2}" y="${p.y + H/2 + 5}" text-anchor="middle" style="font-size:12px">${n}</text></g>`;
    const sub = SUB[kinds[n]] || "";
    return `<g class="node" data-node="${nid(n)}">
      <rect class="bx" x="${p.x}" y="${p.y}" width="${W}" height="${H}" rx="9"/>
      <text class="nt" x="${p.x + 12}" y="${p.y + 22}">${esc(n)}</text>
      ${sub ? `<text class="ns" x="${p.x + 12}" y="${p.y + 39}">${sub}</text>` : ""}</g>`;
  };
  const edgeLine = e => {
    const a = pos[e.src], b = pos[e.dst];
    const x1 = a.x + (e.src === "START" ? W/2 + 34 : W), y1 = a.y + H/2;
    const x2 = b.x + (e.dst === "END" ? W/2 - 34 : 0), y2 = b.y + H/2;
    const mx = (x1 + x2) / 2;
    return `<path class="flow${e.conditional ? " dash" : ""}" data-edge="g-${wf.name}-${e.src}-${e.dst}"
      d="M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}" marker-end="url(#garr)"/>`;
  };
  return `<div style="overflow-x:auto"><svg viewBox="0 0 ${width} ${height}" class="arch graphchart"
      style="max-width:${width}px" role="img">
    <defs><marker id="garr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
      orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="head"/></marker></defs>
    ${wf.edges.map(edgeLine).join("")}
    ${["START", ...wf.nodes.map(n => n.name), "END"].map(nodeBox).join("")}
  </svg></div>`;
}

// Which workflow is running RIGHT NOW, set by graph_start and cleared by
// graph_end. The Overview panel used to read the last COMPLETED run, so during
// a gather it still showed triage — and since animateGraphStage only lights a
// chart that is on screen, nothing lit up either. One wrong chart caused both
// bugs: you saw the old shape, and you saw it stay dark.
let GRAPH_LIVE = null;
// The workflow to keep showing once a run ENDS. Without it the panel fell back
// to d.graph.runs[0] the instant graph_end fired — and that payload is only
// refreshed by the /api/data poll, so for one beat it still named the PREVIOUS
// run. Observed as: swap to gather, flick back to triage, then back to gather
// when the poll caught up. Remembering locally means the panel never shows a
// workflow older than the one it just watched.
let GRAPH_SHOWN = null;

// --- the compact Overview panel: the harness auto-decides, this reflects it.
function graphPanel(d){
  const g = d.graph || {enabled: false, workflows: [], runs: [], stats: {quick: 0, full: 0}};
  // Overview is a STATUS surface — "what just happened" — while the Graph tab
  // is a reference one: "what shapes exist". So this shows the workflow that
  // most recently RAN, from the trace. Pinning it to workflows[0] meant Overview
  // showed triage forever, seconds after a gather, which is why the panel read
  // as leftovers rather than as news.
  // A run in flight wins over the last finished one — during a gather you want
  // to watch the gather, not read about the triage that came before it.
  const showing = GRAPH_LIVE || GRAPH_SHOWN || ((g.runs || [])[0] || {}).workflow;
  const last = (g.runs || [])[0];
  const wf = (g.workflows || []).find(w => w && w.name === showing)
             || (g.workflows || [])[0];
  const tot = g.stats.quick + g.stats.full;
  const seg = (cls, n, label, pct) =>
    `<div class="${cls}" style="width:${pct}%">${pct >= 14 ? `${n} ${label}` : ""}</div>`;
  const split = !tot
    ? `<div class="meta" style="margin:6px 0 10px">no graph turns yet — every message will route here once it's on</div>`
    : `<div class="splitbar">
        ${seg("seg-skip", g.stats.quick, "quick", Math.round(g.stats.quick / tot * 100))}
        ${seg("seg-ret", g.stats.full, "full", 100 - Math.round(g.stats.quick / tot * 100))}
      </div><div class="meta" style="margin:6px 0 10px">${g.stats.quick} answered by the small model alone — the loop never woke</div>`;
  // The flag gates TRIAGE — the per-message door — and nothing else. `waku
  // gather` is a routine you start yourself and runs regardless, so the old
  // copy ("off = every turn runs the classic loop") was quietly false the
  // moment a second workflow existed.
  if (!g.enabled && !last)
    return `<div class="card"><div class="meta">The per-message graph door is <b>off</b> — every chat turn
      runs the classic loop above. Switch on <b>graph workflows</b> in
      <a class="reveal" onclick="location.hash='settings'">Behaviour</a> to triage each message first.
      Workflows you run yourself, like <code>make gather</code>, do not need the flag —
      <a class="reveal" onclick="location.hash='graph'">see them here</a>.</div></div>`;
  const when = GRAPH_LIVE
    ? `<span class="live-dot"></span><b>${esc(GRAPH_LIVE)}</b> running now`
    : last
    ? `last run: <b>${esc(last.workflow || "")}</b>${last.ms ? ` · ${(last.ms/1000).toFixed(1)}s` : ""}${
        last.steps ? ` · ${last.steps} nodes` : ""}`
    : "live — nodes light up as a turn flows through";
  return `<div class="card" style="cursor:pointer" onclick="location.hash='graph'">
    ${g.enabled ? split : ""}${wf ? graphSVG(wf) : ""}
    <div class="meta" style="margin-top:8px">${when} · click for the full story</div></div>`;
}

// --- live animation: same machinery as the loop's STAGE map. hot() lights
// every copy on the page, so the Overview panel and the Graph tab glow together.
const GRAPH_KINDS = new Set(["graph_start", "node_start", "node_end", "route", "graph_end"]);
function animateGraphStage(ev){
  if (!document.querySelector(".graphchart")) return;
  const status = t => document.querySelectorAll(".arch-status").forEach(
    st => st.innerHTML = `<span class="live-dot"></span>${t}`);
  // Every graph event carries `workflow`, so the ids can be scoped to the chart
  // that is actually running instead of lighting every chart on the page.
  const w = ev.workflow || "";
  if (ev.type === "graph_start"){
    // Swap the Overview chart to this workflow before anything runs, so the
    // nodes about to light up are the ones on screen.
    graphLive(w);
    status(`${w} starts`);
    hot(`[data-node="g-${w}-START"]`, "hot", 1000);
  }
  else if (ev.type === "node_start"){
    status(`${w} · ${ev.node}`);
    // Held, not pulsed: a node is lit for as long as it is WORKING. Pulsing on
    // node_end only ever showed you what had already finished, which is the
    // opposite of watching it happen — and with four nodes in one wave it is
    // the difference between seeing a fan-out and seeing four blinks.
    document.querySelectorAll(`[data-node="g-${w}-${ev.node}"]`)
      .forEach(el => el.classList.add("hot"));
  }
  else if (ev.type === "node_end"){
    document.querySelectorAll(`[data-node="g-${w}-${ev.node}"]`)
      .forEach(el => el.classList.remove("hot"));
    hot(`[data-node="g-${w}-${ev.node}"]`, "done", 900);
  }
  else if (ev.type === "route"){
    status(`route → ${ev.target}`);
    hot(`[data-edge="g-${w}-${ev.router}-${ev.target}"]`, "live", 1400);
    hot(`[data-node="g-${w}-${ev.target}"]`, "hot", 1400);
  }
  else if (ev.type === "graph_end"){
    hot(`[data-node="g-${w}-END"]`, "hot", 1000);
    graphLive(null);
  }
}

// Setting the live workflow re-renders, so the panel swaps the moment a run
// starts rather than at the next poll.
function graphLive(name){
  if (GRAPH_LIVE === name) return;
  if (name) GRAPH_SHOWN = name;   // sticky: outlives the run, so no flick-back
  GRAPH_LIVE = name;
  if (typeof render === "function") render();
}

// ---------------------------------------------------------------------------
// THE RUNNER — N nodes as a row of live cards.
//
// The topology chart above shows the SHAPE: "these four are independent". It
// cannot show that they actually ran together, because a picture has no time
// axis and a viewer cannot tell four boxes lit at once from four lit very fast
// in sequence. So the cards carry the time: they start together, tick while
// running, and finish out of order. Watching three settle while one spins is
// the proof the chart can only promise.
//
// Same shape as the Arena, deliberately — arena.py tags every event with `spec`
// and routes it to a card; the graph engine already tags every event with
// `node`. Swap the key, reuse .cmp-grid/.cmp-col, and it reads as a sibling
// because it is one.
let graphRun = {running: false, workflow: "", nodes: {}, order: [], waves: [],
                digest: "", draft: "", error: "", ticker: null};

function graphResetRun(workflow){
  graphRun = {running: true, workflow, nodes: {}, order: [], waves: [],
              digest: "", draft: "", error: "", ticker: graphRun.ticker};
}

function graphApplyEvent(ev){
  const R = graphRun;
  const k = ev.kind;
  if (k === "graph_start"){
    R.order = ev.nodes || [];
    R.order.forEach(n => R.nodes[n] = {status: "waiting"});
  } else if (k === "node_start"){
    // A wave is "the nodes that started before any of them finished". That is
    // exactly what the engine means by a wave, and it is what the row groups by.
    const open = R.waves[R.waves.length - 1];
    if (open && !open.closed) open.nodes.push(ev.node);
    else R.waves.push({nodes: [ev.node], closed: false});
    R.nodes[ev.node] = {status: "running", startedAt: performance.now()};
  } else if (k === "node_end"){
    const w = R.waves[R.waves.length - 1];
    if (w) w.closed = true;   // first finish closes the wave for new members
    R.nodes[ev.node] = {status: ev.error ? "error" : "done", ms: ev.ms,
                        keys: ev.keys || [], error: ev.error || ""};
  } else if (k === "route"){
    R.route = {target: ev.target, reason: ev.reason};
  } else if (k === "graph_end"){
    R.running = false; R.totalMs = ev.ms;
  } else if (k === "done"){
    R.running = false;
    R.digest = ev.digest || ""; R.draft = ev.draft_path || ""; R.error = ev.error || "";
  }
}

async function runGraph(workflow){
  if (graphRun.running) return;
  graphResetRun(workflow);
  // Without a ticker the elapsed numbers freeze and the cards look identical to
  // a sequential run — the one thing this view exists to disprove.
  clearInterval(graphRun.ticker);
  graphRun.ticker = setInterval(() => { if (graphRun.running) render(); }, 100);
  render();
  try {
    const res = await fetch("/api/graph/stream", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({workflow}),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;){
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const p of parts){
        const line = p.trim();
        if (!line.startsWith("data:")) continue;
        try { graphApplyEvent(JSON.parse(line.slice(5))); } catch (e) { /* partial frame */ }
        render();
      }
    }
  } catch (e){
    graphRun.error = String(e);
  } finally {
    graphRun.running = false;
    clearInterval(graphRun.ticker);
    render();
  }
}

function graphCol(name){
  const n = graphRun.nodes[name] || {status: "waiting"};
  if (n.status === "waiting")
    return `<div class="cmp-col" style="opacity:.5"><div class="cmp-h"><b>${esc(name)}</b></div>
      <div class="meta">queued</div></div>`;
  if (n.status === "running"){
    const el = ((performance.now() - n.startedAt) / 1000).toFixed(1);
    return `<div class="cmp-col"><div class="cmp-h"><b>${esc(name)}</b></div>
      <div class="meta"><span class="live-dot"></span>${el}s</div></div>`;
  }
  if (n.status === "error")
    return `<div class="cmp-col err"><div class="cmp-h"><b>${esc(name)}</b></div>
      <div class="meta" style="color:var(--bad)">${esc(n.error)}</div></div>`;
  // The bar is scaled to the SLOWEST node in this node's wave, and every faster
  // node prints what it spent waiting at the barrier. That number is the honest
  // cost of wave execution — printing it teaches more than hiding it would.
  const wave = graphRun.waves.find(w => w.nodes.includes(name));
  const peers = (wave ? wave.nodes : [name]).map(x => (graphRun.nodes[x] || {}).ms || 0);
  const slowest = Math.max(...peers, 1);
  const pct = Math.round((n.ms || 0) / slowest * 100);
  const waited = slowest - (n.ms || 0);
  return `<div class="cmp-col"><div class="cmp-h"><b>${esc(name)}</b>
      <span class="chip">${n.ms}ms</span></div>
    <div class="wavebar"><i style="width:${pct}%"></i></div>
    <div class="meta">${waited > 20 && peers.length > 1
      ? `waited ${(waited/1000).toFixed(1)}s at the barrier`
      : (peers.length > 1 ? "set the pace for this wave" : "")}</div>
    <div class="meta">${(n.keys || []).map(k => `<span class="chip">${esc(k)}</span>`).join(" ")}</div>
  </div>`;
}

function graphRunPanel(){
  const R = graphRun;
  const btn = `<button class="btn" onclick="runGraph('gather')" ${R.running ? "disabled" : ""}>
    ${R.running ? "running…" : "Run gather"}</button>`;
  let h = `<h2>Run it — watch the wave <span class="meta" style="font-weight:400">
    the chart shows the shape; these cards show it happening</span></h2>
    <div class="card">${btn}
    <span class="meta" style="margin-left:10px">fetches GitHub, the web, your calendar and your
    memory — together. Proposes only: the digest lands in the outbox.</span>`;
  if (R.error) h += `<div class="meta" style="color:var(--bad);margin-top:10px">${esc(R.error)}</div>`;
  R.waves.forEach((w, i) => {
    const done = w.nodes.filter(n => (R.nodes[n] || {}).ms != null);
    const slowest = done.length ? Math.max(...done.map(n => R.nodes[n].ms)) : 0;
    const sum = done.reduce((a, n) => a + R.nodes[n].ms, 0);
    h += `<div class="meta" style="margin:14px 0 6px">wave ${i + 1} · ${w.nodes.length}
      node${w.nodes.length > 1 ? "s" : ""}${slowest ? ` · ${(slowest/1000).toFixed(1)}s`
      + (w.nodes.length > 1 ? ` (in sequence it would be ${(sum/1000).toFixed(1)}s)` : "") : ""}</div>
      <div class="cmp-grid">${w.nodes.map(graphCol).join("")}</div>`;
  });
  if (R.totalMs) h += `<div class="meta" style="margin-top:12px">finished in
    ${(R.totalMs/1000).toFixed(1)}s${R.draft ? ` · saved to <code>${esc(R.draft)}</code>` : ""}</div>`;
  if (R.digest) h += `<div class="card" style="margin-top:10px">${renderMarkdown(R.digest)}</div>`;
  return h + `</div>`;
}
