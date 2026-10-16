// Helpers shared by the pages.

export async function api(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let message = res.statusText;
    try { message = (await res.json()).error || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  return res.json();
}

export const SYMBOLS = { inaccuracy: '?!', mistake: '?', blunder: '??' };

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

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
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
