from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from coredb.reconcile import apply_target


ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("finsights_coredb")
TARGETS = ("postgres", "mysql-node-1", "mysql-node-2")


def load_secrets(path: str | None) -> None:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("secrets file must contain a JSON object")
        for name, value in data.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("secret names and values must be strings")
            os.environ[name] = value
        return

    load_dotenv(ROOT / ".env", override=False)


def load_manifest(target: str) -> dict[str, Any]:
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; choose one of {', '.join(TARGETS)}")
    path = ROOT / target / "config.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError(f"{path.relative_to(ROOT)} must have version: 1")
    if manifest.get("target") != target:
        raise ValueError(f"{path.relative_to(ROOT)} target must be {target!r}")

    for database in manifest.get("databases", []):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database.get("name", "")):
            raise ValueError(f"invalid database name in {path.relative_to(ROOT)}")
        for schema in database.get("schemas", []):
            schema_path = (path.parent / schema).resolve()
            if path.parent.resolve() not in schema_path.parents:
                raise ValueError(f"schema path escapes {path.parent.name}: {schema}")
            if not schema_path.is_file():
                raise ValueError(f"schema file does not exist: {schema_path}")

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile a Finsights database target from YAML/JSON config")
    parser.add_argument("apply", choices=["apply"], help="apply the desired database configuration")
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--secrets-file", help="temporary JSON file with environment-variable secrets")
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("COREDB_LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    load_secrets(args.secrets_file)
    manifest = load_manifest(args.target)
    apply_target(manifest)
    LOG.info("Reconciliation completed for %s", args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
