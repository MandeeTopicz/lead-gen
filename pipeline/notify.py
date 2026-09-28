"""Desktop notifications, so a halted scheduled run gets your attention."""

import json
import subprocess
import sys


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    script = f"display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
