// Train: today's spaced-repetition queue (due mistakes + a few new ones), Chessly style.
// Puzzles failed during the session come back once more at the end ("again").
import { api, el } from './common.js';
import { createPuzzle } from './puzzle.js';
import { progressRing } from './widgets.js';

const $ = id => document.getElementById(id);
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

function updateBar() {
  const total = queue.length;
  $('counter').textContent = `Puzzle ${Math.min(index + 1, total)} of ${total}`;
  $('session-score').textContent = `✓ ${tally.solved} · ✗ ${tally.failed}`;
  const bar = $('session-progress');
  bar.hidden = false;
  bar.firstElementChild.style.width = `${(index / Math.max(total, 1)) * 100}%`;
}

function next() {
  index += 1;
  if (index >= queue.length) return finish();
  showCurrent();
}

function showCurrent() {
  const item = queue[index];
  updateBar();
  puzzle.show(item);
  const tag = item.again ? 'Again' : item.queue === 'new' ? 'New' : 'Review';
  $('status-chip').hidden = item.mode === 'blind' && !item.again;   // "New"/"Review" would give calm positions away
  $('status-chip').textContent = tag;
  $('status-chip').className = `chip ${item.again ? 'failed' : item.queue === 'new' ? '' : 'solved'}`;
}

async function finish() {
  $('review').hidden = true;
  const stats = await api('/api/train/stats');
  const total = tally.solved + tally.failed;
  const box = $('train-done');
  box.hidden = false;
  box.replaceChildren(
    el('div', { class: 'celebrate' }, total && tally.failed === 0 ? '🏆' : '🎉'),
    el('h1', {}, 'Session complete!'),
    el('p', { class: 'muted' }, `${tally.solved} of ${total} solved.` +
       (tally.failed ? ' The ones you missed come back tomorrow.' : ' Perfect run!')),
    el('div', { class: 'chips' },
      el('span', { class: 'chip solved' }, `✓ ${tally.solved} solved`),
      el('span', { class: 'chip failed' }, `✗ ${tally.failed} to repeat`),
      el('span', { class: 'chip' }, `🔥 ${stats.streak}-day streak`),
      el('span', { class: 'chip' }, `★ ${stats.mastered} mastered`)),
    el('div', { class: 'actions' },
      el('a', { class: 'btn primary big', href: '/' }, 'Home'),
      el('a', { class: 'btn big', href: '/review' }, 'Practice more')),
  );
}

// Real-game mode (default from config.toml, remembered per browser once changed).
const BLIND_KEY = 'train-blind';
function blindPref() {
  try { return localStorage.getItem(BLIND_KEY); } catch { return null; }
}
$('blind-toggle').addEventListener('change', () => {
  try { localStorage.setItem(BLIND_KEY, $('blind-toggle').checked ? '1' : '0'); } catch { /* unavailable */ }
  load();
});

async function load() {
  const pref = blindPref();
  const data = await api(`/api/train/queue${pref === null ? '' : `?blind=${pref}`}`);
  $('blind-toggle').checked = data.blind;
  const s = data.stats;
  queue = data.items;
  const doneToday = s.done_today;
  const goal = doneToday + queue.length;
  $('ring').replaceChildren(progressRing(doneToday, goal, queue.length ? `${queue.length}` : '✓',
                                         queue.length ? 'to do' : 'done'));
  $('streak').replaceChildren(el('div', { class: 'flame' }, '🔥'), el('div', { class: 'streak-n' }, s.streak),
                              el('div', { class: 'muted small' }, 'day streak'));
  $('train-chips').replaceChildren(
    el('span', { class: 'chip' }, `${s.due} due for review`),
    el('span', { class: 'chip' }, `${s.new} new`),
    s.lines_due ? el('a', { class: 'chip failed', href: '/openings' }, `📖 ${s.lines_due} opening positions due →`) : '',
  );
  if (queue.length) {
    $('train-sub').textContent = data.blind
      ? 'Positions from your games, as you met them: spot the threat, then play your move. Some are mistakes you made, some you got right.'
      : 'Solve the positions you got wrong. Ones you miss come back sooner; ones you know drift away.';
    $('start-btn').hidden = false;
  } else {
    $('train-sub').textContent = s.session?.open
      ? 'Nothing due. You have new games waiting: start a new-games session from Home.'
      : "All done for today. Come back tomorrow, or practise anything you like.";
    $('practice-link').hidden = false;
  }
  $('train-stats').replaceChildren(
    ...[[s.learning, 'learning'], [s.mastered, 'mastered'], [s.unseen, 'not seen yet'], [doneToday, 'solved today']]
      .map(([v, label]) => el('div', { class: 'stat' }, el('div', { class: 'value' }, v), el('div', { class: 'label' }, label))));
}

$('start-btn').addEventListener('click', () => {
  $('train-start').hidden = true;
  $('train-stats').hidden = true;
  $('review').hidden = false;
  index = 0;
  showCurrent();
});

load();
