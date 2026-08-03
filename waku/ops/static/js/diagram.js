// waku dashboard — the architecture SVG (archSVG, byte-frozen) + its live animation.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

// --- Architecture: a calm live SVG that mirrors the whiteboard's structure
// (Harness wraps the ephemeral run · Loop is a cycle · memory feeds up through
// the gate · LLM Ops is a separate loop). Deliberately few arrows + lots of
// air — the detail lives in each tab. Every node is live and clickable.
// DO NOT rewrite this chart. The data-node/data-edge ids each box emits drive
// the live animation via the STAGE map below — keep the two in sync.
function archSVG(d){
  const s = d.stats;
  const box = (x,y,w,h,title,sub,view,cls="",nid="") =>
    `<g class="node ${cls}" ${nid?`data-node="${nid}"`:""} ${view?`onclick="location.hash='${view}'"`:""}>
       <rect class="bx" x="${x}" y="${y}" width="${w}" height="${h}" rx="9"/>
       <text class="nt" x="${x+13}" y="${y+24}">${title}</text>
       ${sub?`<text class="ns" x="${x+13}" y="${y+42}">${sub}</text>`:""}
     </g>`;
  const lbl = (x,y,t) => `<text class="grp" x="${x}" y="${y}">${t}</text>`;
  const flow = (d2,cls="",eid="") => `<path class="flow ${cls}" ${eid?`data-edge="${eid}"`:""} d="${d2}"/>`;
  const flowLbl = (x,y,t,anchor="start") => `<text class="fl" x="${x}" y="${y}" text-anchor="${anchor}">${t}</text>`;

  return `<div style="overflow-x:auto"><svg viewBox="0 -10 1044 674" class="arch" role="img">
    <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" class="head"/></marker></defs>

    <!-- HARNESS container: everything runs on your laptop, including the
         offline LLM Ops loop (tinted sub-panel) -->
    <rect class="container" x="12" y="20" width="1020" height="628" rx="16"/>
    ${lbl(16,4,"HARNESS — runs on your laptop · the turn inside is ephemeral")}

    <!-- the turn: gateway → working memory → loop → reply -->
    ${box(32,72,128,56,"Gateway","cli · voice · web","chat","","gateway")}
    ${flow("M160 100 L192 100","","e-gw-wm")}
    ${box(192,72,144,56,"Working memory","assembled per turn","memory/overview","","wm")}

    <rect class="loopbox" x="370" y="56" width="168" height="166" rx="12"/>
    ${lbl(384,48,"LOOP")}
    ${box(384,72,140,50,"LLM agent","reason","loop","","llm")}
    ${box(384,152,140,52,"Tools","create_event…","tools","","tools")}
    ${flow("M448 122 L448 152")}${flow("M470 152 L470 122")}
    ${flowLbl(456,141,"act")}
    ${flow("M336 100 L370 100","","e-wm-loop")}
    ${flow("M538 100 L558 106")}${flowLbl(542,93,"reply")}
    ${box(558,84,104,52,"Reply","→ back to you","loop","","reply")}
    <!-- The gateway is the door IN and OUT: the reply leaves through the very
         gateway it arrived at (Telegram sends it, the CLI prints it, voice
         speaks it, the dashboard streams it). A clean over-the-top arc. -->
    <path class="flow" data-edge="e-reply-gw" d="M610 84 C610 40 596 34 566 34 L130 34 C104 34 96 44 96 72" marker-end="url(#arr)"/>
    ${flowLbl(348,28,"reply, out the same gateway","middle")}
    <!-- every turn is saved for consolidation: down a clear right lane,
         then left into the consolidation box -->
    <path class="flow dash" data-edge="e-reply-save" d="M650 136 C660 150 660 200 660 600 L430 600" marker-end="url(#arr)"/>
    ${flowLbl(668,214,"save chats",'start')}

    <!-- retrieval gate feeding working memory (the hero) -->
    <path class="gate node" data-node="gate" onclick="location.hash='memory/overview'" d="M264 250 L340 296 L264 342 L188 296 Z"/>
    <text class="nt" x="264" y="292" text-anchor="middle" style="pointer-events:none">Retrieval gate</text>
    <text class="ns" x="264" y="310" text-anchor="middle" style="pointer-events:none">${s.gate_skips} skip · ${s.gate_retrieves} retrieve</text>
    ${flow("M264 250 L264 128","dash","e-gate-wm")}${flowLbl(274,196,"only if needed")}

    <!-- MEMORY: grouped section with a direct link from the gate to each pillar -->
    ${lbl(40,404,"MEMORY — three pillars")}
    <rect class="memgroup" x="28" y="414" width="600" height="128" rx="12"/>
    ${flow("M148 452 L246 336","dash","e-gate-proc")}
    ${flow("M340 452 L272 344","dash","e-gate-sem")}
    ${flow("M542 452 L286 338","dash","e-gate-epi")}
    ${flowLbl(356,392,"the gate reads all three",'middle')}
    ${box(44,452,208,72,"Procedural","how to act · SKILL.md · "+d.skills.length+" skill(s)","memory/skills","","procedural")}
    ${box(264,452,204,72,"Semantic · FTS5","durable facts · "+d.facts.length+" facts","memory/semantic","","semantic")}
    ${box(480,452,132,72,"Episodic",d.episodes.length+" episodes","memory/episodic","","episodic")}

    <!-- consolidation writes back into memory -->
    ${box(44,576,384,52,"Consolidation · every "+d.consolidate_every+" exchanges",d.chat_pending+"/"+d.consolidate_every*2+" queued → distilled into facts","memory/consolidation","","consolidation")}
    ${flow("M340 576 L340 528","","e-consol-sem")}${flowLbl(350,560,"distill")}

    <!-- LLM OPS: the offline improvement loop — inside the harness (it all
         runs on the laptop) but a distinct tinted sub-panel -->
    <rect class="container ops" x="736" y="40" width="280" height="372" rx="14"/>
    ${lbl(752,64,"LLM OPS — offline improvement loop")}
    ${flowLbl(752,80,"observes each run · improves the agent",'start')}
    <!-- every turn crosses the gap to feed the trace -->
    <path class="flow" data-edge="e-reply-trace" d="M660 104 C700 100 726 100 752 106" marker-end="url(#arr)"/>
    ${flowLbl(688,96,"each turn")}
    ${box(752,92,250,50,"Trace",s.trace_files+" file(s) · always on","ops","","trace")}
    ${flow("M878 142 L878 156")}
    ${box(752,156,250,50,"Eval","deterministic + judge","ops")}
    ${flow("M878 206 L878 220")}
    ${box(752,220,250,50,"Release gate",d.eval_report?"det "+d.eval_report.deterministic+" · judge "+d.eval_report.judge:"run make gate","ops")}
    ${flow("M878 270 L878 284")}
    ${box(752,284,250,50,"Release","new prompt · model · config","ops")}
    <!-- feedback: Release improves the Harness — a short arrow across the gap,
         so the outer loop closes without a long wrap crowding the margins -->
    <path class="flow dash" d="M752 312 C712 324 698 352 676 358" marker-end="url(#arr)"/>
    ${flowLbl(596,346,"improved prompt + config",'end')}
  </svg></div>`;
}

