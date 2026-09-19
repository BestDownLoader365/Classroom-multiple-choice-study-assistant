"""Run the local MCQ practice website (development)."""

import sys

from app import create_app
from app.course_runtime import CourseStatus
from app.models import CourseDefinitionError
from app.services import InvalidTimezoneError

try:
    app = create_app()
except (CourseDefinitionError, InvalidTimezoneError) as exc:
    # Global ambiguities (a duplicate course_id, an unreadable manifest, a bad
    # timezone) are assembly failures: no worker may guess a resolution.
    print(str(exc), file=sys.stderr)
    raise SystemExit(1) from exc

# A broken *single* course is different: the worker still starts, that course is
# reported unavailable, and the other courses keep serving.
registry = app.extensions["mcq_services"].course_registry
for _state in registry.states():
    if _state.status is CourseStatus.UNAVAILABLE:
        print(
            f'警告：课程 "{_state.course_id}" 不可用：{_state.reason}',
            file=sys.stderr,
        )
    elif _state.status is CourseStatus.DISABLED:
        print(f'提示：课程 "{_state.course_id}" 已在课程目录中停用。')


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
