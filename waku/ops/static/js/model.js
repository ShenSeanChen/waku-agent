// The LLM page (spec 046; the code keeps the name "model"): how a model works, on your own Mac. Overview
// draws how the harness runs; this page draws how the model inside it runs.
// Three tabs, all from the model file Ollama keeps and the Ollama on this
// machine, through /api/weights/* (waku/ops/weights.py):
//
//   #model/file   the file: every table of numbers, a 16 x 16 corner of one
//                 as real values or as the 4-bit codes it is stored as, and
//                 the vocabulary
//   #model/run    press Enter: one prompt, and for every piece of the answer
//                 five stations light in order (text, pieces, blocks, top 5
//                 candidates, chosen), played back slow, live or step by step
//   #model/lives  where it lives: the file on disk, the share of memory it
//                 holds now, the chip and the last measured speed; Load and
//                 Unload are Ollama's own keep_alive
//
// Ported from the open-models video's standalone page. The page keeps its own
// state in the mw* globals below, so the 5s refresh does not redraw it (see
// render() in main.js): it is built once, then each tab is shown or hidden.

const MW_GIB = 1073741824;
const MW_TOKENS_PER_PAGE = 40;
let mwModel = null, mwCurrent = null, mwTensor = null, mwAsCodes = false, mwReq = 0;
let mwTab = "file", mwLastTps = null, mwMachine = null, mwModels = null, mwWanted = "";
let mwPs = null, mwPsErr = "", mwLoading = false, mwPsTimer = null, mwMemTimer = null;

function mwEl(id){ return document.getElementById(id); }
const mwCommas = n => Number(n).toLocaleString("en-US");
const mwShort = n => n >= 1e9 ? (n/1e9).toFixed(2)+"B" : n >= 1e6 ? (n/1e6).toFixed(1)+"M" : n >= 1e3 ? (n/1e3).toFixed(1)+"K" : String(n);
async function mwGet(route, params, background){
  const r = await fetch("/api/weights/read?" + new URLSearchParams({what: route, ...(params || {})}),
                        background ? {headers: BG} : undefined);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function mwPost(route, body){
  const r = await fetch("/api/weights/do", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: route, ...body})});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

/* ------------------------------------------------------------ the page */
const MW_TABS = [["file", "The file"], ["run", "Press Enter"], ["lives", "Where it lives"]];
const mwArrow = `<div class="mw-arr" aria-hidden="true"><svg class="mw-ic" viewBox="0 0 34 34"><path d="M4 17h24M20 9l8 8-8 8"/></svg></div>`;

