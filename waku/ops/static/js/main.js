// waku dashboard — render/refresh loop, resizers/chrome, voice, bootstrap (LOADS LAST).
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

let activeView = null, activeSub = null;
// The hash keys stay as they are — #settings is linked from graph.js, views.js,
// the README and DEMO-CHECKLIST, and from anyone's bookmark. Only the LABEL
// moved: after the Connections registry took keys, providers and integrations
// out of that page, what remained was two switches that change how a turn runs,
// which is a behaviour, not a setting.
const TITLES = {chat:"Chat & watch", ops:"LLM Ops",
                graph:"Graph workflows — structure around the loop",
                // Keyed by view AND sub for the Arena, now that the sidebar
                // names the two races separately. A single title covering both
                // was right while they hid behind sub-tabs; with two nav rows
                // it reads as a page that does not know which one you clicked.
                compare:"Arena — race models and memory through the same loop",
                "compare/models":"Model race — ten brains, one harness",
                "compare/memory":"Memory race — one brain, five places to put facts",
                settings:"Behaviour — how a turn runs",
                database:"Database — everything Waku stores (state.db)"};
function render(){
  if (!D) return;
  const [v, subRaw] = (location.hash||"#overview").slice(1).split("/");
  const sub = subRaw || null;
  const view = VIEWS[v] ? v : "overview";
  const subChanged = sub !== activeSub || view !== activeView;
  // Two nav rows can share a view, so a row that names a sub only lights up
  // for that sub. Without the fallback, landing on bare #compare would light
  // NEITHER race and the sidebar would show no current page at all.
  const effSub = sub || (view === "compare" ? "models" : null);
  document.querySelectorAll("nav a").forEach(a=>a.classList.toggle("on",
    a.dataset.v === view && (!a.dataset.sub || a.dataset.sub === effSub)));
  document.getElementById("title").textContent =
    TITLES[`${view}/${effSub}`] || TITLES[view] || view[0].toUpperCase()+view.slice(1);
  if (view === "overview" || view === "graph"){
    // don't rebuild mid-animation or the glowing SVG gets wiped
    if (activeView !== view || !animating){ document.getElementById("view").innerHTML = VIEWS[view](D); }
  } else if ((view === "memory" || view === "settings" || view === "database" || view === "compare" || view === "models" || view === "connections" || view === "judgment") && editing && !subChanged){
    // don't wipe an in-progress edit on the 5s refresh — but DO switch sub-tabs
    // ("judgment" is here for hover: rebuilding the table mid-hover destroys the
    // element under the pointer, and a native tooltip never gets to appear)
  } else {
    editing = false;
    // Rebuilding #view innerHTML resets the scroll. On a same-view refresh (the
    // 5s poll, a sort click) keep the reader where they were — only jump to top
    // on an actual navigation (subChanged), where top is correct.
    const main = document.querySelector("main");
    const keepScroll = !subChanged && main;
    const y = keepScroll ? main.scrollTop : 0;
    document.getElementById("view").innerHTML = VIEWS[view](D, sub);
    if (keepScroll) main.scrollTop = y;
  }
  activeView = view; activeSub = sub;
  document.getElementById("n-gw").textContent = (D.chat_log||[]).length || "";
  document.getElementById("n-loop").textContent = D.stats.turns || "";
  document.getElementById("n-graph").textContent =
    (D.graph && (D.graph.stats.quick + D.graph.stats.full)) || "";
  document.getElementById("n-mem").textContent = (D.facts.length + D.episodes.length) || "";
  document.getElementById("n-tools").textContent = (D.calendar.length + D.outbox.length) || "";
  document.getElementById("n-db").textContent = (D.db && D.db.all_tables.length) || "";
  document.getElementById("n-ops").textContent = D.stats.tool_errors || (D.eval_report ? "" : "!");
}
let lastFetch = Date.now();
let lastCompareLoad = 0;   // throttle the Compare scoreboard self-heal to ~5s
// Marks a request as a timer's poll, not a person doing something — the
// hosted gateway (group E) reads this to decide whether a request counts as
// activity, so a background tab can't hold a container awake forever.
const BG = {"X-Waku-Background": "1"};
// Set for the duration of a timer-driven refresh() (and reset in its
// `finally`), so a fetch scheduled as a *side effect* of that refresh's
// render() — e.g. loadAddModels's setTimeout in models.js — can tell it
// wasn't a person opening a tab, without threading a parameter through
// render()/VIEWS. Read it synchronously (or capture it into a local at
// schedule time); it is only meaningful while that refresh's own synchronous
// call chain is still running.
let bgRefresh = false;
// Defer a load out of a render and carry that render's provenance with it.
// The capture has to happen HERE, synchronously, while the render that
// scheduled it is still on the stack: by the time the timeout runs, refresh()
// has hit its `finally` and bgRefresh is false again, so reading it inside
// the callback would mark every timer-driven load as a person opening a tab.
// Three views made this same capture by hand; one of them getting it wrong is
// silent, so there is one copy of it.
function deferBg(load){
  const bg = bgRefresh;
  setTimeout(() => load(bg), 0);
}
let paused = false, pausedMsg = "";   // set by a "paused" reply; see handleNotOk
function tickLive(){
  if (!D) return;
  if (paused){
    // Keep showing the sentence a "paused" reply sent, not the live/updated
    // line — this 1s tick has no fetch of its own, so it can't overwrite it.
    document.getElementById("sub").innerHTML = `<span class="live paused">${esc(pausedMsg)}</span>`;
    return;
  }
  const ago = Math.round((Date.now()-lastFetch)/1000);
  document.getElementById("sub").innerHTML =
    `<span class="live"><span class="dot"></span>live</span> · updated ${ago}s ago · ${esc(D.home)}`;
}
let dockRestored = false;
async function restoreDock(background = false){
  // On page load the dock is empty even though the current thread has messages
  // — restore them so a refresh never looks like it lost the chat.
  // `background` is threaded because refresh() below reaches this on the FIRST
  // refresh of any kind, timer-driven included (a 5s poll that lands before
  // the first one finished). Untagged, that request would read as a person
  // opening the dock and hold a hosted container awake.
  dockRestored = true;
  const sid = D && D.current_session;
  if (!sid || CHAT.length) return;
  await loadThreadInto(sid, {setSession: true, background});
}
// A reply whose body is {"code": "paused", "error": "…"} — the hosted
// container went idle and needs a real user action to wake it. Any other
// non-2xx (a 401 from an expired session included) just keeps the last data:
// it used to reassign D unconditionally, so one bad response blanked the
// whole dashboard.
async function handleNotOk(res){
  let body = null;
  try { body = await res.json(); } catch(e){ /* not JSON — nothing to read */ }
  if (body && body.code === "paused"){
    paused = true;
    pausedMsg = body.error || "Paused. Send a message to wake it.";
    stopTimers();
  }
  tickLive();
}
// The ONE place `paused` is cleared, so every route out of the paused state
// is this one. Paused means FROZEN: both polls are stopped and the 1s tick
// re-asserts the sentence, so Memory, Facts, Sessions, Stats, the model chip
// and the harness animation all hold pre-pause data until something clears
// it. Missing one route out is not a cosmetic bug — it shipped once as a
// banner reading "Paused. Send a message to wake it." while the assistant
// was visibly answering the message that was supposed to wake it.
function resumeLive(){
  if (!paused) return;
  paused = false; pausedMsg = "";
  startTimers();
}
async function refresh(background = false){
  bgRefresh = background;
  try {
    const res = await fetch("/api/data", background ? {headers: BG} : undefined);
    if (!res.ok){ await handleNotOk(res); return; }
    D = await res.json(); lastFetch = Date.now();
    resumeLive();   // a real reply: whatever paused us is over
    render(); tickLive();
    syncModelChip();  // keep the dock's model pill in sync with the active brain
    applyTele();      // reflect the stats on/off choice (default on)
    syncLiveView(background);   // live-update an opened conversation (e.g. new phone messages)
    if (!dockRestored) restoreDock(background);
    // Self-heal the Compare scoreboard: it otherwise only loads on tab-open and
    // after a race, so a slow/interrupted race (or a server blip) can leave it
    // showing a partial set. Re-pull the server totals while viewing the tab —
    // but never mid-race (that's the live fold's job) or mid-edit, and at most
    // every ~5s so we don't hammer the endpoint on the faster render ticks.
    if (activeView === "compare" && !compareState.running && !editing
        && Date.now() - lastCompareLoad > 5000){
      lastCompareLoad = Date.now();
      loadCompareHistory(background);
    }
  } catch(e){ /* server restarting — keep showing last data */ }
  finally { bgRefresh = false; }
}
// --- resizable columns: drag the thin handle between nav|main and main|dock.
// Width lives in a CSS var + localStorage, so it survives refreshes.
function wireResizer(id, cssVar, key, fromRight, min, max){
  const el = document.getElementById(id);
  if (!el) return;
  el.onmousedown = e => {
    e.preventDefault();
    document.body.classList.add("resizing");
    const move = ev => {
      let w = fromRight ? (window.innerWidth - ev.clientX) : ev.clientX;
      const hi = typeof max === "function" ? Math.max(min, max()) : max;
      w = Math.max(min, Math.min(hi, w));
      document.documentElement.style.setProperty(cssVar, w + "px");
      localStorage.setItem(key, w);
    };
    const up = () => { document.body.classList.remove("resizing");
      document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up); };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  };
}
function wireChrome(){
  // The dock keeps its drag handle. The rail collapses instead of resizing,
  // and the collapse is not remembered: a rail that reopens closed hides the
  // navigation from someone coming back (Memory's rule).
  const dw = localStorage.getItem("dockW"); if (dw) document.documentElement.style.setProperty("--dock-w", dw+"px");
  const rail = document.getElementById("nav"), btn = document.getElementById("nav-collapse");
  // The handle stops where main would drop below --main-min: the same limit
  // the dock's CSS clamp holds, so a drag can never squeeze or cover the page.
  // A custom property reads back as its expression ("calc(480px + …)"), not
  // as pixels, so resolve it the way the dock's CSS does: by laying it out.
  const px = v => { const p = document.createElement("div");
    p.style.cssText = `position:absolute;visibility:hidden;width:var(${v})`;
    document.body.appendChild(p); const w = p.getBoundingClientRect().width; p.remove(); return w; };
  const dockMax = () => Math.min(680, window.innerWidth - rail.getBoundingClientRect().width
    - document.getElementById("dock-resizer").getBoundingClientRect().width - px("--main-min"));
  wireResizer("dock-resizer", "--dock-w", "dockW", true, 260, dockMax);
  if (btn) btn.onclick = () => {
    const c = rail.classList.toggle("collapsed");
    btn.setAttribute("aria-expanded", String(!c));
    btn.setAttribute("aria-label", c ? "Expand the sidebar" : "Collapse the sidebar");
    btn.innerHTML = c ? "&#8250;" : "&#8249;";
    // collapsed, the letter alone is on screen, so the name goes in the tooltip
    rail.querySelectorAll(":scope > a").forEach(a => { a.title = c ? a.getAttribute("aria-label") : ""; });
  };
}

