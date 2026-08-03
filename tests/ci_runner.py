"""Run the unittest suite and surface its traceback as a CI annotation."""

from pathlib import Path
import subprocess
import sys


def annotation_escape(value):
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def main():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    sys.stdout.write(result.stdout)
    if result.returncode:
        details = result.stdout[-12_000:]
        print(
            "::error title=Cross-platform test failure::"
            + annotation_escape(details)
        )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
