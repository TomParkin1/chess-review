// The "new games" session: puzzles from the games just synced/uploaded, in game order,
// then a summary card of what this batch shows.
import { api, el } from './common.js';
import { createPuzzle } from './puzzle.js';
import { countBar } from './widgets.js';

const $ = id => document.getElementById(id);
const KIND_LABELS = {
  missed_opportunity: 'Missed opportunities', positional: 'Positional slips', loses_material: 'Losing material',
  hanging_piece: 'Hanging pieces', missed_tactic: 'Missed tactics', king_safety: 'King safety',
  missed_capture: 'Missed captures', allowed_mate: 'Walking into mate', missed_mate: 'Missed mates',
  fork: 'Forks', discovered_attack: 'Discovered attacks',
};

let info = null;
let items = [];
let index = 0;
const tally = { solved: 0, failed: 0 };

const puzzle = createPuzzle({
  onResult: (item, result) => {
    if (result.startsWith('solved')) tally.solved += 1; else tally.failed += 1;
    updateBar();
  },
  onNext: () => {
    index += 1;
    if (index >= items.length) finish(); else showCurrent();
  },
});

function updateBar() {
  const m = items[index];
  const games = [...new Set(items.map(i => i.game_id))];
  const gameNo = m ? games.indexOf(m.game_id) + 1 : games.length;
  $('counter').textContent = `Game ${gameNo} of ${games.length} · puzzle ${Math.min(index + 1, items.length)} of ${items.length}`;
  $('session-score').textContent = `✓ ${tally.solved} · ✗ ${tally.failed}`;
  const bar = $('session-progress');
  bar.hidden = false;
  bar.firstElementChild.style.width = `${(index / Math.max(items.length, 1)) * 100}%`;
}

function showCurrent() {
  updateBar();
  puzzle.show(items[index]);
}

async function finish() {
  $('review').hidden = true;
  $('session-start').hidden = true;
  const s = await api('/api/session/done', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ batch_ids: info.batch_ids }),
  });
  const box = $('session-done');
  box.hidden = false;
  const attempted = tally.solved + tally.failed;
  const kindMax = Math.max(1, ...s.top_kinds.map(([, n]) => n));
  const clockMax = Math.max(1, ...s.blunder_clock.map(([, n]) => n));
  const anyClock = s.blunder_clock.some(([, n]) => n);
  const lessons = [];
  if (s.top_kinds.length) lessons.push(`Your most common problem this batch: ${(KIND_LABELS[s.top_kinds[0][0]] || s.top_kinds[0][0]).toLowerCase()}.`);
  const lowTime = s.blunder_clock.filter(([label]) => label.startsWith('under') || label.startsWith('20')).reduce((t, [, n]) => t + n, 0);
  if (s.blunders && lowTime / s.blunders >= 0.4) lessons.push(`${lowTime} of your ${s.blunders} blunders came with under a minute left: watch the clock.`);

  box.replaceChildren(...[
    el('div', { class: 'celebrate' }, '📋'),
    el('h1', {}, 'Batch summary'),
    el('p', { class: 'muted' }, `${s.games} games · ${s.wins}W ${s.draws}D ${s.losses}L` +
       (s.accuracy != null ? ` · ${s.accuracy}% average accuracy` : '') +
       ` · ${s.mistakes} mistakes, ${s.blunders} blunders`),
    attempted ? el('div', { class: 'chips' },
      el('span', { class: 'chip solved' }, `✓ ${tally.solved} solved`),
      el('span', { class: 'chip failed' }, `✗ ${tally.failed} missed (they're now in daily training)`)) : null,
    lessons.length ? el('div', { class: 'lessons' }, ...lessons.map(l => el('p', {}, '💡 ', l))) : null,
    el('div', { class: 'summary-grid' },
      el('div', {}, el('div', { class: 'section-title' }, 'What went wrong'),
        ...(s.top_kinds.length ? s.top_kinds.map(([k, n]) => countBar(KIND_LABELS[k] || k, n, kindMax, 'var(--mistake)'))
                               : [el('p', { class: 'muted small' }, 'No mistakes or blunders. Nice!')])),
      anyClock ? el('div', {}, el('div', { class: 'section-title' }, 'Clock left at each blunder'),
        ...s.blunder_clock.map(([label, n]) => countBar(label, n, clockMax, 'var(--blunder)'))) : null),
    s.worst_game ? el('p', {}, 'Toughest game: ',
      el('a', { href: `/game/${s.worst_game.id}` }, `vs ${s.worst_game.opponent}`),
      ` (${s.worst_game.accuracy}% accuracy, ${s.worst_game.result}). Open its report to see the key moments.`) : null,
    el('div', { class: 'actions' },
      el('a', { class: 'btn primary big', href: '/train' }, 'Go to daily training'),
      el('a', { class: 'btn big', href: '/' }, 'Home')),
  ].filter(Boolean));
}

async function load() {
  info = await api('/api/session');
  if (!info.open) {
    $('session-title').textContent = 'No new games';
    $('session-sub').textContent = 'Sync from chess.com on the Home page to bring in your latest games.';
    $('home-link').hidden = false;
    return;
  }
  items = info.items;
  const waiting = info.games - info.analysed;
  $('session-title').textContent = `${info.games} new game${info.games === 1 ? '' : 's'}`;
  $('session-sub').textContent = items.length
    ? `${items.length} mistake${items.length === 1 ? '' : 's'} to learn from, in game order.` +
      (waiting ? ` ${waiting} game(s) are still being analysed; their mistakes will join daily training.` : '')
    : (waiting ? `Still analysing ${waiting} game(s)… come back in a few minutes.` : 'No mistakes or blunders in these games!');
  $('start-btn').hidden = !items.length;
  $('skip-session').hidden = false;
}

$('start-btn').addEventListener('click', () => {
  $('session-start').hidden = true;
  $('review').hidden = false;
  index = 0;
  showCurrent();
});
$('skip-session').addEventListener('click', finish);

load();
