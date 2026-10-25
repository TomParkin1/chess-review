// Everything about the chessboard that pages share: creation with the saved settings,
// move-mark badges on squares, the promotion picker and move sounds.
import { Chessground } from 'chessground';
import { markIcon } from './icons.js';

// ------------------------------------------------------------ settings

const SETTINGS_KEY = 'chess-review-settings';
const DEFAULTS = { board: 'brown', pieces: 'neo', sound: true, coords: true, badges: true };

export function getSettings() {
  try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}') }; } catch { return { ...DEFAULTS }; }
}

export function saveSettings(changes) {
  const next = { ...getSettings(), ...changes };
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(next)); } catch { /* unavailable */ }
  document.documentElement.dataset.board = next.board;
  document.documentElement.dataset.pieces = next.pieces;
  return next;
}

// ------------------------------------------------------------ board

// Pass viewOnly: false for boards that will ever take moves: chessground only binds
// its input listeners when the board is created.
export function makeBoard(el, config = {}) {
  const cg = Chessground(el, {
    viewOnly: true,
    coordinates: getSettings().coords,
    animation: { enabled: true, duration: 150 },
    highlight: { lastMove: true, check: true },
    drawable: { enabled: true, visible: true },
    ...config,
  });
  keepRealBounds(cg, el);
  return cg;
}

// Chessground measures the board once (a memo, cleared by its own ResizeObserver) and places
// arrows from that size. Measured while the board was hidden (0×0), every arrow lands at NaN (B17:
// "<line> attribute x1: Expected length, NaN"). So a hidden board keeps its last real size (or a
// unit square) and is measured again next time; arrows only depend on the board's aspect ratio.
// The cached size also holds the board's position, which goes stale when something above it opens
// or closes (Practice's filter form) without a scroll or resize: clicks then hit the wrong squares.
// So it is measured afresh at the start of every click or touch on the board.
// Chessground rebuilds `state.dom` (and the memo) whenever the board flips, so the guard is put
// back on every rebuild. (No redrawAll() on window resize any more: it dropped the badge layer.)
function keepRealBounds(cg, el) {
  let dom;
  let last = null;
  const guard = d => {
    const measure = d.bounds;
    const bounds = () => {
      const b = measure();
      if (b.width && b.height) return (last = b);
      measure.clear();
      return last || new DOMRect(0, 0, 1, 1);
    };
    bounds.clear = measure.clear;
    d.bounds = bounds;
  };
  const current = cg.state.dom;
  Object.defineProperty(cg.state, 'dom', { configurable: true, enumerable: true, get: () => dom, set: d => { dom = d; guard(d); } });
  cg.state.dom = current;
  for (const type of ['mousedown', 'touchstart']) el.addEventListener(type, () => dom.bounds.clear(), { capture: true, passive: true });
}

// A badge (move-mark icon) in the corner of one square, drawn above the pieces.
export function createBadge(cg, boardEl) {
  const layer = document.createElement('div');
  layer.className = 'badge-layer';
  boardEl.append(layer);
  return {
    show(square, mark) {
      layer.replaceChildren();
      if (!square || !mark || !getSettings().badges) return;
      const white = cg.state.orientation === 'white';
      const file = square.charCodeAt(0) - 97, rank = +square[1] - 1;
      const col = white ? file : 7 - file, row = white ? 7 - rank : rank;
      const badge = document.createElement('div');
      badge.className = 'square-badge';
      badge.style.left = `${(col + 1) * 12.5}%`;
      badge.style.top = `${row * 12.5}%`;
      badge.innerHTML = markIcon(mark, 28);
      layer.append(badge);
    },
    clear() { layer.replaceChildren(); },
  };
}

// Ask which piece to promote to. Resolves to 'q' | 'r' | 'b' | 'n' (queen if dismissed).
export function choosePromotion(boardEl, color, square, orientation) {
  return new Promise(resolve => {
    const white = orientation === 'white';
    const file = square.charCodeAt(0) - 97;
    const col = white ? file : 7 - file;
    const fromTop = (color === 'white') === white;   // the promoting side's back rank is on top
    const overlay = document.createElement('div');
    overlay.className = 'promotion';
    const pieces = [['q', 'queen'], ['n', 'knight'], ['r', 'rook'], ['b', 'bishop']];
    pieces.forEach(([code, name], i) => {
      const cell = document.createElement('button');
      cell.className = 'promotion-choice';
      cell.style.left = `${col * 12.5}%`;
      cell.style.top = `${(fromTop ? i : 7 - i) * 12.5}%`;
      cell.title = name;
      const piece = document.createElement('piece');
      piece.className = `${name} ${color}`;
      cell.append(piece);
      cell.addEventListener('click', e => { e.stopPropagation(); finish(code); });
      overlay.append(cell);
    });
    overlay.addEventListener('click', () => finish('q'));
    function finish(code) { overlay.remove(); resolve(code); }
    boardEl.append(overlay);
  });
}

export function isPromotion(piece, dest) {
  return piece?.type === 'p' && (dest[1] === '8' || dest[1] === '1');
}

// ------------------------------------------------------------ sounds (synthesised, no files)

let audio = null;
function tone(freq, duration, { type = 'sine', gain = 0.12, delay = 0, slide = null } = {}) {
  if (!getSettings().sound) return;
  try {
    audio = audio || new AudioContext();
    const t = audio.currentTime + delay;
    const osc = audio.createOscillator();
    const amp = audio.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t);
    if (slide) osc.frequency.exponentialRampToValueAtTime(slide, t + duration);
    amp.gain.setValueAtTime(gain, t);
    amp.gain.exponentialRampToValueAtTime(0.0001, t + duration);
    osc.connect(amp).connect(audio.destination);
    osc.start(t);
    osc.stop(t + duration);
  } catch { /* audio not available */ }
}

export const sound = {
  move: () => tone(420, 0.07, { type: 'triangle', gain: 0.18, slide: 260 }),
  capture: () => { tone(300, 0.09, { type: 'square', gain: 0.07, slide: 160 }); tone(520, 0.05, { type: 'triangle', gain: 0.1 }); },
  check: () => { tone(660, 0.08, { type: 'triangle', gain: 0.12 }); tone(880, 0.1, { type: 'triangle', gain: 0.1, delay: 0.07 }); },
  correct: () => { tone(660, 0.12, { gain: 0.12 }); tone(990, 0.18, { gain: 0.12, delay: 0.1 }); },
  wrong: () => tone(220, 0.25, { type: 'sawtooth', gain: 0.06, slide: 150 }),
};

// Sound for a move given its SAN ('Nxe5+', 'O-O', ...).
export function moveSound(san) {
  if (!san) return;
  if (/[+#]/.test(san)) sound.check();
  else if (san.includes('x')) sound.capture();
  else sound.move();
}
