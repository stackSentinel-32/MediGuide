"""Run the offline checks in separate processes so their test services stay isolated."""
import subprocess
import sys
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    for args in (["compileall", "-q", "client", "server", "demo"], ["server.check_providers"], ["server.check_chat"], ["demo", "--check"], ["demo.check_reports"], ["server.benchmark_reports"]):
        subprocess.run([sys.executable, "-m", *args], cwd=root, check=True)
