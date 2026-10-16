// Learn from your mistakes: each of my mistakes becomes an interactive puzzle.
//
// Flow per mistake:  solving -> (checking) -> wrong: snap back, explain, keep solving
//                                          -> right: play the opponent's reply and continue,
//                                                    until playout_moves are found -> done
// "View solution" ends as failed, "Skip" records a skip and moves on.
import { Chess } from 'chess.js';
import { api, arrow, lastMove, legalDests, makeBoard, SYMBOLS } from './common.js';

const $ = id => document.getElementById(id);
const form = $('filters');
// Must not start viewOnly: chessground only binds its input listeners when the board is created.
const board = makeBoard($('board'), {
  viewOnly: false, animation: { enabled: true, duration: 200 }, movable: { color: undefined },
});

let items = [];
let index = 0;
let optionsLoaded = false;
let puzzle = null;                       // state of the mistake on screen
const session = { solved: 0, failed: 0 };

const KIND_LABELS = {
  missed_opportunity: 'Missed opportunity', positional: 'Positional', loses_material: 'Loses material',
  hanging_piece: 'Hanging piece', missed_tactic: 'Missed tactic', king_safety: 'King safety',
  missed_capture: 'Missed capture', allowed_mate: 'Allowed mate', missed_mate: 'Missed mate',
  fork: 'Fork', discovered_attack: 'Discovered attack',
};
const STATUS_CHIPS = {
  solved: 'Solved before', solved_with_help: 'Solved with help before',
  failed: 'Failed before', skipped: 'Skipped before',
};

// ------------------------------------------------------------ filters

const STORAGE_KEY = 'review-filters-v2';
const FIXED_SELECTS = ['status', 'judgement', 'sort', 'last'];  // options known up front

function saveFilters() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(new FormData(form)))); } catch { /* unavailable */ }
}

function restoreFilters() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch { /* ignore */ }
  for (const [name, value] of Object.entries(saved)) {
    const field = form.elements[name];
    if (!field) continue;
    if (FIXED_SELECTS.includes(name)) field.value = value;
    else field.dataset.saved = value;  // applied once the options have loaded
  }
}

function fillOptions(options) {
  for (const [name, values] of Object.entries(options)) {
    const select = form.elements[name];
    if (!select || FIXED_SELECTS.includes(name)) continue;
    for (const v of values) select.append(new Option(KIND_LABELS[v] || v.charAt(0).toUpperCase() + v.slice(1), v));
    if (select.dataset.saved) select.value = select.dataset.saved;
  }
}

function query() {
  const params = new URLSearchParams();
  for (const [k, v] of new FormData(form)) if (v) params.set(k, v);
  return params.toString();
}

async function load() {
  const q = query();
  const data = await api(`/api/mistakes?${q}`);
  if (!optionsLoaded) {
    fillOptions(data.options);
    optionsLoaded = true;
    if (query() !== q) return load();  // saved filters became selectable
  }
  items = data.items;
  $('empty').hidden = items.length > 0;
  $('review').hidden = items.length === 0;
  if (items.length) show(0);
}

form.addEventListener('change', () => { saveFilters(); load(); });

// ------------------------------------------------------------ small DOM helpers

// rich(el, 'plain', ['strong', 'bold'], ['span.ok', 'green']) — builds text safely.
function rich(el, ...parts) {
  el.replaceChildren(...parts.filter(p => p !== null && p !== undefined && p !== '').map(p => {
    if (typeof p === 'string') return document.createTextNode(p);
    const [tag, cls] = p[0].split('.');
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    node.textContent = p[1];
    return node;
  }));
}

const colorName = c => (c === 'white' ? 'White' : 'Black');
const article = w => (/^[aeiou]/.test(w) ? 'an' : 'a');

// ------------------------------------------------------------ showing a mistake

function show(i) {
  index = (i + items.length) % items.length;
  const m = items[index];
  puzzle = {
    item: m, fen: m.fen, last: lastMove(m.prev_move_uci), step: 0,
    attempts: 0, hints: 0, hintLevel: 0, phase: 'solving', recorded: false,
    onLine: true,     // all moves so far followed the engine's best line
  };

  $('counter').textContent = `Mistake ${index + 1} of ${items.length}`;
  updateSessionScore();
  $('badge').textContent = m.judgement;
  $('badge').className = `badge ${m.judgement}`;
  $('move-title').textContent = `${m.move_label}${SYMBOLS[m.judgement]}`;
  $('status-chip').hidden = !m.status;
  $('status-chip').textContent = STATUS_CHIPS[m.status] || '';
  $('status-chip').className = `chip ${m.status || ''}`;
  const result = { win: 'won', loss: 'lost', draw: 'drawn' }[m.my_result];
  $('context').textContent = `vs ${m.opponent} · ${m.date} · ${m.opening} · ${m.phase} · game ${result}`;
  $('game-link').href = `/game/${m.game_id}#${m.ply}`;

  const goal = m.playout_moves > 1 ? ` It takes ${m.playout_moves} good moves in a row.` : '';
  rich($('prompt'), 'You played ', ['strong', `${m.move_san}${SYMBOLS[m.judgement]}`],
       ` here. Find a better move for ${colorName(m.my_color)}.${goal}`);
  $('feedback').hidden = true;
  resetHint();
  $('solve-actions').hidden = false;
  $('done-actions').hidden = true;
  $('answer').hidden = true;

  board.set({ orientation: m.my_color });
  setSolvingBoard();
}

