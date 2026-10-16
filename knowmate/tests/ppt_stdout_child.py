"""Test-owned Python child; never invoked by the product setup helper."""

import sys
import time
from pathlib import Path

STAGES = ("ready", "release_seen", "stdout_before", "stdout_after", "done", "before_return")
CONTROLS = ("normal", "delay_ready", "delay_release", "stop_flush", "stop_return", "missing_release")


def run_child(progress: Path, release: Path, done: Path, control: str = "normal") -> int:
    """Emit only fixed ASCII progress and exercise independently controlled delays."""
    if control not in CONTROLS:
        return 2
    started = time.monotonic()

    def mark(stage: str) -> None:
        with progress.open("a", encoding="ascii", newline="\n") as stream:
            stream.write(f"{stage} {int((time.monotonic() - started) * 1000)}\n")
            stream.flush()

    if control == "delay_ready":
        time.sleep(35)
    mark("ready")
    # Absence of release is a failure, never permission to continue writing.
    deadline = time.monotonic() + 30
    while control == "missing_release" or not release.exists():
        if time.monotonic() >= deadline:
            return 3
        time.sleep(.05)
    if control == "delay_release":
        time.sleep(35)
    mark("release_seen")
    mark("stdout_before")
    sys.stdout.write("{}\n")
    if control == "stop_flush":
        time.sleep(35)
    sys.stdout.flush()
    mark("stdout_after")
    done.touch()
    mark("done")
    mark("before_return")
    if control == "stop_return":
        time.sleep(35)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_child(*(Path(value) for value in sys.argv[1:4]), sys.argv[4]))
