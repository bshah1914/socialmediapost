"""Entry point.

    python run.py                    # web dashboard + scheduler on :5000
    flask --app run cycle            # one cycle now (no scheduler)
    flask --app run ig-login         # first Instagram login
"""

import os
import sys

# Flask CLI commands (cycle, post, ig-login) run once; they must not start the scheduler.
if {"cycle", "post", "ig-login"} & set(sys.argv[1:]):
    os.environ.setdefault("TECHNEWS_CLI", "1")

from technews import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    # use_reloader=False: one process, one scheduler
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False)