VIEWS.model = function(d, sub){
  if (d && d.hosted) return uiNotice("note", "Looking inside a model only works on your own machine, with Ollama installed.");
  mwTab = MW_TABS.some(t => t[0] === sub) ? sub : "file";
  setTimeout(mwMount, 0);
  const tabs = uiTabs(MW_TABS.map(([k, label]) => ({label, href: "#model/" + k, on: k === mwTab})));
  return `<div class="mw" id="mw">
  <div class="mw-top">${tabs}<label class="mw-pick"><span class="lbl-mono">model</span><select id="mw-model" aria-label="Local model" onchange="mwChooseModel(this.value)"></select></label></div>
  <div id="mw-none"></div>

  <section class="mw-pane" id="mw-p-file">
    <div class="mw-facts">
      <div class="mw-fact"><b id="mw-f-size">&ndash;</b><span>one file on this computer</span></div>
      <div class="mw-fact"><b id="mw-f-total">&ndash;</b><span>numbers (the &ldquo;weights&rdquo;)</span></div>
      <div class="mw-fact"><b id="mw-f-tables">&ndash;</b><span>tables of numbers</span></div>
      <div class="mw-fact"><b id="mw-f-bits">&ndash;</b><span id="mw-f-bits-l">bits per number, on average</span></div>
    </div>
    <div class="mw-file">
      <aside class="mw-side">
        <div id="mw-where"></div>
        <input id="mw-q" type="text" placeholder="Search tables, e.g. attn_q or blk.12" autocomplete="off" spellcheck="false" oninput="mwRenderList()">
        <div class="mw-hint" id="mw-hint"></div>
        <div class="mw-list" id="mw-list" onclick="mwListClick(event)"></div>
      </aside>
      <div class="mw-panel">
        <div id="mw-tensor"><p class="mw-soft">Reading the file header&hellip;</p></div>
        <details class="mw-vocab"><summary>The vocabulary: every token this model knows</summary>
          <div class="mw-vocab-head"><span class="mw-soft" id="mw-vocab-l"></span>
            <span class="mw-grow"></span><span class="mw-soft">from id</span>
            <input id="mw-tstart" type="number" min="0" value="1000" onchange="mwLoadTokens()">
            ${uiButton("&larr;", {size: "sm", onclick: "mwPageTokens(-1)", title: "Previous page"})}${uiButton("&rarr;", {size: "sm", onclick: "mwPageTokens(1)", title: "Next page"})}</div>
          <div class="mw-tokens" id="mw-tokens"></div>
        </details>
      </div>
    </div>
  </section>

  <section class="mw-pane" id="mw-p-run">
    <div class="mw-prompt">
      <input id="mw-prompt" type="text" value="The capital of France is" autocomplete="off" spellcheck="false" aria-label="Prompt"
        onkeydown="if(event.key==='Enter'){event.preventDefault();mwStartRun()}" oninput="mwPromptInput()">
      ${uiButton(`<span id="mw-run-l">Run</span>`, {level: "primary", onclick: "mwStartRun()", title: "Run (Enter)"})}
    </div>
    <div class="mw-stations" id="mw-stations" onclick="mwStationClick(event)">
      <div class="mw-st"><header><svg class="mw-ic" viewBox="0 0 40 40"><path d="M6 8h28v18H18l-8 7v-7H6z"/><path d="M12 14h16M12 20h10"/></svg><h4>text</h4></header>
        <div class="mw-body"><div id="mw-s-text" class="mw-s-text"></div></div></div>
      ${mwArrow}
      <div class="mw-st"><header><svg class="mw-ic" viewBox="0 0 40 40"><rect x="4" y="14" width="9" height="12" rx="2"/><rect x="15.5" y="14" width="9" height="12" rx="2"/><rect x="27" y="14" width="9" height="12" rx="2"/></svg><h4>tokens</h4></header>
        <div class="mw-body"><b class="mw-big" id="mw-s-pieces">&ndash;</b><div class="mw-sq" id="mw-s-sq"></div><span class="mw-soft">tokens in: your words, plus the chat format around them</span></div></div>
      ${mwArrow}
      <div class="mw-st"><header><svg class="mw-ic" viewBox="0 0 40 40"><rect x="7" y="6" width="26" height="7" rx="1.5"/><rect x="7" y="16.5" width="26" height="7" rx="1.5"/><rect x="7" y="27" width="26" height="7" rx="1.5"/></svg><h4 id="mw-s-blocks-l">blocks</h4></header>
        <div class="mw-body"><svg class="mw-stack" id="mw-stack" viewBox="0 0 120 252"></svg></div></div>
      ${mwArrow}
      <div class="mw-st mw-st-wide"><header><svg class="mw-ic" viewBox="0 0 40 40"><path d="M6 9h28M6 17h18M6 25h10M6 33h5"/></svg><h4>candidates</h4><span class="mw-soft mw-right">top 5</span></header>
        <div class="mw-body"><div class="mw-cands" id="mw-cands"></div></div></div>
      ${mwArrow}
      <div class="mw-st"><header><svg class="mw-ic" viewBox="0 0 40 40"><circle cx="20" cy="20" r="14"/><path d="M13 20.5l5 5 9-10"/></svg><h4>chosen</h4></header>
        <div class="mw-body"><div id="mw-chosen" class="mw-chosen none">&ndash;</div><div class="mw-chosen-p" id="mw-chosen-p">&nbsp;</div></div></div>
      <div class="mw-loop"><span>again, with one more token</span></div>
    </div>
    <ol class="mw-caps" id="mw-caps"></ol>
    <div class="mw-answer" id="mw-answer"></div>
    <div class="mw-ctl">
      ${uiButton("live", {size: "sm", onclick: "mwSetMode('live')", title: "Real speed", cls: "mw-m-live"})}
      ${uiButton("slow", {size: "sm", onclick: "mwSetMode('slow')", title: "Slow motion", cls: "mw-m-slow"})}
      <select id="mw-speed" aria-label="Slow motion speed" onchange="if(mwMode==='slow')mwSchedule()">
        <option value="3000">3 s / token</option><option value="1500" selected>1.5 s / token</option><option value="700">0.7 s / token</option><option value="300">0.3 s / token</option></select>
      ${uiButton("pause", {size: "sm", onclick: "mwTogglePause()", title: "Pause (space)", cls: "mw-m-pause"})}
      ${uiButton("&larr;", {size: "sm", onclick: "mwStep(-1)", title: "Step back (left arrow)"})}
      ${uiButton("&rarr;", {size: "sm", onclick: "mwStep(1)", title: "Step forward (right arrow)"})}
      <input id="mw-scrub" type="range" min="0" max="0" value="0" aria-label="Position" oninput="mwScrub(this.value)">
      <span class="mw-pos" id="mw-pos">0 / 0</span>
    </div>
    <div class="mw-counters">
      <div><b id="mw-c-loops">0</b><span id="mw-c-loops-l">loops so far</span></div>
      <div><b id="mw-c-tps">&ndash;</b><span>tokens per second, measured</span></div>
      <div><b id="mw-c-prompt">&ndash;</b><span>tokens in the prompt</span></div>
      <div><b id="mw-c-total">&ndash;</b><span>total time</span></div>
      <div><b id="mw-c-load">&ndash;</b><span>of it: getting the model ready</span></div>
    </div>
    <div id="mw-run-err"></div>
    <p class="mw-soft mw-foot">A token is a word or a piece of one. The lighting shows the order of the steps, slowed down.</p>
  </section>

  <section class="mw-pane" id="mw-p-lives">
    <div class="mw-panels">
      <div class="mw-pn"><svg class="mw-ic mw-ic-lg" viewBox="0 0 80 80"><rect x="10" y="16" width="60" height="48" rx="6"/><path d="M10 46h60"/><circle cx="58" cy="55" r="2.5"/><path d="M20 55h18"/><path d="M22 26h36M22 34h24"/></svg>
        <h3>Storage</h3><span class="mw-soft">keeps the file when the power is off</span>
        <b class="mw-huge" id="mw-st-size">&ndash;</b><span class="mw-soft">one file, on disk</span><code class="mw-path" id="mw-st-path"></code></div>
      <div class="mw-flow" id="mw-flow"><svg viewBox="0 0 130 80"><path class="shaft" d="M6 40h108"/><path d="M96 20l22 20-22 20"/></svg></div>
      <div class="mw-pn"><svg class="mw-ic mw-ic-lg" viewBox="0 0 80 80"><path d="M6 24h68v30H46l-3-4h-6l-3 4H6z"/><rect x="14" y="31" width="10" height="14" rx="1.5"/><rect x="28" y="31" width="10" height="14" rx="1.5"/><rect x="42" y="31" width="10" height="14" rx="1.5"/><rect x="56" y="31" width="10" height="14" rx="1.5"/><path d="M12 54v6M20 54v6M28 54v6M52 54v6M60 54v6M68 54v6"/></svg>
        <h3>Memory</h3><span class="mw-soft">where the numbers must be to compute</span>
        <b class="mw-huge off" id="mw-memstate">&ndash;</b>
        <div class="mw-membar" id="mw-membar"><i></i></div>
        <div class="mw-scale"><span>0</span><span id="mw-memtotal"></span></div>
        <span class="mw-memsub" id="mw-memsub"></span></div>
      <div class="mw-pn"><svg class="mw-ic mw-ic-lg" viewBox="0 0 80 80"><rect x="20" y="20" width="40" height="40" rx="5"/><rect x="31" y="31" width="18" height="18" rx="2"/><path d="M28 20v-9M40 20v-9M52 20v-9M28 69v-9M40 69v-9M52 69v-9M20 28h-9M20 40h-9M20 52h-9M69 28h-9M69 40h-9M69 52h-9"/></svg>
        <h3>Chip</h3><span class="mw-soft" id="mw-gpu-l">does the multiplying</span>
        <b class="mw-chip" id="mw-chipname">&ndash;</b>
        <b class="mw-huge" id="mw-ch-tps">&ndash;</b><span class="mw-soft" id="mw-ch-tps-l">tokens per second, after a run</span></div>
    </div>
    <p class="mw-point" id="mw-point"></p>
    <div class="mw-actions">
      ${uiButton("Load into memory", {level: "primary", onclick: "mwLoadUnload(true)", cls: "mw-b-load"})}
      ${uiButton("Unload", {onclick: "mwLoadUnload(false)", cls: "mw-b-unload"})}
      <span class="mw-loadmsg" id="mw-loadmsg"></span>
    </div>
    <p class="mw-soft mw-foot" id="mw-foot3"></p>
  </section>
</div>`;
};

/* Called after the page's markup is in place, and again on every tab change
   (main.js does not rebuild this view on the 5s refresh). */
