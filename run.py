"""Run the local MCQ practice website."""

import sys

from app import create_app
from app.repositories import GlossaryError, QuestionBankError

try:
    app = create_app()
except (QuestionBankError, GlossaryError) as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1) from exc


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
