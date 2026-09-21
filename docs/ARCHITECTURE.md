# Technical Architecture

## 1. Purpose and Scope

This project is a local, **multi-course** multiple-choice learning application built with Flask. Each course is declared by `courses/<course_id>/course.json` and loads its own English question bank and optional domain glossary from JSON. Question, chapter, source and glossary-term IDs are local to a course, so two courses may both own `q001` with completely different content; a learner belongs to the platform (`user_id`), while every piece of learning state belongs to `(learner, course)`. The application provides optional Chinese learning aids, grades single-choice and multiple-choice answers, stores personal learning history, gives normal practice a random coverage guarantee, and reviews mistakes through one-answer correction, same-chapter transfer verification, and fixed-interval spaced repetition (SRS) for corrected questions. On top of that loop, a dashboard aggregates each learner's recent statistics and chapter mastery, and a mock-exam mode draws a fixed, optionally timed question set whose mistakes flow back into the same correction system.

Operational details, URLs, per-course publication and readiness live in [COURSE_GUIDE.md](COURSE_GUIDE.md); the namespace migration, its validation and its rollback live in [MULTI_COURSE_MIGRATION.md](MULTI_COURSE_MIGRATION.md).

The application deliberately uses a small deployment model:

- the development server runs one Flask process on loopback;
- production runs two threaded Gunicorn workers behind one Nginx reverse proxy;
- each worker loads every enabled course's `course.json` + content into immutable in-memory repositories at startup, reconciles it once, and then holds a fixed generation per course for its whole lifetime;
- all workers share one local SQLite database through short-lived connections;
- server-rendered HTML uses a small amount of CSS and JavaScript;
- username/password accounts isolate each learner's records.

The project has four different kinds of state:

| State | Storage | Examples |
|---|---|---|
| Question-bank state (per course) | the file each course manifest points at (a `versions/<sha256>/questions.json` snapshot, a plain-file `questions.json`, or the legacy root adapter), then immutable in-memory objects | question text, options, correct answers, explanations, Chinese translations |
| Glossary content (per course, optional) | the file that course's manifest points at (or `null`), then immutable in-memory objects | canonical terms, aliases, Chinese translations (`term_zh`), Chinese definitions (`definition_zh`), dynamic categories |
| Persistent learner state | `instance/mcq.db`, namespaced by `(learner_id, course_id)` | users, attempts, question correction state and SRS schedule, weak-chapter verification, normal/review progress |
| Temporary browser state | Flask's signed session cookie | signed-in user ID, flash messages, the last course (navigation preference only) |

Question content is not copied into SQLite. Persistent records refer to questions by their stable question IDs.

## 2. Technology Stack

- Python 3 (the deployed WSL2 environment currently uses Python 3.10)
- Flask 3.x for routing, sessions, templates, and HTTP handling
- Gunicorn 23.x as the production WSGI server
- Nginx as the production HTTP listener and reverse proxy
- systemd for Linux service supervision inside WSL2 Ubuntu
- Jinja for server-rendered HTML
- SQLite through Python's standard `sqlite3` module
- Werkzeug password hashing through Flask's dependency stack
- Vanilla JavaScript for form behavior and bilingual display
- Plain CSS for responsive styling
- Pytest for unit and integration tests

Python dependencies are managed only through `requirements.txt`: Flask `>=3.1,<4.0`, Gunicorn `>=23.0,<24.0`, and Pytest `>=8.3,<9.0`. No JavaScript build step, Redis instance, background worker, WebSocket server, or external database service is required.

## 3. High-Level Component Flow

```mermaid
flowchart TD
    Browser[Browser] --> Nginx[Nginx loopback :8080 in production]
    Nginx --> Gunicorn[Gunicorn 127.0.0.1:8001]
    Gunicorn --> FlaskApp[Flask create_app]
    DevBrowser[Development browser] -. Werkzeug dev server 127.0.0.1:5000 .-> FlaskApp
    FlaskApp --> Health[GET /health liveness]
    FlaskApp --> Ready["GET /ready and /ready/<course_id>"]
    FlaskApp --> Routes["app/routes/web.py (course_id always in the URL)"]
    Routes --> Registry[CourseRegistry: read-only CourseState lookup]
    Routes --> Guard["guarded learner transaction (course_consistency)"]

    subgraph Deployment["Deployment-wide repositories"]
        UserRepo[UserRepository]
        RateLimitRepo[RateLimitRepository]
        CourseRepo[CourseRepository: courses + schema_meta]
        CrossCourse["CrossCourseQueries (explicit cross-course reads)"]
    end

    subgraph PerCourse["One graph per enabled course: CourseServices"]
        QRepo["QuestionRepository (in-memory, read-only)"]
        GRepo["GlossaryRepository or None (in-memory)"]
        ProgressRepo[ProgressRepository]
        AttemptRepo[AttemptRepository]
        WrongRepo[WrongQuestionRepository]
        WeakRepo[WeakKnowledgePointRepository]
        ExamRepo[ExamRepository]
        RegistryRepo[QuestionRegistryRepository]
        StateRepo[QuestionBankStateRepository]
        Sync[QuestionBankSyncService]
    end

    subgraph Content["Published content files, loaded once per worker"]
        Manifest["courses/<course_id>/course.json"]
        QBank["questions.json snapshot"]
        Gloss["glossary.json snapshot or none"]
    end

    Manifest --> Loader[CourseLoader]
    QBank --> Loader
    Gloss --> Loader
    Loader --> Bundle[CourseBundle]
    Bundle --> QRepo
    Bundle --> GRepo
    Registry --> PerCourse
    CourseRepo --> Registry
    Sync --> RegistryRepo
    Sync --> StateRepo

    Routes --> Templates[Jinja templates]
    Templates --> Browser
    UserRepo --> SQLite[(instance/mcq.db)]
    RateLimitRepo --> SQLite
    CourseRepo --> SQLite
    ProgressRepo --> SQLite
    AttemptRepo --> SQLite
    WrongRepo --> SQLite
    WeakRepo --> SQLite
    ExamRepo --> SQLite
    RegistryRepo --> SQLite
    StateRepo --> SQLite
    CrossCourse --> SQLite
```

The route layer translates HTTP input into service calls, and it resolves **which course** a request belongs to from the URL alone. Every service of that course comes from one immutable `CourseServices` graph; repositories own data access; domain dataclasses carry data between those layers. There is no process-wide "current course" and no global content repository.

## 4. Project Layout and File Responsibilities

```text
MCQ_Template/
├── run.py
├── wsgi.py
├── gunicorn.conf.py
├── requirements.txt
├── README.md
├── pytest.ini
├── .gitignore                     # instance/, courses/ and root *.json are not versioned
├── docs/
│   ├── ARCHITECTURE.md
│   ├── COURSE_GUIDE.md
│   ├── FRONTEND_DESIGN_SYSTEM.md
│   ├── GLOSSARY_GUIDE.md
│   ├── MULTI_COURSE_MIGRATION.md
│   └── QUESTION_GUIDE.md
├── deploy/
│   ├── mcq-template.service
│   └── nginx-mcq-template.conf
├── instance/
│   └── mcq.db
├── courses/                       # one directory per course; content is not versioned
│   └── <course_id>/
│       ├── course.json            # manifest: course_id, title, enabled, order, paths
│       ├── questions_candidate.json   # working copy the CLI reads by default
│       ├── glossary_candidate.json    # optional glossary working copy
│       ├── versions/<sha256>/     # immutable publications: what --add and every publish write
│       │                          # (each content type keeps the current + previous version)
│       ├── .publish.lock          # publication lock taken while a publish switches over
│       ├── questions.json         # published copy, plain-file layout only (older courses)
│       └── glossary.json          # same; unread once the manifest points at versions/
├── app/
│   ├── __init__.py
│   ├── course_runtime.py          # CourseRegistry / CourseState / AppServices
│   ├── models/
│   │   ├── __init__.py
│   │   ├── course.py              # Course, CourseDefinition, slug validation
│   │   └── domain.py
│   ├── repositories/
│   │   ├── __init__.py
│   │   ├── database.py
│   │   ├── course_loader.py       # manifest discovery, validation, bundles
│   │   ├── course_repository.py   # permanent course identity + schema_meta
│   │   ├── course_scope.py        # required course_id validation
│   │   ├── schema_migrations.py   # transactional namespace migration
│   │   ├── rate_limit_repository.py
│   │   ├── question_bank_state_repository.py
│   │   ├── glossary_loader.py
│   │   ├── glossary_repository.py
│   │   ├── question_loader.py
│   │   ├── question_registry_repository.py
│   │   ├── question_repository.py
│   │   ├── user_repository.py
│   │   ├── progress_repository.py
│   │   ├── attempt_repository.py
│   │   ├── exam_repository.py
│   │   ├── wrong_question_repository.py
│   │   └── weak_knowledge_point_repository.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── course_consistency.py  # Flask-independent learner-write guard
│   │   ├── course_service.py      # one course's dependency graph + reconciliation
│   │   ├── grading_service.py
│   │   ├── local_time.py
│   │   ├── progress_state.py
│   │   ├── quiz_service.py
│   │   ├── question_bank_sync_service.py
│   │   ├── question_fingerprint.py
│   │   ├── srs_service.py
│   │   ├── exam_service.py
│   │   ├── statistics_service.py
│   │   ├── global_statistics_service.py
│   │   ├── wrong_question_service.py
│   │   └── weak_knowledge_point_service.py
│   ├── web/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── course_context.py      # signed form contexts + course-aware url_for
│   │   └── view_helpers.py
│   ├── routes/
│   │   ├── __init__.py
│   │   └── web.py
│   ├── templates/
│   │   ├── base.html
│   │   ├── auth.html
│   │   ├── courses.html
│   │   ├── error.html
│   │   ├── glossary.html
│   │   ├── home.html
│   │   ├── dashboard.html
│   │   ├── stats.html
│   │   ├── quiz.html
│   │   ├── quiz_setup.html
│   │   ├── exam.html
│   │   ├── exam_setup.html
│   │   ├── exam_report.html
│   │   └── mistakes.html
│   └── static/
│       ├── css/style.css
│       └── js/
│           ├── app.js
│           └── glossary.js
├── scripts/
│   ├── check_courses.py
│   ├── check_glossary.py
│   ├── check_question_bank.py
│   ├── course_tooling.py
│   ├── delete_course.py
│   ├── migrate_courses.py
│   ├── publish_course.py
│   ├── rename_course.py
│   ├── start_production.sh
│   └── stop_production.sh
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_bundled_glossary.py
    ├── test_bundled_question_bank.py
    ├── test_course_isolation.py
    ├── test_course_loader.py
    ├── test_course_migration.py
    ├── test_course_scripts.py
    ├── test_course_web.py
    ├── test_dashboard_web.py
    ├── test_exam_service.py
    ├── test_exam_web.py
    ├── test_form_context.py
    ├── test_global_statistics_service.py
    ├── test_glossary_loader.py
    ├── test_glossary_repository.py
    ├── test_glossary_web.py
    ├── test_grading_service.py
    ├── test_learning_upgrade.py
    ├── test_local_time.py
    ├── test_progress_state.py
    ├── test_progress_sync.py
    ├── test_question_bank_sync.py
    ├── test_question_loader.py
    ├── test_quiz_service.py
    ├── test_repositories.py
    ├── test_security.py
    ├── test_srs.py
    ├── test_srs_web.py
    ├── test_stale_worker.py
    ├── test_statistics_service.py
    ├── test_stats_web.py
    ├── test_view_helpers.py
    ├── test_web.py
    └── test_wrong_question_service.py
```

### Root files

#### `run.py`

This is the development-only executable entry point. It calls `create_app()`, converts a question-bank or glossary loading failure into a readable terminal message, and starts the Werkzeug development server with debug enabled on `127.0.0.1:5000`. Production never executes `app.run()`.

#### `wsgi.py`

This is the production WSGI entry point loaded as `wsgi:app`. It refuses to start unless `MCQ_SECRET_KEY` is present, explicitly disables Flask debug/testing mode, and wraps the application in `ProxyFix` configured for exactly one trusted Nginx proxy.

#### `gunicorn.conf.py`

Defines the loopback-only `127.0.0.1:8001` bind, two `gthread` workers, four threads per worker, timeouts, journald-compatible stdout/stderr logging, and bounded worker recycling.

#### `requirements.txt`

Defines the single Python dependency set used to run and test the project, including the production Gunicorn dependency. The Linux production packages are installed in `.venv-prod`; the existing Windows `.venv` remains separate.

#### `deploy/`

Contains this machine's systemd unit and Nginx site configuration. It is **not** source-controlled: `.gitignore` excludes `deploy/`, so a fresh clone does not contain this directory and must recreate the two files. They do not become active merely by editing them; deployment copies them to `/etc` and reloads the relevant service (see [§18](#18-local-production-deployment)).

#### `scripts/start_production.sh` and `scripts/stop_production.sh`

Convenience operations scripts for WSL. The start script validates Nginx, starts Gunicorn and Nginx, verifies both services, and polls the `/ready` readiness URL (which fails while a worker still serves a superseded question bank). The stop script stops Nginx before Gunicorn and verifies that both are inactive. They control already-deployed services and do not copy templates into `/etc`.

#### `courses/<course_id>/questions_candidate.json` (working copy) and the manifest's `questions` pointer

The **candidate** is the maintainer's working copy and the default input of every `check_*.py` and `publish_course.py` command; no worker ever loads it. What a worker loads is the file the course manifest's `questions` field points at: an immutable `versions/<sha256>/questions.json` (what `--add` and every publish write) or a plain root `questions.json` in the plain-file layout, and — when no manifest exists at all — the root `questions.json` through the legacy adapter.
Every application process reads and validates each enabled course's bank once during
startup. Editing a candidate has no effect until it is published and the workers are restarted: `python run.py` in
development or `mcq-template.service` in production. Publish it atomically
(`scripts/publish_course.py`) rather than with `cp` or an editor save: a partially
written file is read by a worker starting in that window and reported as misleading
invalid JSON. A root `questions.json` without a manifest is still loaded as the
`legacy` course, which is the pre-multi-course layout. A `questions.json` sitting
next to a manifest that points at `versions/<sha256>/` is read by nothing at all:
`check_courses.py` reports it as an unreferenced copy so it cannot be mistaken for
the live content.

#### `courses/<course_id>/glossary_candidate.json` (working copy) and the manifest's `glossary` pointer

Holds one course's domain-neutral terminology metadata: canonical terms,
aliases, Chinese translations (`term_zh`), the Chinese definition (`definition_zh`) and
optional categories. As with the question bank, the **candidate** is only the maintainer's working copy; a worker loads the file the manifest's `glossary` field points at. Every process validates and loads it once at startup, and a
manifest may declare `"glossary": null` to state that the course has none (a missing field is a manifest error).
The English `definition` field was retired: the loader ignores a leftover key (so a
previously published copy stays rollback-loadable) and `check_glossary.py`
reports it as a retired field to delete. It is independent of learner state and
of every question-bank fingerprint (`bank_version` plus the grading, content,
placement and catalogue fingerprints), is never written to SQLite, and therefore never triggers
reconciliation or fences sibling workers — but a running worker keeps serving the glossary it loaded, so a glossary publish still needs a restart to be visible.

#### `docs/QUESTION_GUIDE.md`, `docs/GLOSSARY_GUIDE.md`, `docs/COURSE_GUIDE.md` and `docs/MULTI_COURSE_MIGRATION.md`

The first two are the user-facing authoring contracts for the two startup-loaded JSON
files. They document the fields accepted by the current loaders, validation commands,
content-quality guidance, replacement behavior, and release checklists. `COURSE_GUIDE.md`
covers the manifest, URLs, per-course publishing/disable/delete workflow and the
new-course order of operations; `MULTI_COURSE_MIGRATION.md` covers the namespace
migration, its field-level verification and rollback.

#### `scripts/check_courses.py`

Course-level gate: validates every manifest (schema version, slug, required paths, path
containment, declared glossary) and loads every enabled course's question bank *and*
glossary through the application's own loader, reporting per-course status. It also
lists — informationally, without changing the exit code — any `questions.json` /
`glossary.json` in the course directory that no manifest path points at, because such a
copy is never read and would otherwise look like the live content. Read-only: a
global catalogue ambiguity exits ``2``, a single broken course exits ``1`` while the
other courses are still reported.

