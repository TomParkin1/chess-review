// Openings: explore my repertoire (built from my games) and drill my lines, Chessly style.
import { Chess } from 'chess.js';
import { choosePromotion, createBadge, isPromotion, makeBoard, moveSound, sound } from './board.js';
import { api, arrow, el, lastMove, legalDests, sideToMove } from './common.js';
import { markIcon } from './icons.js';

const $ = id => document.getElementById(id);
const START = new Chess().fen();
const board = makeBoard($('board'), {
  viewOnly: false, movable: { color: undefined }, orientation: 'white',
  premovable: { enabled: true, showDests: true },   // move ahead while the opponent's reply is being played
});
const badge = createBadge(board, $('board'));

let color = 'white';
let family = '';
let tab = 'explore';
let path = [];          // explore: [{uci, san, fen}]

const params = () => new URLSearchParams({ color, ...(family ? { family } : {}) });
const checkOf = fen => (new Chess(fen).inCheck() ? sideToMove(fen) : false);

// ------------------------------------------------------------ explore

function currentFen() { return path.length ? path[path.length - 1].fen : START; }

function setExploreBoard() {
  const fen = currentFen();
  const chess = new Chess(fen);
  board.set({
    fen, orientation: color, turnColor: sideToMove(fen), check: checkOf(fen),
    lastMove: path.length ? lastMove(path[path.length - 1].uci) : undefined,
    movable: { free: false, color: sideToMove(fen), dests: legalDests(chess), showDests: true,
               events: { after: (orig, dest) => exploreMove(orig, dest) } },
  });
}

async function exploreMove(orig, dest, promo) {
  const chess = new Chess(currentFen());
  const piece = chess.get(orig);
  const promotion = promo || (isPromotion(piece, dest)
    ? await choosePromotion($('board'), sideToMove(currentFen()), dest, color) : undefined);
  const mv = chess.move({ from: orig, to: dest, promotion });
  moveSound(mv.san);
  path.push({ uci: mv.from + mv.to + (mv.promotion || ''), san: mv.san, fen: chess.fen() });
  loadNode();
}

function playUci(uci) {
  exploreMove(uci.slice(0, 2), uci.slice(2, 4), uci[4]);
}

function goToPath(ucis) {
  const chess = new Chess();
  path = ucis.map(uci => {
    const mv = chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] });
    return { uci, san: mv.san, fen: chess.fen() };
  });
  loadNode();
}

