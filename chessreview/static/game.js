// Game Report: a chess.com-style review of one game: coach, accuracy, move marks,
// key moments, eval graph, time per move, plus stepping through moves and free analysis.
import { createAnalysis } from './analysis.js';
import { createBadge, makeBoard, moveSound } from './board.js';
import { api, arrow, el, emptyState, errorState, formatEval, lastMove, playedAt, sideToMove, winChance } from './common.js';
import { createEvalBar } from './evalbar.js';
import { MARK_ORDER, MARKS, markIcon } from './icons.js';

let data;
try {
  data = await api(`/api/game/${window.GAME_ID}`);
} catch (err) {
  document.querySelector('.game-layout').replaceChildren(err.status === 404
    ? emptyState('Game not found', 'It may have been deleted, or the link is out of date.', { href: '/games', label: 'All games' })
    : errorState(err, () => location.reload(), 'this game'));
  await new Promise(() => {});    // nothing to show: stop here quietly
}
const { game, positions } = data;
const $ = id => document.getElementById(id);
const myColor = game.my_color;
const analysed = positions.some(p => p.eval);
const hasClocks = positions.some(p => p.spent_ms != null);
let current = 0;
let orientation = myColor;

// viewOnly must be false at creation so the analysis board can take moves later.
const board = makeBoard($('board'), {
  orientation, viewOnly: false, movable: { color: undefined }, animation: { enabled: false },
});
const badge = createBadge(board, $('board'));
const evalBar = createEvalBar(document.querySelector('.board-wrap'));
const analysis = createAnalysis({ board, panel: $('analysis'), evalBar });

const moveLabel = i => `${Math.floor(i / 2) + 1}${i % 2 === 0 ? '.' : '...'}${positions[i].move_san}`;
const whoMoved = i => (positions[i].is_mine ? 'You' : 'Opponent');
const BAD = new Set(['inaccuracy', 'mistake', 'miss', 'blunder']);

// ------------------------------------------------------------ coach

const OPPONENT_KIND = {
  hanging_piece: 'It leaves a piece hanging.', fork: 'It allows a fork.', allowed_mate: 'It allows a forced mate.',
  missed_mate: 'They missed a forced mate.', missed_capture: 'They missed a way to win material.',
  missed_tactic: 'They missed a tactic.', loses_material: 'It loses material.', king_safety: 'It exposes their king.',
  missed_opportunity: 'They let their advantage slip.', positional: 'It weakens their position.',
  discovered_attack: 'It allows a discovered attack.',
};
const VERDICT = {
  brilliant: 'is brilliant!!', great: 'is a great move!', best: 'is best', excellent: 'is excellent', good: 'is good',
  book: 'is a book move', forced: 'is forced', inaccuracy: 'is an inaccuracy', mistake: 'is a mistake',
  miss: 'is a miss', blunder: 'is a blunder',
};
const GOOD_TEXT = {
  brilliant: () => 'A sound sacrifice: giving up material that the engine fully approves of.',
  great: () => 'The only move that keeps the position: everything else is much worse.',
  best: () => "The engine's top choice.",
  excellent: p => `Almost as good as the best move${p.best_san ? ` (${p.best_san})` : ''}.`,
  good: p => `A solid move.${p.best_san ? ` Best was ${p.best_san}.` : ''}`,
  book: () => `A known opening move: ${game.opening}.`,
  forced: () => 'The only legal move.',
};

