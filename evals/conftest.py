import sys
from pathlib import Path

# evals/ sits next to milli/, not inside it — make both importable when
# running `pytest evals` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
