"""A process that refuses to die quietly: it catches BrokenPipeError (what
a closed read end raises) and keeps writing instead of exiting.  Used to
test that run_shell's timeout path reaches even a child that ignores the
ordinary "the pipe closed" signal and requires SIGKILL."""

import sys
import time

while True:
    try:
        sys.stdout.write("x" * 1000 + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        time.sleep(0.01)
        continue