#### `scripts/check_glossary.py`

Glossary gate: validates a glossary with `GlossaryLoader`, compares canonical terms and
aliases with learner-facing question-bank text, reports orphan entries, and emits
conservative manual-review candidates. It is read-only and does not generate or modify
glossary data. `publish_course.py` re-runs it (offline) before switching over a glossary.

Per course it reads the default working copies (`questions_candidate.json` /
`glossary_candidate.json` inside the course directory) when they exist and the published
files otherwise, always printing which two files it read; `--published` forces the
deployed files.

The three ``check_<subject>.py`` scripts are the read-only gates of the content flow
(check → publish → restart worker); the publish half and the full flow are documented in
[`COURSE_GUIDE.md`](COURSE_GUIDE.md) §7.10.

#### `scripts/delete_course.py`

The only supported way to remove a course, because a course is more than its directory.
It removes the content directory (manifest, published content, `versions/<sha256>/`
copies, `.publish.lock`) *and* every database reference in one audited operation: the
`courses` identity row, all course-scoped learner rows (`quiz_progress`, `attempts`,
`wrong_questions`, `weak_knowledge_points`, `exam_sessions` plus the `exam_questions`
slots they own), `question_bank_state`, `question_registry` and — when it points at the
deleted course — `schema_meta.default_course_id`.

The order of operations is the protocol: after a verified timestamped backup the
directory is moved atomically into `courses/.trash/<id>.<stamp>` (reversible), the
database transaction commits (the commit point), and only then is the quarantined
directory removed. A failed transaction restores the directory to its original path; a
failed restore (or a failed final removal) exits `3` and keeps the recognizable
quarantine entry, `--purge` finishes that cleanup, and a run interrupted between the
phases is resumed by the next one, which adopts that course's single quarantine entry.
Safety also comes from a `--dry-run` report that includes the phase plan, a refusal
while learner rows exist unless `--force` is given, a strict `--courses-dir` and
`courses/.trash` containment check on every path, a refusal for the persisted
`legacy_course_id` namespace and the legacy root-file layout, and a per-table total
row-count re-check inside the transaction that rolls back rather than commit a partially
scoped deletion. Exit codes: `0` deleted (or dry run), `1` refused (nothing written, or
fully compensated), `2` usage/IO, `3` committed but a quarantine directory still needs
manual cleanup.

#### `scripts/check_question_bank.py`

Pre-deploy gate for `questions.json`: validates the file with `QuestionLoader`, then
dry-runs the registry diff against a read-only copy of the live database and prints
exactly what startup reconciliation would do (new, content-only, placement-changed,
catalogue-changed, presentation-only, grading-changed, deleted, resurrected). The
candidate it validates is the explicit positional path, or — when none is given — the
course's default working copy (`courses/<course_id>/questions_candidate.json`) if it
exists, else the currently published file (`--published` forces that last case), and the
report always names the file it read. Exit
codes: `0` deployable (including allowed-but-destructive cleanups, which always print
a warning and never claim the deploy is data-loss-free), `1` invalid bank, `2` a
retired question ID is reused for a different question (startup would refuse to run),
and `3` under `--strict` when the update would clear learner state. It also reports
the two bank-level classifications — `catalogue-changed` (menus/filter validation
changed, so the coordinated restart is required) and `presentation-only` (bytes
changed with no fingerprint-visible change: labels or formatting, which is deployable
without fencing) — and lists retired IDs that have no recorded grading identity
(pre-registry tombstones), which a candidate bank must not rely on for identity
checks.

#### How to read the two bank-level report lines

`catalogue-changed` and `presentation-only` are *derived classifications*, not field
diffs: the database stores no label snapshot, only the `bank_version` and the
fingerprints. The script prints `presentation-only: yes` only when all three of these
hold: the candidate's raw bytes differ from the recorded `bank_version`; no
per-question fingerprint changed; and the catalogue shape is unchanged. In other words
the edit can only live in untracked fields (bank/source/chapter labels, `lecture`,
`filename`) or in JSON formatting/whitespace.

Consequences worth knowing:

- `presentation-only: no` must **not** be read as “labels did not change”. It is also
  printed for a byte-identical candidate (nothing to deploy), for any per-question
  change (a question *wording* edit is a `content_fingerprint` change and shows up
  under `content-only`), for any catalogue-shape change (reported by
  `catalogue-changed: yes`), for a database whose `question_bank_state` row is missing
  (no baseline to compare), and for a publish that mixes label edits with any tracked
  change.
- A formatting-only rewrite of an unchanged bank reports `presentation-only: yes`, so
  the flag answers “is this change attributable to labels/formatting?”, not “are the
  labels different?”.
- The flag never influences the exit code. Decide whether the coordinated restart is
  required from `catalogue-changed` and the per-question lines; `presentation-only`
  only confirms that this publish has no fencing or learner-data impact.

#### `scripts/publish_course.py`