function mwMount(){
  if (!mwEl("mw")) return;
  MW_TABS.forEach(([k]) => mwEl("mw-p-" + k).classList.toggle("on", k === mwTab));
  mwStartPolling();
  if (!mwRun) mwRun = mwNewRun("");
  if (!mwMachine) mwGet("machine").then(m => { mwMachine = m; mwRenderChip(); mwRenderMem(); mwRenderFoot(); }).catch(() => {});
  const start = () => {
    if (!mwModels.length){
      mwEl("mw-none").innerHTML = uiNotice("note", "No local model was found in Ollama's folder (~/.ollama/models). Install Ollama and pull a model, for example <code>ollama pull qwen3:8b</code>, then open this page again.");
      return;
    }
    mwFillPicker();
    const pick = mwModels.find(m => m.name === mwWanted) || mwModels.find(m => m.name === "qwen3:8b") || mwModels[0];
    if (mwModel && mwModel.name === pick.name) mwRedrawAll(); else mwLoadModel(pick.name);
  };
  if (mwModels) { start(); return; }
  mwGet("models").then(({models}) => { mwModels = models; start(); })
    .catch(e => { mwEl("mw-none").innerHTML = uiNotice("failed", esc(e.message)); });
}
// Show one tab without rebuilding the page: main.js calls this when only the
// sub-tab of #model changed.
function mwShowTab(sub){
  mwTab = MW_TABS.some(t => t[0] === sub) ? sub : "file";
  document.querySelectorAll("#mw .tabs .tab").forEach(a => {
    const on = a.getAttribute("href") === "#model/" + mwTab;
    a.classList.toggle("on", on); a.setAttribute("aria-selected", on ? "true" : "false");
  });
  MW_TABS.forEach(([k]) => { const p = mwEl("mw-p-" + k); if (p) p.classList.toggle("on", k === mwTab); });
  if (mwTab === "lives") mwPollPs();
}
// The Models page's "Look inside" link lands here with the model it named.
function mwOpen(name){
  mwWanted = name;
  if (mwModel && mwModel.name !== name && mwModels && mwModels.some(m => m.name === name)) mwLoadModel(name);
  location.hash = "#model/file";
}
// The Models page calls this under "On this computer". It asks once which
// model files Ollama keeps here, then draws one "Look inside" per file.
let mwFilesAsked = false;
function mwLookInsideLine(d){
  if (d && d.hosted) return "";
  if (!mwModels && !mwFilesAsked){
    mwFilesAsked = true;
    deferBg(bg => mwGet("models", null, bg)
      .then(r => { mwModels = r.models; if (activeView === "models" && !editing) render(); })
      .catch(() => {}));
  }
  if (!mwModels || !mwModels.length) return "";
  return `<div class="mw-inside"><span class="meta">Model files on this computer:</span>${mwModels.map(m =>
    `<code>${esc(m.name)}</code>${uiButton("Look inside", {level: "tertiary", size: "sm", onclick: "mwOpen(this.dataset.model)",
      attrs: `data-model="${esc(m.name)}"`, title: "See the numbers in this model's file"})}`).join("")}</div>`;
}
function mwFillPicker(){
  const s = mwEl("mw-model");
  if (!s || !mwModels) return;
  s.innerHTML = mwModels.map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
  if (mwModel) s.value = mwModel.name;
}
function mwChooseModel(name){ mwWanted = name; mwLoadModel(name); }
function mwRedrawAll(){
  if (!mwModel) return;
  mwRenderFacts(); mwRenderList(); mwRenderWhere(); mwRenderTensor(); mwLoadTokens();
  mwBuildStack(); mwResetCaps(); mwRest(); mwRenderStorage(); mwRenderChip(); mwRenderMem();
}

