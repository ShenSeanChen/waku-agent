// The first-run Waku Memory question (spec 020), shown on Overview.
//
// Its own file because setup.js promises it never posts: the setup gate saves
// nothing and test_first_run_setup.py holds it to that. This card does post,
// once, to /api/settings, so it lives here.

// Asked once, on Overview, because the setup gate only shows when there is no model, and every install
// should be asked. Nothing is sent before the answer: "Sign in" records the
// choice and opens the browser, "Keep local only" records it and stops.
// `memoryNote` keeps the answer on screen after the card stops being pending.
let memoryNote = "";

function memoryChoiceCard(d){
  const m = d.memory_choice;
  if (m && m.pending) return uiCard(`<b>Keep your memory on waku.one?</b>
    <div class="meta" style="margin:calc(var(--spacing) * 1.5) 0">${esc(m.what)}</div>
    <div>${uiButton("Sign in to Waku Memory", {level: "primary", size: "sm", onclick: "chooseMemory('agent')"})}
      ${uiButton("Keep local only", {level: "tertiary", size: "sm", onclick: "chooseMemory('off')"})}
      <span id="memory-choice-msg" class="meta"></span></div>`);
  const line = memoryNote || (m && m.status) || "";
  return line ? uiCard(`<span class="meta">${esc(line)}</span>`) : "";
}

async function chooseMemory(mode){
  const msg = document.getElementById("memory-choice-msg");
  if (msg) msg.textContent = mode === "agent" ? "Check your browser to sign in..." : "Saving...";
  const r = await postJSON("/api/settings", {memory_mode: mode});
  memoryNote = r.error ? ("Error: " + r.error) : (r.memory_message || "Saved.");
  render();
}
