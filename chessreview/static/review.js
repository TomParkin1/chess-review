// Practice: browse my mistakes with filters and solve them as puzzles (puzzle.js).
import { api, emptyState, guard } from './common.js';
import { createPuzzle } from './puzzle.js';

const $ = id => document.getElementById(id);
const form = $('filters');
const urlParams = new URLSearchParams(location.search);
const onlyGame = urlParams.get('game');          // "practise this game's mistakes"
const lowClock = urlParams.get('low_clock');     // "Train this" for time trouble (Insights)
// Filters given in the URL (from Insights' "Train this") replace the saved ones.
const FILTER_KEYS = ['status', 'judgement', 'last', 'opening', 'phase', 'result', 'color', 'time_class', 'kind', 'sort'];
const fromLink = FILTER_KEYS.some(k => urlParams.has(k)) || lowClock;

let items = [];
let index = 0;
let optionsLoaded = false;
const session = { solved: 0, failed: 0 };

const KIND_LABELS = {
  missed_opportunity: 'Missed opportunity', positional: 'Positional', loses_material: 'Loses material',
  hanging_piece: 'Hanging piece', missed_tactic: 'Missed tactic', king_safety: 'King safety',
  missed_capture: 'Missed capture', allowed_mate: 'Allowed mate', missed_mate: 'Missed mate',
  fork: 'Fork', discovered_attack: 'Discovered attack',
};

// ------------------------------------------------------------ filters

const STORAGE_KEY = 'review-filters-v3';
// Recommended defaults: what I haven't solved, real errors only, recent games (current habits), worst first.
const RECOMMENDED = { status: 'unsolved', judgement: 'mistake,blunder', last: '50', sort: 'worst' };
const FIXED_SELECTS = ['status', 'judgement', 'sort', 'last'];  // options known up front

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
    if (FIXED_SELECTS.includes(name)) field.value = value;
    else field.dataset.saved = value;  // applied once the options have loaded
  }
}

function fillOptions(options) {
  for (const [name, values] of Object.entries(options)) {
    const select = form.elements[name];
    if (!select || FIXED_SELECTS.includes(name)) continue;
    for (const v of values) select.append(new Option(KIND_LABELS[v] || v.charAt(0).toUpperCase() + v.slice(1), v));
    if (select.dataset.saved) select.value = select.dataset.saved;
  }
}

function query() {
  const params = new URLSearchParams();
  for (const [k, v] of new FormData(form)) if (v) params.set(k, v);
  if (onlyGame) params.set('game', onlyGame);
  if (lowClock) params.set('low_clock', lowClock);
  return params.toString();
}

function load() {
  return guard(document.querySelector('main'), loadMistakes, 'your mistakes');
}

async function loadMistakes() {
  const q = query();
  const data = await api(`/api/mistakes?${q}`);
  if (!optionsLoaded) {
    fillOptions(data.options);
    optionsLoaded = true;
    if (query() !== q) return load();  // saved filters became selectable
  }
  items = data.items;
  renderSummary();
  $('empty').replaceChildren(data.analysed
    ? 'No mistakes match these filters.'
    : emptyState('No analysed games yet', 'Sync your chess.com games on Home. Once they are analysed, your ' +
                 'mistakes appear here as puzzles.', { href: '/', label: 'Go to Home' }));
  $('empty').hidden = items.length > 0;
  $('review').hidden = items.length === 0;
  if (items.length) show(0);
}

form.addEventListener('change', () => { saveFilters(); renderSummary(); load(); });

// One-line summary of the active filters; the full form is behind "Filters".
function renderSummary() {
  const parts = [];
  let custom = false;
  for (const field of form.elements) {
    if (field.tagName !== 'SELECT') continue;
    const label = field.selectedOptions[0]?.textContent || '';
    const isDefault = field.value === (RECOMMENDED[field.name] ?? '');
    if (!isDefault) custom = true;
    if (field.value || field.name in RECOMMENDED) {
      parts.push(field.name === 'opening' || field.name === 'kind' || field.name === 'phase' ||
                 field.name === 'color' || field.name === 'result' || field.name === 'time_class'
        ? `${field.closest('label').firstChild.textContent.trim().toLowerCase()}: ${label}` : label.toLowerCase());
    }
  }
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
  renderSummary();
  load();
});

// ------------------------------------------------------------ puzzles

// Real-game mode: no "you blundered here" clues, threat step first (remembered in this browser).
const BLIND_KEY = 'practice-blind';
const blindToggle = $('blind-toggle');
try { blindToggle.checked = localStorage.getItem(BLIND_KEY) === '1'; } catch { /* unavailable */ }
blindToggle.addEventListener('change', () => {
  try { localStorage.setItem(BLIND_KEY, blindToggle.checked ? '1' : '0'); } catch { /* unavailable */ }
  if (items.length) show(index);
});

function show(i) {
  index = (i + items.length) % items.length;
  $('counter').textContent = `${blindToggle.checked ? 'Position' : 'Mistake'} ${index + 1} of ${items.length}`;
  puzzle.show({ ...items[index], mode: blindToggle.checked ? 'blind' : undefined });
}

const puzzle = createPuzzle({
  onResult: (item, result) => {
    if (result.startsWith('solved')) session.solved += 1;
    else session.failed += 1;
    $('session-score').textContent = `This session: ✓ ${session.solved} · ✗ ${session.failed}`;
  },
  onNext: () => show(index + 1),
  onPrev: () => show(index - 1),
});

if (onlyGame || fromLink) {
  const box = $('game-filter');
  box.hidden = false;
  const what = onlyGame ? 'mistakes from one game' : lowClock ? 'mistakes made with under a minute on the clock'
    : 'mistakes matching this weakness';
  box.innerHTML = `Practising ${what}. <a href="/review">Show all mistakes</a>`;
}
restoreFilters();
load();