// --- voice on the dashboard: record in the browser, transcribe on the server
// with the SAME local Whisper `make voice` uses. Text lands in the input for
// you to review, then Send — nothing leaves the machine.
// Voice capture records WAV (uncompressed PCM) via the Web Audio API — NOT
// MediaRecorder's WebM/Opus, which faster-whisper/PyAV often can't decode
// ("transcription failed [Errno …]"). WAV is trivially decodable server-side.
let micCtx = null, micStream = null, micNode = null, micBuf = [], micOn = false;
const micHint = (msg) => { const i = document.getElementById("dmsg");
  if (i){ i.placeholder = msg; setTimeout(()=>{ i.placeholder = "Message Waku…"; }, 8000); } };

async function toggleMic(){
  const btn = document.getElementById("mic");
  if (micOn){ await stopMic(); return; }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    micHint("voice needs a normal browser tab at localhost:7777 — not the IDE preview pane");
    return;
  }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({audio:true});
    micCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = micCtx.createMediaStreamSource(micStream);
    micNode = micCtx.createScriptProcessor(4096, 1, 1);
    micBuf = [];
    micNode.onaudioprocess = e => micBuf.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    source.connect(micNode); micNode.connect(micCtx.destination);
    micOn = true; btn.classList.add("rec");
  } catch(e){
    console.warn("mic error:", e);
    micHint(e && e.name === "NotAllowedError"
      ? "mic blocked — click the lock icon in the address bar → allow Microphone → reload (macOS: also System Settings ▸ Privacy ▸ Microphone ▸ your browser)"
      : "mic unavailable: " + (e && e.message || e));
  }
}