function renderCoach() {
  const coach = $('coach');
  const i = current - 1;
  const avatar = el('div', { class: 'coach-avatar' }, '♞');
  if (i < 0) {
    coach.replaceChildren(avatar, el('div', { class: 'coach-text' },
      el('div', { class: 'coach-title' }, 'Game Report'),
      el('div', { class: 'coach-body' }, analysed
        ? `${game.opening}. Step through the moves with → or click any move, mark or moment.`
        : 'This game is still being analysed. Moves are available; marks and evaluations will follow.')));
    return;
  }
  const p = positions[i];
  const mark = p.mark;
  const label = mark ? MARKS[mark].label.toLowerCase() : '';
  const title = el('div', { class: 'coach-title' });
  title.innerHTML = markIcon(mark, 22);
  const verdict = mark ? VERDICT[mark] : '';
  title.append(` ${p.move_san} ${verdict}`, el('span', { class: 'eval' }, formatEval(positions[current].eval)));
  const body = el('div', { class: 'coach-body' });
  if (BAD.has(mark)) {
    if (p.is_mine) body.append(p.explanation || '');
    else {
      body.append(`Your opponent's ${label}. ${OPPONENT_KIND[p.kind] || ''}`);
      const reply = positions[i + 1];
      if (BAD.has(reply?.mark)) body.append(` You didn't take the chance: ${reply.move_san}.`);
      else if (['best', 'great', 'brilliant', 'excellent'].includes(reply?.mark)) body.append(` You punished it with ${reply.move_san}.`);
    }
    if (p.best_line) body.append(el('span', { class: 'line' }, `Best line: ${p.best_line}`));
  } else if (mark && GOOD_TEXT[mark]) {
    body.append(GOOD_TEXT[mark](p));
  }
  if (p.spent_ms != null) body.append(el('span', { class: 'line' }, `Time spent: ${formatTime(p.spent_ms)}`));
  coach.replaceChildren(avatar, el('div', { class: 'coach-text' }, title, body));
}

// ------------------------------------------------------------ navigation

function goTo(index, { sound = false } = {}) {
  if (analysis.active) stopAnalysis();
  const step = index - current;
  current = Math.max(0, Math.min(positions.length - 1, index));
  const pos = positions[current];
  const prev = positions[current - 1];

  board.set({ fen: pos.fen, turnColor: sideToMove(pos.fen), lastMove: lastMove(prev?.move_uci),
              check: /[+#]$/.test(prev?.move_san || '') ? sideToMove(pos.fen) : false });
  if (sound && step === 1) moveSound(prev?.move_san);
  badge.show(prev?.move_uci?.slice(2, 4), prev?.mark);

  const shapes = [];
  if ($('best-toggle').checked) {
    if (prev && BAD.has(prev.mark)) shapes.push(arrow(prev.best_uci, 'green'));   // what should have been played
    else if (pos.best_uci && current < positions.length - 1) shapes.push(arrow(pos.best_uci, 'paleBlue'));
  }
  board.setAutoShapes(shapes.filter(Boolean));
  evalBar.set(analysed ? pos.eval : null, orientation);

  renderCoach();
  document.querySelectorAll('#moves .mv').forEach(m => m.classList.toggle('current', +m.dataset.index === current));
  document.querySelector('#moves .mv.current')?.scrollIntoView({ block: 'nearest' });
  document.querySelectorAll('#timechart .bar').forEach(b => b.classList.toggle('current', +b.dataset.index === current));
  drawCursor();
  history.replaceState(null, '', `#${current}`);
}

function flip() {
  orientation = orientation === 'white' ? 'black' : 'white';
  board.set({ orientation });
  goTo(current);
}

function startAnalysis() {
  showTab('moves');
  // Start a few moves back, so ◀ can step through the moves that led here.
  const from = Math.max(0, current - 8);
  analysis.start(positions[from].fen, { last: lastMove(positions[from - 1]?.move_uci),
                                       history: positions.slice(from, current).map(p => p.move_uci) });
  badge.clear();
  $('analyse-btn').textContent = 'Back to game';
  $('analyse-btn').classList.add('active');
}

function stopAnalysis() {
  analysis.stop();
  $('analyse-btn').textContent = 'Analyse';
  $('analyse-btn').classList.remove('active');
}

function showTab(name) {
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('[data-panel]').forEach(p => { p.hidden = p.dataset.panel !== name; });
}

document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
document.querySelectorAll('[data-nav]').forEach(b => b.addEventListener('click', () => {
  const target = { first: 0, prev: current - 1, next: current + 1, last: positions.length - 1 }[b.dataset.nav];
  goTo(target, { sound: true });
}));
$('flip-btn').addEventListener('click', flip);
$('best-toggle').addEventListener('change', () => goTo(current));
$('analyse-btn').addEventListener('click', () => {
  if (analysis.active) { stopAnalysis(); goTo(current); } else startAnalysis();
});
$('start-review').addEventListener('click', () => { showTab('moves'); goTo(1, { sound: true }); });
$('practise-link').href = `/review?game=${game.id}`;

document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || e.metaKey || e.ctrlKey) return;
  if (analysis.active) {
    if (e.key === 'ArrowLeft') { e.preventDefault(); analysis.back(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); analysis.forward(); }
    else if (e.key === 'Escape' || e.key === 'a') { stopAnalysis(); goTo(current); }
    else if (e.key === 'f') flip();
    return;
  }
  const keys = { ArrowLeft: current - 1, ArrowRight: current + 1, ArrowUp: 0, ArrowDown: positions.length - 1,
                 Home: 0, End: positions.length - 1 };
  if (e.key in keys) { e.preventDefault(); goTo(keys[e.key], { sound: true }); }
  else if (e.key === 'f') flip();
  else if (e.key === 'a') startAnalysis();
});

