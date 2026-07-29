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
  const SUB = {llm: "small model", agent: "THE loop, as a node", tool: "local read", fn: ""};
  const nodeBox = n => {
    const p = pos[n];
    if (n === "START" || n === "END")
      return `<g class="node" data-node="g-${n}">
        <rect class="bx" x="${p.x + W/2 - 34}" y="${p.y + H/2 - 15}" width="68" height="30" rx="15"/>
        <text class="nt" x="${p.x + W/2}" y="${p.y + H/2 + 5}" text-anchor="middle" style="font-size:12px">${n}</text></g>`;
    const sub = SUB[kinds[n]] || "";
    return `<g class="node" data-node="g-${n}">
      <rect class="bx" x="${p.x}" y="${p.y}" width="${W}" height="${H}" rx="9"/>
      <text class="nt" x="${p.x + 12}" y="${p.y + 22}">${esc(n)}</text>
      ${sub ? `<text class="ns" x="${p.x + 12}" y="${p.y + 39}">${sub}</text>` : ""}</g>`;
  };
  const edgeLine = e => {
    const a = pos[e.src], b = pos[e.dst];
    const x1 = a.x + (e.src === "START" ? W/2 + 34 : W), y1 = a.y + H/2;
    const x2 = b.x + (e.dst === "END" ? W/2 - 34 : 0), y2 = b.y + H/2;
    const mx = (x1 + x2) / 2;
    return `<path class="flow${e.conditional ? " dash" : ""}" data-edge="g-${e.src}-${e.dst}"
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

// --- the compact Overview panel: the harness auto-decides, this reflects it.
function graphPanel(d){
  const g = d.graph || {enabled: false, workflows: [], stats: {quick: 0, full: 0}};
  const wf = g.workflows[0];
  const tot = g.stats.quick + g.stats.full;
  const seg = (cls, n, label, pct) =>
    `<div class="${cls}" style="width:${pct}%">${pct >= 14 ? `${n} ${label}` : ""}</div>`;
  const split = !tot
    ? `<div class="meta" style="margin:6px 0 10px">no graph turns yet — every message will route here once it's on</div>`
    : `<div class="splitbar">
        ${seg("seg-skip", g.stats.quick, "quick", Math.round(g.stats.quick / tot * 100))}
        ${seg("seg-ret", g.stats.full, "full", 100 - Math.round(g.stats.quick / tot * 100))}
      </div><div class="meta" style="margin:6px 0 10px">${g.stats.quick} answered by the small model alone — the loop never woke</div>`;
  if (!g.enabled)
    return `<div class="card"><div class="meta">Off — every turn runs the classic loop above. Switch on
      <b>graph workflows</b> in <a class="reveal" onclick="location.hash='settings'">Settings</a> and every message
      is triaged first: trivial ones get a fast small-model reply, real tasks run the same loop as a graph node.
      You never pick a mode — the harness decides, this chart just shows which door each turn took.</div></div>`;
  return `<div class="card" style="cursor:pointer" onclick="location.hash='graph'">
    ${split}${wf ? graphSVG(wf) : ""}
    <div class="meta" style="margin-top:8px">live — nodes light up as a turn flows through · click for the full story</div></div>`;
}

// --- live animation: same machinery as the loop's STAGE map. hot() lights
// every copy on the page, so the Overview panel and the Graph tab glow together.
const GRAPH_KINDS = new Set(["graph_start", "node_start", "node_end", "route", "graph_end"]);
function animateGraphStage(ev){
  if (!document.querySelector(".graphchart")) return;
  const status = t => document.querySelectorAll(".arch-status").forEach(
    st => st.innerHTML = `<span class="live-dot"></span>${t}`);
  if (ev.type === "graph_start"){ status("graph workflow starts"); hot(`[data-node="g-START"]`, "hot", 1000); }
  else if (ev.type === "node_start") status(`graph · ${ev.node}`);
  else if (ev.type === "node_end") hot(`[data-node="g-${ev.node}"]`, "hot", 1000);
  else if (ev.type === "route"){
    status(`route → ${ev.target}`);
    hot(`[data-edge="g-${ev.router}-${ev.target}"]`, "live", 1400);
    hot(`[data-node="g-${ev.target}"]`, "hot", 1400);
  }
  else if (ev.type === "graph_end") hot(`[data-node="g-END"]`, "hot", 1000);
}
