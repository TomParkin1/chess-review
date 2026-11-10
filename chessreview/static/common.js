// Helpers shared by the pages.

// A failed API call: `message` says what went wrong (the server's {error}), `status` is the
// HTTP status (0 when the server can't be reached).
export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function api(url, options = {}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch {
    throw new ApiError("Can't reach Chess Review. Is the server still running?", 0);
  }
  if (!res.ok) {
    let message = res.statusText || `HTTP ${res.status}`;
    try { message = (await res.json()).error || message; } catch { /* not JSON */ }
    throw new ApiError(message, res.status);
  }
  return res.json();
}

// ------------------------------------------------------------ error and empty states

// Loading something failed: what, why, and a way to try again.
export function errorState(err, retry, what = 'this page') {
  return el('div', { class: 'state-box error-state', role: 'alert' },
    el('strong', {}, `Couldn't load ${what}.`),
    el('p', { class: 'muted' }, err?.message || String(err)),
    retry ? el('button', { class: 'btn', onclick: retry }, 'Try again') : null);
}

// Nothing to show yet, and the next step (`action` = {href, label}).
export function emptyState(title, text, action = null) {
  return el('div', { class: 'state-box empty-state' },
    el('strong', {}, title),
    text ? el('p', { class: 'muted' }, text) : null,
    action ? el('a', { class: 'btn primary', href: action.href }, action.label) : null);
}

// Run a loader that draws its own data; if it fails, show why at the top of `where`, with
// Try again (which runs it again). Returns the loader's result, or undefined after a failure.
export async function guard(where, load, what) {
  where.querySelector(':scope > .error-state')?.remove();
  try {
    return await load();
  } catch (err) {
    console.error(err);
    const box = errorState(err, () => { box.remove(); guard(where, load, what); }, what);
    where.prepend(box);
    return undefined;
  }
}

// The data a page can't work without. If it fails to load, show why at the top of `where`
// (Try again reloads the page) and never resolve, so the page's script stops there quietly.
export async function need(where, url, what) {
  try {
    return await api(url);
  } catch (err) {
    console.error(err);
    where.prepend(errorState(err, () => location.reload(), what));
    return new Promise(() => {});
  }
}

export const SYMBOLS = { inaccuracy: '?!', mistake: '?', blunder: '??' };

// Every page scores a puzzle the same way: solved with hints or retries still counts as solved (B15).
export const isSolved = result => result === 'solved' || result === 'solved_with_help';

// What identifies a puzzle item: a library puzzle by its id, one of my positions by game and ply.
export const itemKey = item => (item.mode === 'library' ? `lib:${item.puzzle_id}` : `${item.game_id}:${item.ply}`);

// "Threat check first" (real-game mode): one preference for Practice and Today's reviews.
// Returns true/false, or null when never set (each page then has its own default).
export function threatCheckPref() {
  try {
    const v = localStorage.getItem('threat-check') ?? localStorage.getItem('practice-blind') ?? localStorage.getItem('train-blind');
    return v === null ? null : v === '1';
  } catch { return null; }
}

export function setThreatCheckPref(on) {
  try { localStorage.setItem('threat-check', on ? '1' : '0'); } catch { /* unavailable */ }
}

// Evals arrive from White's point of view as {cp, mate}.
export function formatEval(ev) {
  if (!ev) return '';
  if (ev.mate !== null && ev.mate !== undefined) return ev.mate > 0 ? `#${ev.mate}` : `#-${-ev.mate}`;
  const v = (ev.cp || 0) / 100;
  return (v > 0 ? '+' : '') + v.toFixed(2);
}

// Lichess winning chances on a -1..1 scale (White's point of view).
export function winChance(ev) {
  if (!ev) return 0;
  if (ev.mate !== null && ev.mate !== undefined) return ev.mate > 0 ? 1 : -1;
  const cp = Math.max(-1000, Math.min(1000, ev.cp || 0));
  return 2 / (1 + Math.exp(-0.00368208 * cp)) - 1;
}

export function arrow(uci, brush) {
  return uci ? { orig: uci.slice(0, 2), dest: uci.slice(2, 4), brush } : null;
}