// ------------------------------------------------------------ header, players, marks, moments

function renderHeader() {
  const opp = myColor === 'white' ? game.black : game.white;
  const result = { win: 'You won', loss: 'You lost', draw: 'Draw' }[game.my_result];
  document.title = `vs ${opp} · Game Report`;
  const meta = [playedAt(game), `${game.time_class} ${game.time_control || ''}`,
                game.opening, game.termination].filter(Boolean).join(' · ');
  $('report-head').replaceChildren(
    el('h1', {}, `${result} vs ${opp}`),
    el('div', { class: 'report-meta' }, meta,
       game.link ? el('span', {}, ' · ', el('a', { href: game.link, target: '_blank', rel: 'noopener' }, 'View on chess.com')) : null));
}

function renderPlayers() {
  const acc = v => (v == null ? '–' : v.toFixed(1));
  const player = (name, elo, accuracy, color) => el('div', { class: 'player' },
    el('div', { class: 'name' }, name, (color === myColor ? ' (you)' : '')),
    el('div', { class: 'elo' }, elo ?? ''),
    el('div', { class: `accuracy-box ${color}` }, acc(accuracy)));
  $('players').replaceChildren(
    player(game.white, game.white_elo, game.accuracy_white, 'white'),
    el('div', { class: 'label', title: 'Lichess accuracy method; chess.com uses its own formula' }, 'Accuracy'),
    player(game.black, game.black_elo, game.accuracy_black, 'black'));
}

function renderMarks() {
  const counts = { white: {}, black: {} };
  positions.forEach(p => {
    if (!p.mark) return;
    const color = sideToMove(p.fen);
    counts[color][p.mark] = (counts[color][p.mark] || 0) + 1;
  });
  $('marks').replaceChildren(...MARK_ORDER.map(mark => {
    const w = counts.white[mark] || 0, b = counts.black[mark] || 0;
    const name = el('td', { class: 'mark-name' });
    name.innerHTML = markIcon(mark, 20);
    name.append(MARKS[mark].label);
    return el('tr', { class: w + b ? '' : 'zero' },
      el('td', { class: 'count' }, w), name, el('td', { class: 'count' }, b));
  }));
}

function renderMoments() {
  const notable = new Set(['blunder', 'mistake', 'miss', 'brilliant', 'great']);
  const swings = positions.slice(0, -1).map((p, i) => ({
    i, p, swing: Math.abs(winChance(positions[i + 1].eval) - winChance(p.eval)),
  })).filter(m => notable.has(m.p.mark));
  swings.sort((a, b) => (b.p.is_mine - a.p.is_mine) || (b.swing - a.swing));
  const top = swings.slice(0, 6).sort((a, b) => a.i - b.i);
  if (!top.length) {
    $('moments').replaceChildren(el('div', { class: 'muted small' }, analysed ? 'A clean game: no big swings.' : 'Waiting for analysis.'));
    return;
  }
  $('moments').replaceChildren(...top.map(({ i, p }) => {
    const row = el('div', { class: 'moment', onclick: () => { showTab('moves'); goTo(i + 1); } });
    row.innerHTML = markIcon(p.mark, 20);
    row.append(el('strong', {}, moveLabel(i)), ` ${MARKS[p.mark].label}`,
               el('span', { class: 'who' }, `${whoMoved(i)} · ${formatEval(positions[i + 1].eval)}`));
    return row;
  }));
}

