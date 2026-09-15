"""Check approved public/synthetic audio against the isolated Windows API."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.windows_audio_smoke import main

if __name__ == "__main__":
    raise SystemExit(main())
