"""Explicit, non-destructive import/export for a stopped single bot instance."""
import argparse
import json
import os
from pathlib import Path

from src.persistence import ConfigPersistenceError, store_from_env, validate_config


def import_config(store, value):
    validate_config(value)
    result = store.execute(["SET", store.key, json.dumps(value, ensure_ascii=False), "NX"])
    if result not in ("OK", None):
        raise ConfigPersistenceError("Unexpected Redis import response.")
    if store.load() != value:
        raise ConfigPersistenceError("Redis contains different configuration; refusing to overwrite it.")


def export_config(store, path):
    value = store.load()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("import", "export"))
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        store = store_from_env()
        if store is None:
            raise ConfigPersistenceError("Set FEZ_CONFIG_BACKEND=redis for migration.")
        if args.operation == "import":
            with args.file.open(encoding="utf-8") as source:
                value = json.load(source)
            import_config(store, value)
        else:
            export_config(store, args.file)
    except ConfigPersistenceError as error:
        parser.exit(1, f"{error}\n")
    except (OSError, ValueError):
        parser.exit(1, "Cannot read or create the state file (exports never overwrite existing files).\n")
    print(f"Configuration {args.operation} verified.")


if __name__ == "__main__":
    main()
