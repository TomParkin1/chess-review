// The "new games" session: first a self-review of the games that went wrong (where did it
// turn? picked before any engine verdict), then puzzles from the games just synced/uploaded,
// in game order, then a summary card of what this batch shows.
import { makeBoard, sound } from './board.js';
import { api, arrow, busy, el, guard, isSolved, lastMove, sideToMove, SYMBOLS } from './common.js';
import { createPuzzle } from './puzzle.js';
import { countBar } from './widgets.js';

const $ = id => document.getElementById(id);
const KIND_LABELS = {
  missed_opportunity: 'Missed opportunities', positional: 'Positional slips', loses_material: 'Losing material',
  hanging_piece: 'Hanging pieces', missed_tactic: 'Missed tactics', king_safety: 'King safety',
  missed_capture: 'Missed captures', allowed_mate: 'Walking into mate', missed_mate: 'Missed mates',
  fork: 'Forks', discovered_attack: 'Discovered attacks',
};

let info = null;
let items = [];
let index = 0;
const tally = { solved: 0, failed: 0 };
let reviewGames = [];
const srTally = { found: 0, tried: 0 };

const puzzle = createPuzzle({
  onResult: (item, result) => {
    if (isSolved(result)) tally.solved += 1; else tally.failed += 1;
    updateBar();
  },
  onNext: () => {
    index += 1;
    if (index >= items.length) finish(); else showCurrent();
  },
});

function updateBar() {
  const m = items[index];
  const games = [...new Set(items.map(i => i.game_id))];
  const gameNo = m ? games.indexOf(m.game_id) + 1 : games.length;
  $('counter').textContent = `Game ${gameNo} of ${games.length} · puzzle ${Math.min(index + 1, items.length)} of ${items.length}`;
  $('session-score').textContent = `✓ ${tally.solved} · ✗ ${tally.failed}`;
  const bar = $('session-progress');
  bar.hidden = false;
  bar.firstElementChild.style.width = `${(index / Math.max(items.length, 1)) * 100}%`;
}

function showCurrent() {
  updateBar();
  puzzle.show(items[index]);
}

async function finish() {
  $('review').hidden = true;
  $('self-review').hidden = true;
  $('session-start').hidden = true;
  const s = await api('/api/session/done', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ batch_ids: info.batch_ids }),
  });
  const box = $('session-done');
  box.hidden = false;
  const attempted = tally.solved + tally.failed;
  const kindMax = Math.max(1, ...s.top_kinds.map(([, n]) => n));
  const clockMax = Math.max(1, ...s.blunder_clock.map(([, n]) => n));
  const anyClock = s.blunder_clock.some(([, n]) => n);
  const lessons = [];
  if (s.top_kinds.length) lessons.push(`Your most common problem this batch: ${(KIND_LABELS[s.top_kinds[0][0]] || s.top_kinds[0][0]).toLowerCase()}.`);
  const lowTime = s.time_trouble_blunders || 0;
  if (s.blunders && lowTime / s.blunders >= 0.4) lessons.push(`${lowTime} of your ${s.blunders} blunders came in time trouble (under 10% of your clock): watch the clock.`);

  box.replaceChildren(...[
    el('div', { class: 'celebrate' }, '📋'),
    el('h1', {}, 'Batch summary'),
    el('p', { class: 'muted' }, `${s.games} games · ${s.wins}W ${s.draws}D ${s.losses}L` +
       (s.accuracy != null ? ` · ${s.accuracy}% average accuracy` : '') +
       ` · ${s.mistakes} mistakes, ${s.blunders} blunders`),
    attempted || s.self_review?.tries ? el('div', { class: 'chips' },
      s.self_review?.tries ? el('span', { class: 'chip' },
        `🔍 turning points you found: ${s.self_review.hits} of ${s.self_review.tries}`) : null,
      attempted ? el('span', { class: 'chip solved' }, `✓ ${tally.solved} solved`) : null,
      attempted ? el('span', { class: 'chip failed' }, `✗ ${tally.failed} missed (they're now in daily training)`) : null) : null,
    lessons.length ? el('div', { class: 'lessons' }, ...lessons.map(l => el('p', {}, '💡 ', l))) : null,
    el('div', { class: 'summary-grid' },
      el('div', {}, el('div', { class: 'section-title' }, 'What went wrong'),
        ...(s.top_kinds.length ? s.top_kinds.map(([k, n]) => countBar(KIND_LABELS[k] || k, n, kindMax, 'var(--mistake)'))
                               : [el('p', { class: 'muted small' }, 'No mistakes or blunders. Nice!')])),
      anyClock ? el('div', {}, el('div', { class: 'section-title' }, 'Share of your clock left at each blunder'),
        ...s.blunder_clock.map(([label, n]) => countBar(label, n, clockMax, 'var(--blunder)'))) : null),
    s.worst_game ? el('p', {}, 'Toughest game: ',
      el('a', { href: `/game/${s.worst_game.id}` }, `vs ${s.worst_game.opponent}`),
      ` (${s.worst_game.accuracy}% accuracy, ${s.worst_game.result}). Open its report to see the key moments.`) : null,
    el('div', { class: 'actions' },
      el('a', { class: 'btn primary big', href: '/train' }, 'Go to daily training'),
      el('a', { class: 'btn big', href: '/' }, 'Home')),
  ].filter(Boolean));
}