The single publish entry point for every course-content type: add a course, publish
`questions.json` and/or `glossary.json`, and enable/disable a course. A publish reads the
candidate once, validates exactly those frozen bytes, archives them under
`versions/<sha256>/`, and switches the manifest over with a single `os.replace` (the
legacy root-file layout falls back to an atomic single-file replace). `--add` creates a
course in exactly that shape: the initial bytes are validated first, archived under
`versions/<sha256>/`, and only then does the manifest (written inside the course's
publication lock) point at them, so a course directory never carries an unreferenced
`questions.json`/`glossary.json` that looks like the live content. `--questions` and
`--glossary` default to the course's working copies
(`courses/<course_id>/questions_candidate.json` and `glossary_candidate.json`), so a bare
`--course <course_id>` publishes exactly the candidates a maintainer just edited; an
explicit path always wins, a glossary candidate is ignored for a course whose manifest
declares `glossary: null`, and a command with no content at all writes nothing. It re-runs
the matching gate by default — `check_question_bank.py` against the database for questions,
`check_glossary.py` for the glossary — and refuses to switch over content that fails it;
`--skip-preflight` is the explicit, discouraged escape hatch. Direct `cp` over a live file
(or an editor's in-place save) can truncate the JSON while a worker starts, which surfaces
as a misleading `Invalid JSON in question bank at line 1, column N`; this script removes
that failure mode. Publishing still requires a coordinated restart of all workers, because
a structural change advances the bank generation.

After publishing (and after `--add`) the command runs one retention pass over the course's
`versions/` directory: the digest directory the manifest points at is always kept, and
each content type (`questions.json` and `glossary.json` counted separately) keeps the
`keep - 1` most recently written other versions — `--keep-versions N` defaults to `2`,
i.e. the current version plus the one before it, which is what makes a one-step rollback
possible without a history journal. Superseded copies lose only the known content file
(a directory that still holds anything else is left in place), and a failed cleanup is
reported without failing the publish that already took effect. `--prune` runs the pass
alone, `--no-prune` skips it, and because rollback re-points the manifest at an existing
digest directory (no bytes are rewritten), an archived version can be published again
exactly like a working copy.

#### `instance/mcq.db`

The SQLite database created automatically on first startup. It contains accounts and learner activity, but not question text.

#### `pytest.ini`

Contains Pytest configuration used by the test suite (`testpaths = tests`, quiet output).

#### `scripts/course_tooling.py`

The thin shared tooling layer behind every course CLI script. It resolves a course through the application's own `CourseLoader`, freezes candidate bytes once, validates them with the application's own loaders, writes immutable `versions/<sha256>/` copies, switches the manifest with a single `os.replace()`, takes the course publication lock (`.publish.lock`), runs the per-content-type retention pass, and provides `preferred_candidate()` (explicit path, then the course's working copy, then the published file). No business rule is duplicated here: a rule change belongs in `app/` and is picked up automatically.

#### `scripts/migrate_courses.py`

The transactional multi-course migration and inspection CLI: `--dry-run` only reads (table list, `schema_version`, persisted `legacy_course_id`, whether a migration is needed, per-table row counts, orphan `exam_questions` rows); a real run copies the database to a timestamped backup next to it unless `--no-backup`, then migrates. `--layout` additionally materialises the legacy root files as `courses/<--legacy-course-id>/`, using that same id for the directory name, the manifest `course_id` and the persisted `schema_meta.legacy_course_id`; the flag is validated as a course slug before anything is written (an invalid or traversing value exits `2` with no writes). A global catalogue ambiguity — most likely a root `questions.json` left next to `courses/<id>/course.json` — is reported and exits `1` rather than aborting with a traceback (see [`MULTI_COURSE_MIGRATION.md`](MULTI_COURSE_MIGRATION.md)). Exit codes: `0` success, `1` migration refused (nothing written), `2` usage/IO.

#### `scripts/rename_course.py`

The administrative namespace rename: it backs the database up (unless `--no-backup`) with the shared verified snapshot, verifies every precondition read-only *before* touching anything, rewrites `course_id` across every course-scoped table in one `BEGIN IMMEDIATE` transaction with before/after row-count validation, refuses to run when the target namespace already owns data, leaves `exam_questions` untouched (it follows its parent session), and updates the persisted `legacy_course_id` and — new — a `schema_meta.default_course_id` that pointed at the renamed course.

`--rename-directory` runs a three-phase protocol whose commit point is the database transaction: stage `courses/<from>` into `courses/.rename-staging-…` while atomically rewriting its manifest, commit the database, then promote the staging directory. Failures before the commit are compensated (manifest bytes and directory name restored) and exit `1`; a failed compensation or a failure after the commit exits `3` with the paths and the `--recover` command, and the state file records the phase. `--recover` finishes or undoes an interrupted run from the state file plus the observable state, and refuses combinations the protocol cannot produce. Exit codes: `0` success, `1` refused (nothing written, or fully compensated), `2` usage/IO, `3` committed with the filesystem half still pending.

## 5. Application Assembly

### `app/__init__.py`

This module is the composition root, and it is deliberately thin. `create_app()` performs exactly these steps:

1. Create the Flask application (with `instance/` as the instance path) and load configuration, cookie defaults, and optional test overrides. If `MCQ_SECRET_KEY` is absent outside tests, generate an ephemeral development secret and log a warning. `SESSION_COOKIE_SECURE` is not a fixed default: it is resolved from the declared environment (`MCQ_ENV`), the strict boolean `MCQ_SESSION_COOKIE_SECURE`, and the entry point's own value, in that order of precedence (see `app/config.py`); tests get `False` without depending on the machine's environment, `development` gets `False` so a plain-HTTP `python run.py` session works, `production` and an *unknown* environment keep `True`, and a production downgrade may only come from code, never from an exported variable.
2. Build the `CourseLoader` (courses directory plus the root-file legacy adapter paths) and discover every course **definition**: one per `courses/<course_id>/course.json`, plus the synthetic `legacy` definition when the root `questions.json` exists. A duplicate `course_id` or an unreadable manifest directory raises `CourseDefinitionError` here — that is an application-assembly failure.
3. Run `Database.initialize()`, which calls `schema_migrations.ensure_schema()`, enforces the attempt-retention window, and registers the accepted course metadata (`courses` rows) for the discovered definitions. `ensure_schema()` first probes the file read-only: a fresh or already-current database takes no backup and adds no files. When the table layout really has to change, the `StartupMigrationPolicy` decides — `REFUSE` (what an undeclared environment resolves to) aborts with the explicit CLI command, `BACKUP_AND_MIGRATE` (the default, and what every declared environment resolves to, production included) writes and verifies a timestamped `mcq.db.bak-<UTC>` snapshot first, and `MIGRATE_AFTER_EXTERNAL_BACKUP` is only for a caller that already produced one. A failed backup aborts startup before the migration transaction begins; a migration failure rolls back and leaves that verified snapshot behind. Concurrent starts serialise on `mcq.db.migrate.lock` so a rolling restart produces exactly one backup.
4. Create the deployment-wide repositories once: `UserRepository`, `RateLimitRepository`, `CourseRepository` and `CrossCourseQueries`. There is deliberately no global `QuestionRepository`/`GlossaryRepository` at this level: content is only reachable through a course.
5. Call `build_course_registry()`, which for each **enabled** definition loads the `CourseBundle` (immutable in-memory `QuestionRepository`, optional `GlossaryRepository`, headline and publication identity), assembles the course's `CourseServices` (scoped persistence repositories + every service), and reconciles that course's bank once inside `BEGIN IMMEDIATE` (re-checking the publication identity inside the transaction and reloading once if the course was republished during startup). A course-scoped failure is recorded as `CourseStatus.UNAVAILABLE`; the other courses keep loading. Undeployed database identities are added as `CourseStatus.UNDEPLOYED`.
6. Assemble `AppServices`: `CourseServices` of the navigation default course, the `CourseRegistry`, the deployment-wide repositories, the resolved display timezone, and the persisted `legacy_course_id` (read-only).
7. Register the serializer used to sign `form_context`, the built-in `course_id`-aware template `url_for`, and publish everything under `app.extensions["mcq_services"]`, `["mcq_form_serializer"]`, `["mcq_course_loader"]` and `["mcq_cross_course"]` for tests and diagnostics.
8. Register `GET /health`, `GET /ready` and `GET /ready/<course_id>`.
9. Register the `after_request` security-header hook and the authenticated web blueprint.

Dependencies are created once and explicitly passed to the objects that need them. Route functions therefore never construct databases or services during individual requests, and no request can observe a half-assembled course.

Important configuration values are:

| Setting | Purpose |
|---|---|
| `SECRET_KEY` | Signs Flask session cookies. Tests may override it; development generates a new random value when `MCQ_SECRET_KEY` is absent; `wsgi.py` requires the environment value in production. |
| `COURSES_DIR` | Directory holding one `<course_id>/course.json` manifest per course (default `<project root>/courses`). |
| `QUESTION_FILE` | Path the **legacy root adapter** reads as `questions.json` (default `<project root>/questions.json`). It is not the only active content path: manifest courses load their own files, and when the root file is absent `legacy` exists only as a database identity. |
| `GLOSSARY_FILE` | Same, for the legacy root `glossary.json`. |
| `DATABASE` | Path to the shared SQLite database (default `instance/mcq.db`). |
| `DEFAULT_COURSE_ID` | Navigation preference (environment `MCQ_DEFAULT_COURSE`): the course a browser without an explicit course URL is sent to. It never re-owns historical data. |
| `KNOWLEDGE_VERIFICATION_TARGET` | Distinct correct Review question IDs required per active chapter; currently `2`. |
| `DISPLAY_TIMEZONE` | Display timezone for rendered timestamps and dashboard trend days; defaults to the server local zone (environment `MCQ_DISPLAY_TIMEZONE`, IANA name such as `Asia/Shanghai`); invalid names fail startup fast. |
| `PERMANENT_SESSION_LIFETIME` | Lifetime of a persistent login/session cookie (30 days). |
| `SESSION_COOKIE_HTTPONLY` | Prevents browser JavaScript from reading the session cookie; enabled by default. |
| `SESSION_COOKIE_SAMESITE` | Uses `Lax` cross-site behavior for the session cookie. |
| `SESSION_COOKIE_SECURE` | Resolved per environment instead of globally fixed (see `app/config.py` and the `MCQ_ENV` / `MCQ_SESSION_COOKIE_SECURE` rows below): `development` gets `False` so a plain-HTTP `python run.py` session can log in, `production` and *unknown* keep `True`, and tests get `False`. An exported `MCQ_SESSION_COOKIE_SECURE=false` cannot downgrade production — that has to be an explicit value passed in code, and it logs a warning. |
| `MCQ_ENV` | Declares the deployment kind (`production` / `development` / `testing`); anything else fails startup. Unset means *unknown*, which resolves conservatively (Secure cookie, no automatic startup migration) rather than like development. `run.py` and `wsgi.py` `setdefault` it to `development` and `production`. |
| `MCQ_SESSION_COOKIE_SECURE` | Strict boolean override (`1/0`, `true/false`, `yes/no`, `on/off`); any other value fails startup instead of being coerced, because `bool("false")` is `True`. |
| `MAX_CONTENT_LENGTH` | Request-body limit (64 KiB). |
| `ENABLE_CSRF` | Turns the CSRF check on; enabled by default. |
| `AUTH_LOGIN_ACCOUNT_LIMIT` / `AUTH_LOGIN_IP_LIMIT` / `AUTH_LOGIN_WINDOW_SECONDS` | Login throttle per account (10) and per IP (100) inside a 5-minute window. |
| `AUTH_REGISTER_IP_LIMIT` / `AUTH_REGISTER_WINDOW_SECONDS` | Registration throttle per IP (10 per hour). |

The ephemeral development secret prevents a source-controlled fallback secret, but it also intentionally invalidates existing development sessions after a process restart. The production secret is stable, generated outside the repository, and loaded from the systemd environment file.

## 6. Domain Model

### `app/models/domain.py`

This file contains immutable dataclasses and the quiz-mode enum:

- `QuizMode`: distinguishes normal practice, mistake review, and mock exams.
- `ExamStatus`: the mock-exam lifecycle (`in_progress`, `submitted`, `expired`).
- `Option`: one answer option with English text and optional Chinese text.
- `SourceDocument` / `Chapter`: the normalized course-material catalogue used by all filters and labels.
- `LEGACY_SOURCE` / `LEGACY_CHAPTER`: the single definition of the synthetic `legacy` / `Uncategorized` catalogue. Both the loader (which materializes it for a bank without a catalogue) and the in-memory repository (which falls back to it) import these, so a legacy question is always filed under the same course material and chapter.
- `Question`: one validated question, its options, correct answer IDs, explanations, one stable source reference, one or more chapter references, plus optional section/pages.
- `GlossaryTerm`: one canonical English term, its Chinese translation, aliases, an optional Chinese definition (`definition_zh`), and an optional arbitrary category.
- `Glossary`: root glossary metadata and an immutable tuple of `GlossaryTerm` objects.
- `User`: one local account with a UUID, username, password hash, and creation time.
- `Attempt`: one persisted answer event.
- `WrongQuestion`: the current mistake and correction state for one user/question pair. Its `corrected` property maps to the legacy SQLite `mastered` column. It also carries the SRS schedule: `srs_level` (0 for a freshly corrected question) and `next_review_at` (ISO timestamp, `None` while uncorrected or unscheduled).
- `WeakKnowledgePoint`: one user/chapter pair containing active state and distinct verified Review question IDs.
- `ExamSession`: one persisted mock exam with its status, fixed size, optional time limit, deadline, and submission results.
- `ExamQuestion`: one fixed exam slot with its saved selection and graded outcome.

These objects do not know about Flask, HTML, or SQL. They are the shared data language between repositories and services.

### Package `__init__.py` files

The `__init__.py` files in `models`, `repositories`, `services`, and `routes` re-export the public objects used by other layers. They keep imports concise and define the intended public surface of each package.

## 7. Repository Layer

Repositories are responsible for loading or persisting data. They do not decide learning rules or render pages.

Two kinds of repository exist, and the difference is the namespace rule of the whole application:

* **content repositories** (`question_loader.py`, `question_repository.py`, `glossary_loader.py`, `glossary_repository.py`) are loaded once per course and are read-only in memory;
* **persistence repositories** (everything else that touches learner state) are constructed with a **required, immutable `course_id`** and restrict every statement to it, so `get_all()`/`list_all()`/`count()`/`distinct_question_ids()` mean "this course", never "the whole database". `app/repositories/course_scope.py::require_course_id()` enforces that missing namespace loudly at construction time.

Genuinely cross-course operations (resolving which course owns a bare `exam_id`, counting rows per course for a report) live in `app/repositories/database.py::CrossCourseQueries`, plus the migration/administrative CLI scripts, so an accidental cross-course read is always a visible, reviewed choice.

### `app/repositories/database.py`

`Database.initialize()` is a three-stage startup: it runs the transactional namespace migration once (`schema_migrations.ensure_schema`), then the per-`(learner_id, course_id, question_id)` attempt-retention sweep as its own step, then records the accepted course metadata. The ordering is deliberate — the migration touches no learner semantics, and neither the retention sweep nor course registration is part of its transaction. Before any of that, a read-only `probe_schema()` decides whether the layout has to change at all, and `app/repositories/database_backup.py::timestamped_backup()` is what produces the verified pre-migration snapshot when the chosen `StartupMigrationPolicy` requires one.

`Database.connect()` reuses the context-local connection when called inside `Database.transaction()`. Otherwise, it is a context manager that:

- opens a short-lived SQLite connection;
- configures rows for name-based column access;
- commits successful operations;
- rolls back failed operations;
- always closes the connection.

`Database.transaction()` uses `BEGIN IMMEDIATE` and a context-local connection so progress token checks, answer attempts, correction/weak-point updates and progress changes commit or roll back together. SQLite serializes these transactions across threads and Gunicorn workers. Repository operations inside the transaction reuse its connection.

Schema changes are **not** applied by guarded `ALTER TABLE` statements. The single place that rebuilds a table layout is `schema_migrations.ensure_schema()`; see [§11](#11-sqlite-schema) for the table bodies, the primary keys and the transaction shape.

### `app/repositories/course_loader.py`

`CourseLoader` owns course discovery and content loading. `discover_definitions()` walks `COURSES_DIR` for `course.json` manifests, validates each one (`schema_version: 1`, `course_id` slug, required `questions`, explicit `glossary` key, resolved-and-contained paths), and finally appends the **legacy root adapter** definition when the root `questions.json` exists. A directory without a manifest is ignored with a log line, not treated as a course. A duplicate `course_id` or an unreadable manifest raises `CourseDefinitionError` (application assembly fails); a single broken course raises `CourseLoadError` (that course becomes `unavailable`).

`load_bundle()` loads one definition into a `CourseBundle`: the in-memory `QuestionRepository`, the optional `GlossaryRepository`, the bank fingerprint and the *publication identity* — a SHA-256 over the names and bytes of every content file the manifest currently points at. `publication_identity()` is recomputed inside the reconciliation transaction, which is how the "worker preloaded the old files, then a newer publication landed" race is detected.

Safety rules enforced here: `course_id` must be a URL-safe lowercase ASCII slug; every declared path is resolved relative to the manifest directory and must stay inside it (so request parameters are never concatenated into filesystem paths); the manifest is authoritative for the displayed course title whenever it exists, so a disabled and an enabled course cannot disagree about their own name, and only the manifest-less legacy adapter takes its title from the question bank.

### `app/repositories/course_repository.py`

Owns the permanent `courses` table (identity plus the accepted metadata: `title`, `title_zh`, `enabled`, `sort_order`, timestamps) and the `schema_meta` keys. `accept()` upserts metadata while preserving `created_at`; `list_accepted()` returns every identity this database knows, and `is_enabled()` is re-read inside learner transactions so a course disabled after a page was rendered cannot accept new writes.

Two different IDs live here and must never be confused:

* `legacy_course_id` — decided once by the namespace migration and persisted. The public API only offers reading it (`legacy_course_id()`), because changing it would silently re-own history. The one supported way to move it is the explicit administrative rename in `scripts/rename_course.py`.
* `default_course_id` — a pure navigation preference (`default_course_id()` / `set_default_course_id()`), which never re-owns historical data.

### `app/repositories/course_scope.py`

`require_course_id()` validates the namespace a bound repository was constructed with and raises `ValueError` when it is missing or malformed. It exists so that "forgot the course scope" can never degrade into a silent cross-course read or write.

### `app/repositories/schema_migrations.py`

The one place that rebuilds a table layout. `ensure_schema()` creates a fresh database or migrates an existing one transactionally, losslessly and idempotently, and returns a `SchemaInfo` (`schema_version`, persisted `legacy_course_id`, whether the file existed, whether anything was migrated, the backup path when one was taken). `SCHEMA_VERSION` is `2`; the pre-multi-course layout is version `1`. It adds the `course_id` namespace column to every learner table, replaces the `question_bank_state` singleton with one row per course, widens the `question_registry` primary key, and records the schema version plus the persisted legacy assignment. It deliberately does **not** run retention cleanup, weak-knowledge backfill, question-bank diffing, registry reconciliation, content cleanup, fingerprint recomputation or score rewriting. See [§11](#11-sqlite-schema).

The read-only `probe_schema()` is what decides whether any of that is necessary (`needs_migration` covers an old version, a missing `schema_meta`, *and* a same-version database with a missing table), so a normal startup neither backs up nor writes; `migration_lock()` serialises the decision across sibling workers; and `StartupMigrationPolicy` says whether the startup may do it at all, with `REFUSE` raising `StartupMigrationRefused` and a message that names the explicit CLI command.

### `app/repositories/rate_limit_repository.py`

`RateLimitRepository` owns the `auth_rate_limits` table that backs login and registration throttling. Identifiers are SHA-256 hashed before storage so raw usernames and IP addresses are never persisted. Each `(scope, identifier_hash)` row is a fixed-window counter (`window_started_at`, `expires_at`, `attempt_count`): `consume()` atomically clears expired rows, increments the counter, and reports whether the allowance holds, while `is_limited()` checks without consuming. Consumption runs inside `Database.transaction()`, so a rejected login and its recorded failure commit or roll back together.

### `app/repositories/question_bank_state_repository.py`

`QuestionBankStateRepository` is bound to one `course_id` and owns that courses single `question_bank_state` row: the last loaded raw bank fingerprint (diagnostic only), the normalized shape of that courses catalogue, and that courses structural bank generation counter which stale workers compare against. There is deliberately no global generation: a structural update of course A bumps only As row. It never touches learner data; per-question reconciliation lives in `QuestionBankSyncService`. `app/course_runtime.py` and `app/services/course_service.py` construct the course-scoped repositories (one graph per course); the rate-limit repository stays deployment-wide and is injected into the web blueprint. `Database` itself only manages connections, transactions, schema, and the per-`(learner, course, question)` retention sweep.

### `app/repositories/question_registry_repository.py`

`QuestionRegistryRepository` owns the permanent `question_registry` table — one row per question ID ever loaded, holding its grading identity (`question_type`, the option-ID set, the correct-answer set), a content fingerprint, a placement fingerprint (`source_id` + `chapter_ids`), and a lifecycle status. Rows are never deleted: a question that leaves the bank is only marked `retired`, leaving a tombstone that prevents the ID from being silently recycled for a human-different question later. Reappearing retired IDs are resurrected when the grading identity matches exactly, and rejected with a startup error otherwise; tombstones adopted from pre-registry learner history carry no grading identity and are therefore adopted on their first reappearance (see “Pre-registry tombstone compatibility”).

### `app/repositories/question_loader.py`

`QuestionLoader` reads raw JSON bytes, calculates a SHA-256 source fingerprint (recorded for diagnostics only; it never drives any data reset), validates the full document, and converts it into immutable `Question` and `Option` objects. Per-question content and grading fingerprints are computed later from the normalized model, so JSON whitespace or field order can never register as a change.

Validation includes:

- root and question-list types;
- unique question IDs;
- supported question types (`single` or `multiple`);
- at least two options per question;
- unique option IDs within a question;
- valid and non-duplicated correct-answer IDs;
- exactly one correct answer for single-choice questions;
- valid English and optional Chinese text fields.
- unique source/chapter IDs and valid chapter-to-source relationships;
- one source and one or more same-source chapter references on every question in a catalogued bank;
- positive, non-duplicated page numbers when supplied.

For backward compatibility, a bank with no `sources` and `chapters` arrays is loaded into one synthetic `legacy` / `Uncategorized` source and chapter (`LEGACY_SOURCE` / `LEGACY_CHAPTER` in `app/models/domain.py`, the one definition both this loader and `question_repository.py` use). A bank that opts into the catalogue must provide valid metadata for every question.

An invalid bank raises `QuestionBankError` before the website starts, preventing a partially loaded quiz.

### `app/repositories/question_repository.py`

Stores all validated questions and the normalized curriculum catalogue in memory. It provides ordered iteration, lookup by question ID, server-side source/chapter filtering, ordered source/chapter access, and per-chapter question counts. It also holds the English and Chinese question-bank titles. When a bank declares no catalogue, it falls back to the same `LEGACY_SOURCE` / `LEGACY_CHAPTER` constants the loader uses.

### `app/repositories/glossary_loader.py`

Reads and validates one course's glossary file (`schema_version` `1`, required root and term fields, unique IDs, normalized canonical-term uniqueness, alias types, empty aliases, duplicate aliases and cross-entry term/alias collisions). It creates frozen `Glossary` and `GlossaryTerm` objects. Normalization is used only to reject duplicate or ambiguous labels; original strings remain unchanged for display and literal browser matching, and every validation error identifies the entry and field that failed. The English `definition` field is retired — the loader ignores a leftover key on purpose so an earlier published copy stays rollback-loadable, and `scripts/check_glossary.py` reports it instead.

### `app/repositories/glossary_repository.py`

Holds one course's glossary for the life of the process, read-only and independent of SQLite. It provides tuple-based term access, ID lookup, categories in stable first-appearance order, and defensive JSON-ready serialization for templates. It is not passed into grading, correction, weak-knowledge, wrong-question, attempt or progress logic, and it is not part of any question-bank fingerprint — so glossary maintenance never triggers reconciliation and never fences sibling workers.

### `app/repositories/user_repository.py`

Creates and authenticates local users. User IDs are UUID strings, usernames are unique without case sensitivity, and passwords are stored as Werkzeug password hashes rather than plaintext. `list_all()` returns every account ordered by username for the cross-account statistics service.

### `app/repositories/progress_repository.py`

Stores one JSON round state and question-bank fingerprint per `(learner_id, mode)`. `get()` distinguishes a missing row from a persisted null state; `save()` upserts the row. A null state marks cleared progress and must never be resurrected from a stale copy. The route wrapper owns the transaction covering progress and answer-related repositories.

### `app/repositories/attempt_repository.py`

Inserts one row for every graded answer, then retains only the ten most recent rows for that `(learner_id, question_id)` pair in the same transaction. Application startup also prunes older databases to the same limit. The selected option IDs are serialized as JSON so both single and multiple selections can use the same column. `list_for_learner()` returns one learner's retained window in chronological order for the statistics service; `list_all()` returns every account's window in the same order for the global statistics service — still bounded by learners × questions × the retention limit.

### `app/repositories/exam_repository.py`

Persists mock-exam sessions and their fixed question slots. Every read is scoped by `learner_id` so one account can never load another account's exam. `save_answer()` replaces one slot's selection (an empty selection clears it) and records the visited position for resume; `apply_grading()` stores per-slot outcomes; `finalize()` flips `in_progress` to a finished status with a conditional `UPDATE`, which is the idempotency guard against double submission. The active-exam query also filters out sessions whose deadline has passed (`get_active_for_learner`), so an expired exam never surfaces as resumable, and `list_expired_in_progress()` feeds the touch-based settlement sweep.

### `app/repositories/wrong_question_repository.py`

Maintains question-level state for each `(learner_id, question_id)` pair:

- a real wrong answer in either mode creates or reopens the record;
- every wrong answer increments `wrong_count`, clears the legacy `review_streak`, sets legacy `mastered=0`, and resets the SRS schedule (`srs_level=0`, `next_review_at=NULL`) so a failed review always returns to the plain correction flow first;
- one correct Review answer for an uncorrected record sets `review_streak=1` and `mastered=1`, whose current domain meaning is `corrected`, and starts the SRS schedule at level 0 with the supplied `next_review_at`;
- a correct Review answer for an already-corrected, due record advances the SRS schedule through `record_srs_reviewed()` (guarded by `mastered=1`);
- a correct Review answer for an already-corrected but not-yet-due record leaves the SRS schedule untouched;
- `get_due()` returns one learner's corrected records whose `next_review_at` has been reached (inclusive comparison on ISO UTC strings);
- a correct transfer question with no prior wrong record never creates one;
- resetting mistakes deletes only the signed-in learner's rows and leaves attempts intact.

The old columns are retained to avoid a destructive migration. They no longer represent chapter mastery or a two-answer streak.

### `app/repositories/weak_knowledge_point_repository.py`

Stores one row per `(learner_id, chapter_id)`. `active` records whether reinforcement remains required; `verified_question_ids` is a defensive JSON array of stable, distinct question IDs; `last_wrong_at` and `updated_at` provide minimal state timestamps. A wrong answer activates/reopens every chapter on the question and resets its JSON progress to `[]`. Verification saves are additive and learner-scoped. Malformed JSON is treated as empty rather than causing a request failure.

## 8. Service Layer

Services contain the application rules that are independent of HTTP and HTML.

The central boundary is:

```text
Normal Selection Policy != Review Selection Policy

Normal: filter → fairness selection → round queue
Review: wrong correction state + due SRS schedule + weak knowledge state
        → original/srs/transfer selection → review queue
```

### `app/services/course_service.py`

The single composition root for **course-scoped** dependencies. `assemble_course_services()` takes one `CourseBundle` and constructs every repository and service of that course with the *same* `course_id`, then returns an immutable `CourseServices` (content repositories from the bundle, persistence repositories bound to the namespace, and every service). Because the whole graph is built per course, a wiring mistake cannot silently mix namespaces: `QuestionBankSyncService`, `WrongQuestionService` and `ExamService` validate at construction that everything they were handed is bound to the same course, so a mis-assembled graph fails at startup instead of writing one course's reconciliation into another course's history.

`synchronize_course()` then runs that one course's startup reconciliation and records the loaded generation (`with_generation()` returns a copy carrying it). The publication identity is re-checked *inside* the sync transaction, so a publication that landed while this worker was starting makes the caller discard the preloaded snapshot and reload instead of writing stale content back into the database (see `app/course_runtime.py::_load_and_synchronize`, which retries once).

### `app/services/course_consistency.py`

Flask-independent guards for every learner write, applied *inside* the transaction rather than by a pre-check. `guarded_learner_transaction()` opens `Database.transaction()` (`BEGIN IMMEDIATE`) and then:

1. checks the course is still enabled (`CourseServabilityChangedError`, mapped to 503);
2. compares this worker's loaded generation with the course's live `question_bank_state` generation (`StaleWorkerError`, mapped to 503);
3. re-validates the signed form context — bound course, operation and generation (`StaleFormError`, mapped to 409);
4. yields so the caller can perform the business operation and the learner writes, then commits.

This closes two races: a worker whose outer pre-check passed just before a sibling published a structural change, and a form rendered by a superseded worker (or for a different course). Both are rejected with zero learner writes. The web layer owns the HTTP mapping (503 stale/unavailable, 409 stale form, 404 unknown course).

### `app/services/grading_service.py`

`GradingService` validates submitted option IDs, removes duplicates, and grades by comparing sets of option IDs.

- Single-choice answers must match the one correct ID.
- Multiple-choice answers are correct only when the selected set exactly equals the correct set.
- Display order does not affect grading.
- Unknown option IDs raise `AnswerValidationError`.

### `app/services/quiz_service.py`

`QuizService` contains two explicitly separate selection policies plus shared grading:

**Normal Selection Policy**

1. `QuestionRepository.get_filtered()` resolves the live eligible IDs from source/chapter filters.
2. A SHA-256 scope signature is calculated from the sorted eligible IDs.
3. If the prior signature matches, validated remaining IDs are consumed; otherwise a new shuffled bag is created.
4. At a cycle boundary, the next full eligible set is shuffled. IDs already selected in the current round are retained for later in the new bag, not duplicated in the current round.
5. `all` shuffles every eligible ID exactly once and leaves no remaining bag.

Normal selection receives no wrong-question or weak-knowledge input. The route carries the resulting `fairness_scope` and `fairness_remaining_ids` into the next normal progress state only when `POST /quiz/start` creates a new round.

**Review Selection Policy**

- starts with all real wrong questions whose legacy database flag maps to `corrected=False`;
- each item records `original_correction`, `srs_review`, or `transfer_verification` and an optional target chapter;
- after pending originals, corrected questions whose SRS `next_review_at` has been reached are selected as `srs_review` items;
- after due SRS reviews, active weak chapters choose a random live same-chapter ID not already verified and not equal to the immediately preceding occurrence where an alternative exists;
- if a wrong original needs spacing, another pending original or a transfer question is preferred before falling back to immediate repetition;
- a question occupies exactly one state at a time (`corrected=False` rows never carry a due timestamp), so it cannot enter the queue twice through different roles;
- no weighted sampling and no Normal fairness state are used;
- when no distinct candidate exists, selection returns finite shortage metadata and logs a warning.

Both modes retain deterministic option shuffling from the round seed, question ID, and occurrence index. Answers are graded by option ID and passed to `WrongQuestionService`.

The deterministic option shuffle is important: options change between rounds, but refreshing a page or showing feedback does not reorder the current question.

### `app/services/wrong_question_service.py`

`WrongQuestionService` records every attempt exactly once and applies question/chapter boundaries.

For a normal answer:

- every result is written to `attempts`;
- an incorrect result creates or updates `wrong_questions`;
- the incorrect result also activates and resets every chapter in the question;
- a correct result does not change correction or knowledge verification.

For a review answer:

- every result is written to `attempts`;
- an incorrect result creates/reopens the concrete wrong question, clears its SRS schedule, and resets all of its chapter verification;
- a correct result marks an existing uncorrected wrong question corrected after that one answer and starts its SRS schedule (level 0, one day later);
- a correct result for an already-corrected question whose SRS review is due advances the schedule one level (3/7/15/30 days, capped at 30);
- a correct result for an already-corrected question that is not due leaves the schedule untouched;
- every correct result contributes its question ID once to each active chapter on the question;
- a transfer question is not inserted into `wrong_questions` unless its submitted answer is actually wrong.

It also joins persistent wrong-question records with live in-memory questions and each question's latest incorrect selection from `attempts`. Source/chapter filtering is applied to this joined model. If a recorded question ID no longer exists in `questions.json`, the item is skipped and a warning is logged. The same join/filter pipeline powers `get_due_srs_items()` / `get_due_srs_count()` / `get_filtered_due_srs_question_ids()`, which back the home-page "due today" entry and SRS review selection.

### `app/services/srs_service.py`

Pure, Flask-free spaced-repetition scheduling rules shared by the wrong-question service and its tests:

- `SRS_INTERVAL_DAYS = (1, 3, 7, 15, 30)` is the single source of truth for the level ladder; the last entry caps every level at or beyond it;
- `initial_schedule(now)` returns level 0 with `next_review_at` one day out, applied whenever a correction completes;
- `advanced_schedule(level, now)` increments the level and returns the next due timestamp for the new level;
- `is_due(next_review_at, now)` compares aware UTC datetimes and treats the exact scheduled moment as due;
- `utc_now()` / `parse_timestamp()` centralize time handling; naive stored timestamps are interpreted as UTC so naive/aware comparisons never mix.

Every function accepts `now` explicitly, which keeps scheduling deterministic under test. There is no background scheduler: "due" is only evaluated when a learner opens a page or enters Review.

### `app/services/weak_knowledge_point_service.py`

`WeakKnowledgePointService` owns chapter-level rules:

- all `question.chapter_ids` activate and reset after any real wrong answer;
- only a correct Review answer adds verification;
- IDs are intersected with current live same-chapter repository IDs and deduplicated;
- two distinct IDs mark the chapter inactive/completed;
- a later wrong answer reactivates it with 0/2;
- summaries join stable chapter titles, pending concrete wrong counts, available distinct question counts and verification progress;
- old wrong rows with no weak state are backfilled once with active 0/2 state.

### `app/services/progress_state.py`

Pure, HTTP-independent helpers that validate and describe the per-mode quiz progress state dictionaries. It centralizes the progress `session_key()` names, the `quiz_limit()` parsing for the selected practice size, the `is_valid_progress_state()` structural validation used to accept or clear a stored round, `valid_state_for()` which drops invalid entries, and `active_summary()` which builds the resume banner data for an unfinished round. `reconcile_state()` removes unusable question IDs from an unfinished round in lockstep across the queue, review items, and per-question results, decrementing the round counters so a bank edit never strands a round. `PRACTICE_MODES` names the two modes that own a resumable round; mock exams keep their own persisted state and are deliberately excluded. Keeping these rules here lets the route layer and tests share one definition of what a valid round looks like without touching Flask.

### `app/services/question_fingerprint.py`

Pure fingerprint helpers over the normalized `Question` model. `grading_fingerprint()` hashes exactly the grading identity (type, option-ID set, correct-answer set); `placement_fingerprint()` hashes the filing identity (`source_id` plus `chapter_ids`), which drives chapter filtering, Review selection and chapter progress; `catalogue_fingerprint()` hashes the shape of the whole catalogue (source IDs in order, chapter IDs with their source assignment and order, sorted by `(order, id)`); `content_fingerprint()` hashes every validated content field. All canonicalize before hashing, so JSON formatting can never register as a change, and none ever replaces `question.id` as identity. `catalogue_fingerprint()` deliberately excludes source/chapter *labels*, so a title edit cannot fence workers.

### `app/services/question_bank_sync_service.py`

`QuestionBankSyncService` runs once per course per worker startup inside a single `BEGIN IMMEDIATE` transaction. It bootstraps an empty registry from the deployed bank (adopting IDs that only exist in learner history as retired tombstones, and backfilling a placement baseline for rows written before placement tracking), classifies the diff (`diff_questions()` is a pure function shared with the pre-deploy check script), fails fast when a retired ID is reused for a grading-different question, and then applies only what actually changed:

| Change to one course's published bank | Learner history | Generation |
|---|---|---|
| Content-only (`text`/`text_zh`, explanation, option text or order, an added wrong option, `section`, `pages`, JSON formatting) | fully preserved | unchanged |
| Chapter/source move (`chapter_ids` or `source_id`) | preserved, but it is a **structural** change: it decides chapter filtering, Review/weak-knowledge selection and chapter progress | +1 |
| Catalogue shape (a source or chapter added, removed, reordered or re-assigned) | preserved; still structural, because each worker builds its own menus and validates submitted filter values against them (the old worker would reject a chapter the new one serves with `400`) | +1 |
| Catalogue labels (bank `title`/`title_zh`, `sources[].title`/`lecture`/`filename`, `chapters[].title`) | preserved | unchanged — workers may briefly disagree about wording |
| Grading identity (`type`, correct-answer set, removed/renamed option IDs) | exactly that question's `attempts` and `wrong_questions` rows are cleared, and it is stripped from weak-point verifications, unfinished rounds and unfinished exams | +1 |
| Deleted question | `attempts` are kept, but its correction/SRS state, weak-point references, progress-queue entries and unfinished-exam slots are dropped (exam positions resequenced, `question_count` shrunk) | +1 |
| New question | untouched | +1 |
| Retired ID reuse (same ID, different grading identity) | nothing is written: that course becomes `unavailable` at startup and only that course is affected | — |

It also compares the stored `catalogue_fingerprint()` with the loaded catalogue and bumps the generation when the catalogue shape changed; a database that predates catalogue tracking adopts the current shape as its baseline instead. Missing weak rows are backfilled from the surviving live wrong-question IDs with safe 0/2 progress. Users are never touched, no flash or banner is shown for bank maintenance, everything is idempotent, and concurrent workers serialize on the same transaction — the second worker's diff simply finds nothing to do, so one publication bumps a course's generation at most once however many structural changes it contains. The current publication is re-checked *inside* the transaction (`CoursePublicationChangedError`), so a worker that preloaded superseded files reloads instead of writing them back.

Two consequences are easy to miss. First, "attempts are preserved" is not the same as "displayed statistics stay identical": every learner-facing statistic counts only *live* questions, so deleting a question can lower cumulative counts and move accuracy until the question is restored. Second, a retired ID is reserved **inside its own course** and the fingerprint algorithms never include `course_id`, so the same hash may legitimately appear in two courses; the namespace migration recomputes nothing.

### `app/services/exam_service.py`

`ExamService` owns the mock-exam lifecycle. Creation validates the requested size and time limit against fixed allow-lists and the live bank size, then freezes a random, deduplicated question set with one option seed. Answers are stored verbatim (empty selections clear a slot) without grading or attempt writes, so learners can revisit questions freely. The server-side deadline is authoritative: `is_expired()` uses an inclusive comparison, `finalize_if_expired()` auto-submits on any exam page load or answer save after the deadline, and `finalize_expired_for_learner()` settles all of one learner's expired exams when they open the home, exam, or dashboard page, so results, attempts, and mistakes are persisted promptly without any background worker; `get_active_session()` additionally hides deadline-passed exams from resume entry points. All time checks accept an injected `now` for deterministic tests. `submit()` grades the frozen answers, flips the session through the repository's conditional update (making double submits no-ops), and forwards each answered question to `WrongQuestionService.record_attempt()` exactly once with mode `mock_exam`; unanswered slots count as wrong in the score but create neither attempts nor mistake records. Reports aggregate the stored grading per chapter in curriculum order.

### `app/services/local_time.py`

Resolves and converts the display timezone. Storage and all comparisons stay in UTC; this module only governs what users see. `resolve_display_timezone()` turns the configured IANA name into a `zoneinfo.ZoneInfo`, falls back to the server-local zone when unset, and raises `InvalidTimezoneError` for unknown names so misconfiguration fails at startup. `to_display()`, `display_day()`, and `timezone_label()` are the only conversions used by the statistics service and the view helpers.

### `app/services/statistics_service.py`

`StatisticsService` aggregates the dashboard from one learner's retained attempt window plus the existing wrong-question state machine. It reports total/correct counts and accuracy, 7- and 30-day activity counts (inclusive day cutoffs on UTC timestamps), pending/corrected mistake counts with due SRS reviews, per-chapter mastery (attempts, accuracy, coverage against the live bank, and a threshold-based status: not started, weak below 60%, progressing below 80%, good below 90%, mastered at 90%+, with a low-sample flag below 3 attempts), and a 7-day per-day trend that fills empty days with zeros and buckets attempts by display-timezone calendar day. All bands are module constants and every function accepts an injectable `now`; the zone defaults to UTC and `create_app` injects the resolved display zone. The window cutoff (`count_since`) and day-bucketing (`daily_trend`) rules live at module level so the global statistics service shares them exactly.

### `app/services/global_statistics_service.py`

`GlobalStatisticsService` is the reference-only counterpart of `StatisticsService`: it aggregates the retained attempt window across every registered account and never reports single-account detail. It feeds the `/stats` page with registered/active account counts (active means at least one retained attempt), group totals and accuracy, the same 7/30-day activity windows, the same 7-day zero-filled trend, and a per-chapter difficulty board — attempts, accuracy, distinct answering accounts, and a difficulty band (unattempted, hard below 60%, medium below 80%, easy at 80%+, reusing the personal mastery thresholds) sorted hardest first with unattempted chapters last in curriculum order. Per-learner concepts (pending mistakes, SRS due counts) are excluded by design. Window, trend, and low-sample rules are imported from `statistics_service`, and `now`/display-zone injection keeps every rule unit-testable.

## 9. HTTP and Session Layer

### `app/routes/web.py`

This module creates the Flask blueprint and defines all browser endpoints.

Every learning row below is registered twice: canonically as `/course/<course_id><path>` (the URL's course is the only authority) and as the course-less legacy alias `<path>`. The alias redirects a `GET` after resolving the course and answers 409 to any other method, so a stale form is never guessed into a course from the session (see `resolve_course_context` below).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness: return `{"status":"ok"}` after application assembly succeeds; public and used for layered production checks |
| GET | `/ready` | Aggregate readiness over this worker's *declared, enabled* courses: `{"status":"ready",...}` with 200 only while every one of them is `ready`, otherwise `{"status":"degraded","courses":{"<course_id>":{"status","worker_generation","database_generation","reason"},...},"enabled_course_count":N,"ready_course_count":M}` with 503. The aggregate is a monitoring signal only: a stale course A never makes course B's routes fail |
| GET | `/ready/<course_id>` | Per-course readiness: 200 when that course is `ready`, 503 when it is `stale`/`unavailable`/`disabled`, 404 when no such course is declared |
| GET/POST | `/login` | Show the login form or authenticate a user; a signed-in visitor is redirected to the preferred course's home on a healthy worker and to `/courses` (with the bank-update notice) when no course can be served |
| GET/POST | `/register` | Show the registration form or create a user; the same stale-aware redirect applies after a successful registration |
| POST | `/logout` | Clear the signed-in session |
| GET | `/` | Redirect to the session's preferred course, or to `/courses` when none is known; a preference pointing at a course this worker cannot serve answers 503 instead of silently landing in another course |
| GET | `/courses` | Show the course list: one card per course this worker knows (including `disabled` and `undeployed` states) with its status label, linking into a course only when it is `ready` |
| GET | `/glossary` | Show this course's glossary with live search and category filtering (canonically `/course/<course_id>/glossary`) |
| POST | `/quiz/start` | Start or restart normal practice |
| GET | `/quiz/setup` | Dynamically list course materials/chapters before normal practice |
| GET | `/quiz` | Render the current normal-practice question or result |
| POST | `/quiz/answer` | Validate and grade the current normal answer |
| POST | `/quiz/next` | Advance normal practice |
| GET | `/mistakes` | Show only the signed-in user's mistake records |
| POST | `/mistakes/reset` | Reset only the signed-in user's mistake state to zero |
| POST | `/review/start` | Start or restart correction plus weak-chapter review |
| GET | `/review` | Render the current review question or result |
| POST | `/review/answer` | Validate and grade the current review answer |
| POST | `/review/next` | Advance the review queue |
| GET | `/dashboard` | Show per-learner totals, chapter mastery, and recent activity |
| GET | `/stats` | Show cross-account totals, chapter difficulty, and group activity; reference-only, no single-account detail, but fenced by the bank generation exactly like `/dashboard` |
| GET | `/exam` | Show mock-exam configuration, the resumable exam, and history |
| POST | `/exam/start` | Validate the configuration and create a fixed question set |
| GET | `/exam/<exam_id>` | Render one exam question without feedback; auto-submits when expired |
| POST | `/exam/<exam_id>/answer` | Save one exam answer without grading feedback |
| POST | `/exam/<exam_id>/submit` | Finalize the exam exactly once and redirect to its report |
| GET | `/exam/<exam_id>/report` | Show the immutable score report for a finished exam |

`/health` and `/ready` are registered directly on the Flask application before the web blueprint. They therefore do not run the blueprint's account requirement and do not expose learner, database, question, or secret data. `/health` answers while the process is merely alive (a stale worker must still be able to serve the login/logout pages); `/ready` is the signal monitoring and the start script should use, because a worker whose bank generation no longer matches the database only answers 503 for learning pages.

The blueprint's second `before_request` hook, `resolve_course_context`, does three things. First it binds the request to exactly one course: the `<course_id>` URL variable is the only authority, and the course''s live state is recomputed against the database (so a course whose generation moved is fenced immediately, without any process-wide flag). A course-less legacy URL is redirected for a `GET` — resolving an `exam_id` to the course that actually owns it, after checking learner ownership — and refused with 409 for anything else, so a stale form is never guessed into a course from the session. Second, it rejects a `POST` whose signed `form_context` is missing or untrusted; that token binds the course, the operation the form performs and the worker generation the page was rendered from. Third, it records `session["last_course_id"]`, which is only a navigation preference for `/`. `POST /logout`, `GET /login`, `GET/POST /register`, `/courses`, and `GET /glossary` are exempt from the per-course fence, so a learner on a shared device can always sign out, pick another course, and read reference material. Those exempt account pages share `_redirect_after_sign_in()`, which never hands a learner to a page that can only answer 503: on a healthy worker it redirects to the course home, on a degraded one to `/courses` plus the notice "题库正在更新，暂时只能浏览课程列表与术语表；…". Each course''s 503 page names the affected course, renders "题库正在更新，请稍后刷新页面；如果长时间未恢复，请联系管理员。", sets `Retry-After`, offers the course selector and a logout form, and never links back into the same 503, so it cannot loop. The same request may also be rejected inside the write transaction by `app/services/course_consistency.py`, which re-checks the course''s servability, the worker''s loaded generation and the signed form context after `BEGIN IMMEDIATE`; that is what closes the "outer pre-check passed, sibling published, then we write" race.

### `app/web/` request helpers

Three small modules keep cross-cutting HTTP concerns out of the route functions:

- `app/web/auth.py` holds the CSRF check, the registration-field validation, the constant-time token comparison, and the login/registration rate-limit helpers. It issues and checks the CSRF token carried by mutating forms, and consults `RateLimitRepository` to throttle repeated failed logins. It deliberately does *not* hold the blueprint's account gate: resolving `session["user_id"]` to a real user is the `require_account` `before_request` hook in `app/routes/web.py`, which redirects to `/login` when the account is missing or invalid. Its `tokens_match()` is the single token comparison used by both the CSRF check and the quiz `answer_token` checks: it compares UTF-8 bytes in constant time, so a caller-controlled form field can never raise (the previous `secrets.compare_digest(str, str)` raised `TypeError` for non-ASCII input and turned a 400 into a 500).
- `app/web/course_context.py` holds the signed `form_context` that binds every learning form to `(course_id, operation, generation)`, its reader (`read_form_context()`, which verifies the signature and the 12-hour age window), the endpoint-to-operation mapping, and the course-aware template `url_for`. The signed `operation` is always the operation the form **posts to** — the endpoint a template passes to `form_context(<form action endpoint>)`, never the page that rendered it — because the write guard compares it with the endpoint the submit actually reached; signing the rendering page's endpoint would make every submit answer 409.
- `app/web/view_helpers.py` holds the template and catalogue helpers that assemble the course/chapter selection lists and other view models shared by the practice and review screens, plus the display-timezone-aware timestamp/duration formatters injected into every template.

The authentication hook resolves `session["user_id"]` to a real user. Missing or invalid accounts are redirected to `/login`. Protected views then run inside the `shared_progress` wrapper, which is the phase pipeline `_load_practice_progress()` → the view → `_persist_practice_progress()`: it loads both practice modes from SQLite, defensively drops question IDs that are no longer answerable, runs the view, and persists changed states in one transaction. Server-side rows are the only source of progress; any ancient cookie copy is purged without being read. Bank maintenance is silent — no flash or banner. A stale worker never reaches the wrapper because `resolve_course_context` already answered 503, and the wrapper still re-checks the generation inside the write transaction (`guarded_learner_transaction`) so a publication that lands between the pre-check and the write changes nothing. `POST /logout` deliberately stays outside the wrapper so signing out always works. `_persist_practice_progress()` decides whether a row needs writing by comparing the practice state only: `bank_version` is diagnostic, so two workers running banks that differ only in wording must not overwrite the same `quiz_progress` row back and forth inside the global write lock.

Quiz answer submission is split the same way: `_answer()` validates the request (round status, `answer_token`, selection) and grades it, while `_record_answer_outcome()` is the only place that writes the round's counters, its per-slot entry and the temporary `feedback` payload that a refreshed page renders. Exam navigation is expressed through two intent-named helpers, `_to_exam()` (unfinished exam) and `_to_exam_report()` (finished exam), instead of repeating the course-scoped `url_for()` call at every branch.

The routes use the Post/Redirect/Get pattern after answer submissions. This prevents a normal browser refresh from resubmitting the form.

### Shared quiz progress structure

`ProgressRepository` stores normal and review progress in `quiz_progress`, keyed by `(learner_id, mode)`. The request-local keys `quiz_progress_normal` and `quiz_progress_review` are kept in `g`, never written to the browser cookie. Each device reads the same state when opening or refreshing a page; there is no background polling or push update. Devices must reach application instances sharing the same SQLite file. Logout preserves both modes. Restarting a mode replaces that account’s round for every device; resetting mistakes clears its review round while preserving normal progress and answer history.

Each state dictionary contains fields such as:

| Field | Meaning |
|---|---|
| `mode` | `normal` or `review` |
| `question_ids` | Current ordered queue of question IDs |
| `current_index` | Position in the queue |
| `correct_count` / `incorrect_count` | Attempt counters for the current round |
| `status` | Whether the current question is pending or answered |
| `answer_token` | Token for the current question occurrence, checked by answer and next forms and rotated on advance; the status check prevents duplicate grading. It is compared with `app/web/auth.py::tokens_match()` (constant time over UTF-8 bytes), so any mismatched, missing or non-ASCII submission is a 400, never an unhandled error |
| `option_seed` | Seed used to keep option order stable for the round |
| `requested_size` | Selected normal-practice size |
| `chapter_ids` / `source_ids` | Stable curriculum filters for restarting the same focused round |
| `initial_question_count` | Number of questions present at round start |
| `fairness_scope` / `fairness_remaining_ids` | Normal-only eligible-set signature and remaining coverage bag |
| `review_items` | Review-only queue metadata: question ID, `original_correction` / `srs_review` / `transfer_verification`, and optional target chapter |
| `corrected_count` / `knowledge_completed_count` | Review round outcomes |
| `review_shortages` | Finite completion metadata when a chapter has too few distinct live questions |
| `feedback` | Temporary result data for the answered question |

The signed session cookie retains authentication and flash messages only; server-side rows are the sole source of practice progress and any legacy cookie copy is discarded unread. A null state is retained after reset or invalidation to prevent stale devices from restoring cleared progress. Answer and next forms carry the current answer token, preventing stale devices from answering or advancing a later question. Authenticated responses use `Cache-Control: no-store`.

Old Normal progress remains valid without fairness fields; the next new Normal round initializes them defensively. Old unfinished Review progress without `review_items` cannot preserve occurrence roles safely, so validation clears only that Review row to a null tombstone. Wrong questions, weak points, attempts, Normal progress and users remain intact.

### Review queue behavior

`POST /review/start` persists all currently uncorrected originals as role-bearing queue items; when none are pending, all currently due SRS reviews are queued instead. Answer submission persists grading and feedback but does not randomize the next candidate. Only `POST /review/next`, after advancing beyond the existing queue, asks the Review Selection Policy for one later item: a pending original first, then a due SRS review, then a same-chapter transfer. The generated question ID, role, target chapter, option seed, token and feedback therefore remain stable across GET refreshes and devices.

The queue ends only when its selected scope has no uncorrected original, no due SRS review, and no active weak chapter below 2/2. If the live bank cannot supply two distinct IDs, the service does not loop or count a duplicate; it leaves the chapter active, logs a warning, and completes the finite page with a shortage explanation.

### Error handling

The blueprint converts common HTTP errors into the Chinese `error.html` page. This covers invalid submissions, expired quiz state, missing pages, removed questions, unsupported methods, server errors, the stale-course 503 (which also gets a `Retry-After` header), the unavailable/disabled-course 503, the unknown-course 404, and the stale-form 409. The page names the affected course and offers navigation that still works on this worker (the course selector, and the course glossary when it exists). A missing, rotated, mismatched or non-ASCII `answer_token`/`csrf_token` is an *invalid submission*: `app/web/auth.py::tokens_match()` compares the two tokens as UTF-8 bytes in constant time, so every caller-controlled string is rejected with 400 and no submission can turn a rejection into an unhandled error.

## 10. Presentation Layer

### `app/templates/base.html`

Defines the shared document structure, header, signed-in username, logout action, flash messages, stylesheet, JavaScript, and bilingual toggle.

### `app/templates/auth.html`

Renders both login and registration forms. The route passes a page mode so one template can present the correct fields and links.

### `app/templates/courses.html`

Renders the course selector (`/courses`) from the worker's course states: one card per declared course with its Chinese/English title, `course_id`, and a status badge (可学习 / 等待更新 / 已停用 / 未部署 / 不可用). Only a `ready` course gets a "开始学习" link into `/course/<course_id>/`; every other state explains itself instead of linking into a page that could only answer 503. With no declared course the page shows an empty state pointing at `courses/` and the legacy root `questions.json`. Each card is a column whose last child (the launcher or the state note) is pinned to the bottom edge, so a row's launcher buttons share one baseline even when the English titles wrap to a different number of lines.

### `app/templates/home.html`

Composed as a single-column editorial task board: a quiet nameplate masthead, one dominant task band, a conditional agenda for in-flight work, and a resource directory.

- The masthead keeps the bank title at nameplate scale with its English italic subtitle and a mono bank-size metadata line (`题库 · N 题`), so the title informs context without owning the viewport.
- The task band (`.task-band`) opens with the system's 2px ink top rule and makes the primary task the page's largest serif heading: an in-progress normal practice renders its mono position (`第 X / Y 题 · 还剩 Z 题`) with the 4px completed-work progress indicator (`current - 1` of `total`), while a fresh learner sees the start copy and bank size. The action column pairs one prominent two-line primary CTA (`.button-large` + `.button-copy`; the secondary line carries the resume position or the 10/20/50 practice sizes) with a ghost restart action guarded by `data-confirm-restart`.
- The agenda (`.home-agenda`) is the single home for every unfinished obligation and renders only when at least one exists: a "继续模拟考试" row for an active exam, a "继续错题巩固" row (its progress text folds in the due SRS count when present) with a confirm-guarded ghost restart form beside it, or a due-SRS submit row ("待复习 X 题") posting to `POST /review/start`. With nothing actionable the whole section is omitted — there is no static placeholder row.
- The directory (`.directory-grid`) presents exam setup, the personal dashboard, the cross-account stats page (marked 仅供参考), mistakes (carrying the pending/corrected counts — the only place those metrics appear), and glossary as `.resource-row` entries in a two-column ruled grid that collapses to one column on small screens.

### `app/templates/quiz_setup.html`

Builds the practice selector entirely from the repository catalogue. It supports All Chapters, one or more chapter selections, and a group-level selector for all chapters in each source document, followed by the existing quiz-size control.

### `app/templates/quiz.html`

Renders normal practice and review with one shared template. It handles:

- question progress;
- stable option order;
- English-first and optional Chinese text;
- single- and multiple-choice controls;
- correct, missed, and incorrectly selected option states;
- explanations and role-aware correction/knowledge-point feedback;
- round-completion summaries.

Review adds only a small text role in the existing question-type metadata ("错题纠正" / "间隔复习" / "同知识点强化") and semantic sentences inside the existing feedback block. It does not add another question card, navigation system, modal, or client-side state.

The two-column answer summary renders each answer paragraph on one template line. `.answer-summary p` uses `white-space: pre-line` so bank-authored `\n` breaks survive; that also means any newline leaked into those `<p>` tags by multi-line Jinja blocks would surface as empty lines pushing the answer text down and misaligning the columns.

### `app/templates/mistakes.html`

Renders only the current account's mistake records, including source/chapter/page context, latest wrong answer, wrong count, and corrected status. Correct answers and explanations are server-rendered after correction. The summary row lays out its stat cards count-agnostically in one evenly divided line (the same component serves the glossary two-card stats): dividers sit exactly on the equal column boundaries, every card shares the same inner padding, and the first card stays flush with the container's left edge. The row adds a "今日待复习" card with the currently due SRS count — its numeral turns accent whenever reviews are due — which also counts toward whether the start-review action is enabled. Above the existing mistake table, a second section built from the same `section-heading`, `table-card`, `mistake-table`, status, and metadata primitives summarizes each weak chapter, pending corrections, distinct 0/2 progress, completion, and any insufficient-question warning. GET filters support source, chapter, or both through the shared custom picker. A filtered review retains the same scope. Reset clears that learner's wrong and weak rows, cancels Review, and preserves attempts and Normal progress.

### `app/templates/dashboard.html`

Renders the signed-in learner's metrics strip (totals, accuracy, 7/30-day activity, pending/corrected mistakes with due SRS count), a zero-filled seven-day bar chart of daily attempts and accuracy, and the chapter mastery table with coverage and threshold-based status labels. A learner without attempts sees a calm empty state with one recovery action while the chapter table still lists every chapter as not started.

### `app/templates/stats.html`

Renders the cross-account counterpart of the dashboard under a 全体学习数据 · 仅供参考 heading: a metrics strip (registered/active accounts, group totals and accuracy, 7/30-day group activity), the same zero-filled seven-day trend chart, and the chapter difficulty board — per-chapter group attempts, accuracy bar, distinct answering accounts, and difficulty labels reusing the `.status` palette, sorted hardest first. No username or single-account metric ever renders; the page links back to the personal dashboard for contrast. With no attempts at all it shows the same calm empty-state pattern while the difficulty board still lists every chapter as unattempted.

### `app/templates/exam_setup.html`

Collects the mock-exam configuration — question count from a fixed allow-list rendered as selectable rows, and a time limit chosen through the shared custom dropdown picker inside the setup controls bar (10–90 minutes in 10-minute steps, or untimed as the default) — surfaces the resumable in-progress exam, and lists recent exam history with score, accuracy, duration, status, and report links. The start action is disabled when the bank is smaller than every allowed exam size.

### `app/templates/exam.html`

Presents one exam question per page with the shared question-card primitives but no grading feedback: options restore the saved selection, navigation saves through the answer form, a progress bar and answered counter track position, and a server-seeded countdown mirror auto-submits at zero. A separate submit panel reports the answered count and confirms before finalizing.

### `app/templates/exam_report.html`

Shows the immutable score (`correct / total`), accuracy, elapsed time, and submission status, a per-chapter breakdown in curriculum order, and every wrong or unanswered question with the shared answered-options markup, correct answer, and explanations. Glossary highlighting keeps working in all rendered question content. Its answer summary follows the same single-line paragraph rule as the quiz feedback block.

### `app/templates/glossary.html`

Renders the authenticated, course-neutral vocabulary page from `GlossaryRepository` metadata. It provides live search, a dynamically generated category picker, English-first recall cards, and per-card Chinese reveal controls.

### `app/templates/error.html`

Provides a consistent recovery page for friendly HTTP errors. For a stale-bank 503 it hides the home/mistakes links (both would immediately 503 again) and offers the glossary and a logout form instead, so a learner is never trapped in a 503 loop.

### `app/static/css/style.css`

Contains the full visual system and responsive behavior. The learning upgrade adds only local spacing/title/note rules for `.knowledge-summary`; colors, fonts, borders, cards, buttons, focus rings and breakpoints are reused. The SRS home entry is an `.agenda-row` (a link, or a submit button with the same appearance-reset grammar) inside `.agenda-list`; both mistake tables inherit the existing `max-width: 640px` table-to-card conversion, so no separate mobile UI or horizontal dependency is introduced. Normal quiz has no intentional visual change.

The dashboard, global stats, and mock-exam pages follow the same rule: `.metric-strip`, `.trend-chart`, `.mastery-bar`, `.data-table`, `.exam-nav`, and `.exam-submit-panel` are built from the existing tokens (paper/sheet surfaces, ink rules, accent progress, mono metadata, square corners, no shadows), and `.data-table` reuses the same 640px table-to-card conversion as the mistake tables. The stats page adds no CSS of its own — its difficulty labels reuse the `.status-*` text colors with text, never color alone. Exam status labels reuse the `.status` text-and-dot pattern so state is never conveyed by color alone.

The presentation invariant is that review reinforcement reuses the existing quiz/mistakes structures, CSS primitives, feedback patterns, bilingual/glossary behavior, keyboard focus, and 820px/640px responsive behavior. Role and progress differences are written as text and never conveyed by color alone.

The home task board holds the same line: `.home-masthead`, `.task-band`, `.home-agenda`/`.agenda-*` and `.directory-grid` add only local composition rules. The agenda rows derive from the `.resource-row` interaction grammar (140ms color transitions, 4px copy shift, accent-dark hover, 3px focus ring), the prominent CTA reuses `.button-large`/`.button-copy` with a single inverse-contrast rule for its secondary line, and the band header reuses the 2px ink top rule and the 4px square progress primitive. Colors, typography roles, focus rings and the 820px/640px breakpoints are all reused. No new tokens, dependencies, `!important` rules, or backend data were introduced.

### `app/static/js/app.js`

Adds small client-side enhancements:

- enables answer submission only after at least one option is selected;
- displays the number of selected options;
- disables the submit button during submission;
- asks for confirmation before discarding unfinished progress or submitting an exam (the shared `data-confirm` family);
- toggles Chinese learning aids and stores the preference in `localStorage`;
- moves focus to answer feedback for accessibility;
- implements the shared accessible listbox picker and submits marked GET forms as soon as a picker option is selected;
- keeps the global and per-source chapter selectors synchronized, including an indeterminate state when only part of a source is selected;
- mirrors the mock-exam countdown from the server-rendered remaining seconds and triggers the same submit form at zero (the server stays authoritative for expiry);
- mirrors the exam page's selection count in its hint without disabling navigation, since saving an empty selection clears a slot.

### `app/static/js/glossary.js`

Safely highlights canonical terms and aliases in marked English content, owns the keyboard-accessible Chinese-definition popover, and provides client-side glossary search, category filtering, visible counts, empty state, and Chinese reveal behavior.

It builds a literal, case-insensitive matcher from the canonical terms and aliases of **the current course only**, ordered longest-first, so `standard cell` wins over `cell`. A match is accepted only when any letter/number edge is not attached to another Unicode letter, number, combining mark or underscore — consequently `die` does not match inside `dielectric`, while punctuation-bearing data such as `SC-1`, `Cu/low-k`, `AR(1)` or `χ²` is not constrained to an ASCII token grammar. Only containers explicitly marked `data-glossary-highlight` are scanned; the engine walks text nodes with `TreeWalker`, builds replacements with a `DocumentFragment` and never rewrites container `innerHTML`, and it skips scripts, styles, form controls, Chinese translation blocks, existing highlights and the popover. Highlight controls support pointer activation, Enter, Space, visible focus, Escape, outside-click close and viewport-safe repositioning, and stopping the highlight's own event prevents a term inside an answer row from selecting that answer.

On the glossary page the search covers canonical English, aliases, the Chinese term, the Chinese definition and the category; the category picker is populated from repository-derived categories, English is shown first, and each card uses a real button to reveal or hide Chinese. The quiz-size, mistake-filter and glossary-filter pickers share one `initializePicker` helper in `app.js` with hidden inputs and server-rendered listbox buttons (no native `<select>`, no third-party UI library); glossary category choices filter cards immediately in the browser, while mistake source/chapter choices submit the GET form immediately.

The server repeats important validation, so client-side JavaScript is not treated as a security boundary.

## 11. SQLite Schema

The schema is defined by `app/repositories/schema_migrations.py::TABLE_BODIES` and `INDEX_STATEMENTS`; `ensure_schema()` creates a fresh database from exactly those bodies or rebuilds an existing one into them. The current multi-course layout is `SCHEMA_VERSION = 2` (the pre-multi-course layout is version `1`). One file holds every course: courses are a **namespace inside it** (`course_id`), not one database per course. No table stores question text, chapter titles or glossary content.

Migration mechanics, at a glance:

```text
dedicated connection
  PRAGMA foreign_keys = OFF (before the transaction)
  BEGIN IMMEDIATE
    re-read the schema version after taking the write lock
    create the new tables (one per table body)
    copy every old row (missing columns fall back to a documented default)
    field-level validation (row counts, course_id NOT NULL/non-empty, exam_questions parentage)
    replace the old tables, then create the indexes
    write schema_version + the persisted legacy_course_id
  COMMIT
  PRAGMA foreign_key_check (full, after commit)
```

Any failure rolls the whole thing back and the CLI reports the reason; re-running always reaches the same state. A pre-existing parent/child violation in the old data aborts the migration with a report instead of silently dropping rows. The transaction itself never runs retention cleanup, weak-knowledge backfill, question-bank diffing, registry reconciliation or fingerprint recomputation — those are separate stages (`Database.enforce_attempt_retention`, `register_courses`, and each course's `QuestionBankSyncService.synchronize()` at startup).

### `schema_meta`

| Column | Purpose |
|---|---|
| `key` | PRIMARY KEY: `schema_version`, `legacy_course_id`, optional `default_course_id` |
| `value` | Stored value |

### `courses`

| Column | Purpose |
|---|---|
| `course_id` | PRIMARY KEY: the permanent, URL-safe course slug |
| `title` / `title_zh` | Accepted metadata (the manifest is the source of truth for the displayed name) |
| `enabled` | `0`/`1`; a disabled course is not loaded on any worker |
| `sort_order` | Display order in the selector and course list |
| `created_at` / `updated_at` | UTC ISO timestamps; `created_at` is preserved across metadata upserts |

The `legacy` row always exists because `legacy_course_id` is part of the schema this build writes: learner tables declare a restrictive foreign key onto `courses`, and rows written under the legacy namespace must always have an owner. `Database.register_courses()` re-inserts the **persisted** `legacy_course_id` (not the module default), so a renamed namespace is never resurrected as a phantom `legacy` course on every startup.

### `users`

| Column | Purpose |
|---|---|
| `id` | UUID primary key |
| `username` | Case-insensitive unique login name (`COLLATE NOCASE`) |
| `password_hash` | Werkzeug password hash |
| `created_at` | UTC ISO timestamp |

Users are a **deployment-wide identity**: one account signs in to every course on this deployment. The table has no `course_id`.

### `quiz_progress`

| Column | Purpose |
|---|---|
| `learner_id` / `course_id` / `mode` | Composite PRIMARY KEY, one round per account, course and practice mode (`normal` or `review`) |
| `bank_version` | Last bank fingerprint that wrote the row; informational only — rounds survive cosmetic bank edits and are reconciled per question instead; it never triggers a write by itself |
| `state` | JSON round state, or SQL NULL for cleared progress (a tombstone that must never be resurrected from a stale device) |

### `attempts`

| Column | Purpose |
|---|---|
| `id` | Auto-incrementing attempt ID |
| `learner_id` | User UUID |
| `course_id` | Course namespace (required) |
| `question_id` | Stable course-local ID |
| `mode` | `normal`, `review`, or `mock_exam` |
| `selected_answers` | JSON array of selected option IDs |
| `is_correct` | Boolean stored as `0` or `1` |
| `answered_at` | UTC ISO timestamp |

At most ten rows are retained for each `(learner_id, course_id, question_id)` triple, ordered by `answered_at` and then `id`; the window is shared across all three modes. The retention rule is enforced after every insert and once during each application startup (`Database.enforce_attempt_retention()`), which is a step **outside** the namespace migration. The cumulative `wrong_count` in `wrong_questions` is independent of this rolling window.


### `wrong_questions`

| Column | Purpose |
|---|---|
| `learner_id` / `course_id` / `question_id` | Composite PRIMARY KEY: one current correction record per account, course and question |
| `wrong_count` | Total number of wrong answers |
| `review_streak` | Legacy compatibility column: `0` before correction, `1` after correction |
| `mastered` | Legacy compatibility column mapped to domain `corrected` |
| `srs_level` | Spaced-repetition level; `0` for a freshly corrected question, incremented after each passed due review |
| `next_review_at` | UTC ISO timestamp when the next SRS review becomes due; `NULL` while uncorrected or unscheduled |
| `last_wrong_at` | Most recent wrong-answer timestamp |
| `last_reviewed_at` | Most recent review timestamp, if any |

The columns keep their historical SQL names (the namespace migration copies them into the new layout instead of renaming anything); knowledge-point completion is never inferred from them. An invariant ties the two state machines together: `mastered=0` rows always have `next_review_at IS NULL`, so a question is either pending correction or scheduled, never both. A question corrected before this schema existed keeps `next_review_at = NULL` until it is corrected again.

### `weak_knowledge_points`

| Column | Purpose |
|---|---|
| `learner_id` / `course_id` / `chapter_id` | Composite PRIMARY KEY, one weak state per account, course and chapter |
| `active` | Whether this chapter still requires reinforcement |
| `verified_question_ids` | JSON array of distinct correct Review question IDs (defensive: malformed JSON reads as empty) |
| `last_wrong_at` | Most recent wrong answer that activated/reset the chapter |
| `updated_at` | Most recent state change |

Every query and write is bound to the learner *and* the course. Question content and chapter titles remain in the live per-course `QuestionRepository`; only stable course-local IDs are persisted, which is why `chapter_id` intentionally has no foreign key to any content table.


### `exam_sessions`

| Column | Purpose |
|---|---|
| `id` | Random hex primary key (**deployment-wide unique**, not course-local) |
| `course_id` | The namespace this exam belongs to; every read also filters on it |
| `learner_id` | User UUID owning the exam; every query is scoped by it |
| `status` | `in_progress`, `submitted`, or `expired` |
| `question_count` | Fixed number of questions drawn at creation |
| `time_limit_seconds` | Optional limit; `NULL` means untimed |
| `option_seed` | Seed keeping each slot's option order stable |
| `created_at` / `started_at` | UTC ISO timestamps (identical at creation) |
| `deadline_at` | `started_at + time_limit_seconds`, or `NULL` when untimed |
| `submitted_at` | Finalization timestamp, if finished |
| `current_position` | Last visited slot, used to resume the exam |
| `correct_count` | Graded score, filled at finalization |
| `duration_seconds` | Elapsed time capped at the limit, filled at finalization |

### `exam_questions`

| Column | Purpose |
|---|---|
| `exam_id` / `position` | Composite primary key fixing the slot order |
| `question_id` | Stable course-local ID; `UNIQUE (exam_id, question_id)` keeps a frozen set deduplicated |
| `selected_answers` | JSON array of the saved selection, `NULL` until answered |
| `is_correct` | Graded outcome, `NULL` until submission |
| `answered_at` | UTC ISO timestamp of the latest save, `NULL` when cleared |
| `grading_fingerprint` | The question's grading identity at creation; detects drifted slots in historical reports, `NULL` for pre-tracking exams |

This table deliberately stores **no `course_id`**: a slot is always reached through its parent session, so the namespace cannot desynchronise. Every slot statement proves parent membership with `EXISTS (… exam_sessions.course_id = ?)`, and the schema carries `FOREIGN KEY (exam_id) REFERENCES exam_sessions(id) ON DELETE RESTRICT`.

The question set is fixed at creation and never re-drawn, so refreshes, reopens, and cross-device resumes all see identical slots. Submission flips `status` with a conditional `UPDATE ... WHERE status = 'in_progress'`, which makes repeated submits no-ops before any attempt or mistake side effects run.

### `question_registry`

| Column | Purpose |
|---|---|
| `course_id` / `question_id` | Composite PRIMARY KEY: the permanent per-course question identity |
| `status` | `active` while in the bank, `retired` after deletion; rows are never removed |
| `question_type` | Grading identity: `single` or `multiple` at last sight |
| `option_ids` | Grading identity: JSON array of the sorted option IDs |
| `correct_answers` | Grading identity: JSON array of the sorted correct option IDs |
| `content_fingerprint` | SHA-256 over every validated content field of the normalized model |
| `placement_fingerprint` | SHA-256 over the filing identity (`source_id` + `chapter_ids`); `NULL` for rows written before placement tracking, which the next startup adopts as the baseline without a generation bump |
| `first_seen_at` / `last_seen_at` | UTC ISO timestamps of first sight and latest registry change |
| `retired_at` | Deletion timestamp, `NULL` while active |

A retired ID is reserved permanently **inside its own course**: the same ID may be an active question in another course, and two courses legitimately hash identical question content.

### `question_bank_state`

| Column | Purpose |
|---|---|
| `course_id` | PRIMARY KEY: one state row per course (the pre-multi-course singleton `id = 1` row migrates into the legacy course) |
| `bank_version` | Raw-bytes SHA-256 of the last loaded question bank **of this course**; diagnostic only |
| `generation` | **This course's** structural bank generation; bumped when its question set changes, a grading identity changes, a question's `chapter_ids`/`source_id` placement changes, or the catalogue shape (sources/chapters added, removed, reordered or re-assigned) changes |
| `catalogue_fingerprint` | Normalized shape of this course's loaded catalogue (source IDs in order, chapter IDs with their source assignment and order); `NULL` for rows written before catalogue tracking, which the next startup adopts as the baseline without a generation bump |

There is deliberately no global generation: a structural publication of course A bumps only A's row, which is why a stale worker fences that course and leaves every other course serving.

### `auth_rate_limits`

| Column | Purpose |
|---|---|
| `scope` / `identifier_hash` | PRIMARY KEY of a fixed-window counter (`login-account`, `login-ip`, `register-ip`); identifiers are SHA-256 hashed, so raw usernames and IP addresses are never stored |
| `window_started_at` / `expires_at` | Unix timestamps bounding the window |
| `attempt_count` | Attempts consumed inside the window |

Throttling is deployment-wide (one account, one IP), not per course.

### Foreign keys and indexes

Foreign keys are deliberately minimal: `course_id` on every course-scoped table, and `exam_questions.exam_id`, reference their parents with `ON DELETE RESTRICT`. There is **no** foreign key from `attempts`/`wrong_questions` to live questions or from `weak_knowledge_points` to live chapters, because historical records must remain valid after the corresponding content is deleted.

`INDEX_STATEMENTS` creates exactly these indexes:

```text
attempts               (learner_id, course_id, question_id, answered_at, id)
attempts               (course_id, answered_at, id)
wrong_questions        (learner_id, course_id, mastered, next_review_at)
weak_knowledge_points  (learner_id, course_id, active)
exam_sessions          (learner_id, course_id, created_at)
exam_sessions          (course_id, status, deadline_at)
```

## 12. Question-Bank Contract

A minimal bilingual question looks like this:

```json
{
  "schema_version": 2,
  "sources": [{"id": "pd2", "title": "Physical Design 2"}],
  "chapters": [
    {"id": "floorplanning", "source_id": "pd2", "title": "Floorplanning", "order": 1},
    {"id": "macro-placement", "source_id": "pd2", "title": "Macro Placement", "order": 2}
  ],
  "questions": [
  {
  "id": "q001",
  "source_id": "pd2",
  "chapter_ids": ["floorplanning", "macro-placement"],
  "section": "Macro placement",
  "pages": [27],
  "text": "Which statements are correct?",
  "text_zh": "哪些陈述是正确的？",
  "type": "multiple",
  "options": [
    {
      "id": "a",
      "text": "First statement",
      "text_zh": "第一项陈述"
    },
    {
      "id": "b",
      "text": "Second statement",
      "text_zh": "第二项陈述"
    }
  ],
  "correct_answers": ["a"],
  "explanation": "English explanation.",
  "explanation_zh": "中文辅助解析。"
  }
  ]
}
```

The `text_zh` and `explanation_zh` fields are optional. English remains the authoritative question content. Option IDs, rather than display positions such as A or B, define the answer.

Question IDs are permanent identities and must remain stable; ordinary bank maintenance (wording, translations, explanations, option text or order, added wrong options, section/pages, formatting) is reconciled per question at startup and never clears learner history. Changing a question's type, its correct-answer set, or removing/renaming an existing option ID changes its grading identity and clears exactly that question's attempts and correction state. Deleting a question keeps its attempts and silently drops its correction/SRS state and review references; the retired ID stays reserved forever, so re-adding the same question restores it, while reusing the ID for a different question fails startup. In a catalogued bank, `chapter_ids` must be a non-empty array of unique chapter IDs belonging to the question's `source_id`; the legacy singular `chapter_id` remains accepted for backward compatibility.

The bundled bank catalogue is maintained as schema v2 metadata directly; new generators should emit `source_id`, `chapter_ids`, `section`, and `pages` from the start.

## 13. End-to-End Request Examples

Every path below is shown in its course-scoped form `/course/<course_id>/…`, which is the canonical registration; the same routes also exist as course-less legacy aliases whose `GET` redirects to the explicit course URL and whose other methods answer `409`. The route layer resolves the course from the URL before any service is called, then opens a `guarded learner transaction` for every write.

### Starting and answering a normal quiz

```text
Browser POST /course/<course_id>/quiz/start
  -> web.py validates the requested size and stable chapter IDs
  -> QuestionRepository resolves eligible IDs before Normal Selection Policy
  -> QuizService validates the eligible-set scope, consumes the coverage bag,
     crosses a cycle boundary without duplicating an ID in the round, and limits IDs
  -> web.py stores the queue, remaining bag, scope signature and shuffle seed in SQLite
  -> redirect to GET /course/<course_id>/quiz
  -> web.py loads the current Question from QuestionRepository
  -> QuizService.order_options() creates a stable option order
  -> quiz.html renders the page

Browser POST /course/<course_id>/quiz/answer
  -> web.py validates shared progress state, answer token, and non-empty selection
  -> QuizService.answer()
  -> GradingService validates and grades option IDs
  -> WrongQuestionService records the Attempt
  -> an incorrect answer updates WrongQuestionRepository
  -> web.py stores feedback and redirects to GET /course/<course_id>/quiz
  -> quiz.html renders correct, missed, and wrong option feedback
```

### Reviewing a mistake

```text
Browser POST /course/<course_id>/review/start
  -> WrongQuestionService returns this user's uncorrected real wrong IDs
  -> QuizService creates persisted original_correction items
  -> if no original is pending, due SRS reviews are queued as srs_review items
  -> if nothing is due either, an active weak chapter supplies one persisted
     transfer_verification item
  -> web.py stores role-bearing Review progress in its own account/mode row

Browser POST /course/<course_id>/review/answer
  -> the answer is graded and persisted
  -> one correct original answer marks that concrete question corrected and
     starts its SRS schedule at level 0, one day later
  -> one correct due srs_review answer advances the schedule one level
     (3/7/15/30 days, capped)
  -> only a correct Review answer adds its question ID once to each active chapter
  -> any wrong answer creates/reopens the concrete wrong row, clears its SRS
     schedule, and resets all chapters
  -> role and chapter progress are stored in feedback; no next candidate is randomized

Browser POST /course/<course_id>/review/next
  -> advances the persisted occurrence and rotates the answer token
  -> only when the existing queue is exhausted, Review Selection Policy chooses
     another pending original, then a due SRS review, then a same-chapter transfer
  -> no candidate plus active <2/2 state produces a finite shortage summary
```

### Running a mock exam

```text
Browser POST /course/<course_id>/exam/start
  -> web.py parses the requested size/time limit
  -> ExamService validates both against fixed allow-lists and the live bank size
  -> ExamRepository persists the session and one frozen, deduplicated slot list
  -> redirect to GET /course/<course_id>/exam/<id> ( refreshes and other devices see the same set )

Browser POST /course/<course_id>/exam/<id>/answer
  -> web.py resolves ownership; ExamService rejects expired/finished exams
  -> GradingService validates option IDs without grading feedback
  -> ExamRepository stores the selection verbatim and the visited position

Browser POST /course/<course_id>/exam/<id>/submit (or any page load after the deadline)
  -> ExamService grades the frozen answers
  -> ExamRepository flips status with a conditional UPDATE (double submits are no-ops)
  -> each answered question is recorded once as a mock_exam Attempt through
     WrongQuestionService, so mistakes rejoin the regular correction/SRS flow
  -> redirect to GET /course/<course_id>/exam/<id>/report with score, chapter breakdown, and
     wrong-question explanations
```

### Dashboard statistics

```text
Browser GET /course/<course_id>/dashboard
  -> ExamService settles any expired exams first (touch-based sweep)
  -> StatisticsService aggregates the learner's retained attempt window:
     totals, accuracy, 7/30-day activity, chapter mastery with coverage and
     threshold statuses, and a zero-filled 7-day trend bucketed by
     display-timezone calendar day
  -> wrong-question counts come from WrongQuestionService, never redefined
```

## 14. Test Architecture

The tests use temporary question/glossary files and temporary SQLite databases, so they do not modify `instance/mcq.db`.

Two rules keep request-level tests honest. First, a `POST` must carry the signed `form_context` a real template renders: `tests/conftest.py` signs one automatically unless a test is deliberately exercising the guard. Second, a test that compares **two independent renders** byte for byte must pass both bodies through `mask_signed_form_context()`: `form_context` is signed with a `TimestampSigner`, so its signature encodes the second it was signed in and the two renders legitimately differ there whenever they straddle a second boundary. Masking exactly that field keeps the comparison strict (everything else still has to match) while removing a flake that would otherwise hide real regressions.

| Test file | Main coverage |
|---|---|
| `tests/conftest.py` | Shared domain objects, valid JSON fixtures, generated course trees, the autouse fixture that signs a `form_context` for tests that do not render a template, and `mask_signed_form_context()` — the helper that blanks the time-stamped signed context before two independent renders are compared byte for byte |
| `tests/test_bundled_glossary.py` | Bundled glossary validity, coverage, scale, aliases, and categories |
| `tests/test_bundled_question_bank.py` | Completeness and quality rules for the real bundled bank |
| `tests/test_course_loader.py` | Manifest discovery, validation, bundle loading, and the worker registry |
| `tests/test_course_isolation.py` | Behavioural namespace separation: two courses reusing the same local question/chapter/source IDs never observe, clear or advance each other's state |
| `tests/test_course_migration.py` | Namespace migration: field-level equivalence, idempotency, and fault injection at all five stages |
| `tests/test_course_scripts.py` | CLI tooling: read-only preflight, `--add`/`--disable`/`--enable`, atomic publish with the bytes frozen once, and `delete_course.py` |
| `tests/test_course_web.py` | Multi-course web behaviour: switching, signed learning forms, stale-form rejection, per-course fencing, readiness, workers |
| `tests/test_form_context.py` | Regression coverage that every rendered learning form signs the operation its own `action` performs; this client sends only the tokens the templates rendered |
| `tests/test_glossary_loader.py` | Glossary schema, normalization, aliases, collisions, and arbitrary categories |
| `tests/test_glossary_repository.py` | Immutable lookup, category ordering, and defensive serialization |
| `tests/test_glossary_web.py` | Authenticated vocabulary UI, highlighting hooks, and glossary/question-bank state separation |
| `tests/test_repositories.py` | SQLite repositories, JSON weak-point persistence, and account-scoped queries |
| `tests/test_question_loader.py` | JSON parsing, validation, and bilingual fields |
| `tests/test_question_bank_sync.py` | Per-question reconciliation: content edits preserve everything, chapter/source moves and catalogue-shape changes bump the generation without clearing data, catalogue labels deliberately do not (both workers keep serving), catalogue baselines are adopted on upgrade, grading changes clear one question, deletions keep attempts but drop state, weak-point/progress/exam reconciliation, resurrection and retired-ID reuse, bootstrap and concurrent startup, pre-deploy check exit codes and bank-level reports |
| `tests/test_stale_worker.py` | Stale-worker fencing: learning pages 503, logout/login/glossary stay reachable, login/register never redirect into the 503, the 503 page cannot loop, readiness versus liveness, and the single generation-mismatch warning |
| `tests/test_grading_service.py` | Exact single/multiple grading and invalid options |
| `tests/test_quiz_service.py` | Limits, coverage cycles/boundaries/scope/all, stable option shuffle, review selection |
| `tests/test_wrong_question_service.py` | Wrong counts, one-answer correction, distinct verification, resets, isolation |
| `tests/test_learning_upgrade.py` | Transfer success/failure, multi-chapter state, policy isolation, resume, old DB/progress, insufficient candidates, UI summaries |
| `tests/test_srs.py` | SRS interval ladder and cap, due boundary inclusivity, UTC/naive handling, schedule persistence, due queries, legacy schema migration, correction/advance/reset state machine, review selection priority |
| `tests/test_srs_web.py` | End-to-end SRS flow through HTTP: correction schedules +1 day, home due entry, srs_review role and badge, level advance, failure returning to correction, per-user isolation, session resume |
| `tests/test_progress_state.py` | Characterization coverage for the progress-state helpers: validation, resume summaries, session keys, and quiz-size parsing |
| `tests/test_progress_sync.py` | Independent clients/workers, resume and completion, concurrency, stale forms, reset, legacy migration, transaction rollback, wording-only bank differences never rewriting progress, two-render comparisons that mask the time-stamped form context |
| `tests/test_web.py` | Public health response, login, registration, page flows, shared progress, duplicate protection, feedback, errors, non-ASCII `answer_token`/CSRF submissions staying a 400 instead of a server error, and the masked two-render comparison that keeps refresh assertions from going flaky |
| `tests/test_write_concurrency.py` | Real threads over one database: eight simultaneous submissions of the same answer grade exactly once, a concurrent replay cannot change the verdict, and six simultaneous exam submissions finalize the exam once |
| `tests/test_view_helpers.py` | Roman-statement stem rendering helper and its template wiring |
| `tests/test_security.py` | Security headers, CSP, CSRF enforcement, login rate limiting, payload limits, and the constant-time token comparison (`tokens_match`) never raising on caller input |
| `tests/test_exam_service.py` | Exam creation/frozen sets, config validation, answer persistence, ownership, grading, idempotent submit, mistake sync, SRS reopening, deadline rules, expiry sweep, reports, history |
| `tests/test_exam_web.py` | Exam pages end to end: no feedback during exams, refresh stability, resume, submission results, locked answers, history links, expiry settlement via home/dashboard visits, legacy attempts-table migration |
| `tests/test_statistics_service.py` | Dashboard aggregation: totals, accuracy, 7/30-day boundaries, chapter mastery bands, low-sample flags, zero-filled trends, display-timezone bucketing, isolation |
| `tests/test_dashboard_web.py` | Dashboard page: login guard, empty state, rendered metrics/mastery/trend, mock-exam reflection, per-user scoping, configured-timezone dates |
| `tests/test_stats_web.py` | Cross-account statistics page: login guard, empty state, rendered metrics and chapter difficulty, group scoping |
| `tests/test_global_statistics_service.py` | Cross-account aggregation: totals, activity windows, chapter-difficulty ranking and bands |
| `tests/test_local_time.py` | Timezone resolution/fallback/errors, conversion helpers, offset labels, tz-aware formatting, startup fail-fast |

Run all tests with:

```bash
pytest
```

## 15. Startup Sequences

### Development

From a clean checkout or folder, create and activate a platform-native virtual environment:

```bash
python -m venv .venv
```

Activate the environment, then run:

```bash
pip install -r requirements.txt
python run.py
```

At startup:

1. Flask configuration is created (secret key, display timezone, course directory, database path) and the course loader is prepared.
2. `instance/mcq.db` and the current tables are created, migrated or extended if needed, then the deployment-wide repositories are built.
3. The course registry loads and fully validates every enabled course's manifest, published question bank and optional glossary, and reconciles each bank per question against the persistent registry. A single broken course becomes `unavailable` and keeps its learner data untouched, while the other courses keep serving.
4. The signed-form serializer, the `/health` and `/ready` endpoints, the security headers and the web blueprint are attached.
5. The Werkzeug development server listens on `http://127.0.0.1:5000` with debug enabled and the reloader disabled.

The first learner creates an account through `/register`, then starts a quiz from the home page.

The Windows `.venv` contains Windows executables and is not reused by WSL. Production uses the separate Linux `.venv-prod` created with `/usr/bin/python3 -m venv .venv-prod` and installs the same `requirements.txt` rather than introducing another dependency-management format.

### Production process startup

1. systemd reads `/etc/systemd/system/mcq-template.service`.
2. systemd reads `MCQ_SECRET_KEY` from the root-owned `/etc/mcq-template/mcq-template.env` without exposing it in the repository.
3. systemd changes to the project root and executes `.venv-prod/bin/gunicorn --config gunicorn.conf.py wsgi:app` as `fangsihan`.
4. `wsgi.py` rejects a missing production secret, calls `create_app()` with debug/testing disabled, and applies one-layer `ProxyFix`.
5. Each of the two Gunicorn workers discovers the course catalogue, validates and loads every enabled course's manifest, published question bank and optional glossary, initializes/checks the SQLite schema, and builds its own immutable per-course services and repositories.
6. Gunicorn listens only on `127.0.0.1:8001`.
7. Nginx listens only on the loopback address (`127.0.0.1:8080` and `[::1]:8080`) and proxies HTTP requests to Gunicorn.

The startup is fail-fast for the deployment and isolated for the content: a missing production secret, an unavailable upstream port or a failed worker boot prevents a healthy Gunicorn service (Nginx then reports 502 rather than silently falling back to the Flask development server), and a *global* catalogue ambiguity — a duplicate `course_id` or an unreadable manifest directory — aborts assembly. A single course whose manifest-declared content is invalid or missing does **not** stop the worker: that course is recorded as `unavailable`, its learner state is left untouched, and the other courses keep serving.

## 16. Important Invariants

Developers should preserve these rules when extending the application:

1. Never trust answer text or display position for grading; always use option IDs.
2. Always scope persistent learning queries to the authenticated user.
3. Store question content in the JSON bank, not in attempt rows.
4. Normal selection never reads wrong-question or weak-knowledge state.
5. Review selection never reads or consumes Normal fairness state.
6. A transfer question is not a wrong question unless the learner actually answers it incorrectly.
7. Knowledge verification counts distinct question IDs, never repeated occurrences.
8. Only correct Review answers advance knowledge-point verification.
9. Any wrong answer creates/reopens the question, activates all its chapters, and resets their verification.
10. A weak chapter completes only after the configured two distinct correct Review IDs.
11. Keep current queues, Review roles, option order, feedback and tokens stable across refresh/resume.
12. Keep all persistent learner state scoped by authenticated `learner_id`.
13. Select against live `QuestionRepository` IDs, never stored question content.
14. Validate the complete question bank before serving any page.
15. Treat English question content as authoritative and Chinese text as an optional learning aid.
16. Keep source/chapter names in the root catalogue; templates and services use stable IDs. A question may reference multiple same-source chapters through `chapter_ids`.
17. Apply curriculum filters before fairness/review selection and size limiting, never only in the browser.
18. Reuse the existing visual language, responsive structures and accessible text/focus patterns.
19. Do not expose Normal fairness as a new user-facing control or algorithm panel.
20. Keep glossary labels unambiguous **inside one course** (cross-course collisions are expected and harmless) and keep glossary content outside SQLite and outside every question-bank fingerprint. Publishing a glossary never advances a generation, never makes a worker stale, and never triggers reconciliation.
21. Keep every course namespace closed under its own `course_id`: repositories are constructed with a required `course_id`, every query is restricted to it, `get_all()`/`list_all()`/`count()`/`distinct_question_ids()` mean "this course", and genuinely cross-course work goes through `CrossCourseQueries` or the migration tooling.
22. Never reinterpret content across courses: a missing, disabled or unavailable course answers 404/503 and is never served another course's questions, chapters, glossary or learner state.
23. Treat the URL's `<course_id>` as the only authority for a request's course; `session["last_course_id"]` is a navigation preference for `/` and for legacy `GET` redirects, and a legacy `POST` without a course is always rejected instead of guessed.
24. Scope every learner row by **learner *and* course**: `(learner_id, course_id, …)` is the namespace of persistent learning state, while `user_id` and `exam_id` stay deployment-wide. Exam slots carry no `course_id` of their own — they always follow their parent session.
25. Treat the persisted `legacy_course_id` as read-only at runtime: ordinary application code never rewrites it. The only supported way to move that namespace is the explicit administrative rename in `scripts/rename_course.py`, which updates the key together with the rows it describes.
26. An uncorrected wrong question never carries an SRS due timestamp; a failed review always returns to the correction flow before being rescheduled at level 0.
27. Keep SRS timestamps as UTC ISO strings, evaluate "due" with `next_review_at <= now` (inclusive), and keep the interval ladder in `srs_service.SRS_INTERVAL_DAYS` rather than scattering numbers across layers.
28. A `question.id` is a permanent identity: never change it for content edits, never recycle a retired ID for a different question **inside the same course**, and never judge question identity from text.
29. Keep the worker fence and each course's structure consistent: the question set, grading identities, a question's `chapter_ids`/`source_id` placement, and the catalogue *shape* advance **that course's** `question_bank_state.generation` (labels never do), a stale worker answers 503 on that course's learning pages until it is updated, other courses keep serving, and `generation` is never hand-edited to bypass the check.
30. Keep failure isolation per course: a broken course is `unavailable` and keeps its learner state untouched, while only a *global* ambiguity (duplicate `course_id`, an unreadable manifest directory) may abort application assembly. Readiness is a monitoring signal and must never fence a healthy course.
31. Never let caller-controlled input raise: a form field (including `csrf_token`/`answer_token`) may hold arbitrary text, so reject it with the documented status code instead of crashing — compare tokens with `app/web/auth.py::tokens_match()`, never with `secrets.compare_digest(str, str)`.

## 17. Common Extension Points

- Add a question field: extend `domain.py`, parse it in `question_loader.py`, then render it in the relevant template.
- Change the reinforcement target: update `KNOWLEDGE_VERIFICATION_TARGET` in `app/__init__.py` and keep Review selection, summaries and shortage behavior consistent.
- Change the spaced-repetition ladder: edit `SRS_INTERVAL_DAYS` in `app/services/srs_service.py`; scheduling, due checks, and the home entry all derive from it.
- Add a persistent learner feature: add repository operations first, then service rules, then route/template integration.
- Add a new page: define its route in `web.py`, create a template extending `base.html`, and add an integration test in `test_web.py`.
- Extend Normal selection: keep it limited to live eligible IDs plus `fairness_scope`/`fairness_remaining_ids`; do not inject review signals.
- Extend Review selection: add role-bearing candidate rules through weak/correction services; do not touch the Normal bag.
- Add a new learning mode: extend `QuizMode`, define its queue and persistence rules, add an independent mode in the progress table, and update the database mode constraint if attempts use the new mode.
- Replace the question bank: put the working copy at `courses/<course_id>/questions_candidate.json` and publish it with `python scripts/publish_course.py --course <course_id>` (the default candidate; an explicit `--questions <path>` also works — it re-runs `scripts/check_question_bank.py` by default; never `cp` over the live file) and then restart **all** workers together. Ordinary maintenance is reconciled per question without clearing learner data; grading-identity changes and deletions affect exactly the involved questions, and chapter/source moves or catalogue-shape changes bump the generation so slicing workers stop serving until the shared restart (labels alone do not).
- Remove a course: never `rm -rf courses/<course_id>`. Use `python scripts/delete_course.py --course <course_id> --dry-run` first, then run it without `--dry-run` so the content directory, `courses` identity, course-scoped learner rows, `question_bank_state`, `question_registry` and the `default_course_id` preference are removed together (`--force` is required while learner data exists, and the database is backed up first).
- Replace a course's **subject**: provide schema-compatible question/glossary files for that course and restart all workers. No Python, HTML, JavaScript, CSS or database change is required; the authoring contracts live in [`QUESTION_GUIDE.md`](QUESTION_GUIDE.md) and [`GLOSSARY_GUIDE.md`](GLOSSARY_GUIDE.md).

### Publishing a new question bank for one course

1. Write the candidate bank to its own file — the default location is `courses/<course_id>/questions_candidate.json`, which the commands below then need no path argument for. Never edit the live file in place.
2. Run `python scripts/check_question_bank.py --course <course_id> --db instance/mcq.db` (add `--strict` in CI to fail on updates that clear learner state, and `--simulate` to run the real reconciliation against a temporary copy of the database; `--published` re-checks the deployed file while a working copy exists). Read the report with the semantics of ["How to read the two bank-level report lines"](#how-to-read-the-two-bank-level-report-lines) above: `catalogue-changed: yes` means that course's workers need the restart, while `presentation-only: no` does not mean the labels stayed identical.
3. Publish atomically: `python scripts/publish_course.py --course <course_id> --questions questions_candidate.json` (or simply `--course <course_id>` when the working copy is the default one). The candidate bytes are read once, validated as-is, written to an immutable `versions/<sha256>/questions.json`, re-validated against the publication baseline inside the course publication lock, and then the manifest is switched over with a single `os.replace()`. Filesystem publication and database activation are **not** one transaction, so the command reports `published, pending worker activation` and never bumps the generation itself.
4. Restart all workers (`systemctl restart mcq-template.service`) and confirm `/ready` and `/ready/<course_id>` answer `200`. A course that is still stale only fences its own pages. See [`COURSE_GUIDE.md`](COURSE_GUIDE.md) for the full operational flow.

### Restoring a database backup

`generation` lives in the database, and each worker compares it with the value it loaded at startup. Restoring an older backup therefore makes the *database* generation differ from a running worker's generation, and that worker keeps answering 503 (with `/ready` reporting `stale`) even though no bank file changed. Treat a restore as a deployment that re-establishes the "database + worker bank state" pair: restore the backup, put the matching content in place (atomically), restart all workers together, then confirm `/ready`. Never forge or hand-edit `generation` to skip this step — the check exists to prevent mixed-bank writes.

### Pre-registry tombstone compatibility

When a database predates `question_registry`, the bootstrap adopts question IDs that only appear in learner tables (attempts, progress rounds, exam slots, weak-point verifications) as retired tombstones **without a grading identity** (`option_ids` empty). If such an ID later reappears in the bank, `sync` cannot prove whether it is the same question and adopts it, so the new question inherits the old ID's attempt/review history — always inside that one course.

This is a one-time tolerance for migration-era data, not a general licence to reuse retired IDs; a tombstone that does record a grading identity still rejects a different question and makes that course `unavailable`. `scripts/check_question_bank.py` prints every `legacy tombstones without grading identity` record so maintainers can see the exposure, and new content should always get a fresh ID.

## 18. Local Production Deployment

The long-running local production deployment runs entirely inside WSL2 Ubuntu:

```text
Internet
  -> Sakura FRP TCP tunnel
  -> Windows localhost:8080
  -> WSL2 Nginx HTTP loopback 127.0.0.1:8080 / [::1]:8080
  -> Gunicorn 127.0.0.1:8001
  -> Flask wsgi:app
```

The path names, the `fangsihan` service account and the `/home/fangsihan/CodeSpace/Python/MCQ_Template` working directory below are **this machine's values**, not general project requirements; a different host substitutes its own account and checkout path. Sakura FRP remains an external transport concern. Nginx terminates plain HTTP on port 8080, but only on the loopback interface — the Windows-side `localhost:8080` forwarding and the tunnel are what make it reachable — and Gunicorn is loopback-only on port 8001. Port 8001 is used because the host rejects binds to 8000 even though neither Windows nor WSL reports a visible listener there. No domain, DNS, TLS, certificate, Nginx `stream` block, Windows startup task, or WSL auto-start behavior is part of this deployment.

### Port and trust boundaries

| Endpoint | Listener | Reachability | Purpose |
|---|---|---|---|
| `127.0.0.1:5000` | Werkzeug | WSL loopback, development only | `python run.py` |
| `127.0.0.1:8001` | Gunicorn | WSL loopback only | Private Nginx upstream |
| `127.0.0.1:8080`, `[::1]:8080` | Nginx | Loopback only; reached from Windows through WSL localhost forwarding | Stable local HTTP endpoint and Sakura FRP target |

Sakura FRP must target TCP `127.0.0.1:8080`. It must never target 8001 directly. Because Sakura FRP carries the HTTP bytes transparently rather than acting as an HTTP reverse proxy, the application trusts exactly one HTTP proxy: Nginx.

`wsgi.py` is the production composition entry point. It requires `MCQ_SECRET_KEY`, forces debug and testing off, and applies Werkzeug `ProxyFix(x_for=1, x_proto=1, x_host=1)`. Nginx supplies `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Host`, and `X-Forwarded-Proto`; larger ProxyFix counts would trust headers from untrusted clients. `run.py` remains the development-only entry point.

### Gunicorn

Gunicorn is configured by `gunicorn.conf.py` with:

- bind `127.0.0.1:8001`;
- two `gthread` workers and four threads per worker;
- request timeout and graceful shutdown timeout of 30 seconds;
- keep-alive of 5 seconds;
- access/error logs directed to stdout/stderr for journald;
- `max_requests=1000` with jitter of 100.

The conservative worker count serves concurrent local requests without creating unnecessary SQLite writers. Each worker has its own question-bank objects, while learner writes converge on the shared SQLite file through a new connection per repository operation.

### systemd supervision

`deploy/mcq-template.service` is this machine's unit file (the `deploy/` directory is git-ignored, so a fresh clone must recreate it); its active copy is `/etc/systemd/system/mcq-template.service`. It:

- runs Gunicorn as the unprivileged `fangsihan` user and group;
- uses the real project root as `WorkingDirectory`;
- loads the required secret from `/etc/mcq-template/mcq-template.env`;
- restarts only after failure, with a five-second delay;
- sends `SIGTERM` and allows 35 seconds for shutdown;
- disables bytecode writes with `PYTHONDONTWRITEBYTECODE=1`;
- applies `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, and `ProtectHome=read-only`;
- grants a write exception only to `/home/fangsihan/CodeSpace/Python/MCQ_Template/instance` for SQLite.

The environment directory is root-owned with mode `0700`, and the environment file is root-owned with mode `0600`. The `instance` directory uses mode `0700`, and `mcq.db` uses mode `0600`. Enabling the Linux unit makes it start when that WSL distribution boots; it does not cause Windows to start WSL.

### Nginx HTTP proxy

Nginx uses `deploy/nginx-mcq-template.conf`, installed as `/etc/nginx/sites-available/mcq-template` and linked from `sites-enabled`. It listens **only on the loopback interface** (`listen 127.0.0.1:8080 default_server;` and `listen [::1]:8080 default_server;`), preserves the incoming HTTP Host, records forwarding metadata, limits request bodies to 2 MiB because the application has no upload feature, applies bounded proxy timeouts, and explicitly denies hidden files, SQLite files, and common backup suffixes. Static files remain served by Flask because they are small and this avoids a second asset-path contract.

The site is a normal Nginx `http`/`server`/`location` reverse proxy; there is no `stream` block. It uses `server_name _`, has no domain dependency, and deliberately configures no HTTPS redirect, TLS certificate, or certificate automation. The default port-80 site is disabled to avoid owning a port outside this deployment.

Project-specific logs are:

- `/var/log/nginx/mcq-template.access.log`;
- `/var/log/nginx/mcq-template.error.log`;
- `journalctl -u mcq-template` for Gunicorn and Flask output.

### Template deployment

The files in the (git-ignored) `deploy/` directory are templates, while `/etc` contains the active system configuration:

```text
deploy/mcq-template.service
  -> /etc/systemd/system/mcq-template.service

deploy/nginx-mcq-template.conf
  -> /etc/nginx/sites-available/mcq-template
  -> /etc/nginx/sites-enabled/mcq-template (symbolic link)
```

Installing or updating the templates requires privileged copy/reload operations:

```bash
sudo install -m 0644 deploy/mcq-template.service \
    /etc/systemd/system/mcq-template.service
sudo systemctl daemon-reload

sudo install -m 0644 deploy/nginx-mcq-template.conf \
    /etc/nginx/sites-available/mcq-template
sudo ln -sfn /etc/nginx/sites-available/mcq-template \
    /etc/nginx/sites-enabled/mcq-template
sudo nginx -t
sudo systemctl restart mcq-template
sudo systemctl reload nginx
```

`nginx -t` must succeed before Nginx is reloaded. The start/stop convenience scripts intentionally do not perform these deployment copies, so editing a repository template alone does not change the active `/etc` configuration.

### Operations and verification

From the WSL project root:

```bash
./scripts/start_production.sh
./scripts/stop_production.sh
```

`start_production.sh` confirms PID 1 is systemd, runs `nginx -t`, starts `mcq-template.service`, starts `nginx.service`, checks both active states, and retries the Nginx readiness URL for up to ten seconds. `stop_production.sh` stops Nginx before Gunicorn and verifies that both are inactive. Non-root execution uses `sudo` for privileged operations.

The unauthenticated liveness and readiness endpoints are available for layered checks:

```bash
curl http://127.0.0.1:8001/health        # Gunicorn -> Flask (process alive)
curl http://127.0.0.1:8001/ready         # Gunicorn -> Flask (may serve learning traffic)
curl http://127.0.0.1:8080/health        # Nginx -> Gunicorn -> Flask
curl http://127.0.0.1:8080/ready         # Nginx -> Gunicorn -> Flask
```

`/health` answers `200` while the process is alive, so it is not sufficient to prove a worker is serving learners. `/ready` answers `503` with

```json
{"status": "stale", "worker_generation": 3, "database_generation": 4}
```

whenever the worker still holds a superseded question bank; restart all workers in that case.

The final host-side acceptance check is:

```powershell
curl.exe http://localhost:8080/health
```

A `200` response containing `{"status":"ok"}` from Windows proves the complete local path required by Sakura FRP.
