"""Run from a release checkout without needing an editable installation."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.database_recovery import main  # noqa: E402

if __name__=='__main__':raise SystemExit(main())
