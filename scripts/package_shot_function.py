"""Assemble Linux CPython 3.12 wheels and the FC handler; does not invoke FC.

This is packaging, not the M1 stage acceptance test. Native libraries are
copied into an ignored artifact and never installed on the control server.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/fc-artifacts/shot-detection-v1.zip"))
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Refusing to overwrite an existing deployment artifact; choose a new output filename")
    root = Path(__file__).resolve().parents[1]
    source = root / "cloud_functions" / "shot_detection"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fc-shot-package-") as temporary:
        folder = Path(temporary)
        subprocess.run([sys.executable, "-m", "pip", "install", "--target", str(folder), "--only-binary=:all:",
            "--platform", "manylinux_2_28_x86_64", "--platform", "manylinux_2_27_x86_64", "--platform", "manylinux2014_x86_64",
            "--implementation", "cp", "--python-version", "3.12", "--abi", "cp312", "--no-compile",
            "-r", str(source / "requirements.txt")], check=True)
        for name in ("index.py", "detector.py"):
            shutil.copy2(source / name, folder / name)
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for file in sorted(folder.rglob("*")):
                if file.is_file() and "__pycache__" not in file.parts:
                    archive.write(file, file.relative_to(folder).as_posix())
    receipt = {"artifact": output.name, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "size_bytes": output.stat().st_size, "runtime": "python3.12", "platform": "Linux x86_64",
        "dependencies": (source / "requirements.txt").read_text().splitlines(),
        "cloud_deployed": False, "stage_validation_run": False}
    output.with_suffix(".json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