/* ------------------------------------------------------------ 1. the file */
function mwDescribe(name, cols){
  const n = name.toLowerCase();
  if (n.startsWith("token_embd")) return "The way in. One row of " + mwCommas(cols) + " numbers for every token the model knows; a token enters the model as its row.";
  if (n === "output.weight") return "The way out. Turns the final numbers into a score for every token the model knows.";
  if (n.includes("norm")) return "Normalisation: keeps the numbers in a steady range before the next step.";
  if (n.includes("attn_q")) return "Attention, the query: what this token is looking for among the earlier ones.";
  if (n.includes("attn_k")) return "Attention, the key: what each token offers to be found by.";
  if (n.includes("attn_v")) return "Attention, the value: what a token passes on once it is found.";
  if (n.includes("attn_output")) return "Attention, the result: mixes what was gathered back into the token.";
  if (/ffn_(up|down|gate)/.test(n)) return "Feed-forward: after attention, reworks each token's numbers on their own.";
  if (n.endsWith(".bias")) return "Offsets added after a table is applied.";
  return "One of the model's tables of numbers.";
}
function mwGroupOf(name){
  const m = /^blk\.(\d+)\./.exec(name);
  if (m) return {key: "blk." + m[1], order: 1 + Number(m[1])};
  if (name.startsWith("token_embd")) return {key: "embedding", order: 0};
  if (name.startsWith("output")) return {key: "output", order: 1e6};
  return {key: "other", order: 1e6 + 1};
}
// Which part of the model a table belongs to: only what its name states.
function mwPartOf(name){
  const m = /^blk\.(\d+)\.(.*)$/.exec(name);
  if (m) return {blk: Number(m[1]), part: /^attn/.test(m[2]) ? "attn" : /^ffn/.test(m[2]) ? "ffn" : "blk"};
  if (name.startsWith("token_embd")) return {part: "emb"};
  if (name.startsWith("output")) return {part: "out"};
  return {part: ""};
}
function mwRenderFacts(){
  const t = mwModel.totals, m = mwModel.metadata;
  mwEl("mw-f-size").innerHTML = (t.file_size / 1e9).toFixed(2) + "<small>GB</small>";
  mwEl("mw-f-total").textContent = mwCommas(t.total_weights);
  mwEl("mw-f-tables").textContent = mwCommas(t.tensor_count);
  mwEl("mw-f-bits").textContent = t.bits_per_weight ? t.bits_per_weight.toFixed(2) : "–";
  mwEl("mw-f-bits-l").textContent = "bits per number, on average" + (m.quantization ? " (" + m.quantization + ")" : "");
}
function mwRenderWhere(){
  const n = mwModel && mwModel.metadata.block_count;
  const p = mwCurrent ? mwPartOf(mwCurrent) : {part: ""};
  const hot = k => p.part === k ? "hot" : "";
  const ttl = n ? (p.blk !== undefined ? "block " + (p.blk + 1) + " of " + n : n + " blocks") : "blocks";
  const blkHot = p.part === "blk" ? "hot" : "";
  mwEl("mw-where").innerHTML = `<div class="lbl-mono">the whole model, and where this table sits</div>
  <svg class="mw-sch" viewBox="0 0 480 104" role="img" aria-label="embedding, then ${esc(ttl)} (attention, feed-forward), then output">
    <rect class="${hot("emb")}" x="1" y="36" width="96" height="44" rx="8"/><text class="${hot("emb")}" x="49" y="63">embedding</text>
    <path d="M101 58h20M114 51l7 7-7 7"/>
    <rect class="ghost" x="134" y="12" width="232" height="80" rx="10"/><rect class="ghost" x="130" y="16" width="232" height="80" rx="10"/>
    <rect class="${blkHot}" x="126" y="20" width="232" height="80" rx="10"/>
    <text class="ttl ${blkHot}" x="242" y="40">${esc(ttl)}</text>
    <rect class="${hot("attn")}" x="136" y="50" width="100" height="40" rx="7"/><text class="${hot("attn")}" x="186" y="75">attention</text>
    <rect class="${hot("ffn")}" x="246" y="50" width="104" height="40" rx="7"/><text class="${hot("ffn")}" x="298" y="75">feed-forward</text>
    <path d="M370 58h20M383 51l7 7-7 7"/>
    <rect class="${hot("out")}" x="396" y="36" width="82" height="44" rx="8"/><text class="${hot("out")}" x="437" y="63">output</text>
  </svg>
  <p class="mw-soft">Tokens go in on the left, pass through ${n ? n : "the"} identical blocks one after another, and scores come out on the right. Each block has two steps: attention, then feed-forward.</p>`;
}
function mwRenderList(){
  if (!mwModel) return;
  const q = mwEl("mw-q").value.trim().toLowerCase();
  const max = Math.max(...mwModel.tensors.map(t => t.count));
  const groups = new Map();
  let shown = 0;
  for (const t of mwModel.tensors){
    if (q && !t.name.toLowerCase().includes(q) && !t.type.toLowerCase().includes(q)) continue;
    const g = mwGroupOf(t.name);
    if (!groups.has(g.key)) groups.set(g.key, {...g, items: [], count: 0});
    const G = groups.get(g.key); G.items.push(t); G.count += t.count; shown++;
  }
  const curGroup = mwCurrent ? mwGroupOf(mwCurrent).key : "embedding";
  const html = [...groups.values()].sort((a, b) => a.order - b.order).map(G => {
    const open = q || G.key === curGroup || G.key === "embedding" || G.key === "blk.0";
    return `<details ${open ? "open" : ""}><summary>${esc(mwGroupLabel(G.key))}<span class="mw-meta">${G.items.length} tables, ${mwShort(G.count)}</span></summary>` +
      G.items.map(t => `<button type="button" class="mw-row${t.name === mwCurrent ? " on" : ""}" data-name="${esc(t.name)}">
        <span class="nm">${esc(t.name)}</span><span class="ct">${mwCommas(t.count)}</span>
        <span class="sh">${t.shape.join(" × ")}, ${esc(t.type)}</span><span></span>
        <span class="bar"><i style="width:${(100 * t.count / max).toFixed(2)}%"></i></span></button>`).join("") +
      `</details>`;
  }).join("");
  mwEl("mw-list").innerHTML = html || `<p class="mw-soft">No table matches.</p>`;
  mwEl("mw-hint").textContent = q ? shown + " of " + mwModel.tensors.length + " tables" :
    "Search by name or type. " + mwModel.tensors.length + " tables in all.";
}
// "blk.7" is the file's name for the eighth of the model's identical blocks.
function mwGroupLabel(key){
  const n = mwModel && mwModel.metadata.block_count, m = /^blk\.(\d+)$/.exec(key);
  if (m) return "block " + (Number(m[1]) + 1) + (n ? " of " + n : "") + " (" + key + ")";
  return {embedding: "the way in (embedding)", output: "the way out (output)"}[key] || key;
}
function mwListClick(e){ const b = e.target.closest(".mw-row"); if (b) mwSelectTensor(b.dataset.name); }