async function stopMic(){
  const btn = document.getElementById("mic"), input = document.getElementById("dmsg");
  micOn = false; btn.classList.remove("rec");
  try { micNode.disconnect(); } catch(e){}
  micStream.getTracks().forEach(t => t.stop());
  const rate = micCtx.sampleRate;
  micCtx.close();
  const wav = encodeWAV(micBuf, rate);
  const hold = input.placeholder; input.placeholder = "transcribing…";
  let r; try { r = await (await fetch("/api/voice", {method:"POST", body:wav})).json(); }
  catch(e){ r = {error:String(e)}; }
  input.placeholder = hold;
  if (r.error){ input.value = ""; micHint("voice: " + r.error); return; }
  if (r.text){ input.value = r.text; input.focus(); }
}

// float32 chunks → 16-bit PCM mono WAV blob
function encodeWAV(chunks, rate){
  let n = 0; chunks.forEach(c => n += c.length);
  const pcm = new Float32Array(n); let off = 0; chunks.forEach(c => { pcm.set(c, off); off += c.length; });
  const buf = new ArrayBuffer(44 + pcm.length * 2), view = new DataView(buf);
  const str = (o, s) => { for (let i=0;i<s.length;i++) view.setUint8(o+i, s.charCodeAt(i)); };
  str(0,"RIFF"); view.setUint32(4, 36 + pcm.length*2, true); str(8,"WAVE"); str(12,"fmt ");
  view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
  view.setUint32(24,rate,true); view.setUint32(28,rate*2,true); view.setUint16(32,2,true); view.setUint16(34,16,true);
  str(36,"data"); view.setUint32(40, pcm.length*2, true);
  let o = 44; for (let i=0;i<pcm.length;i++){ const s = Math.max(-1, Math.min(1, pcm[i])); view.setInt16(o, s<0 ? s*0x8000 : s*0x7FFF, true); o += 2; }
  return new Blob([view], {type:"audio/wav"});
}
function wireMic(){ const b = document.getElementById("mic"); if (b) b.onclick = toggleMic; }

