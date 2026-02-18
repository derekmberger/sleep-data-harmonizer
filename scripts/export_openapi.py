"""Export the FastAPI-generated OpenAPI spec to a static JSON file."""

import json
from pathlib import Path

from main import app

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.json"


def main():
    spec = app.openapi()
    SPEC_PATH.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"Wrote {SPEC_PATH}")


if __name__ == "__main__":
    main()
