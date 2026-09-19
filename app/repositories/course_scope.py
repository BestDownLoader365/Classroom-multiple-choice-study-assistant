"""Course-scope validation shared by every bound repository.

Every learner-persistence repository is constructed with a ``course_id`` that is
required and immutable.  There is deliberately **no** implicit default course:
a missing namespace must be a loud programming error, because silently picking
one would mix two courses' learning history.

All queries in a bound repository are implicitly restricted to that namespace,
so ``get_all()`` means "every account *of this course*", never "the whole
database".  Genuinely cross-course operations live in
:class:`app.repositories.database.CrossCourseQueries` (or in the migration and
CLI tooling) and are therefore always an explicit, reviewable choice.
"""

from app.models import validate_course_id


def require_course_id(course_id: object) -> str:
    """Validate and return a repository's bound ``course_id``.

    Raises :class:`ValueError` when the namespace is missing or malformed, which
    keeps "forgot the course scope" from ever becoming a silent cross-course
    read or write.
    """
    if course_id is None or course_id == "":
        raise ValueError(
            "course_id is required: learner repositories must be bound to "
            "exactly one course namespace."
        )
    return validate_course_id(course_id)
