// Practice: browse my mistakes with filters and solve them as puzzles (puzzle.js).
import { api } from './common.js';
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

const STORAGE_KEY = 'review-filters-v2';
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

async function load() {
  const q = query();
  const data = await api(`/api/mistakes?${q}`);
  if (!optionsLoaded) {
    fillOptions(data.options);
    optionsLoaded = true;
    if (query() !== q) return load();  // saved filters became selectable
  }
  items = data.items;
  $('empty').hidden = items.length > 0;
  $('review').hidden = items.length === 0;
  if (items.length) show(0);
}

form.addEventListener('change', () => { saveFilters(); load(); });

// ------------------------------------------------------------ puzzles

function show(i) {
  index = (i + items.length) % items.length;
  $('counter').textContent = `Mistake ${index + 1} of ${items.length}`;
  puzzle.show(items[index]);
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