// Heatmap: blue for negative, red for positive, stronger for larger. Codes
// shade from paper (0) to ink (the top level). All from tokens.
function mwCellStyle(v, max){
  if (v === null) return "";
  const a = max ? Math.min(1, Math.abs(v) / max) : 0;
  const tone = v < 0 ? "var(--kind-search)" : "var(--bad)";
  const pct = Math.round(6 + 84 * Math.pow(a, 0.8));
  return `background:color-mix(in srgb, ${tone} ${pct}%, transparent);color:${a > 0.55 ? "var(--surface-bg)" : "var(--text-ink)"}`;
}
function mwCodeStyle(c, levels){
  const a = c / (levels - 1);
  return `background:color-mix(in srgb, var(--text-ink) ${Math.round(4 + 80 * a)}%, transparent);color:${a > 0.5 ? "var(--surface-bg)" : "var(--text-ink)"}`;
}
function mwRenderTensor(){
  const d = mwTensor;
  if (!d || !mwModel) return;
  const R = d.total_rows, C = d.total_cols;
  const oneD = d.shape.length < 2;
  const shapeLine = oneD
    ? `1 row &times; ${mwCommas(C)} columns = <b>${mwCommas(d.count)}</b> numbers`
    : `${mwCommas(R)} rows &times; ${mwCommas(C)} columns = <b>${mwCommas(d.count)}</b> numbers` +
      (d.shape.length > 2 ? ` <span class="mw-soft">(first slice of ${d.shape.join(" × ")})</span>` : "");
  let h = `<h3 class="mw-tname">${esc(d.name)}</h3><div class="mw-desc">${esc(mwDescribe(d.name, C))}</div><div class="mw-shape">${shapeLine}</div>`;
  const hasCodes = !!d.codes;
  if (!hasCodes) mwAsCodes = false;
  const showCodes = hasCodes && (mwAsCodes || !d.values);
  h += `<div class="mw-bar2">${uiBadge("stored as " + esc(d.type), "value")}`;
  if (hasCodes) h += `<label class="mw-toggle"><input type="checkbox" ${showCodes ? "checked" : ""} ${d.values ? "" : "disabled"} onchange="mwAsCodes=this.checked;mwRenderTensor()">show the codes the file really stores</label>`;
  if (showCodes) h += `<span class="mw-legend">0 <i class="codes"></i> ${d.code_levels - 1}</span>`;
  else if (d.values) h += `<span class="mw-legend">negative <i class="signs"></i> positive</span>`;
  h += `</div>`;
  const grid = showCodes ? d.codes : d.values;
  if (grid){
    let max = 0;
    if (!showCodes) for (const row of grid) for (const v of row) if (v !== null && Math.abs(v) > max) max = Math.abs(v);
    h += `<div class="mw-grid-wrap"><table class="mw-grid${showCodes ? " codes" : ""}"><thead><tr><th class="r"></th>` +
      grid[0].map((_, c) => `<th>${c}</th>`).join("") + `</tr></thead><tbody>` +
      grid.map((row, r) => `<tr><th class="r">${r}</th>` + row.map(v => showCodes
        ? `<td style="${mwCodeStyle(v, d.code_levels)}">${v}</td>`
        : `<td style="${mwCellStyle(v, max)}">${v === null ? "n/a" : v.toFixed(4)}</td>`).join("") + `</tr>`).join("") +
      `</tbody></table></div>`;
    h += `<p class="mw-meaning">One table is one grid of numbers. This model is <b>${mwCommas(mwModel.totals.tensor_count)}</b> of them.</p>`;
    if (!showCodes) h += `<p>${uiLink("Watch these tables run a prompt &rarr;", "#model/run")}</p>`;
    h += `<p class="mw-soft">Showing the top-left <b>${d.rows} &times; ${d.cols}</b> of <b>${mwCommas(R)} &times; ${mwCommas(C)}</b>` +
      `.</p>`;
    if (showCodes && !d.values) h += uiNotice("warn", "These are the raw stored codes, not decoded numbers.");
    else if (showCodes) h += `<p class="mw-soft">To save space the file keeps each number as one of only <b>${d.code_levels} levels</b> (0 to ${d.code_levels - 1}), plus a scale for each small group. That is how a ${(mwModel.totals.file_size / 1e9).toFixed(1)} GB file holds ${mwShort(mwModel.totals.total_weights)} numbers.</p>`;
  } else {
    h += uiNotice("note", esc(d.note || "No numbers available for this table."));
  }
  mwEl("mw-tensor").innerHTML = h;
}
async function mwSelectTensor(name){
  mwCurrent = name;
  document.querySelectorAll("#mw-list .mw-row").forEach(b => b.classList.toggle("on", b.dataset.name === name));
  mwRenderWhere();
  const id = ++mwReq;
  try {
    const d = await mwGet("tensor", {name: mwModel.name, tensor: name, rows: 16, cols: 16});
    if (id !== mwReq) return;
    mwTensor = d; mwRenderTensor();
  } catch (e){ if (id === mwReq) mwEl("mw-tensor").innerHTML = uiNotice("failed", esc(e.message)); }
}
async function mwLoadTokens(){
  if (!mwModel || !mwEl("mw-tstart")) return;
  const start = Math.max(0, parseInt(mwEl("mw-tstart").value, 10) || 0);
  try {
    const d = await mwGet("tokens", {name: mwModel.name, start, count: MW_TOKENS_PER_PAGE});
    mwEl("mw-tstart").value = d.start;
    mwEl("mw-vocab-l").textContent = "Every token this model knows, " + mwCommas(d.total) + " of them, by number. Ġ marks a leading space.";
    mwEl("mw-tokens").innerHTML = d.tokens.map(t => `<span class="mw-tok"><i>${t.id}</i>${esc(t.text)}</span>`).join("") ||
      `<span class="mw-soft">This file has no token list.</span>`;
  } catch (e){ mwEl("mw-tokens").innerHTML = `<span class="mw-soft">${esc(e.message)}</span>`; }
}
function mwPageTokens(dir){
  const s = mwEl("mw-tstart");
  s.value = Math.max(0, (parseInt(s.value, 10) || 0) + dir * MW_TOKENS_PER_PAGE);
  mwLoadTokens();
}
async function mwLoadModel(name){
  if (!mwEl("mw")) return;
  mwEl("mw-tensor").innerHTML = `<p class="mw-soft">Reading the file header&hellip;</p>`;
  mwEl("mw-list").innerHTML = "";
  try { mwModel = await mwGet("model", {name}); }
  catch (e){ mwEl("mw-tensor").innerHTML = uiNotice("failed", esc(e.message)); return; }
  mwFillPicker();
  mwCurrent = (mwModel.tensors.find(x => x.name === "token_embd.weight") || mwModel.tensors[0] || {}).name || null;
  mwTensor = null; mwAsCodes = false; mwEl("mw-q").value = "";
  mwRenderFacts(); mwRenderList(); mwRenderWhere();
  if (mwCurrent) mwSelectTensor(mwCurrent);
  mwLoadTokens();
  mwResetCaps(); mwResetRun(); mwBuildStack(); mwRenderStorage(); mwPollPs();
}

/* ------------------------------------------------------------ 2. press enter */
const mwStrict = new TextDecoder("utf-8", {fatal: true});
const MW_WS = {" ": "␣", "\n": "↵", "\t": "⇥", "\r": "␍"};
const mwVisible = s => s.replace(/[ \n\t\r]/g, c => MW_WS[c]);
// A piece as it should be read on screen. A piece that is only part of a
// multi-byte character shows as its raw bytes, not as a guessed letter.
function mwPieceLabel(bytes, fallback){
  if (!bytes || !bytes.length) return {text: fallback ? mwVisible(fallback) : "∅", hex: false};
  try { return {text: mwVisible(mwStrict.decode(new Uint8Array(bytes))), hex: false}; }
  catch (_){ return {text: "bytes " + bytes.map(b => b.toString(16).toUpperCase().padStart(2, "0")).join(" "), hex: true}; }
}
const mwSameBytes = (a, b) => a && b && a.length === b.length && a.every((x, i) => x === b[i]);
function mwPct(logprob){
  const p = Math.exp(logprob) * 100;
  if (p >= 99.995) return "100%";
  if (p >= 10) return p.toFixed(1) + "%";
  if (p >= 0.01) return p.toFixed(2) + "%";
  return "<0.01%";
}

