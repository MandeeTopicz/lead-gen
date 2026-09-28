"""The daily run as a macOS launchd job (LaunchAgent). If the Mac is asleep at the scheduled time, launchd runs
the job once when it wakes. The job runs `leadgen run --trigger cron`, which uses the scheduled-run cap."""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from pipeline.context import Paths

LABEL = "com.leadgen.daily"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist(paths: Paths, hour: int, minute: int, uv: str) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "--project", str(paths.root), "leadgen", "run", "--trigger", "cron"],
        "WorkingDirectory": str(paths.root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(paths.data_dir / "cron.log"),
        "StandardErrorPath": str(paths.data_dir / "cron.log"),
        "EnvironmentVariables": {"PATH": f"{Path(uv).parent}:/usr/bin:/bin:/usr/sbin:/sbin"},
    }


def install(paths: Paths, at: str) -> Path:
    hour, minute = (int(x) for x in at.split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("use a 24-hour time like 07:30")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv isn't on PATH; install it with `brew install uv`")
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)  # replace any earlier schedule
    PLIST.write_bytes(plistlib.dumps(plist(paths, hour, minute, uv)))
    _launchctl("bootstrap", _domain(), str(PLIST))
    return PLIST


def remove() -> bool:
    if not PLIST.exists():
        return False
    _launchctl("bootout", f"{_domain()}/{LABEL}", check=False)
    PLIST.unlink()
    return True


def status() -> str:
    if not PLIST.exists():
        return "not scheduled"
    spec = plistlib.loads(PLIST.read_bytes())
    when = spec["StartCalendarInterval"]
    loaded = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True).returncode == 0
    return (f"daily at {when['Hour']:02d}:{when['Minute']:02d} ({'loaded' if loaded else 'NOT loaded'}), "
            f"log: {spec['StandardOutPath']}")


def _launchctl(*args: str, check: bool = True) -> None:
    subprocess.run(["launchctl", *args], check=check, capture_output=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"
