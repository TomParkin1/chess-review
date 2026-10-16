// Review mode: step through my mistakes across all games, and try to find the better move.
import { Chess } from 'chess.js';
import { api, arrow, legalDests, makeBoard, sideToMove, SYMBOLS } from './common.js';

const form = document.getElementById('filters');
const $ = id => document.getElementById(id);
const board = makeBoard($('board'), { animation: { enabled: false } });  // jumps between games shouldn't animate

let items = [];
let index = 0;
let optionsLoaded = false;

// ------------------------------------------------------------ filters

const STORAGE_KEY = 'review-filters';
function saveFilters() {
  const values = Object.fromEntries(new FormData(form));
  values.quiz = form.elements.quiz.checked ? 'on' : 'off';  // unchecked boxes are missing from FormData
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(values)); } catch { /* storage unavailable */ }
}
function restoreFilters() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch { /* ignore */ }
  for (const [name, value] of Object.entries(saved)) {
    const field = form.elements[name];
    if (!field) continue;
    if (field.type === 'checkbox') field.checked = value === 'on';
    else field.dataset.saved = value;  // applied once the options exist
  }
  form.elements.judgement.value = saved.judgement || form.elements.judgement.value;
  form.elements.sort.value = saved.sort || form.elements.sort.value;
}

function fillOptions(options) {
  const labels = {
    missed_opportunity: 'Missed opportunity', positional: 'Positional', loses_material: 'Loses material',
    hanging_piece: 'Hanging piece', missed_tactic: 'Missed tactic', king_safety: 'King safety',
    missed_capture: 'Missed capture', allowed_mate: 'Allowed mate', missed_mate: 'Missed mate',
    fork: 'Fork', discovered_attack: 'Discovered attack',
  };
  for (const [name, values] of Object.entries(options)) {
    const select = form.elements[name];
    if (!select) continue;
    for (const v of values) {
      const text = labels[v] || v.charAt(0).toUpperCase() + v.slice(1);
      select.append(new Option(text, v));
    }
    if (select.dataset.saved) select.value = select.dataset.saved;
  }
}

function query() {
  const params = new URLSearchParams();
  for (const [k, v] of new FormData(form)) if (v && k !== 'quiz') params.set(k, v);
  return params.toString();
}

async function load() {
  const q = query();
  const data = await api(`/api/mistakes?${q}`);
  if (!optionsLoaded) {
    fillOptions(data.options);
    optionsLoaded = true;
    // Saved filters can only be selected once their options exist; reload if they changed the query.
    if (query() !== q) return load();
  }
  items = data.items;
  index = 0;
  $('empty').hidden = items.length > 0;
  $('review').hidden = items.length === 0;
  if (items.length) show(0);
}

form.addEventListener('change', () => { saveFilters(); load(); });

// ------------------------------------------------------------ showing a mistake

const quiz = () => form.elements.quiz.checked;

function show(i) {
  index = (i + items.length) % items.length;
  const m = items[index];
  $('counter').textContent = `${index + 1} / ${items.length}`;
  $('badge').textContent = m.judgement;
  $('badge').className = `badge ${m.judgement}`;
  $('move-title').textContent = `${m.move_label}${SYMBOLS[m.judgement]}`;
  const result = { win: 'won', loss: 'lost', draw: 'drawn' }[m.my_result];
  $('context').textContent = `vs ${m.opponent} · ${m.date} · ${m.opening} · ${m.phase} · game ${result}`;
  $('eval-before').textContent = m.eval_before;
  $('eval-after').textContent = m.eval_after;
  $('explanation').textContent = m.explanation;
  $('explanation').className = `explanation ${m.judgement}`;
  $('best-line').textContent = m.best_line ? `Best line: ${m.best_line}` : '';
  $('game-link').href = `/game/${m.game_id}#${m.ply}`;
  board.set({ orientation: m.my_color });
  if (quiz()) startTry(); else showAnswer();
}

