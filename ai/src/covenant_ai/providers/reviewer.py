"""Local installation reviewer, resolved from the Windows process token."""
import csv
import os
import subprocess
from pathlib import Path


def authorized_windows_reviewer() -> str:
    policy = Path(__file__).resolve().parents[3] / ".covenant-reviewer"
    if os.name != "nt" or not policy.is_file():
        raise ValueError("This installation has no enrolled Windows reviewer.")
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)):
        raise OSError("Cannot resolve Windows system directory.")
    identity = subprocess.check_output([str(Path(buffer.value) / "whoami.exe"), "/user", "/fo", "csv", "/nh"])
    sid = next(csv.reader([identity.decode("oem").strip()]))[1]
    if sid != policy.read_text(encoding="ascii").strip():
        raise ValueError("This Windows account is not the enrolled reviewer.")
    return sid