let mwRun = null, mwCursor = -1, mwMode = "slow", mwTimer = null, mwAborter = null, mwLastPlay = "slow";
// One pass is the five stations lighting in order for the piece under the
// cursor. mwPhase 0..4 is the station lit now; candidates show from 3, the
// pick lands at 4.
let mwPhase = 4, mwPassTimers = [];
const mwStations = () => [...document.querySelectorAll("#mw-stations .mw-st")];
// What each station does, in one sentence. All five stay on screen and the
// lit one is marked, so nothing has to be read while it moves.
function mwCaptions(){
  const m = (mwModel && mwModel.metadata) || {}, n = v => typeof v === "number" ? "<b>" + mwCommas(v) + "</b>" : "many";
  return [
    "Your sentence is cut into tokens: words and pieces of words.",
    "Each token is swapped for its row of " + n(m.embedding_length) + " numbers.",
    "Those numbers pass through " + n(m.block_count) + " blocks of tables, one after another. This is the model's whole job.",
    "The last table scores every one of the " + n(m.vocab_size) + " tokens it knows. These are the top five.",
    "One is picked and added to the sentence. Then it all runs again for the next token.",
  ];
}
function mwLight(i){
  mwStations().forEach((el, k) => el.classList.toggle("lit", k === i));
  const c = mwEl("mw-caps");
  if (!c) return;
  if (!c.children.length) c.innerHTML = mwCaptions().map(t => `<li>${t}</li>`).join("");
  [...c.children].forEach((li, k) => li.classList.toggle("on", k === i));
}
// The captions quote the model's own sizes, so a new model redraws them.
function mwResetCaps(){ const c = mwEl("mw-caps"); if (c) c.innerHTML = ""; }
function mwStopPass(){ mwPassTimers.forEach(clearTimeout); mwPassTimers = []; const st = mwEl("mw-stations"); if (st) st.classList.remove("tick"); }
function mwStartPass(dur){
  mwStopPass();
  const st = mwEl("mw-stations");
  if (!st) return;
  const n = (mwModel && mwModel.metadata.block_count) || 36;
  st.style.setProperty("--mw-per", (dur * 0.34 / (n + 8)).toFixed(1) + "ms");
  mwPhase = 0; mwRender2(); mwLight(0);
  const at = (f, fn) => mwPassTimers.push(setTimeout(fn, dur * f));
  at(0.13, () => mwLight(1));
  at(0.26, () => { mwLight(2); void st.offsetWidth; st.classList.add("tick"); });
  at(0.62, () => { mwPhase = 3; mwRender2(); mwLight(3); });
  at(0.82, () => { mwPhase = 4; mwRender2(); mwLight(4); const c = mwEl("mw-chosen"); if (c) c.classList.add("in"); mwPassTimers = []; });
}
function mwRest(){
  mwStopPass(); mwPhase = 4; mwRender2();
  mwLight(mwCursor >= 0 && !(mwMode === "live" && (mwRun.streaming || mwTimer)) ? 4 : -1);
}
function mwNewRun(prompt){ return {prompt, tokens: [], final: null, streaming: false, error: ""}; }
function mwResetRun(){
  if (mwAborter) mwAborter.abort();
  clearTimeout(mwTimer); mwTimer = null;
  mwRun = mwNewRun(""); mwCursor = -1;
  mwRest();
}
function mwBuildStack(){
  const n = (mwModel && mwModel.metadata.block_count) || 0;
  if (!mwEl("mw-stack")) return;
  mwEl("mw-s-blocks-l").textContent = n ? n + " blocks" : "blocks";
  const h = 252, pitch = n ? h / n : h, bh = Math.max(1.5, pitch - 2);
  let s = "";
  for (let i = 0; i < n; i++) s += `<rect style="--i:${i}" x="1" y="${(i * pitch + 1).toFixed(2)}" width="118" height="${bh.toFixed(2)}" rx="1.5"/>`;
  mwEl("mw-stack").innerHTML = s;
}
function mwRender2(){
  if (!mwRun || !mwEl("mw-stations")) return;
  const toks = mwRun.tokens, n = toks.length, f = mwRun.final;
  const live = mwMode === "live" && (mwRun.streaming || mwTimer);
  mwEl("mw-stations").classList.toggle("busy", !!live);
  const shown = mwPhase >= 4 ? mwCursor : mwCursor - 1;   // last piece already in the sentence

  mwEl("mw-s-text").textContent = mwRun.prompt || mwEl("mw-prompt").value;
  mwEl("mw-s-pieces").textContent = f ? mwCommas(f.prompt_eval_count) : "–";
  mwEl("mw-s-sq").innerHTML = f && f.prompt_eval_count <= 120 ? "<i></i>".repeat(f.prompt_eval_count) : "";

  const t = mwCursor >= 0 && mwPhase >= 3 ? toks[mwCursor] : null;
  let rows = [];
  if (t){
    rows = t.top.slice(0, 5).map(c => ({...c, pick: mwPhase >= 4 && mwSameBytes(c.bytes, t.bytes)}));
    if (mwPhase >= 4 && !rows.some(r => r.pick)) rows.push({bytes: t.bytes, token: t.token, logprob: t.logprob, pick: true});
  }
  let h = "";
  for (let i = 0; i < Math.max(5, rows.length); i++){
    const r = rows[i];
    if (!r){ h += `<div class="mw-cand empty"><span class="t">&nbsp;</span><span class="track"></span><span class="p">&ndash;</span></div>`; continue; }
    const L = mwPieceLabel(r.bytes, r.token), has = typeof r.logprob === "number";
    h += `<div class="mw-cand${r.pick ? " pick" : ""}"><span class="t${L.hex ? " hex" : ""}">${esc(L.text)}</span>` +
      `<span class="track"><i style="width:${has ? (Math.exp(r.logprob) * 100).toFixed(3) : 0}%"></i></span>` +
      `<span class="p">${has ? mwPct(r.logprob) : "&ndash;"}</span></div>`;
  }
  mwEl("mw-cands").innerHTML = h;
  const ch = mwEl("mw-chosen");
  if (t && mwPhase >= 4){
    const L = mwPieceLabel(t.bytes, t.token);
    ch.textContent = L.text; ch.className = "mw-chosen" + (L.hex || L.text.length > 9 ? " long" : "");
    mwEl("mw-chosen-p").textContent = typeof t.logprob === "number" ? mwPct(t.logprob) : "";
  } else { ch.innerHTML = "&ndash;"; ch.className = "mw-chosen none"; mwEl("mw-chosen-p").innerHTML = "&nbsp;"; }

  // the answer so far: the picked pieces' bytes, decoded as one stream
  const dec = new TextDecoder("utf-8");
  let a = mwRun.prompt ? `<span class="pr">${esc(mwRun.prompt)}</span>\n` : "";
  for (let i = 0; i <= shown && i < n; i++){
    const piece = dec.decode(new Uint8Array(toks[i].bytes), {stream: true});
    if (i === shown) a += `<span class="cur">${esc(piece).replace(/\n/g, '<span class="ws">↵</span>\n') || "&nbsp;"}</span>`;
    else a += esc(piece);
  }
  const ans = mwEl("mw-answer");
  ans.innerHTML = a;
  const cur = ans.querySelector(".cur");
  if (cur){ const top = cur.offsetTop - ans.offsetTop; if (top + cur.offsetHeight > ans.scrollTop + ans.clientHeight - 10 || top < ans.scrollTop) ans.scrollTop = top - 20; }

  const sc = mwEl("mw-scrub");
  sc.max = Math.max(0, n - 1); sc.value = Math.max(0, mwCursor); sc.disabled = n < 2;
  mwEl("mw-pos").textContent = (mwCursor + 1) + " / " + n;
  mwEl("mw-c-loops").textContent = mwCommas(shown + 1);
  const extra = f && f.eval_count > n ? " (+" + (f.eval_count - n) + " end token)" : "";
  mwEl("mw-c-loops-l").textContent = "loops so far" + (n ? " of " + mwCommas(n) + extra : "") + (mwRun.streaming ? " …" : "");
  const sec = (ns, d) => (ns / 1e9).toFixed(d) + "<small>s</small>";
  mwEl("mw-c-tps").innerHTML = f && f.eval_duration ? (f.eval_count / f.eval_duration * 1e9).toFixed(1) : "&ndash;";
  mwEl("mw-c-prompt").textContent = f ? mwCommas(f.prompt_eval_count) : "–";
  mwEl("mw-c-total").innerHTML = f ? sec(f.total_duration, 2) : "&ndash;";
  mwEl("mw-c-load").innerHTML = f ? sec(f.load_duration || 0, 2) : "&ndash;";
  mwEl("mw-run-err").innerHTML = mwRun.error ? uiNotice("failed", esc(mwRun.error)) : "";
  document.querySelectorAll(".mw-m-live").forEach(b => b.classList.toggle("on", mwMode === "live"));
  document.querySelectorAll(".mw-m-slow").forEach(b => b.classList.toggle("on", mwMode === "slow"));
  document.querySelectorAll(".mw-m-pause").forEach(b => b.classList.toggle("on", mwMode === "pause"));
  mwEl("mw-run-l").textContent = mwRun.streaming ? "Stop" : "Run";
}

