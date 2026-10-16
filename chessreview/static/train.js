// Today: the daily workout (chessreview/coach.py). A checklist of blocks; each block runs its
// puzzles in the shared puzzle component. Puzzles failed in a block come back once at its end.
import { api, el } from './common.js';
import { createPuzzle } from './puzzle.js';
import { progressRing } from './widgets.js';

const $ = id => document.getElementById(id);
const ICONS = { reviews: '🔁', real: '⚔️', focus: '🎯', convert: '🏁', opening: '📖' };
// Blocks played on another page: [link, label to start, label when done].
const ELSEWHERE = {
  opening: b => ['/openings#drill', 'Drill on Openings →', 'Drill again'],
  convert: b => [`/convert?key=${encodeURIComponent(b.key)}`, 'Play it →', 'Play again'],
};
let plan = null;
let block = null;      // the block being played: { id, title, ... }
let queue = [];
let index = 0;
let tally = { solved: 0, failed: 0 };
const retried = new Set();

const puzzle = createPuzzle({
  onResult: (item, result) => {
    if (result.startsWith('solved')) tally.solved += 1;
    else {
      tally.failed += 1;
      const key = `${item.game_id}:${item.ply}`;
      if (!retried.has(key)) { retried.add(key); queue.push({ ...item, again: true }); }  // once more today
    }
    updateBar();
  },
  onNext: () => next(),
});

// ------------------------------------------------------------ real-game toggle (reviews block)

const BLIND_KEY = 'train-blind';
function blindParam() {
  try {
    const v = localStorage.getItem(BLIND_KEY);
    return v === null ? '' : `?blind=${v}`;
  } catch { return ''; }
}
$('blind-toggle').addEventListener('change', () => {
  try { localStorage.setItem(BLIND_KEY, $('blind-toggle').checked ? '1' : '0'); } catch { /* unavailable */ }
});

// ------------------------------------------------------------ the plan

async function loadPlan() {
  const data = await api('/api/today');
  plan = data.plan;
  let pref = null;
  try { pref = localStorage.getItem(BLIND_KEY); } catch { /* unavailable */ }
  $('blind-toggle').checked = pref === null ? data.blind : pref === '1';

  const left = plan.total - plan.finished;
  $('ring').replaceChildren(progressRing(plan.finished, plan.total, plan.complete ? '✓' : `${left}`,
                                         plan.complete ? 'done' : 'to do'));
  $('streak').replaceChildren(el('div', { class: 'flame' }, '🔥'), el('div', { class: 'streak-n' }, data.streak),
                              el('div', { class: 'muted small' }, 'day streak'));
  const minutes = Math.max(5, Math.round(left * 1.2));
  $('train-sub').textContent = plan.complete
    ? "Workout complete. Come back tomorrow; extra practice is always welcome."
    : plan.total ? `${left} positions left, about ${minutes} minutes. Built from your own games and your current weaknesses.`
      : 'Nothing to train today. Sync new games or practise anything you like.';
  $('start-btn').hidden = plan.complete || !plan.total;
  $('start-btn').textContent = plan.finished ? 'Continue workout' : 'Start workout';
  $('practice-link').hidden = !plan.complete && plan.total > 0;
  $('plan-note').replaceChildren(data.session?.open
    ? el('a', { href: '/session' }, `You also have ${data.session.games} new game(s) to review →`) : '');

  $('blocks').replaceChildren(...plan.blocks.map(renderBlock));
}

function renderBlock(b) {
  const status = b.done ? '✓' : ICONS[b.id] || '•';
  const elsewhere = ELSEWHERE[b.id]?.(b);
  const action = elsewhere
    ? el('a', { class: `btn ${b.done ? '' : 'primary'}`, href: elsewhere[0] }, b.done ? elsewhere[2] : elsewhere[1])
    : b.done ? el('span', { class: 'chip solved' }, 'Done')
      : el('button', { class: 'btn', onclick: () => startBlock(b) }, b.finished ? 'Continue' : 'Start');
  const count = b.id === 'opening' ? `${b.lines_due} due` : b.id === 'convert' ? (b.done ? '1 / 1' : '0 / 1')
    : `${b.finished} / ${b.count}`;
  return el('li', { class: `block-row ${b.done ? 'done' : ''}` },
    el('span', { class: 'block-icon' }, status),
    el('div', { class: 'block-text' }, el('strong', {}, b.title), el('div', { class: 'muted small' }, b.subtitle || '')),
    el('span', { class: 'muted small block-count' }, count),
    action);
}

function nextBlock() {
  return plan.blocks.find(b => !b.done && !ELSEWHERE[b.id]) || null;
}

// ------------------------------------------------------------ playing a block

async function startBlock(b) {
  const data = await api(`/api/today/${b.id}${b.id === 'reviews' ? blindParam() : ''}`);
  block = data.block;
  queue = data.items.filter(i => !i.done_today);
  if (!queue.length) { await loadPlan(); return; }
  index = 0;
  tally = { solved: 0, failed: 0 };
  $('plan-view').hidden = true;
  $('block-done').hidden = true;
  $('review').hidden = false;
  showCurrent();
}

function updateBar() {
  const total = queue.length;
  $('counter').textContent = `${block.title} · ${Math.min(index + 1, total)} of ${total}`;
  $('session-score').textContent = `✓ ${tally.solved} · ✗ ${tally.failed}`;
  const bar = $('session-progress');
  bar.hidden = false;
  bar.firstElementChild.style.width = `${(index / Math.max(total, 1)) * 100}%`;
}

function showCurrent() {
  const item = queue[index];
  updateBar();
  puzzle.show(item);
  // "New"/"Review" would give calm positions away in real-game mode; "Again" is fine.
  $('status-chip').hidden = !item.again && item.mode === 'blind';
  if (item.again || item.mode !== 'blind') {
    const tag = item.again ? 'Again' : item.queue === 'new' ? 'New' : item.queue === 'focus' ? 'Focus' : 'Review';
    $('status-chip').textContent = tag;
    $('status-chip').className = `chip ${item.again ? 'failed' : ''}`;
  }
}

function next() {
  index += 1;
  if (index >= queue.length) return finishBlock();
  showCurrent();
}

async function finishBlock() {
  $('review').hidden = true;
  await loadPlan();
  const following = nextBlock();
  const total = tally.solved + tally.failed;
  const box = $('block-done');
  box.hidden = false;
  box.replaceChildren(
    el('div', { class: 'celebrate' }, plan.complete ? '🏆' : '✅'),
    el('h1', {}, plan.complete ? 'Workout complete!' : `${block.title}: done`),
    el('p', { class: 'muted' }, `${tally.solved} of ${total} solved.` +
       (tally.failed ? ' The ones you missed come back soon.' : '')),
    el('div', { class: 'actions' },
      following ? el('button', { class: 'btn primary big', onclick: () => startBlock(following) }, `Next: ${following.title} ▶`) : '',
      el('button', { class: `btn big ${following ? '' : 'primary'}`, onclick: () => showPlan() }, 'Back to the plan'),
      plan.complete ? el('a', { class: 'btn big', href: '/progress' }, 'See your progress') : ''),
  );
}

function showPlan() {
  $('block-done').hidden = true;
  $('review').hidden = true;
  $('plan-view').hidden = false;
}

$('start-btn').addEventListener('click', () => {
  const b = nextBlock();
  if (b) startBlock(b);
});

loadPlan();
