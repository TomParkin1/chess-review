// Today: the daily workout (chessreview/coach.py). A checklist of blocks; each block runs its
// puzzles in the shared puzzle component. Puzzles failed in a block come back once at its end.
import { api, busy, el, guard, isSolved, itemKey, setThreatCheckPref, threatCheckPref } from './common.js';
import { createPuzzle } from './puzzle.js';
import { showTodayBadge } from './shell.js';
import { progressRing } from './widgets.js';

const $ = id => document.getElementById(id);
const ICONS = { reviews: '🔁', convert: '🏁', endgame: '♟️', real: '⚔️', focus: '🎯' };
// Blocks played on another page: [link, label to start, label when done]. `from=today` makes
// that page offer a way back here when it is done. The endgame to play (an essential endgame or
// a won position) is played on the Convert page.
const ELSEWHERE = {
  convert: b => [`/convert?from=today&key=${encodeURIComponent(b.key)}`, 'Play it →', 'Play again'],
};
let plan = null;
let block = null;      // the block being played: { id, title, ... }
let queue = [];
let index = 0;
let tally = { solved: 0, failed: 0 };
const retried = new Set();
const counted = new Set();   // items in the block's tally (reset with it)

const puzzle = createPuzzle({
  onResult: (item, result) => {
    const key = itemKey(item);
    const tried = `${key}${item.again ? ':again' : ''}`;   // "Again" is its own attempt; Retry isn't
    if (counted.has(tried)) return;
    counted.add(tried);
    if (isSolved(result)) tally.solved += 1;
    else {
      tally.failed += 1;
      if (!retried.has(key)) { retried.add(key); queue.push({ ...item, again: true }); }  // once more today
    }
    updateBar();
  },
  onNext: () => next(),
});

// ------------------------------------------------------------ "Threat check first" toggle (reviews block)

function blindParam() {
  const pref = threatCheckPref();     // shared with Practice
  return pref === null ? '' : `?blind=${pref ? 1 : 0}`;
}
$('blind-toggle').addEventListener('change', () => setThreatCheckPref($('blind-toggle').checked));

// ------------------------------------------------------------ the plan

// Every plan request takes a number; only the newest one's reply is drawn (a slow poll must not
// redraw over a newer budget or refresh).
let planSeq = 0;
async function fetchPlan(url, options) {
  const seq = ++planSeq;
  const data = await api(url, options);
  if (seq === planSeq) render(data);
}

function loadPlan() {
  return guard($('plan-view'), async () => {
    try {
      await fetchPlan('/api/today');
    } catch (err) {
      $('train-sub').textContent = "Couldn't load your plan.";
      $('plan-card').hidden = true;
      throw err;
    }
  }, 'your plan');
}

// Busy: the stale note's button and the header's both rebuild the plan once (B12).
const refreshPlan = busy(async () => {
  $('refresh-btn').disabled = true;
  try {
    await guard($('plan-view'), () => fetchPlan('/api/today/refresh', { method: 'POST' }), 'your plan');
  } finally {
    $('refresh-btn').disabled = false;
  }
});

// The time budget: the plan is rebuilt for it (what's done today is kept and counts).
const setBudget = busy(minutes => guard($('plan-view'), () => fetchPlan('/api/today/budget', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ minutes }),
}), 'your plan'));

function renderBudget(data) {
  $('budget-row').hidden = !data.plan.total && !data.plan.blocks.length;
  $('budget').replaceChildren(...data.budgets.map(m => el('button', {
    class: m === data.plan.budget ? 'active' : '', title: `Plan about ${m} minutes of training a day`,
    onclick: () => { if (m !== plan.budget) setBudget(m); },
  }, `${m} min`)));
}

// While the first games are being analysed there is no plan yet: look again every few seconds.
let waitTimer = null;
function waitForGames(games) {
  clearTimeout(waitTimer);
  if (!plan.total && games.analysed < games.total) waitTimer = setTimeout(loadPlan, 4000);
}

function render(data) {
  plan = data.plan;
  showTodayBadge(plan);
  const pref = threatCheckPref();
  $('blind-toggle').checked = pref === null ? data.blind : pref;

  const left = plan.total - plan.finished;
  $('ring').hidden = !plan.total;      // an empty plan isn't "done": there was nothing to do yet
  $('ring').replaceChildren(progressRing(plan.finished, plan.total, plan.complete ? '✓' : `${left}`,
                                         plan.complete ? 'done' : 'to do'));
  $('streak').replaceChildren(el('div', { class: 'flame' }, '🔥'), el('div', { class: 'streak-n' }, data.streak),
                              el('div', { class: 'muted small' }, 'day streak'));
  const minutes = Math.max(1, plan.minutes_left);
  const { total: games, analysed } = data.games;
  const firstRun = !games || !analysed;
  $('train-sub').textContent =
    !data.username ? 'Welcome! Set your chess.com username on Home, and your first workout is built from your games.'
    : !games ? 'No games yet. Sync your chess.com games on Home, and your first workout is built from them.'
    : !analysed ? `Analysing your games (${analysed} of ${games} done). Your first workout appears here as soon as ` +
                  'the first ones are ready.'
    : plan.complete && plan.total ? "Workout complete. Come back tomorrow; extra practice is always welcome."
    : plan.total ? `${left} positions left, about ${minutes} minutes. Built from your own games and your current weaknesses.`
    : analysed < games ? `Nothing to train yet: ${games - analysed} game(s) are still being analysed.`
    : 'Nothing to train today. Sync new games or practise anything you like.';
  $('start-btn').hidden = plan.complete || !plan.total;
  $('start-btn').textContent = plan.finished ? 'Continue workout' : 'Start workout';
  $('home-link').hidden = !(firstRun && analysed === games);    // nothing analysing: Home is the next step
  $('practice-link').hidden = firstRun || (!plan.complete && plan.total > 0);
  $('refresh-btn').hidden = firstRun;
  $('plan-note').replaceChildren(data.session?.open
    ? el('a', { href: '/session' }, `You also have ${data.session.games} new game(s) to review →`) : '');
  $('stale-note').hidden = !plan.stale;
  $('stale-note').replaceChildren(
    `${plan.new_games} new game${plan.new_games === 1 ? ' was' : 's were'} analysed since this plan was made. `,
    el('button', { class: 'btn small', onclick: refreshPlan }, '⟳ Refresh plan'),
    el('span', { class: 'muted small' }, " (what you've done today is kept)"));
  $('plan-card').hidden = firstRun && !plan.blocks.length;
  $('blocks').replaceChildren(...plan.blocks.map(renderBlock));
  renderBudget(data);
  waitForGames(data.games);
}

