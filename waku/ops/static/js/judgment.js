// The Judgment Arena — the third race. Models vary the brain, Memory varies
// where facts live, and this one varies WHO answers: a System One model that
// can only return a typed answer, against ordinary LLMs asked for the same
// answer as JSON.
//
// The unit on screen is the QUESTION, not the case. Every row is one Noul, one
// Choice or one Score, asked about one piece of text, with the answer written
// by hand beside it. Nothing is combined into a verdict: a Noul is right when
// it lands on the correct side of 0.5, a Choice when it picks the labelled
// option, a Score when its mean rounds to the labelled level.

let jaFixture;                 // undefined = not fetched, null = unavailable
let jaSuite = "";
let jaPicked = new Set();
let jaRun = {running: false, cells: {}, board: null, log: "", error: null};
let jaHistoryOpen = true;
let jaAskedOpen = true;

const PRIMITIVE_MARK = {noul: "N", choice: "C", score: "S"};

async function loadJudgmentArena(){
  // bgRefresh: this is only ever reached as a side effect of the judgment
  // view's own render (see judgment.js's VIEWS.judgment below), which is
  // itself either a person opening the tab or a background refresh's redraw.
  const background = bgRefresh;
  try {
    const r = await fetch("/api/judgment-arena", background ? {headers: BG} : undefined);
    jaFixture = r.ok ? await r.json() : null;
  } catch { jaFixture = null; }
  if (jaFixture && !jaSuite){
    jaSuite = (jaFixture.suites[0] || {}).id || "";
    jaFixture.contestants.filter(c => c.picked).forEach(c => jaPicked.add(c.spec));
  }
  editing = false; render();
}

function suiteOf(id){ return (jaFixture && jaFixture.suites || []).find(s => s.id === id); }
function jaLabel(spec){
  const c = (jaFixture && jaFixture.contestants || []).find(x => x.spec === spec);
  // "claude-haiku-4-5-20251001" is a column header made mostly of a date.
  return (c ? c.label : spec).replace(/-\d{8}$/, "");
}

function pickJudgmentSuite(id){
  if (jaRun.running) return;
  jaSuite = id;
  jaRun = {running: false, cells: {}, board: null, log: "", error: null};
  editing = false; render();
}

function toggleJudge(spec){
  if (jaRun.running) return;
  jaPicked.has(spec) ? jaPicked.delete(spec) : jaPicked.add(spec);
  editing = false; render();
}

function toggleAsked(){ jaAskedOpen = !jaAskedOpen; editing = false; render(); }
function toggleJudgmentHistory(){ jaHistoryOpen = !jaHistoryOpen; editing = false; render(); }

async function runJudgmentArena(){
  if (jaRun.running || !jaPicked.size) return;
  jaRun = {running: true, cells: {}, board: null, log: "asking…", error: null,
           specs: [...jaPicked]};
  jaHistoryOpen = false;      // the grid needs the room more than the archive
  jaAskedOpen = false;
  editing = false; render();
  try {
    const res = await fetch("/api/judgment-arena/stream", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({suite: jaSuite, specs: [...jaPicked]})});
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for(;;){
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const chunks = buf.split("\n\n"); buf = chunks.pop();
      for (const c of chunks){
        if (!c.startsWith("data: ")) continue;
        const ev = JSON.parse(c.slice(6));
        if (ev.kind === "start") jaRun.questions = ev.questions;
        if (ev.kind === "answer"){
          jaRun.cells[`${ev.spec}|${ev.qid}`] = ev;
          jaRun.log = `${jaLabel(ev.spec)}: ${ev.qid}`;
        }
        if (ev.kind === "column-done") jaRun.log = `${jaLabel(ev.spec)} finished`;
        if (ev.kind === "failed") jaRun.error = `${jaLabel(ev.spec)} — ${ev.error}`;
        if (ev.kind === "done"){
          jaRun.board = ev.scoreboard || null;
          if (ev.error) jaRun.error = ev.error;
        }
        editing = false; render();
      }
    }
  } catch(e){ jaRun.error = String(e); }
  jaRun.running = false; jaRun.log = ""; editing = false; render();
}

