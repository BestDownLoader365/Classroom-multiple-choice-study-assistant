"""Course-level operations: add, publish content, enable and disable.

This is the single publish entry point for every course-content type.  Every
change follows the same check-then-publish flow: run the matching
``check_<subject>.py`` gate first, and only publish when it exits ``0``::

    # existing course: check the candidates (read-only) first
    python scripts/check_question_bank.py --course physical_design --db instance/mcq.db
    python scripts/check_glossary.py --course physical_design

    # brand-new course: the per-course gates resolve ``--course`` through the
    # catalogue, so a courses/<course_id>/ directory without course.json is
    # ignored and ``--course <new_id>`` reports "Unknown course" until the
    # course is declared.  Put the candidates in courses/<course_id>/ and create
    # the course first -- ``--add`` validates those frozen bytes itself, archives
    # them under ``versions/<sha256>/`` and writes the manifest -- then the two
    # checks above can name the new course.
    python scripts/publish_course.py --course physical_design --add \
        --title "Physical Design"

    # publish everything the course's default working copies contain
    python scripts/publish_course.py --course physical_design

    # ... or name the files explicitly (an explicit flag always wins)
    python scripts/publish_course.py --course physical_design --questions other.json

    # publish only the glossary (never affects the learner generation)
    python scripts/publish_course.py --course physical_design --glossary new_glossary.json

    # disable / re-enable a course (manifest is the source of truth)
    python scripts/publish_course.py --course physical_design --disable
    python scripts/publish_course.py --course physical_design --enable

    # prune superseded versions/ copies without publishing anything
    python scripts/publish_course.py --course physical_design --prune
    python scripts/publish_course.py --course physical_design --prune --keep-versions 3

``--questions`` defaults to ``courses/<course_id>/questions_candidate.json`` and
``--glossary`` to ``courses/<course_id>/glossary_candidate.json`` (only for a
course that declares a glossary).  When no content flag is given the defaults are
used, so a maintainer who edits only the working copies needs no path arguments;
a file that does not exist is simply not published, and a command with no content
at all still refuses to do anything.

Content publication reads the candidate once, validates exactly those frozen
bytes, archives them under ``versions/<sha256>/`` and switches the manifest over
with a single ``os.replace`` (the legacy root-file layout falls back to an atomic
single-file replace).  A course is born the same way: ``--add`` archives its
initial content instead of writing a plain ``questions.json``/``glossary.json``
next to the manifest, so the course directory never holds a copy that no
manifest path points at.  The command **re-runs the matching check script by
default** — ``check_question_bank.py`` against ``--db`` for ``--questions``,
``check_glossary.py`` offline for ``--glossary`` — and refuses to switch over
content that fails it.  ``--skip-preflight`` is the explicit, discouraged escape
hatch.  A brand-new course created with ``--add`` has no history to diff, so its
question bank is gated by schema validation only.  Nothing here restarts a
worker or bumps a generation.

Exit codes
----------

This command does not invent its own contract for a failed preflight: it runs
the matching ``check_*.py`` and **returns that script's exit code unchanged**, so
a caller sees exactly what the gate decided.  The complete set is therefore::

    ``0``   everything named on the command line was published (also a --enable /
            --disable flip or a --prune-only pass that had nothing to change)
    ``1``   refused, nothing switched over: candidate schema validation failed,
            the glossary preflight returned 1, a write failed, --add found the
            course already declared, the target course could not be resolved, or
            the retention pass could not clean up
    ``2``   usage error, **or** the question-bank preflight returned 2 (a retired
            question ID reused for a different question, or --published combined
            with an explicit path)
    ``3``   the question-bank preflight returned 3 (``--strict``) which reports an
            update that would clear stored learner state.  This command never
            passes --strict itself, so the code only ever arrives from the
            preflight it forwards; it stays in the contract because a forwarded
            code is never rewritten
    ``4``   the question-bank preflight returned 4: the request cannot be answered
            safely (unknown course, ambiguous course, or a database that has not
            been migrated)

``check_glossary.py`` only ever returns ``0``/``1``, so a glossary preflight can
only add ``1`` to the set above.  ``EXIT_CODE_SUMMARY`` below is the single
source of truth for this table; the module docstring and ``--help`` both use it.

Version retention
-----------------

``versions/<sha256>/`` is content-addressed, so publishing a change leaves the
previous copy behind.  Every publish therefore ends with one retention pass:
for **each** content type (``questions.json`` and ``glossary.json`` separately)
the directory the manifest points at is kept, plus the ``keep - 1`` most
recently written other versions — by default the current version and the one
before it, which is what makes a one-step rollback possible:

    python scripts/publish_course.py --course physical_design \\
        --glossary courses/physical_design/versions/<previous-sha256>/glossary.json

Only ``versions/<sha256>/{questions.json,glossary.json}`` is ever deleted (a
directory holding anything else is left alone), and a pruned version is a
derived copy: the working copy next to the manifest stays the source of truth.
``--keep-versions N`` changes the number kept, ``--prune`` runs the pass alone,
and ``--no-prune`` skips it for one run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

#: Every code this command can exit with.  ``2`` is deliberately shared by a
#: usage error and the question-bank preflight's blocking verdict, and a failing
#: preflight's own code is forwarded verbatim, so the gate's whole range belongs
#: to this contract.  ``tests/test_doc_contracts.py`` asserts these numbers match
#: ``check_question_bank.EXIT_*`` and that the module docstring spells them out.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE_OR_BLOCKING_PREFLIGHT = 2
EXIT_PREFLIGHT_STRICT = 3
EXIT_PREFLIGHT_UNRESOLVABLE = 4

EXIT_CODES: dict[int, str] = {
    EXIT_OK: "已发布（或状态切换 / 无需清理的 --prune）",
    EXIT_REFUSED: "被拒绝，未切换任何内容（校验失败 / 写入失败 / 无法解析课程）",
    EXIT_USAGE_OR_BLOCKING_PREFLIGHT: "用法错误，或题库预检返回 2（复用退役 ID 等阻断项）",
    EXIT_PREFLIGHT_STRICT: "题库预检返回 3（--strict 判定会清理学习状态）",
    EXIT_PREFLIGHT_UNRESOLVABLE: "题库预检返回 4（无法安全比对：未知课程 / 未迁移数据库）",
}

#: Single source of truth for the table in the module docstring; ``--help``
#: prints exactly this so the two can never drift apart again.
EXIT_CODE_SUMMARY = "\n".join(
    f"{code}: {description}" for code, description in sorted(EXIT_CODES.items())
)

from app.models import CourseDefinitionError  # noqa: E402
from scripts.course_tooling import (  # noqa: E402
    DEFAULT_KEPT_VERSIONS,
    GLOSSARY_CANDIDATE_NAME,
    QUESTIONS_CANDIDATE_NAME,
    VERSIONS_DIRECTORY,
    ToolingError,
    build_loader,
    default_candidate_path,
    freeze_candidate,
    preflight_baseline,
    prune_versions,
    publication_lock,
    publish_glossary,
    publish_questions,
    resolve_definition,
    validate_bytes,
    validate_glossary_bytes,
    write_file_atomically,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "exit codes:\n"
            f"{EXIT_CODE_SUMMARY}\n\n"
            "A failing preflight's own exit code is forwarded unchanged, so a 2/3/4 "
            "comes from check_question_bank.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--course", required=True, help="target course_id")
    parser.add_argument(
        "--questions",
        type=Path,
        default=None,
        help=(
            "candidate question bank to publish (default: "
            f"<courses-dir>/<course_id>/{QUESTIONS_CANDIDATE_NAME} when it exists)"
        ),
    )
    parser.add_argument(
        "--glossary",
        type=Path,
        default=None,
        help=(
            "candidate glossary to publish (default: "
            f"<courses-dir>/<course_id>/{GLOSSARY_CANDIDATE_NAME} when the course "
            "declares a glossary and that file exists)"
        ),
    )
    parser.add_argument("--add", action="store_true", help="create the course")
    parser.add_argument("--title", default=None)
    parser.add_argument("--title-zh", default="")
    parser.add_argument("--order", type=int, default=0)
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help=(
            "publish without running the matching check script first "
            "(not recommended): check_question_bank.py for --questions, "
            "check_glossary.py for --glossary"
        ),
    )
    parser.add_argument(
        "--keep-versions",
        type=int,
        default=DEFAULT_KEPT_VERSIONS,
        metavar="N",
        help=(
            "how many published versions of each content type to keep after "
            "this command: the one the manifest points at plus N-1 predecessors "
            f"(default: {DEFAULT_KEPT_VERSIONS} = current + previous, so one "
            "rollback stays possible; 1 keeps only the current version)"
        ),
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "only prune superseded versions/ copies and publish nothing "
            "(the retention pass runs automatically after a publish)"
        ),
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help=(
            "publish without the version-retention pass; a rollback copy then "
            "keeps accumulating (emergency use only)"
        ),
    )
    parser.add_argument(
        "--db", default=PROJECT_ROOT / "instance" / "mcq.db", type=Path
    )
    parser.add_argument(
        "--courses-dir", default=PROJECT_ROOT / "courses", type=Path
    )
    parser.add_argument(
        "--question-file", default=PROJECT_ROOT / "questions.json", type=Path
    )
    parser.add_argument(
        "--glossary-file", default=PROJECT_ROOT / "glossary.json", type=Path
    )
    return parser


def _run_glossary_check(questions_path: Path, glossary_path: Path) -> int:
    """Run ``check_glossary.py`` offline over a candidate (questions, glossary) pair.

    A non-zero exit means the glossary is invalid, so the caller refuses to
    publish: the documented flow is check first, publish only when it passes.
    """
    from scripts.check_glossary import main as check_main

    return check_main(
        ["--questions", str(questions_path), "--glossary", str(glossary_path)]
    )


def _apply_default_candidates(
    args: argparse.Namespace, course_root: Path, *, declares_glossary: bool
) -> None:
    """Fill ``--questions``/``--glossary`` from the course's default working copies.

    Only the flags the caller left unset are filled, so an explicit path is never
    overridden.  ``declares_glossary`` is ``False`` for an existing course whose
    manifest says ``glossary: null`` — a glossary candidate is then reported but
    not published, because enabling a glossary stays an explicit decision.
    """
    if args.questions is None:
        default = default_candidate_path(course_root, QUESTIONS_CANDIDATE_NAME)
        if default.is_file():
            args.questions = default
            print(f"未指定 --questions：使用默认候选 {default}")
    if args.glossary is None:
        default = default_candidate_path(course_root, GLOSSARY_CANDIDATE_NAME)
        if default.is_file():
            if declares_glossary:
                args.glossary = default
                print(f"未指定 --glossary：使用默认候选 {default}")
            else:
                print(
                    f"提示：{default} 存在，但该课程 manifest 的 glossary 为 null；"
                    "如需启用术语表请显式传 --glossary。"
                )


def _prune_versions(definition, keep: int) -> int:
    """Run one retention pass under the course's publication lock.

    ``prune_versions`` only deletes copies that no manifest points at, but it
    still takes the lock so it cannot race a publisher that is switching the
    manifest over to a version this pass is about to look at.
    """
    try:
        with publication_lock(definition.root):
            kept, removed = prune_versions(definition, keep=keep)
    except (ToolingError, OSError) as exc:
        print(f"版本清理失败：\n{exc}", file=sys.stderr)
        return 1
    root = definition.root / VERSIONS_DIRECTORY
    if not kept and not removed:
        print(f"版本清理：{root} 没有已发布副本，无需清理。")
        return 0
    print(
        f"版本清理：保留 {len(kept)} 个（每个内容类型最多 {keep} 个：当前版本 + 上一版），"
        f"删除 {len(removed)} 个。"
    )
    for path in removed:
        print(f"  - 删除 {path.relative_to(definition.root)}")
    return 0


def _prune_after_publish(args: argparse.Namespace, definition) -> None:
    """Best-effort retention after a successful publish.

    The publish is already committed at this point, so a failed cleanup must not
    turn a successful publication into a failed command: it is reported and can
    be retried with ``--prune``.
    """
    if args.no_prune:
        print("按 --no-prune 跳过版本清理：旧版本会继续累积。")
        return
    if _prune_versions(definition, args.keep_versions) != 0:
        print(
            "提示：内容已发布生效，但版本清理没有完成；可单独运行 "
            f"--course {definition.course_id} --prune 重试。",
            file=sys.stderr,
        )


def _add_course(args: argparse.Namespace) -> int:
    """Create ``courses/<id>/`` with a manifest and the supplied content.

    The initial content is archived under ``versions/<sha256>/`` exactly like
    every later publish, so a course is born in the content-addressed layout:
    the manifest is the only pointer to it, and no unreferenced copy of a
    published file is ever left next to the manifest for a maintainer to edit
    by mistake.
    """
    from app.models import validate_course_id
    from app.models.course import CourseIdError

    try:
        course_id = validate_course_id(args.course)
    except CourseIdError as exc:
        print(f"--course 不合法：{exc}", file=sys.stderr)
        return 2
    target = (args.courses_dir / course_id).resolve()
    manifest_path = target / "course.json"
    if manifest_path.exists():
        print(f"课程已存在：{manifest_path}", file=sys.stderr)
        return 1
    # A new course may introduce a glossary, so its default candidate counts.
    _apply_default_candidates(args, target, declares_glossary=True)
    if args.questions is None:
        print(
            "--add 需要 --questions：请先创建默认候选 "
            f"{default_candidate_path(target, QUESTIONS_CANDIDATE_NAME)}"
            "（或显式传 --questions <path>）",
            file=sys.stderr,
        )
        return 2
    try:
        payload, digest = freeze_candidate(args.questions.resolve())
        questions = validate_bytes(payload)
        glossary_payload: bytes | None = None
        if args.glossary is not None:
            glossary_payload, _ = freeze_candidate(args.glossary.resolve())
            validate_glossary_bytes(glossary_payload)
    except (ToolingError, OSError) as exc:
        print(f"候选内容校验失败，未创建课程：\n{exc}", file=sys.stderr)
        return 1
    if args.glossary is not None and not args.skip_preflight:
        exit_code = _run_glossary_check(
            args.questions.resolve(), args.glossary.resolve()
        )
        if exit_code != 0:
            print(
                f"\n术语表校验返回 {exit_code}：未创建课程，也没有写入任何文件。",
                file=sys.stderr,
            )
            return exit_code
    # The initial content is a publication like any other: archive the frozen
    # bytes first, then write the manifest that points at them.  The manifest
    # write is the single commit point, and it happens inside the course's
    # publication lock so two concurrent `--add` runs cannot both win.
    questions_relative = f"{VERSIONS_DIRECTORY}/{digest}/questions.json"
    glossary_relative: str | None = None
    if glossary_payload is not None:
        glossary_digest = hashlib.sha256(glossary_payload).hexdigest()
        glossary_relative = f"{VERSIONS_DIRECTORY}/{glossary_digest}/glossary.json"
    manifest = {
        "schema_version": 1,
        "course_id": course_id,
        "title": args.title or course_id,
        "title_zh": args.title_zh,
        "enabled": not args.disable,
        "questions": questions_relative,
        "glossary": glossary_relative,
        "order": args.order,
    }
    target.mkdir(parents=True, exist_ok=True)
    with publication_lock(target):
        # Re-checked inside the lock: the check above runs before validation,
        # so a concurrent `--add` could have declared the course in between.
        if manifest_path.exists():
            print(f"课程已存在：{manifest_path}", file=sys.stderr)
            return 1
        write_file_atomically(target / questions_relative, payload)
        if glossary_payload is not None and glossary_relative is not None:
            write_file_atomically(target / glossary_relative, glossary_payload)
        write_file_atomically(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )
    print(f"已新增课程 {course_id}：{len(questions)} 道题，manifest={manifest_path}")
    print(
        f"已发布内容：{questions_relative}"
        + (f"；{glossary_relative}" if glossary_relative else "")
        + f"（{VERSIONS_DIRECTORY}/ 之外的目录里没有已发布副本）"
    )
    print(
        "状态：created, pending worker activation。请在 worker 启动日志中确认 "
        f'"{course_id}" ready，然后用 /ready/{course_id} 验证。'
    )
    print(f"（内容指纹 sha256={digest[:16]}…）")
    definition = build_loader(
        args.courses_dir, args.question_file, args.glossary_file
    ).find_definition(course_id)
    if definition is not None:
        _prune_after_publish(args, definition)
    return 0


def _set_enabled(args: argparse.Namespace, definition, enabled: bool) -> int:
    """Flip ``enabled`` in the manifest, which is the source of truth."""
    from scripts.course_tooling import read_manifest

    if definition.manifest_path is None:
        print(
            "legacy 根目录布局没有 manifest，无法单独停用；请改用 "
            "scripts/migrate_courses.py --layout 迁移布局。",
            file=sys.stderr,
        )
        return 1
    manifest = read_manifest(definition)
    manifest["enabled"] = enabled
    write_file_atomically(
        definition.manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    action = "启用" if enabled else "停用"
    print(f"已{action}课程 {definition.course_id}（{definition.manifest_path}）")
    print(
        f"状态：{'enabled' if enabled else 'disabled'}, pending worker activation。"
        "学习数据不会被修改。"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.enable and args.disable:
        print("--enable 与 --disable 不能同时使用。", file=sys.stderr)
        return 2
    if args.add and (args.enable or args.disable):
        print("--add 已由 --disable 决定初始状态，不能同时指定。", file=sys.stderr)
        return 2
    if args.add and args.prune:
        print("--add 之后会自动做版本清理，不能同时指定 --prune。", file=sys.stderr)
        return 2
    if args.prune and (args.enable or args.disable):
        print("--prune 只做版本清理，不能与 --enable / --disable 同时使用。", file=sys.stderr)
        return 2
    if args.prune and args.no_prune:
        print("--prune 与 --no-prune 互相矛盾。", file=sys.stderr)
        return 2
    if args.keep_versions < 1:
        print(
            "--keep-versions 必须是 >= 1 的整数（1 = 只保留当前版本，无法回退）。",
            file=sys.stderr,
        )
        return 2
    if args.add:
        return _add_course(args)

    loader = build_loader(args.courses_dir, args.question_file, args.glossary_file)
    try:
        definition = resolve_definition(loader, args.course)
    except (ToolingError, CourseDefinitionError) as exc:
        print(f"无法解析课程：\n{exc}", file=sys.stderr)
        return 1
    print(f"课程 (course_id): {definition.course_id} [{definition.layout}]")

    if args.enable or args.disable:
        return _set_enabled(args, definition, bool(args.enable))

    if args.prune and args.questions is None and args.glossary is None:
        # Retention only: no content flag and no default-candidate fallback,
        # because cleaning up versions must never publish a working copy.
        code = _prune_versions(definition, args.keep_versions)
        print("状态：pruned（只清理 versions/，没有发布任何内容）。")
        return code

    if args.questions is None and args.glossary is None:
        _apply_default_candidates(
            args, definition.root, declares_glossary=definition.declares_glossary
        )

    if args.questions is None and args.glossary is None:
        print(
            "未指定 --questions/--glossary，也没有默认候选文件：\n"
            f"  - {default_candidate_path(definition.root, QUESTIONS_CANDIDATE_NAME)}\n"
            f"  - {default_candidate_path(definition.root, GLOSSARY_CANDIDATE_NAME)}\n"
            "没有需要发布的内容。（--add 用于新增课程，--enable/--disable 用于切换状态）",
            file=sys.stderr,
        )
        return 2

    published_any = False
    if args.questions is not None:
        try:
            payload, digest = freeze_candidate(args.questions.resolve())
            questions = validate_bytes(payload)
        except (ToolingError, OSError) as exc:
            print(f"候选题库校验失败，未替换任何文件：\n{exc}", file=sys.stderr)
            return 1
        if not args.skip_preflight:
            from scripts.check_question_bank import main as check_main

            exit_code = check_main(
                [
                    str(args.questions),
                    "--course",
                    definition.course_id,
                    "--courses-dir",
                    str(args.courses_dir),
                    "--question-file",
                    str(args.question_file),
                    "--glossary-file",
                    str(args.glossary_file),
                    "--db",
                    str(args.db),
                ]
            )
            if exit_code != 0:
                # Forwarded verbatim: see the "Exit codes" section of the module
                # docstring.  A 2/3/4 here is check_question_bank.py's verdict,
                # not this command's own usage error.
                print(
                    f"\n预检返回 {exit_code}：未发布任何内容。", file=sys.stderr
                )
                return exit_code
        baseline = preflight_baseline(definition)
        try:
            published, legacy_layout = publish_questions(
                definition, payload, digest, baseline=baseline
            )
        except (ToolingError, OSError) as exc:
            print(f"\n发布失败，未替换任何文件：\n{exc}", file=sys.stderr)
            return 1
        published_any = True
        print(f"题库已发布：{len(questions)} 道题 -> {published}")
        if legacy_layout:
            print("布局：legacy 单文件原子替换（没有 manifest）")

    if args.glossary is not None:
        try:
            payload, digest = freeze_candidate(args.glossary.resolve())
            glossary = validate_glossary_bytes(payload)
        except (ToolingError, OSError) as exc:
            print(f"候选术语表校验失败，未替换任何文件：\n{exc}", file=sys.stderr)
            return 1
        if not args.skip_preflight:
            # The corpus is the bank this run just published (or the currently
            # deployed one when only a glossary was named), never an unrelated
            # file that happens to sit in the course directory.
            corpus = (
                args.questions.resolve()
                if args.questions is not None
                else definition.questions_path
            )
            exit_code = _run_glossary_check(corpus, args.glossary.resolve())
            if exit_code != 0:
                print(
                    f"\n术语表校验返回 {exit_code}：未发布任何内容。",
                    file=sys.stderr,
                )
                return exit_code
        try:
            published = publish_glossary(definition, payload, digest)
        except (ToolingError, OSError) as exc:
            print(f"\n发布失败：\n{exc}", file=sys.stderr)
            return 1
        published_any = True
        print(f"术语表已发布：{len(glossary.terms)} 条 -> {published}")
        print("术语表不参与题库 generation：学习数据与 worker 围栏都不受影响。")

    if not published_any:  # pragma: no cover - guarded above
        return 2
    _prune_after_publish(args, definition)
    print(
        "\n状态：published, pending worker activation（已发布，等待 worker 激活）。"
        "\n文件系统发布与数据库激活不是同一个事务：请统一重启全部应用工作进程。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

