// Light, dark, or whatever the machine is doing — Waku Memory's theme toggle.
//
// One button cycles the three states, in Memory's order. The choice is stored
// under "waku-theme", the key the Memory console uses, and applied as
// data-theme on <html>; "system" removes the attribute so tokens.css follows
// the OS. The inline script in index.html's <head> applies the stored choice
// before the first paint, so a reader who chose dark never sees a light flash.
const THEMES = ["system", "light", "dark"];
const THEME_LABEL = {system: "Theme: following the system", light: "Theme: light", dark: "Theme: dark"};
// The icon is the state (contrast, sun, moon), drawn at Memory's 16px / 1.5 stroke.
const THEME_ICON = {
  system: '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>',
  light: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  dark: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
};
function currentTheme(){
  try { const t = localStorage.getItem("waku-theme"); return THEMES.includes(t) ? t : "system"; }
  catch (e) { return "system"; }
}
function applyTheme(t){
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  const b = document.getElementById("theme-toggle");
  if (b){
    b.setAttribute("aria-label", THEME_LABEL[t]);
    b.title = THEME_LABEL[t];
    b.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true">${THEME_ICON[t]}</svg>`;
  }
}
function cycleTheme(){
  const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  try { localStorage.setItem("waku-theme", next); } catch (e) {}
  applyTheme(next);
}
