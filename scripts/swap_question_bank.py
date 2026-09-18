"""Atomically publish a validated question bank.

Non-atomic publishing (``cp`` over a live file, an editor saving the JSON in
place) can truncate or partially overwrite the file while a worker is
starting up or reloading.  The worker then reports ``Invalid JSON in question
bank at line 1, column N`` — a publishing artefact mistaken for a broken
bank.  This helper only validates the candidate, writes it to a temporary
file next to the target, fsyncs it, and atomically replaces the target with
``os.replace`` (same filesystem, so the swap is a single rename)::

    python scripts/check_question_bank.py candidate.json     # preflight first
    python scripts/swap_question_bank.py candidate.json
    python scripts/swap_question_bank.py candidate.json --target questions.json

It never restarts anything: after a structural change (added/deleted
questions, grading rule changes, chapter/source moves) restart the whole
service so every worker loads the same bank.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.repositories import QuestionBankError, QuestionLoader  # noqa: E402


def _fsync_directory(directory: Path) -> None:
    """Persist the rename itself, where the filesystem supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "candidate",
        type=Path,
        help="fully written candidate question bank to publish",
    )
    parser.add_argument(
        "--target",
        default=PROJECT_ROOT / "questions.json",
        type=Path,
        help="live question bank to replace (default: project root questions.json)",
    )
    args = parser.parse_args(argv)

    candidate = args.candidate.resolve()
    target = args.target.resolve()
    if not candidate.is_file():
        print(f"候选题库不存在：{candidate}", file=sys.stderr)
        return 1
    if candidate == target:
        print("候选文件就是目标文件，无需替换。")
        return 0
    try:
        questions = QuestionLoader(candidate).load()
    except QuestionBankError as exc:
        print(f"候选题库校验失败，未替换任何文件：\n{exc}", file=sys.stderr)
        return 1

    payload = candidate.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    print(f"已校验并通过原子替换发布 {len(questions)} 道题 -> {target}")
    print("下一步：统一重启全部应用工作进程，让所有 worker 加载同一份题库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
