// First run: the screen you get before Waku has a model to think with.
//
// WHY THIS IS A GATE AND NOT A BANNER. Before this existed, a fresh install
// opened on Overview with a working-looking chat box, and the first message
// came back `APIConnectionError`. That is the worst shape a first run can
// have: everything says ready, one thing is not, and the error names a
// library rather than the missing step. Reported from the live deployment on
// 2026-09-27, where it was worse still -- the hosted free tier row read
// "enabled, CURRENT" while the proxy behind it runs zero replicas.
//
// THE ONE QUESTION, ASKED IN ONE PLACE. `needsSetup` calls
// providerCardStatus, the same function the Models grid uses to colour a
// card. A second derivation of "is this provider usable" is the bug this
// file would otherwise be: the gate and the grid would disagree, and the
// disagreement would show up as a setup screen you cannot get past while
// Models insists everything is fine.
//
// Nothing here saves a key. The provider buttons open models.js's own modal,
// so there stays exactly one path that writes a credential, with one set of
// validation and one place to audit.

// True when this install cannot run a turn.
//
// TWO WAYS TO BE UNABLE TO, AND THE SECOND ONE COST A REAL USER AN EVENING.
// The obvious one is having no usable provider at all. The other is having
// one and not being ON it: the loop uses `settings.provider`, so a setting
// that names a provider which is missing or keyless fails every turn while
// the Models page shows a green, configured card for the key you just pasted.
//
// That happened on 2026-09-28. A hosted tenant's setting said `waku-platform`
// -- the free tier, whose row had just been removed from the build -- and
// their new Anthropic key sat there working and unused.
//
// integrations.apply_provider now adopts the first working key, so nobody
// should reach that state again. This is for everyone already in it: a
// setting written before the fix is still sitting in their .env.
function needsSetup(d){
  if (!d || !d.providers || !d.settings) return false;   // not loaded yet
  const usable = d.providers.filter(
    p => providerCardStatus(p, d.settings) === "enabled");
  if (!usable.length) return true;
  return !usable.some(p => p.key === d.settings.provider);
}

// Which of the two it is. The screen says different things, because "paste a
// key" is wrong advice for somebody who already has one.
function setupIsOrphaned(d){
  return (d.providers || []).some(
    p => providerCardStatus(p, d.settings || {}) === "enabled");
}

// The providers offered by name up front. NOT all of them: a first screen
// with eleven equal choices is a decision, and the point of this screen is to
// remove one. The rest are behind a toggle that expands IN PLACE -- this
// screen never sends anybody to another page, because the page it would send
// them to is hidden behind this one.
const SETUP_SUGGESTED = ["anthropic", "openai", "gemini"];

// Module state, not saved anywhere: it survives the 5s re-render and dies
// with the tab, which is exactly the lifetime "I clicked show all" deserves.
let setupShowAll = false;

function toggleSetupAll(){
  setupShowAll = !setupShowAll;
  render();
}

function setupChoice(p){
  return `<button class="btn btn-primary setup-choice" data-slot="button"
            onclick="openProviderModal('${escAttr(p.key)}')">${esc(p.name)}</button>`;
}

VIEWS.setup = function(d){
  const providers = d.providers || [];
  const byKey = k => providers.find(p => p.key === k);
  const suggested = SETUP_SUGGESTED.map(byKey).filter(Boolean);
  // A deployment can ship with none of the three -- the suggestion list is a
  // preference, not an assumption. Fall back to whatever it does have rather
  // than rendering a screen with no way forward.
  // An orphaned user's shortlist is what they already hold a key for. Our
  // three suggestions are for somebody with nothing; offering them to a
  // person who just needs to switch is asking them to buy a second ticket.
  const ready = providers.filter(
    p => providerCardStatus(p, d.settings || {}) === "enabled");
  const first = ready.length ? ready
    : (suggested.length ? suggested : providers.slice(0, 3));
  const rest = providers.filter(p => !first.includes(p));
  const offered = setupShowAll ? first.concat(rest) : first;
  const more = rest.length
    ? `<p class="setup-note"><button class="btn btn-tertiary" data-slot="button"
         onclick="toggleSetupAll()">${setupShowAll ? "show fewer"
         : `show all ${providers.length} providers`}</button></p>`
    : "";
  const orphaned = setupIsOrphaned(d);
  const named = (d.settings || {}).provider || "";
  const lede = orphaned
    ? `Your current provider, <code>${esc(named)}</code>, cannot answer a turn:
       it is not in this build, or it has no key. You already have a working
       provider. Pick the one to use.`
    : "Waku needs a model to think with. Pick a provider and paste an API key.";
  // No card title: the page header's h1 is already "Set up Waku", and a card
  // that repeats its own page's heading is the shape of a screen assembled
  // from parts rather than designed.
  return uiCard(`
    <p class="setup-lede">${lede}</p>
    <div class="setup-choices">${offered.map(setupChoice).join("")}</div>
    <p class="setup-note">The key is written to <code>.env</code> on this
      machine and is read only when Waku calls that provider. Nothing here
      sends it anywhere else.</p>
    ${more}
  `);
};
