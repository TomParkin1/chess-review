// Convert: play won positions from my games, and essential endgames, against Stockfish
// (chessreview/convert.py decides when a playout is won or failed). The eval stays hidden
// while playing, as in a real game, and is shown as a graph afterwards. After a failed attempt
// the move where the advantage slipped most is shown with Stockfish's choice there, and you can
// try again from that point or watch Stockfish play your side from it.
import { Chess } from 'chess.js';
import { createAnalysis } from './analysis.js';
import { choosePromotion, isPromotion, makeBoard, moveSound, sound } from './board.js';
import { lineChart } from './charts.js';
import { api, arrow, el, errorState, lastMove, legalDests, sideToMove, winChance } from './common.js';
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
let play = null;        // { task, moves, sans, fen, last, evals, streak, hints, phase, partial, lesson }
const FROM_TODAY = new URLSearchParams(location.search).get('from') === 'today';

// ------------------------------------------------------------ lists

async function loadOverview() {
  overview = await api('/api/convert');
  const pawns = overview.winning_cp / 100;
  $('games-note').textContent = overview.positions.length
    ? `The first position in each game where you were clearly winning (+${pawns} or more at your level) ` +
      'and still were a move later. Games you didn\'t win come first.'
    : overview.analysed ? 'No clearly winning positions in your analysed games yet.'
      : 'No analysed games yet: sync your chess.com games on Home, and the positions you won (or should have) appear here. ' +
        'The essential endgames are ready now.';
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
  board.setShapes([]);
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

// `resume` = {moves, evals}: continue from part-way through an earlier attempt (it then
// counts as solved with help at best).
async function start(key, from, resume = null) {
  let task;
  try {
    task = await api(`/api/convert/task?key=${encodeURIComponent(key)}`);
  } catch (err) {
    document.querySelector('main').prepend(errorState(err, () => location.reload(), 'this position'));
    return;
  }
  list = from || [];
  const previousTab = tab === 'play' ? (task.kind === 'endgame' ? 'endgames' : 'games') : (play?.returnTab || tab);
  const replayed = replay(task.fen, resume?.moves || []);
  play = { task, moves: replayed.moves, sans: replayed.sans, fen: replayed.fen, last: replayed.last,
           evals: resume?.evals || [], streak: 0, hints: 0, phase: 'mine', returnTab: previousTab,
           partial: replayed.moves.length > 0, lesson: null };
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
  if (play.partial) showFeedback('', 'Carrying on from the move where it slipped. Find a better plan this time.');
  $('play-tip').hidden = true;
  $('tip-btn').hidden = !task.tip;
  $('play-actions').hidden = false;
  $('play-done').hidden = true;
  $('watch-btn').textContent = 'Watch Stockfish play it';
  play.watched = null;
  $('game-btn').hidden = !task.game_id;
  if (task.game_id) $('game-btn').href = `/game/${task.game_id}#${task.ply}`;
  renderMoves();
  evalBar.hide(true);
  board.set({ orientation: task.color });
  board.setAutoShapes([]);
  board.setShapes([]);
  setMyTurn();
}

// UCI moves from a FEN -> the position, SANs and last move (stops at an illegal move).
function replay(fen, ucis) {
  const chess = new Chess(fen);
  const moves = [];
  const sans = [];
  for (const uci of ucis) {
    let mv;
    try { mv = chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] }); } catch { break; }
    moves.push(uci);
    sans.push(mv.san);
  }
  return { moves, sans, fen: chess.fen(), last: lastMove(moves[moves.length - 1]) };
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
  if (play !== p || p.phase !== 'waiting') return;     // gave up while choosing the piece
  const uci = orig + dest + promotion;
  board.setAutoShapes([]);
  board.set({ movable: { color: p.task.color, dests: new Map() } });   // premoves still allowed
  let r;
  try {
    r = await post('/api/play/move', { key: p.task.key, moves: p.moves, uci, streak: p.streak });
  } catch (err) {
    if (play !== p || p.phase !== 'waiting') return;
    showFeedback('bad', `Couldn't play that (${err.message}).`);
    p.phase = 'mine';
    return setMyTurn();
  }
  // Gave up (or left) while Stockfish was replying: the attempt is over, ignore the reply.
  if (play !== p || p.phase !== 'waiting') return;
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
    if (play !== p || p.phase !== 'waiting') return;
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

