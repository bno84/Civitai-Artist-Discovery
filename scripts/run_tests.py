"""Run the script-based suite with isolated data and ports, retaining per-test output."""

import argparse
import ast
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def child(name):
    path = ROOT / "tests" / (name + ".py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # Keep the production server (and other projects) on their existing ports.
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PORT"
            for target in node.targets
        ):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                node.value = ast.copy_location(
                    ast.Constant(sock.getsockname()[1]), node.value
                )
    ast.fix_missing_locations(tree)
    spawned = []
    original = subprocess.Popen

    class TrackedPopen(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned.append(self)

    subprocess.Popen = TrackedPopen
    sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]
    sys.argv = [str(path)]
    try:
        exec(
            compile(tree, str(path), "exec"),
            {"__name__": "__main__", "__file__": str(path)},
        )
    finally:
        for process in spawned:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        subprocess.Popen = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if args.child:
        child(args.child)
        return
    names = args.only or [path.stem for path in sorted((ROOT / "tests").glob("*.py"))]
    output = ROOT / "reports" / "full-suite"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for index, name in enumerate(names, 1):
        with tempfile.TemporaryDirectory(
            prefix="civitai-suite-", ignore_cleanup_errors=True
        ) as data:
            env = {
                **os.environ,
                "CIVITAI_HISTORY_DATA_DIR": data,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            }
            started = time.monotonic()
            with (output / (name + ".log")).open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        str(Path(__file__).resolve()),
                        "--child",
                        name,
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    code = process.wait(timeout=args.timeout)
                    state = "passed" if code == 0 else "failed"
                except subprocess.TimeoutExpired:
                    state = "timeout"
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        process.kill()
                    process.wait()
            result = {
                "name": name,
                "state": state,
                "seconds": round(time.monotonic() - started, 1),
            }
            results.append(result)
            (output / "results.json").write_text(
                json.dumps(results, indent=2), encoding="utf-8"
            )
            print(
                f"{index}/{len(names)} {name}: {state} ({result['seconds']}s)",
                flush=True,
            )
    failed = [row["name"] for row in results if row["state"] != "passed"]
    print(
        json.dumps(
            {
                "total": len(results),
                "passed": len(results) - len(failed),
                "failed": failed,
            }
        ),
        flush=True,
    )
    raise SystemExit(bool(failed))


if __name__ == "__main__":
    main()