// ------------------------------------------------------------ self-review (before the engine)

const srBoard = makeBoard($('sr-board'), { viewOnly: true, animation: { enabled: true, duration: 150 } });
let sr = null;   // { game, gameIndex, cursor, picked, result }

function moveLabel(m) {
  return `${Math.floor(m.ply / 2) + 1}${m.ply % 2 === 0 ? '.' : '...'}${m.san}`;
}

function isMine(m) { return sideToMove(m.fen) === sr.game.my_color; }

// "Where did it turn?" makes no sense for a game I won (Section 3): there the question is my
// worst moment, and in a draw where it slipped. The answer is the same move either way.
const SR_COPY = {
  loss: { ask: 'Before the engine has its say: where did this game turn? Step through it and pick the move of yours where it went wrong.',
          pick: 'It turned at', it: 'the game turned at' },
  draw: { ask: 'Before the engine has its say: where did this game slip? Step through it and pick the move of yours where it went wrong.',
          pick: 'It slipped at', it: 'the game slipped at' },
  win: { ask: 'You won, but before the engine has its say: where was your worst moment? Step through it and pick the move of yours that gave away the most.',
         pick: 'My worst moment:', it: 'your worst moment was' },
};
const srCopy = () => SR_COPY[sr.game.result] || SR_COPY.loss;

function showReviewGame(i) {
  const game = reviewGames[i];
  sr = { game, gameIndex: i, cursor: 0, picked: null, result: null };
  $('session-start').hidden = true;
  $('review').hidden = true;
  $('self-review').hidden = false;
  $('sr-counter').textContent = `Self-review · game ${i + 1} of ${reviewGames.length}`;
  $('sr-score').textContent = srTally.tried ? `found ${srTally.found} of ${srTally.tried}` : '';
  $('sr-title').textContent = `vs ${game.opponent} · you ${{ win: 'won', loss: 'lost', draw: 'drew' }[game.result]}`;
  $('sr-context').textContent = `${game.date} · ${game.opening}`;
  $('sr-prompt').textContent = srCopy().ask;
  $('sr-feedback').hidden = true;
  $('sr-explanation').hidden = true;
  $('sr-actions').hidden = false;
  $('sr-next-actions').hidden = true;
  updatePickButton();
  srBoard.set({ orientation: game.my_color });
  srBoard.setAutoShapes([]);
  renderReviewMoves();
  goTo(game.moves.length);       // start at the final position; step back from there
}

function renderReviewMoves() {
  const g = sr.game;
  $('sr-moves').replaceChildren(...g.moves.map((m, i) => {
    const num = m.ply % 2 === 0 ? `${m.ply / 2 + 1}. ` : (i === 0 ? `${Math.floor(m.ply / 2) + 1}... ` : '');
    const classes = ['sm', isMine(m) ? 'mine' : 'theirs'];
    if (sr.picked === m.ply) classes.push('picked');
    if (sr.result && sr.result.ply === m.ply) classes.push('turning');
    if (sr.cursor === i + 1) classes.push('at');
    return el('span', {
      class: classes.join(' '),
      onclick: () => {
        goTo(i + 1);
        if (!sr.result && isMine(m)) { sr.picked = m.ply; updatePickButton(); renderReviewMoves(); }
      },
    }, num + m.san);
  }));
}

function updatePickButton() {
  const m = sr.game.moves.find(x => x.ply === sr.picked);
  $('sr-pick').disabled = !m;
  $('sr-pick').textContent = m ? `${srCopy().pick} ${moveLabel(m)}` : 'Pick one of your moves';
}

function goTo(cursor) {
  const g = sr.game;
  sr.cursor = Math.max(0, Math.min(g.moves.length, cursor));
  const fen = sr.cursor < g.moves.length ? g.moves[sr.cursor].fen : g.final_fen;
  srBoard.set({ fen, lastMove: sr.cursor ? lastMove(g.moves[sr.cursor - 1].uci) : undefined });
  document.querySelectorAll('#sr-moves .sm').forEach((c, i) => c.classList.toggle('at', i + 1 === sr.cursor));
}