let lastName = '';
async function loadNode() {
  badge.clear();
  setExploreBoard();
  renderBreadcrumb();
  let node;
  try {
    node = await api(`/api/openings/node?${params()}&fen=${encodeURIComponent(currentFen())}`);
  } catch {
    $('node-summary').textContent = "You haven't reached this position in your games.";
    $('repertoire-note').replaceChildren();
    $('moves').replaceChildren();
    board.setAutoShapes([]);
    return;
  }
  if (!path.length) lastName = '';
  if (node.name) lastName = node.name;
  $('op-name').textContent = lastName || 'Starting position';
  const score = node.games ? Math.round(100 * (node.wins + node.draws / 2) / node.games) : 0;
  $('node-summary').textContent = `Reached in ${node.games} of your games · ${node.wins}W ${node.draws}D ${node.losses}L · ${score}% score`;

  const note = $('repertoire-note');
  note.replaceChildren();
  const shapes = [];
  if (node.my_turn) {
    if (node.problem) {
      note.append(el('div', { class: 'feedback bad' }, el('strong', {}, '⚠ Repertoire problem. '),
        `You usually play ${node.usual_san} here, but the engine usually judges it an inaccuracy or worse. Learn `,
        el('strong', {}, node.learn_san), ' instead.'));
      const usual = node.moves.find(m => m.usual);
      if (usual) shapes.push(arrow(usual.uci, 'red'));
    } else if (node.learn_san) {
      note.append(el('div', { class: 'feedback ok' }, el('strong', {}, 'Your move: '), `${node.learn_san} `,
        el('span', { class: 'muted' }, `(engine's choice: ${node.best_san || '–'})`)));
    } else {
      note.append(el('div', { class: 'feedback' }, "You haven't settled on a move here yet (each move played once)." +
        (node.best_san ? ` The engine suggests ${node.best_san}.` : '')));
    }
    if (node.learn_uci) shapes.push(arrow(node.learn_uci, 'green'));
  } else {
    node.moves.slice(0, 3).forEach(m => shapes.push(arrow(m.uci, 'paleBlue')));
  }
  board.setAutoShapes(shapes.filter(Boolean));

  const head = el('tr', {}, ...['Move', 'Games', 'Results', 'Score', node.my_turn ? 'Engine verdicts' : ''].map(h =>
    el('th', { class: 'muted small' }, h)));
  $('moves').replaceChildren(head, ...node.moves.map(m => {
    const wdl = el('span', { class: 'wdl', title: `${m.wins}W ${m.draws}D ${m.losses}L` },
      el('span', { class: 'w', style: `flex:${m.wins}` }), el('span', { class: 'd', style: `flex:${m.draws}` }),
      el('span', { class: 'l', style: `flex:${m.losses}` }));
    const name = el('td', { class: 'op-move' });
    if (m.book) name.innerHTML = markIcon('book', 16);
    name.append(` ${m.san}`, m.repertoire ? el('span', { class: 'chip solved' }, 'your move') : '');
    const verdict = node.my_turn
      ? (m.errors ? el('span', { class: 'sym mistake' }, `${m.errors} of ${m.games} inaccurate`) : el('span', { class: 'muted' }, 'fine'))
      : '';
    return el('tr', { class: 'clickable', onclick: () => playUci(m.uci) },
      name, el('td', { class: 'count' }, m.games), el('td', {}, wdl), el('td', { class: 'count' }, `${Math.round(m.score)}%`),
      el('td', { class: 'small' }, verdict));
  }));
}

function renderBreadcrumb() {
  const crumbs = [el('span', { class: 'sm', onclick: () => { path = []; loadNode(); } }, 'Start')];
  path.forEach((p, i) => {
    const ply = i;
    const num = ply % 2 === 0 ? `${ply / 2 + 1}. ` : '';
    crumbs.push(el('span', { class: `sm${i === path.length - 1 ? ' current' : ''}`,
                             onclick: () => { path = path.slice(0, i + 1); loadNode(); } }, num + p.san));
  });
  $('breadcrumb').replaceChildren(...crumbs);
}

async function loadOverview() {
  const d = await api(`/api/openings?${params()}`);
  const select = $('family');
  const keep = family;
  select.replaceChildren(new Option('All my openings', ''),
    ...d.families.map(f => new Option(`${f.family} (${f.games})`, f.family)));
  select.value = keep;
  $('problems').replaceChildren(...(d.problems.length ? d.problems.map(p => {
    const row = el('div', { class: 'moment', onclick: () => { showTab('explore'); goToPath(p.path); } });
    row.innerHTML = markIcon('inaccuracy', 18);
    row.append(el('span', {}, el('strong', {}, p.line), ` · you play ${p.usual_san} (${p.games}×), learn `,
                              el('strong', {}, p.learn_san)));
    return row;
  }) : [el('p', { class: 'muted small' }, 'None found: your usual moves hold up.')]));
  $('drill-stats').replaceChildren(
    el('span', { class: 'chip' }, `${d.positions} positions in your repertoire`),
    el('span', { class: 'chip' }, `${d.stats.seen} practised`),
    el('span', { class: 'chip solved' }, `${d.stats.mastered} mastered`),
    el('span', { class: 'chip failed' }, `${d.stats.due} due`));
  $('due-btn').disabled = !d.stats.due;
  $('due-btn').textContent = `Review due (${d.stats.due})`;
}