function renderMoves(sans = play.sans, from = 0) {
  const p = play;
  const startPly = (() => { const parts = p.task.fen.split(' '); return (Number(parts[5]) - 1) * 2 + (parts[1] === 'b' ? 1 : 0); })();
  $('play-moves').replaceChildren(...sans.map((san, i) => {
    const ply = startPly + i;
    const num = ply % 2 === 0 ? `${ply / 2 + 1}. ` : (i === 0 ? `${Math.floor(ply / 2) + 1}... ` : '');
    return el('span', { class: `sm${i >= from && from ? ' demo' : ''}` }, num + san);
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
  if (won && p.partial) result = 'solved_with_help';
  if (won) sound.correct(); else sound.wrong();
  showFeedback(won ? 'ok' : 'bad', `${won ? '✓' : '✗'} ${reason}` +
    (won && p.hints ? ` (with ${p.hints} hint${p.hints > 1 ? 's' : ''})` : ''));
  $('play-prompt').textContent = won
    ? (p.task.goal === 'win' ? 'Converted!' : 'Held!')
    : 'Not this time. It comes back soon.';
  $('play-actions').hidden = true;
  $('play-done').hidden = false;
  $('today-btn').hidden = !FROM_TODAY;
  $('next-btn').classList.toggle('primary', !FROM_TODAY);
  if (!(p.partial && !won)) post('/api/convert/result', { key: p.task.key, result }).catch(() => {});
  showLesson(p, won);
  const evals = p.evals.map(cp => Math.max(-10, Math.min(10, cp / 100)));
  lineChart($('play-chart'), {
    labels: evals.map((_, i) => `After move ${i + 1}`),
    series: [{ name: 'Your eval', values: evals, color: 'var(--series-you)', line: true, dots: true }],
    yFormat: v => (v > 0 ? `+${v.toFixed(0)}` : v.toFixed(0)), yDomain: [-10, 10], height: 150,
  });
}

// ------------------------------------------------------------ learning from it

// The move of mine after which my winning chances fell the most: {at (index into moves), drop}.
function worstMove(p) {
  let prev = p.task.start_cp ?? p.evals[0];
  let worst = null;
  p.evals.forEach((cp, i) => {
    const drop = winChance({ cp: prev }) - winChance({ cp });
    if (!worst || drop > worst.drop) worst = { at: 2 * i, drop, before: prev, after: cp };
    prev = cp;
  });
  return worst && worst.drop > 0 ? worst : null;
}

const pawns = cp => (Math.abs(cp) >= 5000 ? (cp > 0 ? 'mate' : 'getting mated')
  : `${cp > 0 ? '+' : ''}${(cp / 100).toFixed(1)}`);

async function showLesson(p, won) {
  $('play-lesson').hidden = true;
  $('lesson-actions').hidden = won;
  $('from-slip-btn').hidden = true;
  p.lesson = { at: 0 };
  if (won) return;
  const worst = worstMove(p);
  if (!worst) {
    rich('Watch how Stockfish would play your side from the start.');
    return;
  }
  p.lesson = worst;
  $('from-slip-btn').hidden = worst.at === 0;     // the first move: that is just Retry
  const san = p.sans[worst.at];
  const n = moveNumber(p, worst.at);
  rich(`It slipped most with ${n}${san} (${pawns(worst.before)} → ${pawns(worst.after)}).`);
  board.setAutoShapes([arrow(p.moves[worst.at], 'red')]);
  let r;
  try { r = await post('/api/play/hint', { key: p.task.key, moves: p.moves.slice(0, worst.at) }); } catch { return; }
  if (play !== p || !r.san) return;
  if (r.uci === p.moves[worst.at]) return;
  rich(`It slipped most with ${n}${san} (${pawns(worst.before)} → ${pawns(worst.after)}). ` +
       `Stockfish played ${r.san} there (green arrow).`);
  if (play.phase === 'done' && !analysis.active) board.setAutoShapes([arrow(p.moves[worst.at], 'red'), arrow(r.uci, 'green')]);
}

function rich(text) {
  $('play-lesson').hidden = false;
  $('play-lesson').textContent = text;
}

function moveNumber(p, index) {
  const parts = p.task.fen.split(' ');
  const ply = (Number(parts[5]) - 1) * 2 + (parts[1] === 'b' ? 1 : 0) + index;
  return ply % 2 === 0 ? `${ply / 2 + 1}. ` : `${Math.floor(ply / 2) + 1}... `;
}

// Stockfish plays my side from the lesson point (both sides at full strength), move by move.
async function watch() {
  const p = play;
  if (!p || (p.phase !== 'done' && p.phase !== 'watching')) return;
  if (p.phase === 'watching') { p.phase = 'done'; return; }   // the button is "Stop" meanwhile
  stopAnalysis();
  p.phase = 'watching';
  $('watch-btn').textContent = 'Stop';
  const at = p.lesson?.at ?? 0;
  let moves = p.moves.slice(0, at);
  let streak = 0;
  let shown = replay(p.task.fen, moves);
  board.set({ fen: shown.fen, lastMove: shown.last, check: checkOf(shown.fen), movable: { color: undefined } });
  board.setAutoShapes([]);
  renderMoves(shown.sans, at);
  showFeedback('', 'Stockfish is playing your side…');
  let outcome = 'Stopped.';
  for (let n = 0; n < 40; n++) {
    let h, r;
    try {
      h = await post('/api/play/hint', { key: p.task.key, moves });
      if (!h.uci || play !== p || p.phase !== 'watching') break;
      r = await post('/api/play/move', { key: p.task.key, moves, uci: h.uci, streak });
    } catch (err) { outcome = `Stopped (${err.message}).`; break; }
    if (play !== p || p.phase !== 'watching') break;
    moves = [...moves, h.uci, ...(r.reply_uci ? [r.reply_uci] : [])];
    streak = r.streak;
    shown = replay(p.task.fen, moves);
    board.set({ fen: shown.fen, lastMove: shown.last, check: checkOf(shown.fen) });
    moveSound(r.reply_san || r.san);
    renderMoves(shown.sans, at);
    if (r.status !== 'playing') { outcome = `Stockfish: ${r.reason}`; break; }
    await new Promise(res => setTimeout(res, 700));
  }
  if (play !== p) return;
  p.phase = 'done';
  p.watched = moves;
  $('watch-btn').textContent = 'Watch again';
  showFeedback('', outcome, ' Use Analyse to step through it.');
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
  // The whole playout (or Stockfish's demonstration) as history: ◀ steps back through it.
  analysis.start(play.task.fen, { history: play.watched || play.moves });
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
$('from-slip-btn').addEventListener('click', () => {
  const p = play;
  if (!p || p.phase !== 'done' || !p.lesson) return;
  start(p.task.key, list, { moves: p.moves.slice(0, p.lesson.at), evals: p.evals.slice(0, p.lesson.at / 2) });
});
$('watch-btn').addEventListener('click', watch);
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
  if (analysis.active && e.key === 'ArrowLeft') { e.preventDefault(); analysis.back(); }
  else if (analysis.active && e.key === 'ArrowRight') { e.preventDefault(); analysis.forward(); }
  else if (e.key === 'h') hint();
});

try {
  await loadOverview();
} catch (err) {
  document.querySelector('main').prepend(errorState(err, () => location.reload(), 'your positions'));
  await new Promise(() => {});    // nothing to show: stop here quietly
}
const key = new URLSearchParams(location.search).get('key');
if (key) start(key, key.startsWith('endgame/') ? overview.endgames.filter(x => x.unlocked) : overview.positions);
else if (overview.positions.length) preview(overview.positions[0]);
