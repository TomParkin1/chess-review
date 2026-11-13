"""A tiny, deterministic UCI engine for tests on machines without Stockfish.

It looks one move ahead: each legal move is scored by the material it leaves (a mate scores as mate in 1),
and the line shown is that move plus the opponent's best capture in reply. That is enough for the web app's
plumbing (judging, analysis, playouts, sparring) to run; tests that need real strength use the
`real_stockfish` fixture instead.

Run as a subprocess speaking UCI on stdin/stdout: python fake_uci.py
"""

import sys

import chess

VALUES = {chess.PAWN: 100, chess.KNIGHT: 300, chess.BISHOP: 300, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}
OPTIONS = [
    "option name Threads type spin default 1 min 1 max 1024",
    "option name Hash type spin default 16 min 1 max 33554432",
    "option name MultiPV type spin default 1 min 1 max 500",
    "option name UCI_LimitStrength type check default false",
    "option name UCI_Elo type spin default 1320 min 1320 max 3190",
    "option name Skill Level type spin default 20 min 0 max 20",
]


def material(board: chess.Board, color: chess.Color) -> int:
    """Material of `color` minus the opponent's, in centipawns."""
    return sum(VALUES[p.piece_type] * (1 if p.color == color else -1) for p in board.piece_map().values())


def best_capture(board: chess.Board) -> chess.Move | None:
    """The side to move's most valuable capture, else its first legal move (None if there is none)."""
    moves = list(board.legal_moves)
    captures = [m for m in moves if board.is_capture(m)]
    if captures:
        return max(captures, key=lambda m: (VALUES.get(board.piece_type_at(m.to_square) or chess.PAWN, 0), m.uci()))
    return moves[0] if moves else None


def lines(board: chess.Board, count: int) -> list[str]:
    """`info` lines for the `count` best moves by one-move lookahead, best first."""
    me = board.turn
    scored = []
    for move in sorted(board.legal_moves, key=lambda m: m.uci()):
        board.push(move)
        if board.is_checkmate():
            score, pv = "mate 1", [move]
        else:
            reply = best_capture(board)
            if reply is not None:
                board.push(reply)
                cp = material(board, me)
                board.pop()
            else:
                cp = 0  # stalemate
            score, pv = f"cp {cp}", [move] + ([reply] if reply else [])
        board.pop()
        key = 100_000 if score.startswith("mate") else int(score.split()[1])
        scored.append((key, score, pv))
    scored.sort(key=lambda s: -s[0])
    return [
        f"info depth 1 seldepth 2 multipv {i} score {score} nodes 1 pv {' '.join(m.uci() for m in pv)}"
        for i, (_, score, pv) in enumerate(scored[:count], start=1)
    ]


def main() -> None:
    board = chess.Board()
    multipv = 1
    searching = False  # `go infinite`: the bestmove waits for `stop`
    best = "(none)"
    for raw in sys.stdin:
        words = raw.split()
        if not words:
            continue
        command = words[0]
        if command == "uci":
            print("id name FakeFish\nid author chess-review tests")
            print("\n".join(OPTIONS))
            print("uciok")
        elif command == "isready":
            print("readyok")
        elif command == "setoption" and len(words) >= 5 and words[2] == "MultiPV":
            multipv = int(words[4])
        elif command == "position":
            if words[1] == "startpos":
                board, rest = chess.Board(), words[2:]
            else:
                end = words.index("moves") if "moves" in words else len(words)
                board, rest = chess.Board(" ".join(words[2:end])), words[end:]
            for uci in rest[1:]:
                board.push_uci(uci)
        elif command == "go":
            if board.is_checkmate():
                print("info depth 0 score mate 0")
                best = "(none)"
            elif board.is_game_over():
                print("info depth 0 score cp 0")
                best = "(none)"
            else:
                info = lines(board, multipv)
                print("\n".join(info))
                best = info[0].split(" pv ")[1].split()[0]
            searching = "infinite" in words
            if not searching:
                print(f"bestmove {best}")
        elif command == "stop" and searching:
            searching = False
            print(f"bestmove {best}")
        elif command == "quit":
            return
        sys.stdout.flush()


if __name__ == "__main__":
    main()