// ------------------------------------------------------------ move list

function renderMoves() {
  const list = $('moves');
  for (let i = 0; i < positions.length - 1; i++) {
    const p = positions[i];
    if (i % 2 === 0) list.append(el('span', { class: 'num' }, `${i / 2 + 1}.`));
    const cell = el('span', { class: 'mv', 'data-index': i + 1, title: p.mark ? MARKS[p.mark].label : null,
                              onclick: () => goTo(i + 1, { sound: true }) });
    if (p.mark && p.mark !== 'good') cell.innerHTML = markIcon(p.mark, 16);
    cell.append(p.move_san);
    if (p.spent_ms != null) cell.append(el('span', { class: 'spent' }, formatTime(p.spent_ms)));
    list.append(cell);
  }
}

function formatTime(ms) {
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 60) return `${Math.round(s)}s`;
  return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
}

// ------------------------------------------------------------ eval graph (with hover)

const NS = 'http://www.w3.org/2000/svg';
const W = 600, H = 110;
let cursorLine = null;

function svgEl(svg, tag, attrs) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  svg.append(node);
  return node;
}

function tooltip(container) {
  const tip = el('div', { class: 'chart-tip', hidden: true });
  container.append(tip);
  return {
    show(x, y, ...content) { tip.hidden = false; tip.style.left = `${x}px`; tip.style.top = `${y}px`; tip.replaceChildren(...content); },
    hide() { tip.hidden = true; },
  };
}

function renderGraph() {
  const container = $('graph');
  if (!analysed) return;
  const n = positions.length - 1;
  const x = i => (i / n) * W;
  const y = wc => H / 2 - wc * (H / 2 - 4);
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'Evaluation over the game (white area = White better)');
  const points = positions.map((p, i) => `${x(i).toFixed(1)},${y(winChance(p.eval)).toFixed(1)}`);
  svgEl(svg, 'rect', { x: 0, y: 0, width: W, height: H, class: 'black-area' });
  svgEl(svg, 'polygon', { points: `0,${H} ${points.join(' ')} ${W},${H}`, class: 'white-area' });
  svgEl(svg, 'line', { x1: 0, y1: H / 2, x2: W, y2: H / 2, class: 'midline' });
  const notable = new Set(['blunder', 'mistake', 'miss', 'brilliant', 'great']);
  positions.forEach((p, i) => {
    if (!notable.has(p.mark)) return;
    svgEl(svg, 'circle', { cx: x(i + 1), cy: y(winChance(positions[i + 1].eval)), r: 4.5,
                           fill: MARKS[p.mark].color, stroke: '#1b1a18', 'stroke-width': 1.5 });
  });
  const hover = svgEl(svg, 'line', { y1: 0, y2: H, stroke: '#8a8782', 'stroke-width': 1, visibility: 'hidden' });
  cursorLine = svgEl(svg, 'line', { y1: 0, y2: H, class: 'cursor' });
  container.append(svg);

  const tip = tooltip(container);
  const indexAt = e => {
    const rect = svg.getBoundingClientRect();
    return Math.max(0, Math.min(n, Math.round(((e.clientX - rect.left) / rect.width) * n)));
  };
  svg.addEventListener('mousemove', e => {
    const i = indexAt(e);
    const rect = svg.getBoundingClientRect();
    hover.setAttribute('x1', x(i)); hover.setAttribute('x2', x(i)); hover.setAttribute('visibility', 'visible');
    const label = i === 0 ? 'Start' : moveLabel(i - 1);
    const mark = i > 0 ? positions[i - 1].mark : null;
    tip.show((x(i) / W) * rect.width, 0, el('strong', {}, label), ' ', formatEval(positions[i].eval),
             mark ? el('span', { class: 'tip-muted' }, ` · ${MARKS[mark].label}`) : '');
  });
  svg.addEventListener('mouseleave', () => { hover.setAttribute('visibility', 'hidden'); tip.hide(); });
  svg.addEventListener('click', e => { showTab('moves'); goTo(indexAt(e)); });
}