// ---- Live harness animation: light up the diagram as a turn flows through,
// driven by the trace stream so ANY gateway (browser, phone, CLI) triggers it.
// The node/edge ids below MUST match the data-node="…"/data-edge="…" ids that
// archSVG emits above — change one, change the other (that's why both live in
// this file). test_static_assets.py won't catch a mismatch here; the animation
// just silently stops lighting a box.
const STAGE = {
  turn_start:    {nodes:["gateway","wm"],            edges:["e-gw-wm"],                 label:"message in"},
  gate:          {nodes:["gate"],                    edges:["e-gate-wm"],               label:"retrieval gate"},
  llm:           {nodes:["llm"],                     edges:["e-wm-loop"],               label:"agent reasons"},
  tool:          {nodes:["tools"],                   edges:[],                          label:"tool runs"},
  turn_end:      {nodes:["reply","trace"],           edges:["e-reply-trace","e-reply-save"], label:"reply"},
  consolidation: {nodes:["consolidation","semantic"],edges:["e-consol-sem"],            label:"consolidating memory"},
};
let evCursor = null, evQueue = [], playing = false, animating = false;

function hot(sel, cls, ms){
  document.querySelectorAll(sel).forEach(el => {   // every diagram copy lights up
    el.classList.add(cls);
    setTimeout(()=>el.classList.remove(cls), ms);
  });
}
function animateStage(ev){
  if (GRAPH_KINDS.has(ev.type)) return animateGraphStage(ev);   // graph.js owns these
  const spec = STAGE[ev.type];
  if (!spec || !document.querySelector(".arch")) return;
  document.querySelectorAll(".arch-status").forEach(st => st.innerHTML = `<span class="live-dot"></span>${spec.label}`);
  spec.nodes.forEach(n => hot(`[data-node="${n}"]`, "hot", 1000));
  spec.edges.forEach(e => hot(`[data-edge="${e}"]`, "live", 1000));
  if (ev.type==="gate" && ev.decision==="retrieve"){
    ["procedural","semantic","episodic"].forEach(n => hot(`[data-node="${n}"]`,"hot",1000));
    ["e-gate-proc","e-gate-sem","e-gate-epi"].forEach(e => hot(`[data-edge="${e}"]`,"live",1000));
  }
}
function playNext(){
  if (!evQueue.length){ playing=false; animating=false;
    document.querySelectorAll(".arch-status").forEach(st => st.innerHTML=""); return; }
  playing = true; animating = true;
  animateStage(evQueue.shift());
  setTimeout(playNext, 620);   // stagger so stages light up in sequence
}
async function pollEvents(){
  try{
    const r = await (await fetch("/api/events" + (evCursor==null?"":"?cursor="+evCursor))).json();
    if (evCursor != null && r.events.length){
      evQueue.push(...r.events);
      if (!playing) playNext();
    }
    evCursor = r.cursor;
  } catch(e){ /* server busy */ }
}