function showAnswer() {
  const m = items[index];
  board.set({
    fen: m.fen, turnColor: sideToMove(m.fen), viewOnly: true, lastMove: undefined,
    movable: { color: undefined, dests: new Map() },
  });
  board.setAutoShapes([arrow(m.move_uci, 'red'), arrow(m.best_uci, 'green')].filter(Boolean));
  $('answer').hidden = false;
  $('show-btn').hidden = true;
  $('try-btn').hidden = false;
}

// ------------------------------------------------------------ trying again

function startTry(keepMessage = false) {
  const m = items[index];
  const chess = new Chess(m.fen);
  board.set({
    fen: m.fen, turnColor: m.my_color, viewOnly: false, lastMove: undefined,
    movable: { free: false, color: m.my_color, dests: legalDests(chess), showDests: true,
               events: { after: (orig, dest) => onTry(chess, orig, dest) } },
  });
  board.setAutoShapes([arrow(m.move_uci, 'red')]);  // what I played, for reference
  $('answer').hidden = true;
  $('try-panel').hidden = false;
  if (!keepMessage) {
    $('try-msg').textContent = `In the game you played ${m.move_san} (red arrow). Find a better move.`;
  }
  $('try-btn').hidden = true;
  $('show-btn').hidden = false;
}

async function onTry(chess, orig, dest) {
  const m = items[index];
  const piece = chess.get(orig);
  const promotion = piece?.type === 'p' && (dest[1] === '8' || dest[1] === '1') ? 'q' : '';
  const uci = orig + dest + promotion;
  board.set({ movable: { color: undefined } });
  const msg = $('try-msg');
  if (uci === m.best_uci) {
    setMessage('ok', 'Correct!', "That's the engine's best move.");
    return revealAfterTry();
  }
  if (uci === m.move_uci) {
    return retry(`That's the move you played in the game. Try something else.`);
  }
  msg.textContent = 'Checking with Stockfish…';
  try {
    const r = await api('/api/evaluate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ game_id: m.game_id, ply: m.ply, uci }),
    });
    if (r.judgement === 'good') {
      setMessage('ok', 'Good move!', `${r.san} keeps the evaluation at ${r.eval_after} ` +
        `(best was ${m.best_san}, ${r.eval_before}).`);
      revealAfterTry();
    } else {
      retry(`${r.san} is also ${article(r.judgement)} ${r.judgement} (${r.eval_after}). Try again, or show the answer.`, true);
    }
  } catch (err) {
    retry(`Could not check that move: ${err.message}`);
  }
}

function article(word) { return /^[aeiou]/.test(word) ? 'an' : 'a'; }

function setMessage(cls, headline, text) {
  const msg = $('try-msg');
  msg.replaceChildren();
  if (headline) {
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = headline + ' ';
    msg.append(span);
  }
  msg.append(text);
}

// Show a message, then put the position back so I can try another move.
function retry(message, bad = false) {
  setMessage('bad', bad ? 'Not quite.' : '', message);
  const shownFor = index;
  setTimeout(() => { if (index === shownFor && !$('try-panel').hidden) startTry(true); }, 1200);
}

function revealAfterTry() {
  const m = items[index];
  board.set({ viewOnly: true });
  board.setAutoShapes([arrow(m.move_uci, 'red'), arrow(m.best_uci, 'green')].filter(Boolean));
  $('answer').hidden = false;
  $('show-btn').hidden = true;
  $('try-btn').hidden = false;
}

$('try-btn').addEventListener('click', () => startTry());
$('show-btn').addEventListener('click', () => { $('try-panel').hidden = true; showAnswer(); });
$('prev-btn').addEventListener('click', () => { $('try-panel').hidden = true; show(index - 1); });
$('next-btn').addEventListener('click', () => { $('try-panel').hidden = true; show(index + 1); });
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || !items.length) return;
  if (e.key === 'ArrowLeft') $('prev-btn').click();
  if (e.key === 'ArrowRight') $('next-btn').click();
});

restoreFilters();
load();
