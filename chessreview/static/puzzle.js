// The "learn from your mistake" puzzle, shared by Practice, Train and the new-games session.
//
// Flow per mistake:  solving -> (checking) -> wrong: snap back, explain, keep solving
//                                          -> right: play the opponent's reply and continue,
//                                                    until playout_moves are found -> done
// "View solution" ends as failed; "Skip" records a skip and asks the host for the next one.
//
// The host page includes templates/_puzzle.html and calls createPuzzle({...}).show(item).
// Callbacks: onResult(item, result) once per puzzle, onNext(), onPrev() (optional).
import { Chess } from 'chess.js';
import { createAnalysis } from './analysis.js';
import { choosePromotion, createBadge, isPromotion, makeBoard, moveSound, sound } from './board.js';
import { api, arrow, lastMove, legalDests, sideToMove, SYMBOLS } from './common.js';
import { createEvalBar } from './evalbar.js';

const STATUS_CHIPS = {
  solved: 'Solved before', solved_with_help: 'Solved with help before',
  failed: 'Failed before', skipped: 'Skipped before',
};

// rich(el, 'plain', ['strong', 'bold'], ['span.ok', 'green']): builds text safely.
export function rich(el, ...parts) {
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
// Chessground's `check: true` uses its idea of whose turn it is, which may be stale: name the colour.
const checkOf = fen => (new Chess(fen).inCheck() ? sideToMove(fen) : false);
const article = w => (/^[aeiou]/.test(w) ? 'an' : 'a');

export function createPuzzle({ onResult = () => {}, onNext = () => {}, onPrev = null } = {}) {
  const $ = id => document.getElementById(id);
  // Must not start viewOnly: chessground only binds its input listeners when the board is created.
  const board = makeBoard($('board'), {
    viewOnly: false, animation: { enabled: true, duration: 200 }, movable: { color: undefined },
  });
  const evalBar = createEvalBar(document.querySelector('.board-wrap'));
  const badge = createBadge(board, $('board'));
  const analysis = createAnalysis({ board, panel: $('analysis'), evalBar });
  let puzzle = null;
  let solution = [];   // [{fen, last, san}]; index 0 is the mistake position
  let solIndex = 0;
  $('prev-btn').hidden = !onPrev;

  // ------------------------------------------------------------ showing a mistake

  function show(m) {
    puzzle = {
      item: m, fen: m.fen, last: lastMove(m.prev_move_uci), step: 0,
      attempts: 0, hints: 0, hintLevel: 0, phase: 'solving', recorded: false,
      onLine: true,     // all moves so far followed the engine's best line
    };
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

    stopAnalysis();
    badge.clear();
    board.set({ orientation: m.my_color });
    evalBar.set(m.eval_before_white, m.my_color);
    evalBar.hide(true);  // no peeking while solving
    setSolvingBoard();
  }

  function setSolvingBoard() {
    const p = puzzle;
    badge.clear();
    const chess = new Chess(p.fen);
    board.set({
      fen: p.fen, turnColor: p.item.my_color, lastMove: p.last, check: checkOf(p.fen),
      movable: { free: false, color: p.item.my_color, dests: legalDests(chess), showDests: true,
                 events: { after: onMove } },
    });
    board.setAutoShapes(hintShapes());
  }

  function lockBoard() {
    board.set({ movable: { color: undefined, dests: new Map() } });
  }

  // ------------------------------------------------------------ solving

  async function onMove(orig, dest) {
    const p = puzzle;
    if (p.phase !== 'solving') return;
    const chess = new Chess(p.fen);
    const piece = chess.get(orig);
    p.phase = 'checking';
    lockBoard();
    const promotion = isPromotion(piece, dest)
      ? await choosePromotion($('board'), p.item.my_color, dest, p.item.my_color) : '';
    const uci = orig + dest + promotion;

    const m = p.item;
    if (p.step === 0 && uci === m.move_uci) {
      return wrong(p, { uci, san: m.move_san, judgement: m.judgement, after: m.eval_after,
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
    sound.wrong();
    badge.show(r.uci?.slice(2, 4) || null, r.judgement);
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
    board.set({ fen: r.fen_after, turnColor: sideToMove(r.fen_after), lastMove: lastMove(uci), check: checkOf(r.fen_after) });
    sound.correct();
    badge.show(uci.slice(2, 4), r.is_best ? 'best' : 'excellent');

    const found = p.step + 1;
    if (found >= m.playout_moves || r.game_over || !r.reply_uci) {
      return finish(p, p.attempts === 0 && p.hints === 0 ? 'solved' : 'solved_with_help', 900);
    }
    // Play the opponent's best reply, then hand the board back.
    p.phase = 'opponent';
    if (m.best_pv_uci[lineIndex + 1] !== r.reply_uci) p.onLine = false;
    setTimeout(() => {
      if (puzzle !== p) return;
      moveSound(r.reply_san);
      badge.clear();
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
    onResult(p.item, result);
    api('/api/review/result', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ game_id: p.item.game_id, ply: p.item.ply, result,
                             attempts: p.attempts, hints_used: p.hints }),
    }).catch(() => { /* not critical */ });
  }

  // ------------------------------------------------------------ stepping through the best line

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
    stopAnalysis();
    solIndex = Math.max(0, Math.min(solution.length - 1, i));
    badge.clear();
    const s = solution[solIndex];
    const m = puzzle.item;
    board.set({ fen: s.fen, turnColor: sideToMove(s.fen), lastMove: s.last, check: checkOf(s.fen),
                movable: { color: undefined } });
    evalBar.hide(false);
    evalBar.set(solIndex === 0 ? m.eval_before_white : null, m.my_color);  // only the game position has a stored eval
    board.setAutoShapes(solIndex === 0
      ? [arrow(m.move_uci, 'red'), arrow(m.best_uci, 'green')].filter(Boolean)
      : []);
    document.querySelectorAll('#solution-moves .sm').forEach(c =>
      c.classList.toggle('current', +c.dataset.index === solIndex));
  }

  // ------------------------------------------------------------ analysis board

  function startAnalysis() {
    if (puzzle?.phase !== 'done') return;
    const s = solution[solIndex];
    analysis.start(s.fen, { last: s.last });
    $('analyse-btn').textContent = 'Back to puzzle';
    $('analyse-btn').classList.add('active');
  }

  function stopAnalysis() {
    if (!analysis.active) return;
    analysis.stop();
    $('analyse-btn').textContent = 'Analyse';
    $('analyse-btn').classList.remove('active');
  }

  // ------------------------------------------------------------ buttons + keys

  $('analyse-btn').addEventListener('click', () => {
    if (analysis.active) { stopAnalysis(); showSolutionStep(solIndex); } else startAnalysis();
  });
  $('hint-btn').addEventListener('click', giveHint);
  $('solution-btn').addEventListener('click', () => { if (puzzle?.phase === 'solving') finish(puzzle, 'failed'); });
  $('skip-btn').addEventListener('click', () => {
    if (!puzzle || puzzle.phase === 'done') return;
    record(puzzle, 'skipped');
    onNext();
  });
  $('next-btn').addEventListener('click', () => onNext());
  $('prev-btn').addEventListener('click', () => onPrev?.());
  $('retry-btn').addEventListener('click', () => show(puzzle.item));
  $('sol-prev').addEventListener('click', () => showSolutionStep(solIndex - 1));
  $('sol-next').addEventListener('click', () => showSolutionStep(solIndex + 1));

  document.addEventListener('keydown', e => {
    if (e.target.matches('input, select, textarea') || !puzzle || e.metaKey || e.ctrlKey) return;
    if ($('review')?.hidden) return;   // the puzzle isn't on screen (start or summary card)
    const done = puzzle.phase === 'done';
    if (analysis.active) {
      if (e.key === 'ArrowLeft') { e.preventDefault(); analysis.back(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); analysis.forward(); }
      else if (e.key === 'Escape' || e.key === 'a') { stopAnalysis(); showSolutionStep(solIndex); }
      else if (e.key === 'Enter') { e.preventDefault(); onNext(); }
      return;
    }
    if (e.key === 'a' && done) startAnalysis();
    else if (e.key === 'Enter' && done) { e.preventDefault(); onNext(); }
    else if (e.key === 'ArrowLeft' && done) { e.preventDefault(); showSolutionStep(solIndex - 1); }
    else if (e.key === 'ArrowRight' && done) { e.preventDefault(); showSolutionStep(solIndex + 1); }
    else if (e.key === 'h') giveHint();
    else if (e.key === 's') $('skip-btn').click();
  });

  return {
    show,
    get phase() { return puzzle?.phase; },
    get item() { return puzzle?.item; },
  };
}
