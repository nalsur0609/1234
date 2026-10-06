#!/usr/bin/env python3
from pathlib import Path

parts_dir = Path(__file__).with_name("parts")
source = "".join(path.read_text(encoding="utf-8") for path in sorted(parts_dir.glob("*.pyfrag")))
namespace = {"__name__": "__main__", "__file__": str(Path(__file__).resolve())}
exec(compile(source, str(Path(__file__).resolve()), "exec"), namespace, namespace)
