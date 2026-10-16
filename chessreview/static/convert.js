// Convert: play won positions from my games, and essential endgames, against Stockfish
// (chessreview/convert.py decides when a playout is won or failed). The eval stays hidden
// while playing, as in a real game, and is shown as a graph afterwards.
import { Chess } from 'chess.js';
import { createAnalysis } from './analysis.js';
import { choosePromotion, isPromotion, makeBoard, moveSound, sound } from './board.js';
import { lineChart } from './charts.js';
import { api, arrow, el, lastMove, legalDests, sideToMove } from './common.js';
import { createEvalBar } from './evalbar.js';

const $ = id => document.getElementById(id);
const post = (url, body) => api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const checkOf = fen => (new Chess(fen).inCheck() ? sideToMove(fen) : false);

const board = makeBoard($('board'), {
  viewOnly: false, animation: { enabled: true, duration: 200 }, movable: { color: undefined },
  premovable: { enabled: true, showDests: true },
});
const evalBar = createEvalBar(document.querySelector('.board-wrap'));
evalBar.hide(true);
const analysis = createAnalysis({ board, panel: $('analysis'), evalBar });

let overview = null;
let list = [];          // the list the current task came from (for "Next")
let tab = 'games';
let play = null;        // { task, moves, sans, fen, last, evals, streak, hints, phase }

// ------------------------------------------------------------ lists

async function loadOverview() {
  overview = await api('/api/convert');
  const pawns = overview.winning_cp / 100;
  $('games-note').textContent = overview.positions.length
    ? `The first position in each game where you were clearly winning (+${pawns} or more at your level) ` +
      'and still were a move later. Games you didn\'t win come first.'
    : 'No clearly winning positions in your analysed games yet.';
  $('games-list').replaceChildren(...overview.positions.map(p => el('button', {
    class: 'convert-row', onclick: () => start(p.key, overview.positions), onmouseenter: () => preview(p),
  },
  el('span', { class: `chip ${p.status}` }, STATUS[p.status]),
  el('span', { class: 'convert-what' }, `vs ${p.opponent}`, el('span', { class: 'muted small' }, ` · ${p.date}`)),
  el('span', { class: 'eval' }, p.eval),
  el('span', { class: `muted small result-${p.result}` }, { win: 'won', loss: 'lost', draw: 'drawn' }[p.result]))));

  const byLevel = {};
  overview.endgames.forEach(e => { (byLevel[e.level] ||= []).push(e); });
  $('endgame-list').replaceChildren(...Object.entries(byLevel).map(([lvl, items]) => el('div', { class: 'endgame-level' },
    el('div', { class: 'section-title' }, overview.levels[lvl]),
    ...items.map(e => el('button', {
      class: `convert-row ${e.unlocked ? '' : 'locked'}`, disabled: e.unlocked ? null : 'disabled',
      onclick: () => start(e.key, overview.endgames.filter(x => x.unlocked)), onmouseenter: () => preview(e),
    },
    el('span', { class: `chip ${e.status}` }, e.unlocked ? STATUS[e.status] : '🔒'),
    el('span', { class: 'convert-what' }, e.title),
    el('span', { class: 'muted small' }, e.goal === 'win' ? 'win' : 'hold the draw'))))));
}

const STATUS = { new: 'New', solved: 'Solved', failed: 'Try again', mastered: 'Mastered' };

// Hovering a row in the lists shows its position (never during a playout).
function preview(p) {
  if (play) return;
  board.set({ fen: p.fen, orientation: p.color, lastMove: undefined, check: checkOf(p.fen), movable: { color: undefined } });
  board.setAutoShapes([]);
}

