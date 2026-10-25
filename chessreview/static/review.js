// Practice: browse my mistakes with filters and solve them as puzzles (puzzle.js), in sessions of
// SESSION_SIZE with a summary after each (B16). /api/mistakes sends the keys of every match once,
// plus the first session's puzzles; later sessions fetch only their own puzzles (F17).
import { api, el, emptyState, guard, isSolved, setThreatCheckPref, threatCheckPref } from './common.js';
import { createPuzzle } from './puzzle.js';

const $ = id => document.getElementById(id);
const form = $('filters');
const urlParams = new URLSearchParams(location.search);
const onlyGame = urlParams.get('game');          // "practise this game's mistakes"
const lowClock = urlParams.get('low_clock');     // "Train this" for time trouble (Insights)
// Filters given in the URL (from Insights' "Train this") replace the saved ones.
const FILTER_KEYS = ['status', 'judgement', 'last', 'opening', 'phase', 'result', 'color', 'time_class', 'kind', 'sort'];
const FACETS = ['opening', 'phase', 'result', 'color', 'time_class', 'kind'];   // options come from the server
const fromLink = FILTER_KEYS.some(k => urlParams.has(k)) || lowClock;
const SESSION_SIZE = 10;

let keys = [];       // every match, in order ("<game>:<ply>")
let start = 0;       // where the current session starts in `keys`
let items = [];      // the current session's puzzles
let index = 0;
let results = [];    // the result of each session puzzle, once tried
let loadSeq = 0;     // a newer load makes older responses stale

const KIND_LABELS = {
  missed_opportunity: 'Missed opportunity', positional: 'Positional', loses_material: 'Loses material',
  hanging_piece: 'Hanging piece', missed_tactic: 'Missed tactic', king_safety: 'King safety',
  missed_capture: 'Missed capture', allowed_mate: 'Allowed mate', missed_mate: 'Missed mate',
  fork: 'Fork', discovered_attack: 'Discovered attack',
};
const optionLabel = v => KIND_LABELS[v] || v.charAt(0).toUpperCase() + v.slice(1);

// ------------------------------------------------------------ filters

const STORAGE_KEY = 'review-filters-v3';
// Recommended defaults: what I haven't solved, real errors only, recent games (current habits), worst first.
const RECOMMENDED = { status: 'unsolved', judgement: 'mistake,blunder', last: '50', sort: 'worst' };

function saveFilters() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(new FormData(form)))); } catch { /* unavailable */ }
}

function restoreFilters() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch { /* ignore */ }
  if (fromLink) saved = Object.fromEntries(FILTER_KEYS.filter(k => urlParams.has(k)).map(k => [k, urlParams.get(k)]));
  for (const [name, value] of Object.entries(saved)) {
    const field = form.elements[name];
    if (!field) continue;
    if (FACETS.includes(name)) field.dataset.saved = value;  // selectable once the options have loaded
    else field.value = value;
  }
}

// Each facet lists only the values that have matches under the other filters, with counts (B16).
function fillOptions(options) {
  for (const name of FACETS) {
    const select = form.elements[name];
    const current = select.value || select.dataset.saved || '';
    delete select.dataset.saved;
    select.replaceChildren(new Option('All', ''),
      ...(options[name] || []).map(o => new Option(`${optionLabel(o.value)} (${o.count})`, o.value)));
    select.value = current;
  }
}

function query() {
  const params = new URLSearchParams();
  for (const field of form.elements) {
    const value = field.name && (field.value || field.dataset.saved);
    if (value) params.set(field.name, value);
  }
  if (onlyGame) params.set('game', onlyGame);
  if (lowClock) params.set('low_clock', lowClock);
  return params.toString();
}

function load() {
  return guard(document.querySelector('main'), loadMistakes, 'your mistakes');
}

async function loadMistakes() {
  const seq = ++loadSeq;
  const data = await api(`/api/mistakes?${query()}&limit=${SESSION_SIZE}`);
  if (seq !== loadSeq) return;
  fillOptions(data.options);
  renderSummary();
  keys = data.keys;
  $('empty').replaceChildren(data.analysed
    ? 'No mistakes match these filters.'
    : emptyState('No analysed games yet', 'Sync your chess.com games on Home. Once they are analysed, your ' +
                 'mistakes appear here as puzzles.', { href: '/', label: 'Go to Home' }));
  $('empty').hidden = keys.length > 0;
  $('practice-done').hidden = true;
  $('review').hidden = keys.length === 0;
  if (keys.length) beginSession(0, data.items);
}

form.addEventListener('change', () => { saveFilters(); load(); });

// One-line summary of the active filters (the URL's ones too); the full form is behind "Filters".
function renderSummary() {
  const parts = [];
  let custom = false;
  for (const field of form.elements) {
    if (field.tagName !== 'SELECT') continue;
    const label = (field.selectedOptions[0]?.textContent || '').replace(/ \(\d+\)$/, '');
    const isDefault = field.value === (RECOMMENDED[field.name] ?? '');
    if (!isDefault) custom = true;
    if (field.value || field.name in RECOMMENDED) {
      parts.push(FACETS.includes(field.name)
        ? `${field.closest('label').firstChild.textContent.trim().toLowerCase()}: ${label}` : label.toLowerCase());
    }
  }
  if (lowClock) parts.push('in time trouble');
  if (onlyGame) parts.push('from one game');
  $('filter-summary').textContent = `Showing ${parts.join(' · ')}`;
  $('reset-filters').hidden = !custom;
}

