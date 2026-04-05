from pathlib import Path

from app.integrations.definitions import SNAPSHOT_PATH


def main() -> None:
    import httpx

    response = httpx.get("https://g4f.dev/docs/ready_to_use.html", timeout=30.0)
    response.raise_for_status()
    SNAPSHOT_PATH.write_text(response.text, encoding="utf-8")
    print(f"Updated {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