function showTab(name) {
  tab = name;
  document.querySelectorAll('#tabs [data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('[data-panel]').forEach(p => { p.hidden = p.dataset.panel !== name; });
  $('tabs').hidden = name === 'play';
}

function backToList() {
  stopAnalysis();
  const back = play?.returnTab || 'games';
  play = null;
  board.set({ movable: { color: undefined } });
  board.setAutoShapes([]);
  evalBar.hide(true);
  showTab(back);
  loadOverview();
}

// ------------------------------------------------------------ playing

async function start(key, from) {
  const task = await api(`/api/convert/task?key=${encodeURIComponent(key)}`);
  list = from || [];
  const previousTab = tab === 'play' ? (task.kind === 'endgame' ? 'endgames' : 'games') : tab;
  play = { task, moves: [], sans: [], fen: task.fen, last: undefined, evals: [], streak: 0, hints: 0,
           phase: 'mine', returnTab: previousTab };
  stopAnalysis();
  showTab('play');
  $('play-title').textContent = task.title;
  $('play-sub').textContent = task.subtitle;
  $('play-chip').textContent = task.goal === 'win' ? 'Win it' : 'Hold the draw';
  $('play-chip').className = `chip ${task.goal === 'win' ? 'solved' : ''}`;
  const side = task.color === 'white' ? 'White' : 'Black';
  $('play-prompt').textContent = task.kind === 'convert'
    ? `You're ${task.start_eval} as ${side}. Win it: Stockfish defends.`
    : task.goal === 'win' ? `Win as ${side}${task.finish === 'mate' ? ' by checkmate' : ''}. Stockfish defends.`
      : `Hold the draw as ${side}. Stockfish attacks.`;
  $('play-feedback').hidden = true;
  $('play-tip').hidden = true;
  $('tip-btn').hidden = !task.tip;
  $('play-actions').hidden = false;
  $('play-done').hidden = true;
  $('game-btn').hidden = !task.game_id;
  if (task.game_id) $('game-btn').href = `/game/${task.game_id}#${task.ply}`;
  renderMoves();
  evalBar.hide(true);
  board.set({ orientation: task.color });
  board.setAutoShapes([]);
  setMyTurn();
}

function setMyTurn() {
  const p = play;
  board.set({
    fen: p.fen, turnColor: p.task.color, lastMove: p.last, check: checkOf(p.fen),
    movable: { free: false, color: p.task.color, dests: legalDests(new Chess(p.fen)), showDests: true,
               events: { after: (orig, dest) => onMove(orig, dest) } },   // ignore chessground's metadata arg
  });
  board.playPremove();
}

async function onMove(orig, dest) {
  const p = play;
  if (!p || p.phase !== 'mine') return;
  p.phase = 'waiting';
  const piece = new Chess(p.fen).get(orig);
  const promotion = isPromotion(piece, dest) ? await choosePromotion($('board'), p.task.color, dest, p.task.color) : '';
  const uci = orig + dest + promotion;
  board.setAutoShapes([]);
  board.set({ movable: { color: p.task.color, dests: new Map() } });   // premoves still allowed
  let r;
  try {
    r = await post('/api/play/move', { key: p.task.key, moves: p.moves, uci, streak: p.streak });
  } catch (err) {
    showFeedback('bad', `Couldn't play that (${err.message}).`);
    p.phase = 'mine';
    return setMyTurn();
  }
  if (play !== p) return;
  moveSound(r.san);
  p.moves.push(uci);
  p.sans.push(r.san);
  p.streak = r.streak;
  if (r.reply_uci) {
    p.moves.push(r.reply_uci);
    p.sans.push(r.reply_san);
    p.evals.push(r.cp);
  }
  p.fen = r.fen;
  p.last = lastMove(r.reply_uci || uci);
  renderMoves();
  setTimeout(() => {
    if (play !== p) return;
    if (r.reply_uci) moveSound(r.reply_san);
    if (r.status === 'playing') {
      p.phase = 'mine';
      setMyTurn();
    } else {
      board.set({ fen: p.fen, lastMove: p.last, check: checkOf(p.fen), movable: { color: undefined } });
      finish(r.status === 'won' ? (p.hints ? 'solved_with_help' : 'solved') : 'failed', r.reason);
    }
  }, r.reply_uci ? 150 : 0);
}

function renderMoves() {
  const p = play;
  const startPly = (() => { const parts = p.task.fen.split(' '); return (Number(parts[5]) - 1) * 2 + (parts[1] === 'b' ? 1 : 0); })();
  $('play-moves').replaceChildren(...p.sans.map((san, i) => {
    const ply = startPly + i;
    const num = ply % 2 === 0 ? `${ply / 2 + 1}. ` : (i === 0 ? `${Math.floor(ply / 2) + 1}... ` : '');
    return el('span', { class: 'sm' }, num + san);
  }));
}

function showFeedback(cls, text) {
  $('play-feedback').hidden = false;
  $('play-feedback').className = `feedback ${cls}`;
  $('play-feedback').textContent = text;
}

async function hint() {
  const p = play;
  if (!p || p.phase !== 'mine') return;
  const r = await post('/api/play/hint', { key: p.task.key, moves: p.moves });
  if (play !== p || !r.uci) return;
  p.hints += 1;
  board.setAutoShapes([arrow(r.uci, 'green')]);
}

function finish(result, reason) {
  const p = play;
  p.phase = 'done';
  const won = result !== 'failed';
  if (won) sound.correct(); else sound.wrong();
  showFeedback(won ? 'ok' : 'bad', `${won ? '✓' : '✗'} ${reason}` +
    (won && p.hints ? ` (with ${p.hints} hint${p.hints > 1 ? 's' : ''})` : ''));
  $('play-prompt').textContent = won
    ? (p.task.goal === 'win' ? 'Converted!' : 'Held!')
    : 'Not this time. It comes back soon.';
  $('play-actions').hidden = true;
  $('play-done').hidden = false;
  post('/api/convert/result', { key: p.task.key, result }).catch(() => {});
  const evals = p.evals.map(cp => Math.max(-10, Math.min(10, cp / 100)));
  lineChart($('play-chart'), {
    labels: evals.map((_, i) => `After move ${i + 1}`),
    series: [{ name: 'Your eval', values: evals, color: 'var(--series-you)', line: true, dots: true }],
    yFormat: v => (v > 0 ? `+${v.toFixed(0)}` : v.toFixed(0)), yDomain: [-10, 10], height: 150,
  });
}

// ------------------------------------------------------------ analysis + buttons

function stopAnalysis() {
  if (!analysis.active) return;
  analysis.stop();
  $('analysis').hidden = true;
  $('analyse-btn').textContent = 'Analyse';
}

$('analyse-btn').addEventListener('click', () => {
  if (!play || play.phase !== 'done') return;
  if (analysis.active) { stopAnalysis(); return; }
  $('analysis').hidden = false;
  analysis.start(play.fen, { last: play.last });
  $('analyse-btn').textContent = 'Stop analysis';
});
$('hint-btn').addEventListener('click', hint);
$('tip-btn').addEventListener('click', () => {
  if (!play) return;
  $('play-tip').hidden = false;
  $('play-tip').textContent = play.task.tip;
});
$('resign-btn').addEventListener('click', () => {
  if (play && play.phase !== 'done') {
    board.set({ movable: { color: undefined } });
    finish('failed', 'You gave up.');
  }
});
$('retry-btn').addEventListener('click', () => play && start(play.task.key, list));
$('next-btn').addEventListener('click', () => {
  if (!play) return;
  const i = list.findIndex(x => x.key === play.task.key);
  const following = list.slice(i + 1).find(x => x.status !== 'mastered');
  if (following) start(following.key, list);
  else backToList();
});
$('list-btn').addEventListener('click', backToList);
document.querySelectorAll('#tabs [data-tab]').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || !play) return;
  if (e.key === 'h') hint();
});

await loadOverview();
const key = new URLSearchParams(location.search).get('key');
if (key) start(key, key.startsWith('endgame/') ? overview.endgames.filter(x => x.unlocked) : overview.positions);
else if (overview.positions.length) preview(overview.positions[0]);
