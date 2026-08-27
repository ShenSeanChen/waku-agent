/* Theme picker — System / Light / Dark.
 *
 * Loaded from <head>, before the body, unlike every other file in js/. That is
 * deliberate: it stamps the resolved theme onto <html> before the first paint,
 * so a dark-mode user never sees a flash of the light palette.
 *
 * Two values, and keeping them apart is the whole design:
 *   - the CHOICE  ("system" | "light" | "dark") is what the user clicked, and
 *     the only thing localStorage holds;
 *   - the RESOLVED theme ("light" | "dark") is what lands in data-theme.
 * Resolving "system" here, in JS, is why style.css has no prefers-color-scheme
 * queries left: the palette has exactly ONE switch to read instead of two that
 * would have to agree with each other. */

const THEME_KEY = 'waku-theme';
const THEME_ORDER = ['system', 'light', 'dark'];
const THEME_LABEL = {system: 'System', light: 'Light', dark: 'Dark'};
const THEME_DARK_QUERY = window.matchMedia('(prefers-color-scheme:dark)');

/* localStorage throws in private mode and when site data is blocked; a theme
   button is not worth taking the page down for, so fall back to System. */
function themeChoice() {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return THEME_ORDER.includes(stored) ? stored : 'system';
  } catch (err) {
    return 'system';
  }
}

function nextTheme(choice) {
  return THEME_ORDER[(THEME_ORDER.indexOf(choice) + 1) % THEME_ORDER.length];
}

function applyTheme() {
  const choice = themeChoice();
  const dark = choice === 'dark' || (choice === 'system' && THEME_DARK_QUERY.matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  // Runs once from <head> with no body yet, then again on DOMContentLoaded.
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.textContent = THEME_LABEL[choice];
    btn.title = 'Theme: ' + THEME_LABEL[choice] + ' — click for ' + THEME_LABEL[nextTheme(choice)];
  }
}

function cycleTheme() {
  try {
    localStorage.setItem(THEME_KEY, nextTheme(themeChoice()));
  } catch (err) {
    /* no persistence available — the click still applies for this page */
  }
  applyTheme();
}

applyTheme();
// System has to keep following the OS while the page is open, not only at boot.
THEME_DARK_QUERY.addEventListener('change', applyTheme);
// The button lives in the body, which does not exist yet on the first call.
document.addEventListener('DOMContentLoaded', applyTheme);