// Playback: the whole stream is recorded, and the cursor says which loop is on screen.
function mwSpeed(){ return Number((mwEl("mw-speed") || {}).value || 1500); }
function mwSlowTick(){
  mwTimer = null;
  if (mwCursor < mwRun.tokens.length - 1){ mwCursor++; mwStartPass(mwSpeed() * 0.9); }
  mwSchedule();
}
function mwLiveTick(){ mwTimer = null; mwCursor++; mwSchedule(); mwRest(); }
function mwSchedule(){
  clearTimeout(mwTimer); mwTimer = null;
  const toks = mwRun.tokens;
  if (mwMode === "slow"){
    if (mwCursor >= toks.length - 1 && !mwRun.streaming) return;
    mwTimer = setTimeout(mwSlowTick, mwCursor < 0 ? 150 : mwSpeed());
  } else if (mwMode === "live" && !mwRun.streaming && mwCursor < toks.length - 1){
    // replay at the speed it really arrived
    const gap = mwCursor >= 0 ? toks[mwCursor + 1].at - toks[mwCursor].at : 0;
    mwTimer = setTimeout(mwLiveTick, gap > 0 ? gap : 0);
  }
}
function mwSetMode(m){
  mwMode = m;
  const end = mwRun.tokens.length - 1;
  if (m !== "pause" && !mwRun.streaming && mwCursor >= end && end >= 0) mwCursor = -1;   // replay from the start
  if (m === "live" && mwRun.streaming) mwCursor = end;
  mwSchedule(); mwRest();
}
function mwTogglePause(){ if (mwMode === "pause") mwSetMode(mwLastPlay); else { mwLastPlay = mwMode; mwSetMode("pause"); } }
function mwStep(d){
  if (mwMode !== "pause") mwLastPlay = mwMode;
  mwMode = "pause"; clearTimeout(mwTimer); mwTimer = null;
  const c = Math.max(-1, Math.min(mwRun.tokens.length - 1, mwCursor + d));
  const moved = c !== mwCursor; mwCursor = c;
  if (moved && d > 0) mwStartPass(650); else mwRest();
}
function mwScrub(v){
  if (mwMode !== "pause") mwLastPlay = mwMode;
  mwMode = "pause"; clearTimeout(mwTimer); mwTimer = null; mwCursor = Number(v); mwRest();
}
function mwStationClick(e){ const el = e.target.closest(".mw-st"); if (el && !mwPassTimers.length) mwLight(mwStations().indexOf(el)); }
function mwPromptInput(){ if (mwRun && !mwRun.tokens.length && !mwRun.streaming) mwEl("mw-s-text").textContent = mwEl("mw-prompt").value; }

async function mwStartRun(){
  if (mwRun && mwRun.streaming){ mwAborter.abort(); return; }
  const prompt = mwEl("mw-prompt").value;
  if (!prompt.trim() || !mwModel) return;
  mwEl("mw-prompt").blur();
  clearTimeout(mwTimer); mwTimer = null;
  const me = mwRun = mwNewRun(prompt); mwCursor = -1; mwRun.streaming = true;
  if (mwMode === "pause") mwMode = mwLastPlay;
  mwAborter = new AbortController();
  mwRest(); mwSchedule();
  try {
    const r = await fetch("/api/weights/do", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "run", model: mwModel.name, prompt}), signal: mwAborter.signal});
    if (!r.ok){ let m = r.statusText; try { m = (await r.json()).error || m; } catch (_){} throw new Error(m); }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;){
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0){
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
        if (line) mwOnChunk(me, JSON.parse(line));
      }
    }
    if (buf.trim()) mwOnChunk(me, JSON.parse(buf));
  } catch (e){
    if (e.name !== "AbortError") me.error = "Could not run: " + e.message;
  }
  me.streaming = false;
  if (mwRun === me){ if (mwMode === "live"){ mwCursor = me.tokens.length - 1; mwRest(); } else mwRender2(); mwSchedule(); }
}
function mwOnChunk(me, j){
  if (j.error){ me.error = String(j.error); return; }
  const at = performance.now();
  const lps = j.logprobs || [];
  if (lps.length){
    for (const lp of lps) me.tokens.push({token: lp.token, bytes: lp.bytes || Array.from(new TextEncoder().encode(lp.token || "")),
      logprob: lp.logprob, top: lp.top_logprobs || [], at});
  } else if (j.response){   // a model that reports no candidates: the text only
    me.tokens.push({token: j.response, bytes: Array.from(new TextEncoder().encode(j.response)), logprob: null, top: [], at});
  }
  if (j.done){
    me.final = j;
    if (j.eval_count && j.eval_duration){ mwLastTps = {tps: j.eval_count / j.eval_duration * 1e9, n: j.eval_count, model: j.model}; mwRenderChip(); }
  }
  if (mwRun !== me || !mwEl("mw-stations")) return;
  if (mwMode === "live") mwCursor = me.tokens.length - 1;
  if (mwMode === "live" || j.done) mwRender2();
  else { mwEl("mw-pos").textContent = (mwCursor + 1) + " / " + me.tokens.length; mwEl("mw-scrub").max = me.tokens.length - 1; mwEl("mw-scrub").disabled = false; }
}

