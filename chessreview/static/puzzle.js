// The "learn from your mistake" puzzle, shared by Practice, Train and the new-games session.
//
// Flow per mistake:  solving -> (checking) -> wrong: snap back, explain, keep solving
//                                          -> right: play the opponent's reply and continue,
//                                                    until playout_moves are found -> done
// "View solution" ends as failed; "Skip" records a skip and asks the host for the next one.
//
// Real-game mode (item.mode === 'blind', "Threat check first" on the pages): nothing says a mistake
// was made (no badge, no game-move arrow) and in Today's real-game block some items are calm
// positions where I played well. If the position has threat data, a threat step comes first: play
// the opponent's threat (as if I passed) or press "No real threat".
//
// Library mode (item.mode === 'library'): a Lichess puzzle. The opponent's first move is played
// on the board, moves are checked locally against the solution (any mate also counts), and the
// result goes to /api/library/result, which updates my puzzle rating.
//
// The host page includes templates/_puzzle.html and calls createPuzzle({...}).show(item).
// Callbacks: onResult(item, result) once per puzzle, onNext(), onPrev() (optional).
import { Chess } from 'chess.js';
import { createAnalysis } from './analysis.js';
import { choosePromotion, createBadge, dismissPromotion, isPromotion, makeBoard, moveSound, sound } from './board.js';
import { api, arrow, clickable, lastMove, legalDests, sideToMove, SYMBOLS } from './common.js';
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
const other = c => (c === 'white' ? 'black' : 'white');
// What to call my game move: a "miss" (report.py mark: an error right after the opponent's own
// mistake, when it had left me clearly better) is told apart from a plain blunder or mistake.
const verdict = m => (m.mark === 'miss' ? 'miss' : m.judgement);
const MISS_NOTE = ' Your opponent had just gone wrong, and this move let the chance go.';
// The same position with the opponent to move: "what if I passed?" (only used when I'm not in check).
function passedFen(fen) {
  const parts = fen.split(' ');
  parts[1] = parts[1] === 'w' ? 'b' : 'w';
  parts[3] = '-';
  return parts.join(' ');
}

