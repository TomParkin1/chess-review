// Spar (E5): play a position from my games (or the start, or any FEN) out against Stockfish at
// about my strength (chessreview/spar.py), then review my moves at full strength. Stateless like
// Convert: the client keeps the moves and sends them with every request.
import { Chess } from 'chess.js';
import { createAnalysis } from './analysis.js';
import { choosePromotion, isPromotion, makeBoard, moveSound, sound } from './board.js';
import { api, busy, el, emptyState, errorState, guard, lastMove, legalDests, sideToMove } from './common.js';
import { createEvalBar } from './evalbar.js';

const $ = id => document.getElementById(id);
const post = (url, body) => api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const checkOf = fen => (new Chess(fen).inCheck() ? sideToMove(fen) : false);
const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const RESULT = { win: 'You won', draw: 'Draw', loss: 'You lost' };
const MARK = { inaccuracy: '?!', mistake: '?', blunder: '??' };

const board = makeBoard($('board'), {
  viewOnly: false, animation: { enabled: true, duration: 200 }, movable: { color: undefined },
  premovable: { enabled: true, showDests: true },
});
const evalBar = createEvalBar(document.querySelector('.board-wrap'));
evalBar.hide(true);
const analysis = createAnalysis({ board, panel: $('analysis'), evalBar });

let overview = null;
let game = null;    // { source, title, fen (start), color, moves, sans, pos (current FEN), offset, phase, id }

// ------------------------------------------------------------ setup

function levelOf(offset) {
  return overview.levels.find(l => l.offset === offset) || overview.levels[0];
}

function showLevel() {
  const offset = Number($('offset').value);
  const lvl = levelOf(offset);
  const rating = overview.rating ? `your ${overview.rating}` : 'no rating yet';
  $('level-label').textContent = lvl.label;
  $('level-note').textContent = `${rating} ${offset >= 0 ? '+' : '−'} ${Math.abs(offset)}` +
    (lvl.exact ? '' : ' · below 1320 Stockfish swaps some of its moves for weaker ones (measured in engine matches). ' +
                      'Your results show whether to move the slider.');
  const rec = overview.history.by_offset.find(r => r.offset === offset);
  $('level-record').textContent = rec ? `Your record at this level: ${rec.won} won, ${rec.drawn} drawn, ${rec.lost} lost.` : '';
}

const saveOffset = busy(offset => post('/api/spar/offset', { offset }).catch(() => {}));

function row(p) {
  return el('button', { class: 'convert-row', onclick: () => begin({ source: `game:${p.key}`, fen: p.fen, color: p.color,
                                                                      title: `vs ${p.opponent}, move ${Math.floor(p.ply / 2) + 1}`,
                                                                      sub: `${p.date} · ${p.phase} · ${p.eval} for you then` }),
                       onmouseenter: () => preview(p.fen, p.color) },
    el('span', { class: 'chip' }, p.color === 'white' ? '♔ White' : '♚ Black'),
    el('span', { class: 'convert-what' }, `vs ${p.opponent}`, el('span', { class: 'muted small' }, ` · ${p.date}`)),
    el('span', { class: 'eval' }, p.eval));
}

async function loadOverview() {
  overview = await api('/api/spar');
  const { min, max } = { min: overview.levels[0].offset, max: overview.levels.at(-1).offset };
  Object.assign($('offset'), { min, max, step: overview.levels[1].offset - min, value: overview.offset });
  showLevel();
  for (const phase of ['middlegame', 'endgame']) {
    const items = overview.positions[phase];
    $(`list-${phase}`).replaceChildren(...(items.length ? items.map(row)
      : [emptyState(`No ${phase} positions yet`, 'They come from your analysed games. Try the starting position under Other.')]));
  }
}

function preview(fen, color) {
  if (game) return;
  board.set({ fen, orientation: color, lastMove: undefined, check: checkOf(fen), movable: { color: undefined } });
}

