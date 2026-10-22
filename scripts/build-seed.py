"""Regenerate the static GitHub Pages catalogue from reviewed sources."""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import reviewed_source_seed  # noqa: E402


def main() -> None:
    target = PROJECT_ROOT / "data" / "suppliers.seed.json"
    target.write_text(
        json.dumps(reviewed_source_seed(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(reviewed_source_seed())} suppliers to {target}")


if __name__ == "__main__":
    main()