// Best in each column is marked, because the point of the race is which judge
// wins WHICH number — a frontier model can tie on judgment and lose on price by
// three orders of magnitude, and that only reads if they line up.
function scoreboardTable(board){
  const best = {
    hits: Math.max(...board.map(r => r.hits)),
    p50: Math.min(...board.map(r => r.p50_ms == null ? Infinity : r.p50_ms)),
    cost: Math.min(...board.map(r => r.per_1000)),
  };
  const mark = (on, html) => on ? `<b class="ja-best">${html}</b>` : html;
  const rows = board.map(r => [
    esc(jaLabel(r.spec)),
    mark(r.hits === best.hits, `${r.hits}/${r.n}`),
    mark(r.p50_ms === best.p50, r.p50_ms == null ? "--" : `${r.p50_ms} ms`),
    mark(r.per_1000 === best.cost, `$${r.per_1000}`),
    r.unparseable ? `<span class="ja-bad">${r.unparseable}</span>` : "0",
  ]);
  return uiTable(["judge", "answers right", "p50 per call", "per 1000 calls", "unparseable"],
    rows, {caption: "Every judge answered every question about every case. "
                  + "Best in each column is marked."});
}

// Every question in the suite, grouped by the text it was written for. Fifteen
// of them, five of each shape, none repeated — and the grouping shows which
// three travel together in a single call.
function askedCard(suite){
  const rows = [];
  suite.cases.forEach((c, i) => {
    c.questions.forEach((q, k) => {
      rows.push([
        k === 0 ? `<b>case ${i + 1}</b><div class="ja-shape">${esc(c.note)}</div>` : "",
        uiBadge(q.type, "neutral", q.id), `<b>${esc(q.id)}</b>`, esc(q.instructions),
        `<span class="ja-shape">${esc(answerShape(q))}</span>`]);
    });
  });
  return uiCard(
    `<p class="muted">Every question here is different. The three written for one
      case go in <b>one call</b> and come back together; each is scored on its own.
      Five Nouls, five Choices, five Scores.</p>`
    + uiTable(["case", "type", "id", "what the judge is asked", "answer it can give"], rows),
    {title: `The ${suite.n} questions`,
     action: uiButton(jaAskedOpen ? "hide" : "show",
                      {level: "tertiary", size: "sm", onclick: "toggleAsked()"}),
     cls: jaAskedOpen ? "" : "ja-folded"});
}

function answerShape(q){
  if (q.options) return `one of: ${q.options.join(", ")}`;
  if (q.levels) return `${q.levels.length} levels: ${q.levels.map((l, i) => `${i} ${l}`).join(" · ")}`;
  return "a probability from 0 to 1";
}

// One answer. The value is what came back; the dim number beside it is
// confidence, which a Noul does not have because there the probability IS the
// answer. The title is the whole thing, distribution included.
function answerCell(kind, cell){
  if (!cell) return `<span class="ja-cell pending">·</span>`;
  if (cell.unparseable){
    return `<span class="ja-cell bad" title="${esc(cell.error || "")}">unparseable</span>`;
  }
  const a = cell.answer || {};
  const value = a.noul !== undefined ? Number(a.noul).toFixed(2)
              : a.choice !== undefined ? a.choice
              : a.score !== undefined ? Number(a.score).toFixed(2) : "--";
  const conf = a.confidence === undefined ? ""
             : `<span class="ja-conf" title="confidence">${Number(a.confidence).toFixed(2)}</span>`;
  return `<span class="ja-cell ${cell.ok ? "good" : "bad"}"
    data-tip="${esc("the answer, as it came back:\n\n" + JSON.stringify(a, null, 2))}"
    >${esc(String(value))}${conf}</span>`;
}