function showList(name) {
  document.querySelectorAll('#tabs [data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('[data-list]').forEach(p => { p.hidden = p.dataset.list !== name; });
}

function showPanel(name) {
  document.querySelectorAll('[data-panel]').forEach(p => { p.hidden = p.dataset.panel !== name; });
}

// ------------------------------------------------------------ playing

const begin = busy(async ({ source, fen, color, title, sub }) => {
  analysis.stop?.();
  $('analysis').hidden = true;
  evalBar.hide(true);
  const offset = Number($('offset').value);
  game = { source, title, sub, fen, color, moves: [], sans: [], pos: fen, offset, phase: 'waiting', id: null };
  showPanel('play');
  $('play-title').textContent = title;
  $('play-chip').textContent = levelOf(offset).label;
  $('play-sub').textContent = `${sub} · you play ${color}`;
  $('play-feedback').hidden = true;
  $('play-done').hidden = true;
  $('play-actions').hidden = false;
  $('review').hidden = true;
  $('play-moves').replaceChildren();
  board.set({ fen, orientation: color, lastMove: undefined, check: checkOf(fen), movable: { color: undefined } });
  board.setAutoShapes([]);
  if (sideToMove(fen) !== color) await send(null);      // Stockfish moves first
  else myTurn();
});

function myTurn() {
  const g = game;
  g.phase = 'mine';
  $('play-prompt').textContent = 'Your move.';
  $('resign-btn').textContent = myMoves(g) ? 'Resign' : 'Leave';     // before my first move nothing is recorded
  board.set({
    fen: g.pos, turnColor: g.color, lastMove: lastMove(g.moves.at(-1)), check: checkOf(g.pos),
    movable: { free: false, color: g.color, dests: legalDests(new Chess(g.pos)), showDests: true,
               events: { after: (orig, dest) => onMove(orig, dest) } },
  });
  board.playPremove();
}

async function onMove(orig, dest) {
  const g = game;
  if (!g || g.phase !== 'mine') return;
  g.phase = 'waiting';
  const piece = new Chess(g.pos).get(orig);
  const promotion = isPromotion(piece, dest) ? await choosePromotion($('board'), g.color, dest, g.color) : '';
  if (game !== g || g.phase !== 'waiting') return;
  if (promotion === null) { myTurn(); return; }
  board.set({ movable: { color: g.color, dests: new Map() } });     // premoves still allowed
  await send(orig + dest + (promotion || ''));
}

// My move (or null: Stockfish to move), then Stockfish's reply.
async function send(uci) {
  const g = game;
  g.phase = 'waiting';
  $('play-prompt').textContent = 'Stockfish is thinking…';
  let r;
  try {
    r = await post('/api/spar/move', { fen: g.fen, color: g.color, moves: g.moves, uci, offset: g.offset });
  } catch (err) {
    if (game !== g) return;
    $('play-feedback').hidden = false;
    $('play-feedback').className = 'feedback bad';
    $('play-feedback').textContent = `Couldn't play that (${err.message}).`;
    myTurn();
    return;
  }
  if (game !== g || g.phase !== 'waiting') return;      // resigned meanwhile
  if (r.uci) { g.moves.push(r.uci); g.sans.push(r.san); moveSound(r.san); }
  if (r.reply_uci) { g.moves.push(r.reply_uci); g.sans.push(r.reply_san); }
  g.pos = r.fen;
  renderMoves();
  setTimeout(() => {
    if (game !== g || g.phase !== 'waiting') return;
    if (r.reply_uci) moveSound(r.reply_san);
    board.set({ fen: g.pos, lastMove: lastMove(g.moves.at(-1)), check: checkOf(g.pos) });
    if (r.status === 'playing') myTurn();
    else finish(false);
  }, r.reply_uci && r.uci ? 150 : 0);
}

// How many moves I have played (the start may be Stockfish's move).
function myMoves(g) {
  const offset = sideToMove(g.fen) === g.color ? 0 : 1;
  return g.moves.filter((_, i) => i % 2 === offset).length;
}

function renderMoves(marks = null) {
  const g = game;
  const parts = g.fen.split(' ');
  const startPly = (Number(parts[5]) - 1) * 2 + (parts[1] === 'b' ? 1 : 0);
  $('play-moves').replaceChildren(...g.sans.map((san, i) => {
    const ply = startPly + i;
    const num = ply % 2 === 0 ? `${ply / 2 + 1}. ` : (i === 0 ? `${Math.floor(ply / 2) + 1}... ` : '');
    const mark = marks?.[i];
    return el('span', { class: `sm${mark ? ` mark-${mark}` : ''}` }, num + san + (MARK[mark] || ''));
  }));
}

const finish = busy(async resigned => {
  const g = game;
  if (!g || g.phase === 'done') return;
  g.phase = 'done';
  board.set({ movable: { color: undefined } });
  board.cancelPremove?.();
  let r;
  try {
    r = await post('/api/spar/finish', { source: g.source, fen: g.fen, color: g.color, moves: g.moves,
                                         offset: g.offset, resigned });
  } catch (err) {
    $('play-prompt').textContent = `The game is over, but it couldn't be saved (${err.message}).`;
    return;
  }
  if (game !== g) return;
  g.id = r.id;
  overview.history = r.history;
  if (r.result === 'win') sound.correct(); else if (r.result === 'loss') sound.wrong();
  $('play-prompt').textContent = `${RESULT[r.result]}. ${r.reason}`;
  $('play-actions').hidden = true;
  $('play-done').hidden = false;
  $('review-btn').disabled = !g.moves.length;
  $('review-btn').focus();
});

// ------------------------------------------------------------ review (full strength)

const review = busy(async () => {
  const g = game;
  if (!g?.id) return;
  $('review').hidden = false;
  $('review-btn').disabled = true;
  const rows = [];
  const marks = [];
  let from = 0;
  try {
    while (from !== null) {
      $('review-summary').textContent = `Checking your moves at full strength… (${from} of ${g.moves.length})`;
      const r = await post('/api/spar/review', { id: g.id, from });
      if (game !== g) return;
      for (const m of r.moves) {
        marks[m.ply] = m.mine && m.judgement !== 'good' ? m.judgement : null;
        if (m.mine) rows.push(m);
      }
      from = r.next;
    }
  } catch (err) {
    $('review-summary').replaceChildren(errorState(err, () => { $('review-btn').disabled = false; review(); }, 'the review'));
    return;
  }
  renderMoves(marks);
  const n = (k, one, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;
  const count = j => rows.filter(m => m.judgement === j).length;
  $('review-summary').textContent = `Your ${n(rows.length, 'move')}: ${n(count('blunder'), 'blunder')}, ` +
    `${n(count('mistake'), 'mistake')}, ${n(count('inaccuracy'), 'inaccuracy', 'inaccuracies')}.` +
    (rows.some(m => m.judgement !== 'good') ? ' Click one to look at it with the engine.' : '');
  const bad = rows.filter(m => m.judgement !== 'good');
  $('review-moves').replaceChildren(...(bad.length ? bad.map(m => el('button', {
    class: `convert-row`, onclick: () => explore(m.ply),
  },
  el('span', { class: `chip ${m.judgement}` }, m.judgement),
  el('span', { class: 'convert-what' }, `${moveNumber(m.ply)}${m.san}`,
     el('span', { class: 'muted small' }, m.best_san ? ` · best was ${m.best_san}` : '')),
  el('span', { class: 'eval' }, `${m.before} → ${m.after}`)))
    : [el('p', { class: 'muted small' }, 'No inaccuracies, mistakes or blunders: a clean game.')]));
});

function moveNumber(i) {
  const parts = game.fen.split(' ');
  const ply = (Number(parts[5]) - 1) * 2 + (parts[1] === 'b' ? 1 : 0) + i;
  return ply % 2 === 0 ? `${ply / 2 + 1}. ` : `${Math.floor(ply / 2) + 1}... `;
}

// The position before my move, with the game's moves up to it, on the analysis board.
function explore(ply) {
  $('analysis').hidden = false;
  analysis.start(game.fen, { history: game.moves.slice(0, ply), orientation: game.color });
}

// ------------------------------------------------------------ wiring

document.querySelectorAll('#tabs [data-tab]').forEach(b => b.addEventListener('click', () => showList(b.dataset.tab)));
$('offset').addEventListener('input', showLevel);
$('offset').addEventListener('change', () => saveOffset(Number($('offset').value)));
$('start-white').addEventListener('click', () => begin({ source: 'start', fen: START_FEN, color: 'white',
                                                          title: 'From the start', sub: 'The starting position' }));
$('start-black').addEventListener('click', () => begin({ source: 'start', fen: START_FEN, color: 'black',
                                                          title: 'From the start', sub: 'The starting position' }));
$('fen-btn').addEventListener('click', () => {
  const fen = $('fen-input').value.trim();
  let color;
  try { color = new Chess(fen).turn() === 'w' ? 'white' : 'black'; } catch (err) {
    $('fen-error').textContent = `That isn't a valid FEN (${err.message}).`;
    return;
  }
  $('fen-error').textContent = '';
  begin({ source: 'fen', fen, color, title: 'Your position', sub: 'A position you entered' });
});
$('flip-btn').addEventListener('click', () => board.toggleOrientation());
$('resign-btn').addEventListener('click', () => {
  if (!game || game.phase === 'done') return;
  if (!myMoves(game)) { leave(); return; }          // nothing played: back to the positions, no result
  if (confirm('Resign this game?')) finish(true);
});
$('review-btn').addEventListener('click', review);
$('rematch-btn').addEventListener('click', () => game && begin(game));
function leave() {
  analysis.stop?.();
  $('analysis').hidden = true;
  evalBar.hide(true);
  game = null;
  board.set({ movable: { color: undefined } });
  board.cancelPremove?.();
  showPanel('setup');
  showLevel();
}
$('new-btn').addEventListener('click', leave);

guard(document.querySelector('main'), loadOverview, 'your sparring positions');