$('toggle-filters').addEventListener('click', () => {
  form.hidden = !form.hidden;
  $('toggle-filters').setAttribute('aria-expanded', String(!form.hidden));
  $('toggle-filters').textContent = form.hidden ? '⚙ Filters' : '✕ Hide filters';
});
$('reset-filters').addEventListener('click', () => {
  for (const field of form.elements) if (field.tagName === 'SELECT') field.value = RECOMMENDED[field.name] ?? '';
  saveFilters();
  load();
});

// ------------------------------------------------------------ sessions

async function beginSession(from, preloaded = null) {
  const seq = loadSeq;
  const batch = preloaded ?? (await api(`/api/mistakes/items?keys=${keys.slice(from, from + SESSION_SIZE).join(',')}`)).items;
  if (seq !== loadSeq) return;
  start = from;
  items = batch;
  results = [];
  $('practice-done').hidden = true;
  $('review').hidden = false;
  updateScore();
  show(0);
}

function updateScore() {
  const solved = results.filter(isSolved).length;
  const failed = results.filter(r => r && !isSolved(r)).length;
  $('session-score').textContent = solved + failed ? `This session: ✓ ${solved} · ✗ ${failed}` : '';
}

function show(i) {
  index = i;
  const word = blindToggle.checked ? 'Position' : 'Mistake';
  $('counter').textContent = `${word} ${index + 1} of ${items.length} · ${keys.length} match your filters`;
  $('prev-btn').disabled = index === 0;
  puzzle.show({ ...items[index], mode: blindToggle.checked ? 'blind' : undefined });
}

const CHIP = { solved: '✓ Solved', solved_with_help: '✓ With help', failed: '✗ Missed', skipped: 'Skipped' };

// The end of a session: how it went, and the way on (no wrapping round to the first one).
function finishSession() {
  const solved = results.filter(isSolved).length;
  const tried = results.filter(Boolean).length;
  const end = start + items.length;
  const more = end < keys.length;
  const box = $('practice-done');
  box.replaceChildren(
    el('div', { class: 'celebrate' }, solved === tried && tried ? '🎉' : '📋'),
    el('h1', {}, 'Session done'),
    el('p', { class: 'muted' }, `${tried} of ${items.length} tried · positions ${start + 1}–${end} of ${keys.length}`),
    el('div', { class: 'chips' }, el('span', { class: 'chip solved' }, `✓ ${solved} solved`),
       el('span', { class: 'chip failed' }, `✗ ${tried - solved} missed`)),
    el('ol', { class: 'session-list' }, ...items.map((item, i) => el('li', {},
      el('button', { class: 'session-item', title: 'Look at it again', onclick: () => backTo(i) },
        el('span', {}, blindToggle.checked ? `Move ${Math.floor(item.ply / 2) + 1}` : item.move_label),
        el('span', { class: 'muted small' }, ` vs ${item.opponent} · ${item.date}`),
        el('span', { class: `chip ${results[i] || ''}` }, CHIP[results[i]] || 'Not tried'))))),
    more ? null : el('p', {}, `That's all ${keys.length} for these filters.`),
    el('div', { class: 'actions' },
      more ? el('button', { class: 'btn primary big', id: 'next-session', onclick: () => beginSession(end) },
                `Next ${Math.min(SESSION_SIZE, keys.length - end)} ▶`)
           : el('button', { class: 'btn primary big', id: 'start-over', onclick: () => load() }, 'Start over'),
      el('button', { class: 'btn big', onclick: () => { if (form.hidden) $('toggle-filters').click(); window.scrollTo(0, 0); } },
         'Change filters')));
  $('review').hidden = true;
  box.hidden = false;
  window.scrollTo(0, 0);
}

function backTo(i) {
  $('practice-done').hidden = true;
  $('review').hidden = false;
  show(i);
}

// ------------------------------------------------------------ puzzles

// "Threat check first" (real-game mode): name the threat first, no "you blundered here" clues.
// One preference with Today's reviews.
const blindToggle = $('blind-toggle');
blindToggle.checked = threatCheckPref() ?? false;
blindToggle.addEventListener('change', () => {
  setThreatCheckPref(blindToggle.checked);
  if (items.length && !$('review').hidden) show(index);
});

const puzzle = createPuzzle({
  onResult: (item, result) => {
    results[index] = result;
    updateScore();
  },
  onNext: () => (index + 1 < items.length ? show(index + 1) : finishSession()),
  onPrev: () => { if (index > 0) show(index - 1); },
});

if (onlyGame || fromLink) {
  const box = $('game-filter');
  box.hidden = false;
  const what = onlyGame ? 'mistakes from one game'
    : lowClock ? 'mistakes made in time trouble (under 10% of your clock)' : 'mistakes matching this weakness';
  box.innerHTML = `Practising ${what}. <a href="/review">Show all mistakes</a>`;
}
restoreFilters();
load();