function renderBlock(b) {
  const status = b.done ? '✓' : ICONS[b.id] || '•';
  const elsewhere = ELSEWHERE[b.id]?.(b);
  const action = elsewhere
    ? el('a', { class: `btn ${b.done ? '' : 'primary'}`, href: elsewhere[0] }, b.done ? elsewhere[2] : elsewhere[1])
    : b.done ? el('span', { class: 'chip solved' }, 'Done')
      : el('button', { class: 'btn', onclick: () => startBlock(b) }, b.finished ? 'Continue' : 'Start');
  const count = b.id === 'convert' ? (b.done ? '1 / 1' : '0 / 1')
    : `${b.finished} / ${b.count}`;
  // E6: a calculation weakness also gets a link to the trainer (an extra: not in the plan's count).
  const calc = b.calc ? el('div', { class: 'small' }, el('a', { href: '/calculate?from=today' },
    'Also train it by calculating lines →')) : '';
  return el('li', { class: `block-row ${b.done ? 'done' : ''}` },
    el('span', { class: 'block-icon' }, status),
    el('div', { class: 'block-text' }, el('strong', {}, b.title), el('div', { class: 'muted small' }, b.subtitle || ''), calc),
    el('span', { class: 'muted small block-count' }, count),
    action);
}

function nextBlock() {
  return plan.blocks.find(b => !b.done) || null;
}

// Start a block: here for puzzles, on the Convert page for the endgame to play.
function goTo(b) {
  const elsewhere = ELSEWHERE[b.id]?.(b);
  if (elsewhere) location.href = elsewhere[0];
  else startBlock(b);
}

// ------------------------------------------------------------ playing a block

// Busy: a double click on Start/Continue/Next used to fetch and start the block twice (B12).
const startBlock = busy(async b => {
  let data;
  try {
    data = await api(`/api/today/${b.id}${b.id === 'reviews' ? blindParam() : ''}`);
  } catch (err) {
    showPlan();
    await loadPlan();      // the plan may have been rebuilt meanwhile (e.g. after new games)
    if (err.status !== 404) $('plan-view').prepend(el('div', { class: 'state-box error-state', role: 'alert' },
      el('strong', {}, `Couldn't start ${b.title}.`), el('p', { class: 'muted' }, err.message)));
    return;
  }
  block = data.block;
  queue = data.items.filter(i => !i.done_today);
  if (!queue.length) { await loadPlan(); return; }
  index = 0;
  tally = { solved: 0, failed: 0 };
  counted.clear();
  $('plan-view').hidden = true;
  $('block-done').hidden = true;
  $('review').hidden = false;
  showCurrent();
  $('move-title').focus({ preventScroll: true });     // keyboard users start in the puzzle
});

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
    const tag = item.again ? 'Again' : { new: 'New', focus: 'Focus', endgame: 'Endgame' }[item.queue] || 'Review';
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
    el('h1', { tabindex: '-1' }, plan.complete ? 'Workout complete!' : `${block.title}: done`),
    el('p', { class: 'muted' }, `${tally.solved} of ${total} solved.` +
       (tally.failed ? ' The ones you missed come back soon.' : '')),
    el('div', { class: 'actions' },
      following ? el('button', { class: 'btn primary big', onclick: () => goTo(following) }, `Next: ${following.title} ▶`) : '',
      el('button', { class: `btn big ${following ? '' : 'primary'}`, onclick: () => showPlan() }, 'Back to the plan'),
      plan.complete ? el('a', { class: 'btn big', href: '/progress' }, 'See your progress') : ''),
  );
  box.querySelector('h1').focus();      // keyboard and screen-reader users land on the result
}

function showPlan() {
  $('block-done').hidden = true;
  $('review').hidden = true;
  $('plan-view').hidden = false;
}

$('start-btn').addEventListener('click', () => {
  const b = nextBlock();
  if (b) goTo(b);
});
$('refresh-btn').addEventListener('click', refreshPlan);

loadPlan();
