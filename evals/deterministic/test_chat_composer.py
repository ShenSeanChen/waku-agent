"""The shared dashboard chat composer lets the input method handle candidate selection."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="requires Node.js")
def test_enter_sends_only_after_composition():
    render = Path(__file__).resolve().parents[2] / "waku/ops/static/js/render.js"
    script = """
const assert = require("node:assert/strict");
const input = {};
const context = {document: {getElementById: id => id === "dmsg" ? input : null}};
require("node:vm").runInNewContext(require("node:fs").readFileSync(process.argv[1], "utf8"), context);
let sends = 0;
context.sendChat = () => sends++;
context.wireComposer();

function pressEnter(properties) {
  let prevented = false;
  input.onkeydown({key: "Enter", preventDefault() { prevented = true; }, ...properties});
  return prevented;
}

for (const properties of [{isComposing: true}, {isComposing: false, keyCode: 229}, {shiftKey: true}]) {
  assert.equal(pressEnter(properties), false);
  assert.equal(sends, 0);
}
assert.equal(pressEnter({keyCode: 13}), true);
assert.equal(sends, 1);
"""
    subprocess.run(["node", "-e", script, str(render)], check=True)
