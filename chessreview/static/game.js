// Game view: step through a game with engine evals, an eval graph and my mistakes marked.
import { api, arrow, el, formatEval, lastMove, makeBoard, sideToMove, SYMBOLS, winChance } from './common.js';

const { game, positions } = await api(`/api/game/${window.GAME_ID}`);
const myColor = game.my_color;
const analysed = positions.some(p => p.eval);
let current = 0;

const board = makeBoard(document.getElementById('board'), { orientation: myColor, animation: { enabled: false } });
const bestToggle = document.getElementById('best-toggle');

// ------------------------------------------------------------ navigation

function goTo(index) {
  current = Math.max(0, Math.min(positions.length - 1, index));
  const pos = positions[current];
  const prev = positions[current - 1];

  board.set({
    fen: pos.fen,
    turnColor: sideToMove(pos.fen),
    lastMove: lastMove(prev?.move_uci),
    check: /[+#]$/.test(prev?.move_san || ''),
  });
  const shapes = [];
  const explanation = document.getElementById('explanation');
  if (prev?.judgement && prev.judgement !== 'good') {
    // Just played one of my bad moves: show it in red and what I should have played in green.
    shapes.push(arrow(prev.move_uci, 'red'), arrow(prev.best_uci, 'green'));
    explanation.hidden = false;
    explanation.className = `explanation ${prev.judgement}`;
    explanation.replaceChildren(
      el('strong', {}, `${prev.move_san}${SYMBOLS[prev.judgement]} is a ${prev.judgement}. `),
      prev.explanation || '',
      prev.best_line ? el('div', { class: 'muted small' }, `Best line: ${prev.best_line}`) : null,
    );
  } else {
    explanation.hidden = true;
    if (bestToggle.checked && pos.best_uci) shapes.push(arrow(pos.best_uci, 'blue'));
  }
  board.setAutoShapes(shapes.filter(Boolean));

  document.getElementById('eval-text').textContent = analysed ? formatEval(pos.eval) : 'Not analysed yet';
  document.getElementById('best-text').textContent = pos.best_san ? `Best: ${pos.best_line || pos.best_san}` : '';

  document.querySelectorAll('.moves .mv').forEach(m => m.classList.toggle('current', +m.dataset.index === current));
  document.querySelector('.moves .mv.current')?.scrollIntoView({ block: 'nearest' });
  drawCursor();
  history.replaceState(null, '', `#${current}`);
}

document.querySelectorAll('[data-nav]').forEach(b => b.addEventListener('click', () => {
  const nav = b.dataset.nav;
  goTo({ first: 0, prev: current - 1, next: current + 1, last: positions.length - 1 }[nav]);
}));
document.getElementById('flip-btn').addEventListener('click', () => board.toggleOrientation());
bestToggle.addEventListener('change', () => goTo(current));
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea')) return;
  const keys = { ArrowLeft: current - 1, ArrowRight: current + 1, Home: 0, End: positions.length - 1,
                 ArrowUp: 0, ArrowDown: positions.length - 1 };
  if (e.key in keys) { e.preventDefault(); goTo(keys[e.key]); }
  if (e.key === 'f') board.toggleOrientation();
});

// ------------------------------------------------------------ header + move list

function renderHeader() {
  const opp = myColor === 'white' ? game.black : game.white;
  const result = { win: 'Won', loss: 'Lost', draw: 'Drew' }[game.my_result];
  document.title = `vs ${opp} · Chess Review`;
  document.getElementById('game-header').replaceChildren(
    el('h1', {}, `${game.white} – ${game.black}  ${game.result}`),
    el('div', { class: 'muted' },
      `${result} as ${myColor} vs ${opp} · ${game.date} · ${game.time_class} (${game.time_control}) · ` +
      `${game.opening}${game.termination ? ' · ' + game.termination : ''}`),
  );
}

function renderMoves() {
  const list = document.getElementById('moves');
  const myTurnParity = myColor === 'white' ? 0 : 1;  // ply parity of my moves (standard start)
  for (let i = 0; i < positions.length - 1; i++) {
    const p = positions[i];
    if (i % 2 === 0) list.append(el('span', { class: 'num' }, `${i / 2 + 1}.`));
    const bad = p.judgement && p.judgement !== 'good';
    list.append(el('span', {
      class: `mv${i % 2 === myTurnParity ? ' mine' : ''}`,
      'data-index': i + 1,
      title: bad ? p.explanation : null,
      onclick: () => goTo(i + 1),
    }, p.move_san, bad ? el('span', { class: `sym ${p.judgement}` }, SYMBOLS[p.judgement]) : null));
  }
}

// ------------------------------------------------------------ eval graph

const W = 600, H = 120;
let cursorLine = null;

function renderGraph() {
  const container = document.getElementById('graph');
  if (!analysed) return;
  const n = positions.length - 1;
  const x = i => (i / n) * W;
  const y = wc => H / 2 - wc * (H / 2 - 2);
  const points = positions.map((p, i) => `${x(i).toFixed(1)},${y(winChance(p.eval)).toFixed(1)}`);

  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  const add = (tag, attrs) => {
    const node = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    svg.append(node);
    return node;
  };
  add('rect', { x: 0, y: 0, width: W, height: H, class: 'black-area' });
  add('polygon', { points: `0,${H} ${points.join(' ')} ${W},${H}`, class: 'white-area' });
  add('line', { x1: 0, y1: H / 2, x2: W, y2: H / 2, class: 'midline' });
  positions.forEach((p, i) => {
    if (p.judgement && p.judgement !== 'good') {
      const cx = x(i + 1), cy = y(winChance(positions[i + 1].eval));
      const dot = add('circle', { cx, cy, r: 4, class: `marker ${p.judgement}` });
      dot.append(Object.assign(document.createElementNS(ns, 'title'),
        { textContent: `${p.move_san}${SYMBOLS[p.judgement]}` }));
    }
  });
  cursorLine = add('line', { y1: 0, y2: H, class: 'cursor' });
  svg.addEventListener('click', e => {
    const rect = svg.getBoundingClientRect();
    goTo(Math.round(((e.clientX - rect.left) / rect.width) * n));
  });
  container.append(svg);
}

function drawCursor() {
  if (!cursorLine) return;
  const cx = (current / (positions.length - 1)) * W;
  cursorLine.setAttribute('x1', cx);
  cursorLine.setAttribute('x2', cx);
}

// ------------------------------------------------------------ start

renderHeader();
renderMoves();
renderGraph();
const fromHash = parseInt(location.hash.slice(1), 10);
goTo(Number.isFinite(fromHash) ? fromHash : 0);
board.set({ animation: { enabled: true, duration: 150 } });  // animate only after the first position
