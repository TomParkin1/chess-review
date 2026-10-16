// Helpers shared by the pages.
import { Chessground } from 'chessground';

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

// Note: pass viewOnly: false for boards that will ever take moves; chessground only binds
// input listeners at creation, so switching viewOnly off later has no effect.
export function makeBoard(el, config = {}) {
  const cg = Chessground(el, {
    viewOnly: true,
    coordinates: true,
    animation: { enabled: true, duration: 150 },
    highlight: { lastMove: true, check: true },
    drawable: { enabled: true, visible: true },
    ...config,
  });
  // Chessground measures its container; redraw when the window size changes.
  window.addEventListener('resize', () => cg.redrawAll());
  return cg;
}

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