// ------------------------------------------------------------ drill

let drill = null;   // { lines, line, step, chess, tally, failedStep }

async function startDrill(mode) {
  const d = await api(`/api/openings/drill?${params()}&mode=${mode}`);
  if (!d.lines.length) {
    $('drill-done').hidden = false;
    $('drill-done').replaceChildren(el('p', { class: 'muted' },
      mode === 'due' ? 'Nothing due right now.' : 'No lines yet: you need at least two games in a line to drill it.'));
    return;
  }
  drill = { lines: d.lines, line: 0, step: 0, chess: null, tally: { solved: 0, failed: 0 }, failedStep: false };
  $('drill-start').hidden = true;
  $('drill-done').hidden = true;
  $('drill-run').hidden = false;
  startLine();
}

function startLine() {
  const dr = drill;
  dr.step = 0;
  dr.failedStep = false;
  dr.chess = new Chess();
  badge.clear();
  board.cancelPremove();
  board.set({ fen: START, orientation: color, lastMove: undefined, check: false, movable: { color: undefined } });
  board.setAutoShapes([]);
  $('drill-moves').replaceChildren();
  $('drill-feedback').hidden = true;
  updateDrillBar();
  setTimeout(advance, 150);
}

function updateDrillBar() {
  const dr = drill;
  $('drill-counter').textContent = `Line ${dr.line + 1} of ${dr.lines.length}`;
  $('drill-score').textContent = `✓ ${dr.tally.solved} · ✗ ${dr.tally.failed}`;
  $('drill-progress').style.width = `${(dr.line / dr.lines.length) * 100}%`;
}

function playStep(step) {
  const mv = drill.chess.move({ from: step.uci.slice(0, 2), to: step.uci.slice(2, 4), promotion: step.uci[4] });
  const fen = drill.chess.fen();
  board.set({ fen, turnColor: sideToMove(fen), lastMove: [mv.from, mv.to], check: checkOf(fen) });
  moveSound(mv.san);
  const ply = drill.chess.history().length - 1;
  $('drill-moves').append(el('span', { class: `sm${step.mine ? ' mine' : ''}` },
    (ply % 2 === 0 ? `${ply / 2 + 1}. ` : '') + mv.san));
}

function advance() {
  const dr = drill;
  if (!dr || $('drill-run').hidden) return;
  const steps = dr.lines[dr.line];
  if (dr.step >= steps.length) return endLine();
  const step = steps[dr.step];
  if (!step.mine) {
    // Let me drag already: on the opponent's turn a move becomes a premove (chess.com style).
    board.set({ movable: { free: false, color, dests: new Map(), events: { after: (o, d) => drillMove(o, d) } } });
    playStep(step);
    dr.step += 1;
    // My move always follows an opponent move: hand the board over at once (setup moves are paced).
    if (step.setup) setTimeout(advance, 60); else advance();
    return;
  }
  // My move: hand the board over (failedStep is reset only when a new position comes up,
  // so a correct move after a wrong try doesn't count as solved).
  const fen = dr.chess.fen();
  $('drill-prompt').textContent = `Your move as ${color === 'white' ? 'White' : 'Black'}.`;
  board.set({ movable: { free: false, color, dests: legalDests(dr.chess), showDests: true,
                         events: { after: (o, d) => drillMove(o, d) } }, turnColor: sideToMove(fen) });
  board.playPremove();   // a move queued during the opponent's turn is played now
}