function setSolvingBoard() {
  const p = puzzle;
  const chess = new Chess(p.fen);
  board.set({
    fen: p.fen, turnColor: p.item.my_color, lastMove: p.last, check: chess.inCheck(),
    movable: { free: false, color: p.item.my_color, dests: legalDests(chess), showDests: true,
               events: { after: onMove } },
  });
  board.setAutoShapes(hintShapes());
}

function lockBoard() {
  board.set({ movable: { color: undefined, dests: new Map() } });
}

function updateSessionScore() {
  const { solved, failed } = session;
  $('session-score').textContent = solved + failed ? `This session: ✓ ${solved} · ✗ ${failed}` : '';
}

// ------------------------------------------------------------ solving

async function onMove(orig, dest) {
  const p = puzzle;
  if (p.phase !== 'solving') return;
  const chess = new Chess(p.fen);
  const piece = chess.get(orig);
  const promotion = piece?.type === 'p' && (dest[1] === '8' || dest[1] === '1') ? 'q' : '';
  const uci = orig + dest + promotion;
  p.phase = 'checking';
  lockBoard();

  const m = p.item;
  if (p.step === 0 && uci === m.move_uci) {
    return wrong(p, { san: m.move_san, judgement: m.judgement, after: m.eval_after,
                      explanation: "That's the move you played in the game." });
  }
  showFeedback('', 'Checking…');
  let r;
  try {
    r = await api('/api/judge', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fen: p.fen, uci, game_id: m.game_id, ply: m.ply, step: p.step }),
    });
  } catch (err) {
    showFeedback('bad', `Couldn't check that move (${err.message}).`);
    p.phase = 'solving';
    return setSolvingBoard();
  }
  if (puzzle !== p) return;  // moved on while Stockfish was thinking
  if (r.judgement === 'good') right(p, r, uci);
  else wrong(p, r);
}

function wrong(p, r) {
  p.attempts += 1;
  showFeedback('bad', `✗ ${r.san} is ${article(r.judgement)} ${r.judgement} (${r.after}).`,
               r.explanation || '', ' Try again.');
  setTimeout(() => {
    if (puzzle !== p) return;
    p.phase = 'solving';
    setSolvingBoard();  // the piece snaps back
  }, 700);
}

function right(p, r, uci) {
  const m = p.item;
  const lineIndex = 2 * p.step;
  if (m.best_pv_uci[lineIndex] !== uci) p.onLine = false;
  showFeedback('ok', r.is_best ? '✓ Best move!' : `✓ Good move! ${r.san} keeps it at ${r.after}.`);
  board.set({ fen: r.fen_after, lastMove: lastMove(uci) });

  const found = p.step + 1;
  if (found >= m.playout_moves || r.game_over || !r.reply_uci) {
    return finish(p, p.attempts === 0 && p.hints === 0 ? 'solved' : 'solved_with_help', 900);
  }
  // Play the opponent's best reply, then hand the board back.
  p.phase = 'opponent';
  if (m.best_pv_uci[lineIndex + 1] !== r.reply_uci) p.onLine = false;
  setTimeout(() => {
    if (puzzle !== p) return;
    p.fen = r.fen_after_reply;
    p.last = lastMove(r.reply_uci);
    p.step = found;
    p.phase = 'solving';
    rich($('prompt'), `${colorName(m.my_color === 'white' ? 'black' : 'white')} replied `,
         ['strong', r.reply_san], `. Keep going: move ${found + 1} of ${m.playout_moves}.`);
    resetHint();
    setSolvingBoard();
  }, 650);
}

function showFeedback(cls, headline, ...rest) {
  const el = $('feedback');
  el.hidden = false;
  el.className = `feedback ${cls}`;
  rich(el, headline ? ['strong', headline + ' '] : null, ...rest);
}

// ------------------------------------------------------------ hints

function resetHint() {
  if (puzzle) puzzle.hintLevel = 0;
  $('hint').hidden = true;
  $('hint-btn').textContent = '💡 Hint';
  $('hint-btn').disabled = false;
}

function hintMove() {
  const p = puzzle;
  return p.onLine ? p.item.best_pv_uci[2 * p.step] : null;
}

function hintShapes() {
  const p = puzzle;
  if (!p || p.hintLevel < 2) return [];
  const best = hintMove();
  return best ? [{ orig: best.slice(0, 2), brush: 'green' }] : [];
}