function wantText(kind, want){
  if (kind === "noul") return want ? "yes" : "no";
  return String(want);
}

// Past races, kept whole, so reopening one is the run itself, not a summary.
function judgmentHistory(){
  const runs = (jaFixture && jaFixture.runs) || [];
  if (!runs.length) return "";
  return uiCard(runs.slice(0, 6).map((r, i) => {
    const s = suiteOf(r.suite);
    const when = new Date((r.at || 0) * 1000).toLocaleString();
    const line = r.board.map(b => `${jaLabel(b.spec)} ${b.hits}/${b.n}`).join(" · ");
    return uiRow(uiBadge(r.suite), esc(line),
                 `${esc(s ? s.title : r.suite)} · ${esc(when)}`,
                 {onclick: `openJudgmentRun(${i})`});
  }).join(""), {title: `Earlier races (${runs.length})`,
    cls: `ja-history${jaHistoryOpen ? "" : " ja-folded"}`,
    action: uiButton(jaHistoryOpen ? "hide" : "show",
                     {level: "tertiary", size: "sm", onclick: "toggleJudgmentHistory()"})});
}

function openJudgmentRun(i){
  const r = (jaFixture.runs || [])[i];
  if (!r || jaRun.running) return;
  jaSuite = r.suite;
  jaRun = {running: false, cells: {}, board: r.board, log: "", error: null,
           specs: r.specs || r.board.map(b => b.spec), questions: r.questions || null};
  for (const [spec, byQid] of Object.entries(r.cells || {})){
    for (const [qid, cell] of Object.entries(byQid)) jaRun.cells[`${spec}|${qid}`] = cell;
  }
  editing = false; render();
}

// --- the view ---------------------------------------------------------------
VIEWS.judgment = function(){
  if (jaFixture === undefined){ loadJudgmentArena(); return uiCard('<span class="empty">loading…</span>'); }
  if (jaFixture === null) return uiNotice("failed", "The judgment arena did not load. Restart the dashboard after a backend change.");

  const suite = suiteOf(jaSuite);
  if (!suite) return uiNotice("warn", "No suites available.");

  const picks = jaFixture.contestants.map(c => {
    const on = jaPicked.has(c.spec);
    return `<label class="cmp-pick ${on ? "on" : ""}${c.ready ? "" : " off"}"
      title="${c.ready ? esc(c.kind) : esc(c.why)}">
      <input type="checkbox" ${on ? "checked" : ""} ${c.ready ? "" : "disabled"}
        onchange="toggleJudge('${esc(c.spec)}')">
      <span class="mm-prov">${esc(c.kind)}</span> ${esc(jaLabel(c.spec))}</label>`;
  }).join("");

  const run = uiButton(jaRun.running ? "racing…" : "Run the race",
    {level: "primary", onclick: "runJudgmentArena()",
     attrs: jaRun.running || !jaPicked.size ? "disabled" : ""});

  const specs = jaRun.specs || [];
  const rows = (jaRun.questions || []).map(q => [
    // One tooltip for the whole cell, carrying everything about the question --
    // a truncated sentence with the rest hidden behind a hover nobody can find
    // is worse than no tooltip at all.
    `<span class="ja-qcell" data-tip="${esc(questionTip(q))}">`
    + `${uiBadge(q.type, "neutral", q.qid)} <span class="ja-qid">${esc(q.qid)}</span>`
    + `<div class="ja-asks">${esc(clip1(q.asks, 96))}</div></span>`,
    `<span class="ja-state" title="${esc(q.state)}">${esc(clip(q.state, 52))}</span>`,
    `<span class="ja-want">${esc(wantText(q.type, q.want))}</span>`,
    ...specs.map(s => answerCell(q.type, jaRun.cells[`${s}|${q.qid}`])),
  ]);

  const grid = uiTable(["the question", "asked about", "you labelled",
                        ...specs.map(s => esc(jaLabel(s)))],
    rows, {caption: "One row is one question, asked once, scored on its own. "
                  + "A Noul is right on the correct side of 0.5, a Choice when it picks the "
                  + "labelled option, a Score when its mean rounds to the labelled level. "
                  + "The dim number is confidence; hover any answer for its distribution."});

  return `
    <div class="ja-picker">${jaFixture.suites.map(s =>
      uiButton(s.title, {level: s.id === jaSuite ? "primary" : "secondary", size: "sm",
                         onclick: `pickJudgmentSuite('${s.id}')`})).join(" ")}</div>
    ${uiCard(`<p class="muted">${esc(suite.blurb)}</p>
      <div class="cmp-picks">${picks}</div>
      <div class="ja-controls">${run} <span class="muted">${esc(jaRun.log)}</span></div>`,
      {title: `${suite.title} — same questions, same cases, different judge`})}
    ${askedCard(suite)}
    ${jaRun.error ? uiNotice("failed", esc(jaRun.error)) : ""}
    ${jaRun.board ? scoreboardTable(jaRun.board) : ""}
    ${judgmentHistory()}
    ${rows.length ? `<div class="ja-grid" onmouseover="jaTipAt(event)"
        onmouseleave="endHover()">${grid}</div>` : ""}`;
};

