#!/usr/bin/env python3
"""
pgn_to_bulletformat.py

Walks a folder of .pgn files, plays out each game, and writes NNUE training
data in bulletformat's plain-text convention:

    <FEN> | <score> | <result>

For every ply:
  - the FEN is the position BEFORE the move is played (i.e. the position the
    engine was evaluating when it produced the score in that move's comment)
  - the score is read out of the move comment, e.g. "{+0.68/15 0.441s, ...}"
  - the result is the game outcome (1.0 / 0.5 / 0.0)

Positions are skipped (not written) when the move played from them is a
capture or a check, since those SAN strings contain 'x', '+', or '#' and
their static evals are noisy/tactical rather than quiet positions suitable
for NNUE training.

Usage:
    python pgn_to_bulletformat.py <input_folder> [-o output.txt]
                                   [--perspective {white,stm}]
                                   [-j WORKERS] [--recursive]

Requires: python-chess  (pip install python-chess)
"""

import argparse
import concurrent.futures as cf
import io
import os
import re
import sys

import chess
import chess.pgn

# Matches the score token at the start of a move comment, e.g.
#   "+0.68/15 0.441s, n=188982, sd=21"  -> "+0.68"
#   "-M4/12 ..."                        -> mate score
_SCORE_RE = re.compile(r"^\s*([+-]?)(M?)(\d+(?:\.\d+)?)\s*/")

MATE_SCORE_CP = 30000  # sentinel centipawn value used for mate scores

_RESULT_MAP = {
    "1-0": 1.0,
    "0-1": 0.0,
    "1/2-1/2": 0.5,
}


def parse_score_cp(comment: str):
    """Extract a centipawn score (float, in pawns*100) from a move comment.

    Returns None if no score could be parsed.
    """
    if not comment:
        return None
    m = _SCORE_RE.match(comment)
    if not m:
        return None
    sign, is_mate, value = m.groups()
    sign_mult = -1.0 if sign == "-" else 1.0
    if is_mate == "M":
        return sign_mult * MATE_SCORE_CP
    try:
        pawns = float(value)
    except ValueError:
        return None
    return sign_mult * pawns * 100.0


def is_tactical_san(san: str) -> bool:
    """True if the SAN move string is a capture or gives check/mate."""
    return ("x" in san) or ("+" in san) or ("#" in san)


def process_game(game: "chess.pgn.Game", perspective: str) -> list:
    """Play out one parsed game, return a list of bulletformat lines."""
    lines = []

    result_str = game.headers.get("Result", "*")
    white_result = _RESULT_MAP.get(result_str)
    if white_result is None:
        # Unfinished / unknown result games are not usable for training.
        return lines

    try:
        board = game.board()
    except ValueError:
        # Malformed SetUp/FEN header.
        return lines

    node = game
    while node.variations:
        next_node = node.variations[0]
        move = next_node.move

        try:
            san = board.san(move)
        except (ValueError, AssertionError):
            break

        if not is_tactical_san(san):
            score_cp = parse_score_cp(next_node.comment)
            if score_cp is not None:
                mover_is_white = board.turn == chess.WHITE
                if perspective == "white":
                    score_out = score_cp if mover_is_white else -score_cp
                    result_out = white_result
                else:  # stm: score/result relative to side to move (mover)
                    score_out = score_cp
                    result_out = white_result if mover_is_white else 1.0 - white_result

                fen_before = board.fen()
                # bulletformat scores/results are conventionally written as
                # integers / one-decimal floats.
                lines.append(f"{fen_before} | {int(round(score_out))} | {result_out}")

        try:
            board.push(move)
        except (AssertionError, ValueError):
            break
        node = next_node

    return lines


def process_file(path: str, perspective: str) -> list:
    """Parse every game in one PGN file, return all bulletformat lines."""
    out_lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    stream = io.StringIO(text)
    while True:
        try:
            game = chess.pgn.read_game(stream)
        except (ValueError, UnicodeDecodeError):
            break
        if game is None:
            break
        try:
            out_lines.extend(process_game(game, perspective))
        except Exception as e:  # keep going even if one game is malformed
            print(f"  [warn] skipped a game in {path}: {e}", file=sys.stderr)

    return out_lines


def find_pgn_files(folder: str, recursive: bool):
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                if fn.lower().endswith(".pgn"):
                    yield os.path.join(root, fn)
    else:
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(".pgn"):
                yield os.path.join(folder, fn)


def main():
    ap = argparse.ArgumentParser(description="Convert PGN files to bulletformat text data for NNUE training.")
    ap.add_argument("input_folder", help="Folder containing .pgn files")
    ap.add_argument("-o", "--output", default="data.bulletformat", help="Output text file (default: data.bulletformat)")
    ap.add_argument("--perspective", choices=["white", "stm"], default="white",
                     help="Score/result perspective: 'white' (default, white-relative) or "
                          "'stm' (relative to the side to move / mover in each position)")
    ap.add_argument("-j", "--workers", type=int, default=os.cpu_count() or 1,
                     help="Parallel worker processes (default: all CPUs)")
    ap.add_argument("--recursive", action="store_true", help="Recurse into subfolders")
    args = ap.parse_args()

    if not os.path.isdir(args.input_folder):
        print(f"Error: {args.input_folder} is not a directory", file=sys.stderr)
        sys.exit(1)

    files = list(find_pgn_files(args.input_folder, args.recursive))
    if not files:
        print(f"No .pgn files found in {args.input_folder}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(files)} PGN file(s). Parsing with {args.workers} worker(s)...")

    total_positions = 0
    with open(args.output, "w", encoding="utf-8") as out_f:
        if args.workers <= 1 or len(files) == 1:
            for path in files:
                lines = process_file(path, args.perspective)
                for line in lines:
                    out_f.write(line + "\n")
                total_positions += len(lines)
                print(f"  {path}: {len(lines)} positions")
        else:
            with cf.ProcessPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(process_file, path, args.perspective): path for path in files}
                for fut in cf.as_completed(futures):
                    path = futures[fut]
                    try:
                        lines = fut.result()
                    except Exception as e:
                        print(f"  [error] {path}: {e}", file=sys.stderr)
                        continue
                    for line in lines:
                        out_f.write(line + "\n")
                    total_positions += len(lines)
                    print(f"  {path}: {len(lines)} positions")

    print(f"Done. Wrote {total_positions} positions to {args.output}")


if __name__ == "__main__":
    main()