async function submitPick(ply) {
  const r = await api('/api/session/self-review', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ game_id: sr.game.game_id, ply }),
  });
  sr.result = r;
  srTally.tried += 1;
  if (r.hit) srTally.found += 1;
  const turning = sr.game.moves.find(m => m.ply === r.ply);
  const label = `${moveLabel(turning)}${SYMBOLS[r.judgement] || ''}`;
  const picked = sr.game.moves.find(m => m.ply === ply);
  if (r.hit) sound.correct(); else sound.wrong();
  $('sr-feedback').hidden = false;
  $('sr-feedback').className = `feedback ${r.hit ? 'ok' : 'bad'}`;
  const it = srCopy().it;
  const It = it.charAt(0).toUpperCase() + it.slice(1);
  $('sr-feedback').textContent = r.exact ? `✓ Exactly: ${it} ${label}.`
    : r.hit ? `✓ Close enough: ${it} ${label} (you picked ${moveLabel(picked)}).`
      : ply == null ? `${It} ${label}.`
        : `✗ ${It} ${label}, not ${moveLabel(picked)}.`;
  $('sr-explanation').hidden = !r.explanation;
  $('sr-explanation').className = `explanation ${r.judgement}`;
  $('sr-explanation').textContent = r.explanation || '';
  $('sr-actions').hidden = true;
  $('sr-next-actions').hidden = false;
  $('sr-next').textContent = sr.gameIndex + 1 < reviewGames.length ? 'Next game ▶'
    : items.length ? 'Now the puzzles ▶' : 'See the summary ▶';
  renderReviewMoves();
  goTo(sr.game.moves.findIndex(m => m.ply === r.ply));          // the position before the turning move
  srBoard.setAutoShapes([arrow(r.move_uci, 'red'), arrow(r.best_uci, 'green')].filter(Boolean));
}

$('sr-pick').addEventListener('click', () => { if (sr && !sr.result && sr.picked != null) submitPick(sr.picked); });
$('sr-unsure').addEventListener('click', () => { if (sr && !sr.result) submitPick(null); });
$('sr-back').addEventListener('click', () => sr && goTo(sr.cursor - 1));
$('sr-fwd').addEventListener('click', () => sr && goTo(sr.cursor + 1));
$('sr-next').addEventListener('click', () => {
  srBoard.setAutoShapes([]);
  if (sr.gameIndex + 1 < reviewGames.length) showReviewGame(sr.gameIndex + 1);
  else startPuzzles();
});
document.addEventListener('keydown', e => {
  if ($('self-review').hidden || e.target.matches('input, select, textarea')) return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); goTo(sr.cursor - 1); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); goTo(sr.cursor + 1); }
});

function startPuzzles() {
  $('self-review').hidden = true;
  $('session-start').hidden = true;
  if (!items.length) return finish();
  $('review').hidden = false;
  index = 0;
  showCurrent();
}

$('start-btn').addEventListener('click', () => {
  if (reviewGames.length) showReviewGame(0);
  else startPuzzles();
});

async function load() {
  [info, { games: reviewGames }] = await Promise.all([api('/api/session'), api('/api/session/self-review')]);
  if (!info.open) {
    $('session-title').textContent = 'No new games';
    $('session-sub').textContent = 'Sync from chess.com on the Home page to bring in your latest games.';
    $('home-link').hidden = false;
    return;
  }
  items = info.items;
  const waiting = info.games - info.analysed;
  const steps = [];
  if (reviewGames.length) {
    steps.push(`find where ${reviewGames.length === 1 ? 'the game that went wrong' : `${reviewGames.length} games`} ` +
               'turned, before the engine has its say');
  }
  if (items.length) steps.push(`solve ${items.length} mistake${items.length === 1 ? '' : 's'}, in game order`);
  $('session-title').textContent = `${info.games} new game${info.games === 1 ? '' : 's'}`;
  $('session-sub').textContent = steps.length
    ? `First ${steps.join(', then ')}.` +
      (waiting ? ` ${waiting} game(s) are still being analysed; their mistakes will join daily training.` : '')
    : (waiting ? `Still analysing ${waiting} game(s)… come back in a few minutes.` : 'No mistakes or blunders in these games!');
  $('start-btn').hidden = !steps.length;
  $('skip-session').hidden = false;
}

$('skip-session').addEventListener('click', busy(finish));     // once, however often it's clicked

guard(document.querySelector('main'), load, 'your new games');
