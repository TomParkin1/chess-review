// Free analysis board: play moves for either side from a starting position, building a
// variation, while live Stockfish lines stream in from /api/analyse/stream.
// Shared by the game view, the puzzles and Convert. `history` (UCI moves from `fen`) are the moves
// that led to the position being analysed: they are shown greyed out and ◀ can step back through them.
import { Chess } from 'chess.js';
import { choosePromotion, isPromotion, moveSound } from './board.js';
import { formatEval, lastMove, legalDests, sideToMove } from './common.js';

export function createAnalysis({ board, panel, evalBar }) {
  let root = null;          // {fen, last}
  let moves = [];           // [{uci, san, fen}] variation from the root
  let cursor = 0;           // 0 = root position, n = after moves[n-1]
  let base = 0;             // moves[0..base) are history; Reset returns to `base`
  let source = null;        // EventSource of the running analysis
  let active = false;
  let lines = [];

  panel.classList.add('analysis-panel');
  panel.innerHTML = `
    <div class="analysis-head">
      <strong>Analysis <span class="muted small" title="Like Lichess: + means White is better">(White's view)</span></strong>
      <span class="muted small" data-ref="depth"></span>
    </div>
    <div class="analysis-lines" data-ref="lines"></div>
    <div class="analysis-var-head">
      <span class="muted small">Your variation</span>
      <span class="solution-nav">
        <button class="btn small" data-ref="back" title="Back (←)">◀</button>
        <button class="btn small" data-ref="fwd" title="Forward (→)">▶</button>
        <button class="btn small" data-ref="reset" title="Back to where the analysis started">Reset</button>
      </span>
    </div>
    <div class="solution-moves" data-ref="variation"><span class="muted small">Make a move on the board.</span></div>`;
  const ref = name => panel.querySelector(`[data-ref="${name}"]`);
  ref('back').addEventListener('click', () => go(cursor - 1));
  ref('fwd').addEventListener('click', () => go(cursor + 1));
  ref('reset').addEventListener('click', () => go(base));

  function currentFen() { return cursor === 0 ? root.fen : moves[cursor - 1].fen; }

  function start(fen, { last, orientation, history = [] } = {}) {
    root = { fen, last };
    moves = [];
    const chess = new Chess(fen);
    for (const uci of history) {
      let mv;
      try { mv = chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] }); } catch { break; }
      moves.push({ uci: mv.from + mv.to + (mv.promotion || ''), san: mv.san, fen: chess.fen() });
    }
    base = moves.length;
    cursor = base;
    active = true;
    panel.hidden = false;
    if (orientation) board.set({ orientation });
    evalBar?.hide(false);
    refresh();
  }

  function stop() {
    active = false;
    closeSource();
    panel.hidden = true;
    board.set({ movable: { color: undefined, dests: new Map() } });
    board.setAutoShapes([]);
  }

  function go(i) {
    if (!active) return;
    cursor = Math.max(0, Math.min(moves.length, i));
    refresh();
  }

  async function onMove(orig, dest, promo) {
    const chess = new Chess(currentFen());
    const piece = chess.get(orig);
    const promotion = promo || (isPromotion(piece, dest)
      ? await choosePromotion(board.state.dom.elements.wrap, piece.color === 'w' ? 'white' : 'black', dest,
                              board.state.orientation)
      : undefined);
    let mv;
    try { mv = chess.move({ from: orig, to: dest, promotion }); } catch { return refresh(); }
    moves = moves.slice(0, cursor);            // a new move replaces anything after the cursor
    base = Math.min(base, cursor);
    moves.push({ uci: mv.from + mv.to + (mv.promotion || ''), san: mv.san, fen: chess.fen() });
    cursor = moves.length;
    moveSound(mv.san);
    refresh();
  }

  function refresh() {
    const fen = currentFen();
    const chess = new Chess(fen);
    const last = cursor === 0 ? root.last : lastMove(moves[cursor - 1].uci);
    board.set({
      fen, lastMove: last, turnColor: sideToMove(fen), check: chess.inCheck() ? sideToMove(fen) : false,
      movable: { free: false, color: 'both', dests: legalDests(chess), showDests: true,
                 events: { after: (orig, dest) => onMove(orig, dest) } },  // ignore chessground's 3rd (metadata) arg
    });
    board.setAutoShapes([]);
    renderVariation();
    analyse(fen);
  }

  function renderVariation() {
    const el = ref('variation');
    if (!moves.length) {
      el.innerHTML = '<span class="muted small">Make a move on the board.</span>';
      return;
    }
    const startPly = plyOf(root.fen);
    el.replaceChildren(...moves.map((m, i) => {
      const ply = startPly + i;
      const num = ply % 2 === 0 ? `${ply / 2 + 1}. ` : (i === 0 ? `${Math.floor(ply / 2) + 1}... ` : '');
      const chip = document.createElement('span');
      chip.className = `sm${i < base ? ' pre' : ''}${i + 1 === cursor ? ' current' : ''}`;
      chip.textContent = num + m.san;
      chip.addEventListener('click', () => go(i + 1));
      return chip;
    }));
  }

  // ------------------------------------------------------------ live engine

  function closeSource() {
    if (source) { source.close(); source = null; }
  }

  function analyse(fen) {
    closeSource();
    lines = [];
    ref('depth').textContent = 'thinking…';
    ref('lines').replaceChildren();
    const chess = new Chess(fen);
    if (chess.isGameOver()) {
      ref('depth').textContent = chess.isCheckmate() ? 'checkmate' : 'game over';
      const whiteMated = sideToMove(fen) === 'white';
      if (chess.isCheckmate()) evalBar?.set({ cp: null, mate: whiteMated ? -1 : 1 }, undefined, '#');
      else evalBar?.set({ cp: 0, mate: null }, undefined, '½');
      return;
    }
    const es = new EventSource(`/api/analyse/stream?fen=${encodeURIComponent(fen)}`);
    source = es;
    es.onmessage = ev => {
      if (source !== es) return;
      const snap = JSON.parse(ev.data);
      if (snap.lines?.length) {
        lines = snap.lines;
        renderLines(fen, snap.depth);
      }
      if (snap.done) closeSource();   // otherwise EventSource would reconnect and start again
    };
    es.onerror = () => { if (source === es) closeSource(); };
  }

  function renderLines(fen, depth) {
    ref('depth').textContent = `depth ${depth}`;
    evalBar?.set(lines[0].eval);
    board.setAutoShapes(lines.slice(0, 2).map((l, i) => ({
      orig: l.uci.slice(0, 2), dest: l.uci.slice(2, 4), brush: i === 0 ? 'blue' : 'paleBlue',
    })));
    ref('lines').replaceChildren(...lines.map(l => {
      const row = document.createElement('div');
      row.className = 'analysis-line';
      row.title = 'Play this line’s first move';
      const ev = document.createElement('span');
      ev.className = 'eval';
      ev.textContent = formatEval(l.eval);
      const text = document.createElement('span');
      text.className = 'line-moves';
      text.textContent = l.san_line;
      row.append(ev, text);
      row.addEventListener('click', () => {
        const chess = new Chess(fen);
        const mv = chess.move({ from: l.uci.slice(0, 2), to: l.uci.slice(2, 4), promotion: l.uci[4] });
        if (mv) onMove(mv.from, mv.to, mv.promotion);
      });
      return row;
    }));
  }

  return {
    start, stop, go,
    back: () => go(cursor - 1),
    forward: () => go(cursor + 1),
    get active() { return active; },
  };
}

// Ply number (0 = White's first move) of a FEN's side to move.
function plyOf(fen) {
  const parts = fen.split(' ');
  return (parseInt(parts[5], 10) - 1) * 2 + (parts[1] === 'b' ? 1 : 0);
}