export function createPuzzle({ onResult = () => {}, onNext = () => {}, onPrev = null } = {}) {
  const $ = id => document.getElementById(id);
  // Must not start viewOnly: chessground only binds its input listeners when the board is created.
  const board = makeBoard($('board'), {
    viewOnly: false, animation: { enabled: true, duration: 200 }, movable: { color: undefined },
    premovable: { enabled: true, showDests: true },   // move ahead while the reply is being played
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
    dismissPromotion($('board'));     // a picker left open belongs to the previous puzzle
    const blind = m.mode === 'blind';
    const lib = m.mode === 'library';
    puzzle = {
      item: m, fen: m.fen, last: lastMove(m.prev_move_uci), step: 0,
      attempts: 0, hints: 0, hintLevel: 0, phase: blind && m.threat ? 'threat' : 'solving', recorded: false,
      leaving: false,   // Next was pressed: ignore further Next/Skip until the host shows the next one
      shownAt: Date.now(),
      onLine: true,     // all moves so far followed the engine's best line
      blind, lib, threatResult: null, threatShapes: [],
    };
    $('badge').hidden = blind || lib;
    $('badge').textContent = verdict(m);
    $('badge').className = `badge ${verdict(m)}`;
    $('move-title').textContent = lib ? `Puzzle · rated ${m.rating}`
      : blind ? `Move ${Math.floor(m.ply / 2) + 1}` : `${m.move_label}${SYMBOLS[m.judgement]}`;
    $('status-chip').hidden = blind || lib || !m.status;
    $('status-chip').textContent = STATUS_CHIPS[m.status] || '';
    $('status-chip').className = `chip ${m.status || ''}`;
    const result = { win: 'won', loss: 'lost', draw: 'drawn' }[m.my_result];
    $('context').textContent = lib ? 'From the Lichess puzzle database'
      : blind ? `vs ${m.opponent} · ${m.date} · ${m.opening}`
        : `vs ${m.opponent} · ${m.date} · ${m.opening} · ${m.phase} · game ${result}`;
    $('game-link').href = lib ? m.game_url : `/game/${m.game_id}#${m.ply}`;
    $('game-link').textContent = lib ? 'Lichess game' : 'Open game';
    $('game-link').target = lib ? '_blank' : '';

    if (puzzle.phase === 'threat') {
      rich($('prompt'), 'First: what is ', ['strong', colorName(other(m.my_color))],
           ' threatening? Play their most dangerous move, or press “No real threat”.');
    } else {
      solvingPrompt(m);
    }
    $('feedback').hidden = true;
    resetHint();
    $('threat-actions').hidden = puzzle.phase !== 'threat';
    $('solve-actions').hidden = puzzle.phase === 'threat';
    $('done-actions').hidden = true;
    $('answer').hidden = true;

    stopAnalysis();
    badge.clear();
    board.cancelPremove();
    board.setAutoShapes([]);
    board.setShapes([]);          // arrows I drew on the last position (right-click) go too
    board.set({ orientation: m.my_color });
    evalBar.set(m.eval_before_white ?? null, m.my_color);
    evalBar.hide(true);  // no peeking while solving
    if (puzzle.phase === 'threat') setThreatBoard();
    else if (lib) {
      // Show the opponent's move being played, then hand over the board.
      const p = puzzle;
      board.set({ fen: m.start_fen, lastMove: undefined, check: false, movable: { color: m.my_color, dests: new Map() } });
      setTimeout(() => { if (puzzle === p) { sound.move(); setSolvingBoard(); } }, 450);
    } else setSolvingBoard();
  }

  function solvingPrompt(m) {
    const goal = m.playout_moves > 1 ? ` It takes ${m.playout_moves} good moves in a row.` : '';
    if (m.mode === 'library') {
      rich($('prompt'), 'Find the best move for ', ['strong', colorName(m.my_color)], '.');
    } else if (m.mode === 'blind') {
      rich($('prompt'), 'Your move as ', ['strong', colorName(m.my_color)], '. Play what you would play in a game.');
    } else {
      rich($('prompt'), 'You played ', ['strong', `${m.move_san}${SYMBOLS[m.judgement]}`],
           ` here (red arrow). Find a better move for ${colorName(m.my_color)}.${goal}`);
    }
  }

  // ------------------------------------------------------------ threat step (real-game mode)

  function setThreatBoard() {
    const p = puzzle;
    const opp = other(p.item.my_color);
    const fen = passedFen(p.fen);
    badge.clear();
    board.set({
      fen, turnColor: opp, lastMove: p.last, check: false,
      movable: { free: false, color: opp, dests: legalDests(new Chess(fen)), showDests: true,
                 events: { after: (orig, dest) => onThreatMove(orig, dest) } },
    });
    board.setAutoShapes([]);
  }

  async function onThreatMove(orig, dest) {
    const p = puzzle;
    if (p.phase !== 'threat') return;
    const opp = other(p.item.my_color);
    const piece = new Chess(passedFen(p.fen)).get(orig);
    p.phase = 'threat-checking';
    lockBoard();
    const promotion = isPromotion(piece, dest) ? await choosePromotion($('board'), opp, dest, p.item.my_color) : '';
    if (puzzle !== p) return;
    checkThreat(p, orig + dest + promotion);
  }

  async function checkThreat(p, uci) {
    if (p.phase !== 'threat' && p.phase !== 'threat-checking') return;
    p.phase = 'threat-checking';
    lockBoard();
    showFeedback('', 'Checking…');
    let r;
    try {
      r = await api('/api/threat/check', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ game_id: p.item.game_id, ply: p.item.ply, uci }),
      });
    } catch (err) {
      if (puzzle !== p) return;
      p.threatResult = 'skipped';
      showFeedback('bad', `Couldn't check that (${err.message}).`);
      return startSolving(p, 0);
    }
    if (puzzle !== p) return;
    const t = r.threat;
    const opp = colorName(other(p.item.my_color));
    const cost = t.real ? ` (it would cost you ${Math.round(t.gain * 50)}% winning chances)` : '';
    p.threatResult = r.correct ? 'correct' : 'wrong';
    if (r.correct) sound.correct(); else sound.wrong();
    if (t.real) p.threatShapes = [arrow(t.uci, 'yellow')].filter(Boolean);
    if (r.correct && t.real) {
      showFeedback('ok', `✓ Right: ${r.san} is a real threat${cost}.`,
                   r.san !== t.san ? `${t.san} is the strongest one.` : '', ' Now find your move.');
    } else if (r.correct) {
      showFeedback('ok', '✓ Right: nothing serious is threatened.', 'Now find a good move.');
    } else if (t.real) {
      showFeedback('bad', uci ? `✗ ${r.san} isn't the danger.` : '✗ There is a threat.',
                   `${opp} threatens ${t.san}${cost} (yellow arrow). Now find your move.`);
    } else {
      showFeedback('bad', `✗ ${r.san} doesn't threaten much.`, 'Nothing serious is threatened here. Now find a good move.');
    }
    startSolving(p, r.correct ? 350 : 900);
  }

  function startSolving(p, delay) {
    setTimeout(() => {
      if (puzzle !== p) return;
      p.phase = 'solving';
      solvingPrompt(p.item);
      $('threat-actions').hidden = true;
      $('solve-actions').hidden = false;
      setSolvingBoard();
    }, delay);
  }

  function setSolvingBoard() {
    const p = puzzle;
    badge.clear();
    const chess = new Chess(p.fen);
    board.set({
      fen: p.fen, turnColor: p.item.my_color, lastMove: p.last, check: checkOf(p.fen),
      movable: { free: false, color: p.item.my_color, dests: legalDests(chess), showDests: true,
                 events: { after: (orig, dest) => onMove(orig, dest) } },  // ignore chessground's 3rd (metadata) arg
    });
    board.setAutoShapes(hintShapes());
    board.playPremove();   // a move queued during the opponent's reply is played now
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
    if (puzzle !== p) return;     // skipped or moved on while the promotion picker was open
    const uci = orig + dest + promotion;

    const m = p.item;
    if (p.lib) return judgeLocally(p, uci);
    if (p.step === 0 && uci === m.move_uci && m.judgement !== 'good') {
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
      if (puzzle !== p) return;
      showFeedback('bad', `Couldn't check that move (${err.message}).`);
      p.phase = 'solving';
      return setSolvingBoard();
    }
    if (puzzle !== p) return;  // moved on while Stockfish was thinking
    if (r.judgement === 'good') right(p, r, uci);
    else wrong(p, r);
  }

  // Library puzzles: the solution is known, so no engine is needed. Any checkmate also counts.
  function judgeLocally(p, uci) {
    const m = p.item;
    const chess = new Chess(p.fen);
    const mv = chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] });
    if (uci !== m.solution[2 * p.step] && !chess.isCheckmate()) {
      return wrong(p, { uci, san: mv.san });
    }
    const reply = m.solution[2 * p.step + 1];
    let replySan = null;
    const fenAfter = chess.fen();
    if (reply) replySan = chess.move({ from: reply.slice(0, 2), to: reply.slice(2, 4), promotion: reply[4] }).san;
    right(p, { uci, san: mv.san, judgement: 'good', is_best: true, fen_after: fenAfter, reply_uci: reply || null,
               reply_san: replySan, fen_after_reply: chess.fen(), game_over: !reply || chess.isGameOver() }, uci);
  }

  function wrong(p, r) {
    p.attempts += 1;
    sound.wrong();
    badge.show(r.uci?.slice(2, 4) || null, r.judgement || 'mistake');
    if (p.lib) showFeedback('bad', `✗ ${r.san} isn't it.`, 'Try again.');
    else showFeedback('bad', `✗ ${r.san} is ${article(r.judgement)} ${r.judgement} (${r.after}).`,
                      r.explanation || '', ' Try again.');
    setTimeout(() => {
      if (puzzle !== p) return;
      p.phase = 'solving';
      setSolvingBoard();  // the piece snaps back
    }, 500);
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
    // Play the opponent's best reply, then hand the board back. Meanwhile a move can be premoved.
    p.phase = 'opponent';
    board.set({ movable: { free: false, color: m.my_color, dests: new Map(), events: { after: (o, d) => onMove(o, d) } } });
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
    }, 300);
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
    if (!p) return [];
    // Show where I went in the real game (only in the puzzle's starting position, never in
    // real-game mode), and the threat once the threat step has revealed it.
    const shapes = [...(p.step === 0 ? p.threatShapes : []),
                    ...(p.step === 0 && !p.blind ? [arrow(p.item.move_uci, 'paleRed')].filter(Boolean) : [])];
    if (p.hintLevel < 2) return shapes;
    const best = hintMove();
    return best ? [...shapes, { orig: best.slice(0, 2), brush: 'green' }] : shapes;
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
    const calm = m.judgement === 'good';
    const messages = {
      solved: ['ok', '✓ Solved!', ' Well found.'],
      solved_with_help: ['ok', '✓ Solved', ` with ${p.hints ? 'hints' : 'some retries'}.`],
      failed: ['bad', 'Solution', calm ? `: ${m.best_san} is a good move here.` : `: ${m.best_san} was the move.`],
    };
    const [cls, head, tail] = messages[result];
    rich($('prompt'), [`strong.${cls}`, head], tail);
    // The lean answer panel: the result line and the buttons, then one block for what happened in
    // the game, one for why the best move is better, and the best line. The last feedback ("✓ Best
    // move!") and the status chip ("Solved before") would only repeat the result.
    $('feedback').hidden = true;
    $('status-chip').hidden = true;
    $('hint').hidden = true;
    $('solve-actions').hidden = true;
    $('done-actions').hidden = false;

    if (p.blind) {    // threat check mode: now say which move it was
      $('badge').hidden = false;
      $('move-title').textContent = `${m.move_label}${SYMBOLS[m.judgement] || ''}`;
    }
    const ex = $('explanation');
    ex.className = `explanation ${calm || p.lib ? 'neutral' : m.judgement}`;
    // The evals, unless the explanation already quotes the drop ("the evaluation drops from …").
    const evals = m.eval_before && !m.explanation?.includes(m.eval_after) ? ` (${m.eval_before} → ${m.eval_after})` : '';
    const missNote = verdict(m) === 'miss' ? MISS_NOTE : '';
    if (p.lib) libraryNote(m, 'Rating: updating…');
    else if (calm) {
      rich(ex, 'In the game you played ', ['strong', m.move_san], ' here: a good move.',
           m.threat?.real ? ' You dealt with the threat then too.' : '');
    } else if (p.blind) {
      rich(ex, ['strong', `In the game you played ${m.move_san}${SYMBOLS[m.judgement]}, ${article(verdict(m))} ` +
                          `${verdict(m)}${evals}. `], m.explanation || '', missNote);
    } else {
      rich(ex, ['strong', `Why ${m.move_san} was ${article(verdict(m))} ${verdict(m)}${evals}: `], m.explanation || '', missNote);
    }
    $('best-explanation').hidden = !m.best_explanation || p.lib;
    rich($('best-explanation'), ['strong', `Why ${m.best_san} is better: `], m.best_explanation || '');
    $('arrow-legend').innerHTML = p.lib ? '' : calm ? '· <span class="swatch green"></span> a good move'
      : '· <span class="swatch red"></span> your game move <span class="swatch green"></span> best';
    setTimeout(() => {
      if (puzzle !== p) return;
      $('answer').hidden = false;
      buildSolution(m);
      showSolutionStep(0);
    }, delay);
  }

  function libraryNote(m, rating, ...rest) {
    rich($('explanation'), 'Themes: ', ['strong', m.themes.join(', ') || '–'], `. ${rating}`, ...rest);
  }

  // Next, Skip and Enter move on once per puzzle: the second click of a double click (or a held
  // Enter) used to skip the following puzzle unseen and unrecorded (B12).
  const SETTLE_MS = 400;
  const tooSoon = e => !puzzle || puzzle.leaving || e?.detail > 1 || e?.repeat || Date.now() - puzzle.shownAt < SETTLE_MS;
  function advance(e) {
    if (tooSoon(e)) return;
    puzzle.leaving = true;
    onNext();
  }

  function record(p, result) {
    if (p.recorded) return;
    p.recorded = true;
    p.item.status = result;
    onResult(p.item, result);
    if (p.lib) {
      api('/api/library/result', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ puzzle_id: p.item.puzzle_id, result }),
      }).then(r => {
        if (puzzle !== p) return;
        const diff = r.rating_after - r.rating_before;
        libraryNote(p.item, 'Your puzzle rating: ', ['strong', `${r.rating_after}`],
                    r.rated ? ` (${diff >= 0 ? '+' : ''}${diff}).` : ' (only the first try counts).');
      }).catch(() => { /* not critical */ });
      return;
    }
    api('/api/review/result', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ game_id: p.item.game_id, ply: p.item.ply, result,
                             attempts: p.attempts, hints_used: p.hints, mode: p.blind ? 'blind' : 'labelled',
                             calm: p.item.judgement === 'good', threat_result: p.threatResult }),
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
      return clickable(chip);
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
    evalBar.hide(Boolean(puzzle.lib));   // library puzzles have no stored eval
    evalBar.set(solIndex === 0 ? m.eval_before_white ?? null : null, m.my_color);  // only the game position has a stored eval
    const gameMove = m.judgement === 'good' ? null : arrow(m.move_uci, 'red');   // calm: the game move was fine
    board.setAutoShapes(solIndex === 0 ? [gameMove, arrow(m.best_uci, 'green')].filter(Boolean) : []);
    document.querySelectorAll('#solution-moves .sm').forEach(c =>
      c.classList.toggle('current', +c.dataset.index === solIndex));
  }

  // ------------------------------------------------------------ analysis board

  function startAnalysis() {
    if (puzzle?.phase !== 'done') return;
    // From the puzzle position, with the solution moves up to the shown step as history.
    analysis.start(solution[0].fen, { last: solution[0].last,
                                      history: puzzle.item.best_pv_uci.slice(0, solIndex) });
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
  $('no-threat-btn').addEventListener('click', () => { if (puzzle?.phase === 'threat') checkThreat(puzzle, null); });
  $('threat-skip-btn').addEventListener('click', () => {
    if (puzzle?.phase !== 'threat') return;
    puzzle.threatResult = 'skipped';
    $('feedback').hidden = true;
    startSolving(puzzle, 0);
  });
  $('skip-btn').addEventListener('click', e => {
    if (tooSoon(e) || puzzle.phase === 'done') return;
    record(puzzle, 'skipped');
    advance(e);
  });
  $('next-btn').addEventListener('click', advance);
  $('prev-btn').addEventListener('click', e => { if (!tooSoon(e)) onPrev?.(); });
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
      else if (e.key === 'Enter') { e.preventDefault(); advance(e); }
      return;
    }
    if (e.key === 'a' && done) startAnalysis();
    else if (e.key === 'Enter' && done) { e.preventDefault(); advance(e); }
    else if (e.key === 'ArrowLeft' && done) { e.preventDefault(); showSolutionStep(solIndex - 1); }
    else if (e.key === 'ArrowRight' && done) { e.preventDefault(); showSolutionStep(solIndex + 1); }
    else if (e.key === 'n' && puzzle.phase === 'threat') checkThreat(puzzle, null);
    else if (e.key === 'h') giveHint();
    else if (e.key === 's' && !e.repeat) $('skip-btn').click();
  });

  return {
    show,
    get phase() { return puzzle?.phase; },
    get item() { return puzzle?.item; },
  };
}