export function lastMove(uci) {
  return uci ? [uci.slice(0, 2), uci.slice(2, 4)] : undefined;
}

export function sideToMove(fen) {
  return fen.split(' ')[1] === 'w' ? 'white' : 'black';
}

// The board itself lives in board.js; re-exported here for older imports.
export { makeBoard } from './board.js';

// Map of legal destinations for chessground, from a chess.js instance.
export function legalDests(chess) {
  const dests = new Map();
  for (const m of chess.moves({ verbose: true })) {
    if (!dests.has(m.from)) dests.set(m.from, []);
    dests.get(m.from).push(m.to);
  }
  return dests;
}

// Wrap an async click handler so clicks while it runs are ignored (double clicks, B12).
export function busy(fn) {
  let running = false;
  return async (...args) => {
    if (running) return undefined;
    running = true;
    try { return await fn(...args); } finally { running = false; }
  };
}

// Make a clickable element that isn't a button or link reachable from the keyboard: Tab focuses
// it, Enter or Space clicks it, and screen readers call it a button.
const NATIVE = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'LABEL', 'OPTION', 'SUMMARY']);
export function clickable(node) {
  if (NATIVE.has(node.tagName) || node.hasAttribute('tabindex')) return node;
  node.tabIndex = 0;
  if (!node.hasAttribute('role')) node.setAttribute('role', node.tagName === 'TR' ? 'link' : 'button');
  node.addEventListener('keydown', e => {
    if (e.target !== node || (e.key !== 'Enter' && e.key !== ' ')) return;
    e.preventDefault();
    node.click();
  });
  return node;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  if (attrs.onclick) clickable(node);
  for (const c of children.flat()) {
    if (c !== null && c !== undefined) node.append(c instanceof Node ? c : String(c));
  }
  return node;
}

// One row of a games table (Home's recent games and the Games page).
export function gameRow(g) {
  const opp = g.my_color === 'white' ? g.black : g.white;
  const oppElo = g.my_color === 'white' ? g.black_elo : g.white_elo;
  const myElo = g.my_color === 'white' ? g.white_elo : g.black_elo;
  const acc = v => (v === null || v === undefined ? '–' : v.toFixed(1));
  const errors = g.analysed
    ? el('span', {}, el('span', { class: 'sym inaccuracy' }, g.inaccuracies || 0), ' / ',
         el('span', { class: 'sym mistake' }, g.mistakes || 0), ' / ',
         el('span', { class: 'sym blunder' }, g.blunders || 0))
    : el('span', { class: 'muted' }, 'analysing…');
  return el('tr', { onclick: () => { location.href = `/game/${g.id}`; } },
    el('td', {}, el('span', { class: 'result-pill ' + g.my_result }, g.my_result)),
    el('td', {}, el('span', { class: `dot ${g.my_color}`, title: `You played ${g.my_color}` }), ' ', opp,
       oppElo ? el('span', { class: 'muted' }, ` (${oppElo})`) : null),
    el('td', { class: 'num' }, myElo ?? '–'),
    el('td', { class: 'num', title: 'Your accuracy / opponent accuracy (Lichess method)' },
       el('strong', {}, acc(g.my_accuracy)), el('span', { class: 'muted' }, ` / ${acc(g.opp_accuracy)}`)),
    el('td', { title: g.opening }, g.opening_family),
    el('td', { class: 'num' }, Math.ceil(g.num_plies / 2)),
    el('td', { class: 'num' }, errors),
    el('td', { class: 'muted' }, `${g.time_class}`),
    el('td', { class: 'muted num' }, playedAt(g)),
  );
}

export const GAME_TABLE_HEAD =
  '<tr><th>Result</th><th>Opponent</th><th>Your rating</th><th title="Accuracy (Lichess method)">Accuracy</th>' +
  '<th>Opening</th><th>Moves</th><th title="inaccuracies / mistakes / blunders">?! / ? / ??</th><th>Type</th><th>Played</th></tr>';

// 'YYYY-MM-DD HH:MM', or just the date when the time is unknown (old bulk downloads).
export function playedAt(g) {
  const t = g.end_time || g.date || '';
  return t.endsWith('00:00:00') ? t.slice(0, 10) : t.slice(0, 16);
}
