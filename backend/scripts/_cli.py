"""Exit-code guard shared by the command-line entry points that the scheduler runs.

Without it, an unexpected exception (the database unreachable, a bug) ends the process with Python's default exit
code 1, which the GitHub Actions workflow reads as "partial" (a warning, run stays green), and prints a full
traceback into the public run log. With it, such a failure prints one safe line and exits with UNEXPECTED_ERROR (3),
which every workflow step treats as a failure. Set LOG_LEVEL=DEBUG to also print the traceback when debugging.
"""

import os
import sys
import traceback
from collections.abc import Callable

UNEXPECTED_ERROR = 3


def guarded(main: Callable[[], int], name: str) -> int:
    try:
        return main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - the last line of defence of a scheduled command
        hint = ""
        try:
            from sqlalchemy.exc import DBAPIError, OperationalError

            if isinstance(exc, OperationalError | DBAPIError):
                hint = " The database could not be reached or refused the request; check DATABASE_URL and Supabase."
        except ImportError:  # pragma: no cover
            pass
        print(f"{name} failed unexpectedly ({type(exc).__name__}).{hint} Nothing further was done.", file=sys.stderr)
        if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
            traceback.print_exc()
        return UNEXPECTED_ERROR
