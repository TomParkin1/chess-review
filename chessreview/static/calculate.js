// Calculate (E6): a position, a line of moves to read (not played on the board) and a question about
// the position at the end of it (chessreview/calc.py judges it). Sessions of SESSION questions.
import { makeBoard, sound } from './board.js';
import { api, busy, el, emptyState, guard } from './common.js';

const $ = id => document.getElementById(id);
const post = (url, body) => api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const SESSION = 10;
const FROM_TODAY = new URLSearchParams(location.search).get('from') === 'today';

const board = makeBoard($('board'), {
  viewOnly: false, keyboard: false, movable: { color: undefined }, draggable: { enabled: false },
  events: { select: key => onSquare(key) },
});

let q = null;           // the question on screen
let answered = false;
let showingFinal = false;
let finalFen = null;
let mark = null;        // the square to point at in the final position (the answer, or the piece asked about)
let asked = [];         // keys of this session's questions
let tally = { right: 0, wrong: 0 };

function setDepth(o) {
  $('depth-chip').textContent = `${o.plies} half-moves deep`;
}

function showStart() {
  board.set({ fen: q.fen, orientation: q.orientation, lastMove: undefined, check: false });
  board.setAutoShapes([]);
  showingFinal = false;
  $('toggle-btn').textContent = 'Final position';
}

const next = busy(async () => {
  if (asked.length >= SESSION) return done();
  const r = await api(`/api/calc/next?exclude=${encodeURIComponent(asked.join(','))}`);
  q = r.question;
  if (!q) return done(asked.length ? null : 'none');
  asked.push(q.key);
  answered = false;
  showStart();
  $('counter').textContent = `Question ${asked.length} of ${SESSION}${r.due ? ' · review' : ''}`;
  $('q-source').textContent = q.source.startsWith('lib:') ? 'From a Lichess puzzle' : 'From one of your games: the moves played';
  $('q-line').textContent = q.line_san;
  $('q-prompt').textContent = q.prompt;
  $('q-prompt').dataset.key = q.key;      // (the browser regression looks the answer up by it)
  for (const kind of ['where', 'best', 'capture']) $(`answer-${kind}`).hidden = q.kind !== kind;
  $('where-input').value = '';
  $('best-input').value = '';
  $('feedback').hidden = true;
  $('after').hidden = true;
  (q.kind === 'where' ? $('where-input') : q.kind === 'best' ? $('best-input') : $('yes-btn')).focus();
});

function onSquare(key) {
  if (!q || answered || q.kind !== 'where') return;
  $('where-input').value = key;
  submit(key);
}

const submit = busy(async given => {
  if (!q || answered || !given) return;
  answered = true;
  let r;
  try {
    r = await post('/api/calc/answer', { key: q.key, answer: given });
  } catch (err) {
    answered = false;                       // a typo (400) or a lost connection: try again
    $('feedback').hidden = false;
    $('feedback').className = 'feedback';
    $('feedback').textContent = err.status === 400 ? `${err.message}: try again.` : `Couldn't check that (${err.message}).`;
    return;
  }
  tally[r.correct ? 'right' : 'wrong'] += 1;
  $('score').textContent = `✓ ${tally.right} · ✗ ${tally.wrong}`;
  if (r.correct) sound.correct(); else sound.wrong();
  const answerText = q.kind === 'capture' ? (r.expected === 'yes' ? 'Yes' : 'No') : r.expected;
  $('feedback').hidden = false;
  $('feedback').className = `feedback ${r.correct ? 'ok' : 'bad'}`;
  $('feedback').textContent = (r.correct ? `✓ Right: ${answerText}.`
    : `✗ ${r.illegal ? `${given} isn't legal at the end of the line. ` : ''}The answer is ${answerText}. It comes back soon.`) +
    (r.level_changed ? ` Lines are now ${r.plies} half-moves long.` : '');
  setDepth(r);
  finalFen = r.final_fen;
  mark = q.kind === 'where' ? r.expected : q.subject || null;
  for (const kind of ['where', 'best', 'capture']) $(`answer-${kind}`).hidden = true;
  $('after').hidden = false;
  toggle(true);
  $('next-btn').focus();
});

function toggle(final = !showingFinal) {
  if (!q) return;
  if (final && finalFen) {
    board.set({ fen: finalFen, lastMove: [q.line.at(-1).slice(0, 2), q.line.at(-1).slice(2, 4)] });
    board.setAutoShapes(mark ? [{ orig: mark, brush: 'blue' }] : []);
    showingFinal = true;
    $('toggle-btn').textContent = 'Start position';
  } else showStart();
}

function done(reason) {
  $('calc-run').hidden = true;
  const box = $('calc-done');
  box.hidden = false;
  if (reason === 'none') {
    box.replaceChildren(emptyState('No questions yet', 'They come from the Lichess puzzle library and your analysed games. ' +
      'Download the library in Settings or sync your games on Home.', { href: '/settings', label: 'Settings' }));
    return;
  }
  const total = tally.right + tally.wrong;
  box.replaceChildren(
    el('div', { class: 'celebrate' }, tally.right >= total * 0.7 ? '🧠' : '✅'),
    el('h2', { tabindex: '-1' }, `${tally.right} of ${total} right`),
    el('p', { class: 'muted' }, `Your lines are ${$('depth-chip').textContent}. Missed questions come back when they're due.`),
    el('div', { class: 'actions' },
      FROM_TODAY ? el('a', { class: 'btn primary', href: '/train' }, 'Back to Today ▶') : '',
      el('button', { class: `btn ${FROM_TODAY ? '' : 'primary'}`, onclick: restart }, 'Another 10')));
  box.querySelector('h2').focus();
}

function restart() {
  asked = [];
  tally = { right: 0, wrong: 0 };
  $('score').textContent = '';
  $('calc-done').hidden = true;
  $('calc-run').hidden = false;
  next();
}

$('where-btn').addEventListener('click', () => submit($('where-input').value.trim().toLowerCase()));
$('where-input').addEventListener('keydown', e => { if (e.key === 'Enter') submit($('where-input').value.trim().toLowerCase()); });
$('best-btn').addEventListener('click', () => submit($('best-input').value.trim()));
$('best-input').addEventListener('keydown', e => { if (e.key === 'Enter') submit($('best-input').value.trim()); });
$('yes-btn').addEventListener('click', () => submit('yes'));
$('no-btn').addEventListener('click', () => submit('no'));
$('next-btn').addEventListener('click', next);
$('toggle-btn').addEventListener('click', () => toggle());
document.addEventListener('keydown', e => {
  if (e.target.matches('input, textarea')) return;
  if (answered && (e.key === 'Enter' || e.key === 'n')) { e.preventDefault(); next(); }
});

guard(document.querySelector('main'), async () => {
  setDepth(await api('/api/calc'));
  await next();
}, 'the calculation trainer');