function drawCursor() {
  if (!cursorLine) return;
  const cx = (current / (positions.length - 1)) * W;
  cursorLine.setAttribute('x1', cx);
  cursorLine.setAttribute('x2', cx);
}

// ------------------------------------------------------------ time per move (mirrored bars)

function renderTimeChart() {
  if (!hasClocks) return;
  $('time-section').hidden = false;
  const container = $('timechart');
  const moves = positions.slice(0, -1);
  const maxMs = Math.max(...moves.map(p => p.spent_ms || 0), 1000);
  const fullMoves = Math.ceil(moves.length / 2);
  const CW = 600, CH = 130, mid = CH / 2, pad = 14;
  const slot = CW / Math.max(fullMoves, 1);
  const barW = Math.max(2, Math.min(14, slot - 2));
  const scale = ms => (ms / maxMs) * (mid - pad);
  const myParity = myColor === 'white' ? 0 : 1;   // ply parity of my moves (standard start)

  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${CW} ${CH}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', 'Time spent per move: you above the line, opponent below');
  svgEl(svg, 'line', { x1: 0, y1: pad, x2: CW, y2: pad, class: 'grid' });
  svgEl(svg, 'line', { x1: 0, y1: CH - pad, x2: CW, y2: CH - pad, class: 'grid' });
  svgEl(svg, 'text', { x: 2, y: pad - 3, class: 'tick' }).textContent = formatTime(maxMs);

  moves.forEach((p, i) => {
    if (p.spent_ms == null) return;
    const mine = i % 2 === myParity;
    const h = Math.max(1.5, scale(p.spent_ms));
    const cx = Math.floor(i / 2) * slot + slot / 2;
    const bar = svgEl(svg, 'rect', {
      x: cx - barW / 2, width: barW, height: h, y: mine ? mid - h : mid, rx: Math.min(3, barW / 2),
      class: `bar ${mine ? 'bar-you' : 'bar-opp'}`, 'data-index': i + 1,
    });
    bar.dataset.label = `${moveLabel(i)} · ${mine ? 'you' : 'opponent'} · ${formatTime(p.spent_ms)}`;
  });
  svgEl(svg, 'line', { x1: 0, y1: mid, x2: CW, y2: mid, class: 'axis' });
  container.append(svg);

  // Hover target is the whole half-column, bigger than the bar itself.
  const tip = tooltip(container);
  const barAt = e => {
    const rect = svg.getBoundingClientRect();
    const col = Math.floor(((e.clientX - rect.left) / rect.width) * fullMoves);
    const above = (e.clientY - rect.top) / rect.height * CH < mid;
    const ply = col * 2 + (above ? myParity : 1 - myParity);
    return svg.querySelector(`.bar[data-index="${ply + 1}"]`);
  };
  svg.addEventListener('mousemove', e => {
    const bar = barAt(e);
    if (!bar) return tip.hide();
    const rect = svg.getBoundingClientRect();
    tip.show((+bar.getAttribute('x') + barW / 2) / CW * rect.width, 0, bar.dataset.label);
  });
  svg.addEventListener('mouseleave', () => tip.hide());
  svg.addEventListener('click', e => { const bar = barAt(e); if (bar) { showTab('moves'); goTo(+bar.dataset.index); } });
}

// ------------------------------------------------------------ start

renderHeader();
renderPlayers();
renderMarks();
renderMoments();
renderMoves();
renderGraph();
renderTimeChart();
const fromHash = parseInt(location.hash.slice(1), 10);
if (Number.isFinite(fromHash) && fromHash > 0) showTab('moves');
goTo(Number.isFinite(fromHash) ? fromHash : 0);
board.set({ animation: { enabled: true, duration: 150 } });  // animate only after the first position