// Everything a row's question is, as plain lines: native tooltips keep
// newlines, and this is the one place a viewer can read the whole thing.
function questionTip(q){
  return [`case: ${q.note || "--"}`,
          `type: ${(q.type || "").toUpperCase()}`,
          `id: ${q.qid}`,
          "",
          "asked:",
          q.asks || "",
          "",
          `answer it can give: ${q.shape || ""}`,
          `you labelled: ${wantText(q.type, q.want)}`].join("\n");
}

// A hover card we own, rather than the browser's own tooltip.
//
// `title` looked right and was useless: the browser draws it outside the page
// after about a second of unbroken hover, the 5s rebuild kept killing it, and
// nothing in the page can confirm it ever appeared. This is a real element, so
// it shows instantly, survives a rebuild, and can be tested.
// One handler on the table, so the trigger is the whole CELL -- its padding
// included -- rather than the few characters of text inside it. Chasing a
// 13px span with the pointer is not a hover target.
function jaTipAt(event){
  const td = event.target.closest("td");
  const src = td && td.querySelector("[data-tip]");
  if (!src) return jaTipHide();
  jaTip(src, td);
}

function jaTip(el, box_el){
  markEditing();
  let tip = document.getElementById("ja-tip");
  if (!tip){
    tip = document.createElement("div");
    tip.id = "ja-tip";
    tip.className = "ja-tip";
    document.body.appendChild(tip);
  }
  tip.textContent = el.dataset.tip || "";
  tip.hidden = false;
  const box = (box_el || el).getBoundingClientRect(), card = tip.getBoundingClientRect();
  // below the cell by default, above it when there is no room underneath
  const top = box.bottom + 8 + card.height > window.innerHeight
            ? box.top - card.height - 8 : box.bottom + 8;
  tip.style.top = `${Math.max(8, top)}px`;
  tip.style.left = `${Math.min(Math.max(8, box.left),
                               window.innerWidth - card.width - 8)}px`;
}

function jaTipHide(){
  const tip = document.getElementById("ja-tip");
  if (tip) tip.hidden = true;
}

// The 5s refresh rebuilds #view, which destroys whatever the pointer is over --
// and a native tooltip needs about a second of unbroken hover before it shows.
// Pausing the rebuild while the pointer is in the table is what makes the hover
// cards reachable at all.
function endHover(){ editing = false; jaTipHide(); }

function clip1(s, n){
  s = String(s || "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function clip(s, n){
  const lines = String(s || "").split("\n").filter(l => l.trim());
  s = lines[lines.length - 1] || "";        // the part that varies case to case
  return s.length > n ? s.slice(0, n) + "…" : s;
}