async function drillMove(orig, dest) {
  const dr = drill;
  const step = dr.lines[dr.line][dr.step];
  const piece = dr.chess.get(orig);
  const promotion = isPromotion(piece, dest) ? await choosePromotion($('board'), color, dest, color) : '';
  const uci = orig + dest + promotion;
  board.set({ movable: { color: undefined } });
  const fb = $('drill-feedback');
  fb.hidden = false;
  if (uci === step.uci) {
    sound.correct();
    board.setAutoShapes([]);   // clear the hint arrow from a wrong try
    badge.show(dest, 'best');
    if (!dr.failedStep) { dr.tally.solved += 1; record(step.key, 'solved'); }
    fb.className = 'feedback ok';
    fb.replaceChildren(el('strong', {}, '✓ Correct. '), step.fix ? 'This is the engine\'s improvement on your usual move.' : '');
    playStep(step);
    dr.step += 1;
    dr.failedStep = false;
    updateDrillBar();
    setTimeout(() => { badge.clear(); advance(); }, 200);
  } else {
    sound.wrong();
    badge.show(dest, 'mistake');
    if (!dr.failedStep) { dr.failedStep = true; dr.tally.failed += 1; record(step.key, 'failed'); }
    fb.className = 'feedback bad';
    fb.replaceChildren(el('strong', {}, '✗ Not your line. '), `Your repertoire move is ${step.san}`,
      step.fix ? ' (an improvement on what you usually play)' : '', '. Play it to continue.');
    updateDrillBar();
    setTimeout(() => {
      const fen = dr.chess.fen();
      badge.clear();
      board.set({ fen, turnColor: sideToMove(fen), check: checkOf(fen) });
      board.setAutoShapes([arrow(step.uci, 'green')]);
      advance();
    }, 450);
  }
}

function endLine() {
  const dr = drill;
  board.setAutoShapes([]);
  $('drill-prompt').textContent = 'Line complete!';
  dr.line += 1;
  if (dr.line >= dr.lines.length) return setTimeout(finishDrill, 700);
  setTimeout(startLine, 600);
}

function finishDrill() {
  const { solved, failed } = drill.tally;
  drill = null;
  $('drill-run').hidden = true;
  $('drill-start').hidden = false;
  const box = $('drill-done');
  box.hidden = false;
  box.replaceChildren(el('div', { class: 'celebrate' }, failed ? '📘' : '🏆'), el('h2', {}, 'Drill complete'),
    el('p', { class: 'muted' }, `${solved} of ${solved + failed} positions right first time.` +
      (failed ? ' Missed positions come back tomorrow.' : '')));
  loadOverview();
}

function record(key, result) {
  api('/api/openings/result', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ key, result }) }).catch(() => {});
}

// ------------------------------------------------------------ wiring

function showTab(name) {
  tab = name;
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('[data-panel]').forEach(p => { p.hidden = p.dataset.panel !== name; });
  if (name === 'explore') { drill = null; $('drill-run').hidden = true; $('drill-start').hidden = false; loadNode(); }
  else { badge.clear(); board.set({ fen: START, orientation: color, lastMove: undefined, movable: { color: undefined } }); board.setAutoShapes([]); }
}

document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
document.querySelectorAll('[data-color]').forEach(b => b.addEventListener('click', () => {
  color = b.dataset.color;
  family = '';
  document.querySelectorAll('[data-color]').forEach(x => x.classList.toggle('active', x === b));
  path = [];
  loadOverview();
  showTab(tab);
}));
$('family').addEventListener('change', () => { family = $('family').value; path = []; loadOverview(); showTab(tab); });
$('back-btn').addEventListener('click', () => { if (tab === 'explore' && path.length) { path.pop(); loadNode(); } });
$('reset-btn').addEventListener('click', () => { if (tab === 'explore') { path = []; loadNode(); } });
$('learn-btn').addEventListener('click', () => startDrill('learn'));
$('due-btn').addEventListener('click', () => startDrill('due'));
$('drill-stop').addEventListener('click', () => finishDrill());
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || tab !== 'explore') return;
  if (e.key === 'ArrowLeft' && path.length) { e.preventDefault(); path.pop(); loadNode(); }
});

loadOverview();
if (location.hash === '#drill') showTab('drill');   // from Today's "Opening lines" block
else loadNode();