/* ------------------------------------------------------------ 3. where it lives */
function mwRenderStorage(){
  if (!mwModel || !mwEl("mw-st-size")) return;
  mwEl("mw-st-size").innerHTML = (mwModel.totals.file_size / 1e9).toFixed(2) + "<small>GB</small>";
  mwEl("mw-st-path").textContent = mwModel.path;
}
function mwRenderChip(){
  if (!mwEl("mw-chipname")) return;
  mwEl("mw-chipname").textContent = (mwMachine && mwMachine.chip) || "–";
  if (mwLastTps){
    mwEl("mw-ch-tps").textContent = mwLastTps.tps.toFixed(1);
    mwEl("mw-ch-tps-l").textContent = "tokens per second, last run, " + mwLastTps.n + " tokens" + (mwModel && mwLastTps.model !== mwModel.name ? " (" + mwLastTps.model + ")" : "");
  }
}
function mwRenderFoot(){
  const m = mwMachine, el = mwEl("mw-foot3");
  if (!m || !el) return;
  el.textContent = "Memory in units of 1,073,741,824 bytes; file size in units of 1,000,000,000, as Finder shows it." +
    (m.ollama_version ? " Ollama " + m.ollama_version + "." : "");
}
// The one thing this tab is for: a model runs only where it fits in memory.
function mwRenderPoint(){
  const el = mwEl("mw-point"), total = mwMachine && mwMachine.memory_bytes;
  if (!el || !mwModel || !total) return;
  const file = mwModel.totals.file_size / 1e9, gb = total / MW_GIB;
  el.innerHTML = `A model can only run once all of its numbers are in memory. This file is <b>${file.toFixed(1)} GB</b> and this computer has <b>${gb.toFixed(0)} GB</b>, so it ${file < gb * 0.9 ? "fits" : "does not fit"}. That is why size decides where a model can run: press Load and watch the bar fill.`;
}
function mwRenderMem(){
  if (!mwEl("mw-memstate")) return;
  mwRenderPoint();
  const total = mwMachine && mwMachine.memory_bytes;
  mwEl("mw-memtotal").textContent = total ? (total / MW_GIB).toFixed(0) + " GB" : "";
  const m = mwPs && mwModel ? (mwPs.models || []).find(x => x.name === mwModel.name || x.model === mwModel.name) : null;
  const state = mwEl("mw-memstate"), bar = mwEl("mw-membar").firstElementChild;
  mwEl("mw-flow").className = "mw-flow" + (mwLoading ? " loading" : m ? " loaded" : "");
  const gpu = "does the multiplying";
  if (mwPsErr){ state.className = "mw-huge off"; state.textContent = "Ollama is off"; bar.style.width = "0"; mwEl("mw-memsub").textContent = ""; return; }
  if (!m){
    state.className = "mw-huge off"; state.textContent = mwLoading ? "loading…" : "not loaded";
    bar.style.width = "0"; mwEl("mw-memsub").textContent = ""; mwEl("mw-gpu-l").textContent = gpu;
    return;
  }
  state.className = "mw-huge";
  state.innerHTML = (m.size / MW_GIB).toFixed(1) + "<small>GB" + (total ? " of " + (total / MW_GIB).toFixed(0) + ", " + (100 * m.size / total).toFixed(0) + "%" : "") + "</small>";
  bar.style.width = total ? Math.min(100, 100 * m.size / total).toFixed(2) + "%" : "0";
  const left = Date.parse(m.expires_at) - Date.now();
  let sub = "";
  if (Number.isFinite(left)){
    if (left > 864e5) sub = "stays loaded";
    else if (left > 0){ const s = Math.ceil(left / 1000); sub = "unloads in " + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
    else sub = "unloading…";
  }
  mwEl("mw-memsub").textContent = sub;
  if (m.size) mwEl("mw-gpu-l").textContent = "holds " + (100 * (m.size_vram || 0) / m.size).toFixed(0) + "% of the loaded model";
}
async function mwPollPs(background){
  if (!mwEl("mw")) return;
  try { mwPs = await mwGet("ps", null, background); mwPsErr = ""; } catch (e){ mwPsErr = e.message; }
  mwRenderMem();
}
// The two timers' callbacks. A timer's request is a poll, not a person, so it
// is marked as background; leaving the page stops both.
function mwPsTick(){ if (mwEl("mw")) mwPollPs(true); else mwStopPolling(); }
function mwMemTick(){ if (mwTab === "lives" && mwEl("mw")) mwRenderMem(); }
// Polls only while this page is open: Ollama every 2s for what is loaded,
// and the countdown four times a second while "Where it lives" shows.
function mwStartPolling(){
  if (mwPsTimer) return;
  mwPollPs();
  mwPsTimer = setInterval(mwPsTick, 2000);
  mwMemTimer = setInterval(mwMemTick, 250);
}
function mwStopPolling(){ clearInterval(mwPsTimer); clearInterval(mwMemTimer); mwPsTimer = mwMemTimer = null; }

async function mwLoadUnload(load){
  if (mwLoading || !mwModel) return;
  const msg = mwEl("mw-loadmsg");
  const btns = [...document.querySelectorAll(".mw-b-load, .mw-b-unload")];
  msg.className = "mw-loadmsg"; msg.textContent = load ? "loading…" : "";
  mwLoading = load; btns.forEach(b => b.disabled = true); mwRenderMem();
  try {
    const j = await mwPost(load ? "load" : "unload", {model: mwModel.name});
    if (load){ msg.className = "mw-loadmsg good"; msg.textContent = typeof j.wall_seconds === "number" ? "load took " + j.wall_seconds.toFixed(2) + " s" : "loaded"; }
    else msg.textContent = "unloaded";
  } catch (e){ msg.className = "mw-loadmsg bad"; msg.textContent = e.message; }
  mwLoading = false; btns.forEach(b => b.disabled = false);
  await mwPollPs();
  if (!load) setTimeout(mwPsTick, 600);
}

// Keys while the Model page is open: 1 2 3 switch tabs; on Press Enter the
// arrows step, space pauses and Enter runs.
document.addEventListener("keydown", e => {
  if (!mwEl("mw") || e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName) && t.type !== "range") return;
  if (t.closest && t.closest("#dock")) return;
  if (e.key === "1" || e.key === "2" || e.key === "3"){ location.hash = "#model/" + MW_TABS[Number(e.key) - 1][0]; e.preventDefault(); return; }
  if (mwTab !== "run") return;
  if (e.key === "ArrowRight"){ mwStep(1); e.preventDefault(); }
  else if (e.key === "ArrowLeft"){ mwStep(-1); e.preventDefault(); }
  else if (e.key === " "){ mwTogglePause(); e.preventDefault(); }
  else if (e.key === "Enter"){ mwStartRun(); e.preventDefault(); }
});
