import os
import sys
import tempfile
from pathlib import Path

# evals/ sits next to waku/, not inside it — make both importable when
# running `pytest evals` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Every test gets a throwaway home. Without this, a test that builds Settings()
# resolves to the repo's ./.waku (a maintainer's real memory) or to the
# person's real ~/.waku. Set before waku.config is imported, so a .env that
# names WAKU_HOME cannot override it. Tests of the resolution rules pass their
# own env, cwd and user_home.
os.environ["WAKU_HOME"] = tempfile.mkdtemp(prefix="waku-evals-")

# `waku connections` asks Slack about these two tokens (spec 025). Set to
# empty before waku.config loads a .env, which never overrides a variable
# that is set: a maintainer's real tokens never reach Slack from a test.
os.environ["SLACK_APP_TOKEN"] = ""
os.environ["SLACK_BOT_TOKEN"] = ""