function giveHint() {
  const p = puzzle;
  if (!p || p.phase !== 'solving' || $('hint-btn').disabled) return;
  p.hintLevel += 1;
  p.hints += 1;
  const el = $('hint');
  el.hidden = false;
  if (p.hintLevel === 1) {
    rich(el, ['strong', 'Hint: '], p.step === 0 ? p.item.hint : 'Keep looking for the most forcing move.');
    $('hint-btn').textContent = '💡 Show piece';
  } else {
    rich(el, ['strong', 'Hint: '], hintMove()
      ? 'move the highlighted piece.'
      : "you left the engine's main line, so there's no piece hint here.");
    $('hint-btn').disabled = true;
    board.setAutoShapes(hintShapes());
  }
}

// ------------------------------------------------------------ finishing

function finish(p, result, delay = 0) {
  p.phase = 'done';
  lockBoard();
  record(p, result);
  const m = p.item;
  const messages = {
    solved: ['ok', '✓ Solved!', ' Well found.'],
    solved_with_help: ['ok', '✓ Solved', ` with ${p.hints ? 'hints' : 'some retries'}.`],
    failed: ['bad', 'Solution', `: ${m.best_san} was the move.`],
  };
  const [cls, head, tail] = messages[result];
  rich($('prompt'), [`strong.${cls}`, head], tail);
  $('hint').hidden = true;
  $('solve-actions').hidden = true;
  $('done-actions').hidden = false;

  $('eval-before').textContent = m.eval_before;
  $('eval-after').textContent = m.eval_after;
  rich($('explanation'), ['strong', `Why ${m.move_san} was ${article(m.judgement)} ${m.judgement}: `],
       m.explanation);
  $('explanation').className = `explanation ${m.judgement}`;
  setTimeout(() => {
    if (puzzle !== p) return;
    $('answer').hidden = false;
    buildSolution(m);
    showSolutionStep(0);
  }, delay);
}

function record(p, result) {
  if (p.recorded) return;
  p.recorded = true;
  p.item.status = result;
  if (result.startsWith('solved')) session.solved += 1;
  else session.failed += 1;
  updateSessionScore();
  api('/api/review/result', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ game_id: p.item.game_id, ply: p.item.ply, result,
                           attempts: p.attempts, hints_used: p.hints }),
  }).catch(() => { /* not critical */ });
}

// ------------------------------------------------------------ stepping through the best line

let solution = [];   // [{fen, last, san}]; index 0 is the mistake position
let solIndex = 0;

function buildSolution(m) {
  const chess = new Chess(m.fen);
  solution = [{ fen: m.fen, last: lastMove(m.prev_move_uci), san: null }];
  for (const uci of m.best_pv_uci) {
    let mv;
    try { mv = chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] }); } catch { break; }
    solution.push({ fen: chess.fen(), last: [mv.from, mv.to], san: mv.san });
  }
  $('solution-moves').replaceChildren(...solution.slice(1).map((s, i) => {
    const ply = m.ply + i;  // ply of the position this move is played from
    const num = ply % 2 === 0 ? `${ply / 2 + 1}. ` : (i === 0 ? `${Math.floor(ply / 2) + 1}... ` : '');
    const chip = document.createElement('span');
    chip.className = 'sm';
    chip.dataset.index = i + 1;
    chip.textContent = num + s.san;
    chip.addEventListener('click', () => showSolutionStep(i + 1));
    return chip;
  }));
}

function showSolutionStep(i) {
  if (!solution.length || puzzle?.phase !== 'done') return;
  solIndex = Math.max(0, Math.min(solution.length - 1, i));
  const s = solution[solIndex];
  const m = puzzle.item;
  board.set({ fen: s.fen, lastMove: s.last, check: new Chess(s.fen).inCheck(), movable: { color: undefined } });
  board.setAutoShapes(solIndex === 0
    ? [arrow(m.move_uci, 'red'), arrow(m.best_uci, 'green')].filter(Boolean)
    : []);
  document.querySelectorAll('#solution-moves .sm').forEach(c =>
    c.classList.toggle('current', +c.dataset.index === solIndex));
}

// ------------------------------------------------------------ buttons + keys

$('hint-btn').addEventListener('click', giveHint);
$('solution-btn').addEventListener('click', () => { if (puzzle?.phase === 'solving') finish(puzzle, 'failed'); });
$('skip-btn').addEventListener('click', () => {
  if (!puzzle || puzzle.phase === 'done') return;
  record(puzzle, 'skipped');
  show(index + 1);
});
$('next-btn').addEventListener('click', () => show(index + 1));
$('prev-btn').addEventListener('click', () => show(index - 1));
$('retry-btn').addEventListener('click', () => show(index));
$('sol-prev').addEventListener('click', () => showSolutionStep(solIndex - 1));
$('sol-next').addEventListener('click', () => showSolutionStep(solIndex + 1));

document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || !items.length || e.metaKey || e.ctrlKey) return;
  const done = puzzle?.phase === 'done';
  if (e.key === 'Enter' && done) { e.preventDefault(); show(index + 1); }
  else if (e.key === 'ArrowLeft' && done) { e.preventDefault(); showSolutionStep(solIndex - 1); }
  else if (e.key === 'ArrowRight' && done) { e.preventDefault(); showSolutionStep(solIndex + 1); }
  else if (e.key === 'h') giveHint();
  else if (e.key === 's') $('skip-btn').click();
});

restoreFilters();
load();