// --- the two timer-driven polls (5s data, 450ms live-harness animation) —
// stopped while the tab is hidden so a background tab can't hold a hosted
// container awake, and restarted when it's shown again. Named intervals
// (not bare setInterval calls) so stopTimers() has something to clear.
let refreshTimer = null, pollTimer = null;
function startTimers(){
  if (refreshTimer) return;   // idempotent — a rapid hide/show shouldn't double them
  refreshTimer = setInterval(() => refresh(true), 5000);
  pollTimer = setInterval(() => pollEvents(true), 450);
}
function stopTimers(){
  clearInterval(refreshTimer); clearInterval(pollTimer);
  refreshTimer = null; pollTimer = null;
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden){ stopTimers(); }
  // Showing a tab is not a user action that may wake a stopped container, so
  // the refresh is timer-driven AND the timers stay stopped while we are
  // paused — restarting them here would fire background polls at a container
  // the pause deliberately stopped talking to. A 2xx from that one refresh
  // resumes them through resumeLive(), which is the only way back.
  else { if (!paused) startTimers(); refresh(true); }
});

window.addEventListener("hashchange", render);
window.__hold = (v)=>{ animating = v; };   // test hook: freeze the diagram
applyTheme(currentTheme());
watchSlots();
wireDock(); wireChrome(); wireMic();
refresh(); pollEvents();   // initial load: a real user action, no header
setInterval(tickLive, 1000);   // status text only, no fetch of its own
startTimers();
