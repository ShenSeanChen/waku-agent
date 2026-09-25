"""Derive hosted/image/seccomp.json from Docker's own default profile.

RUN BY HAND, WITH NETWORK ACCESS. The output is committed and the runtime reads
the committed file; nothing in the deployment ever runs this. Re-run it when
the pinned moby tag moves, and commit the result as its own change.

THE ONE EDIT, and why it is an edit rather than an addition. A tenant owns
their own files under /data. On XFS, a file's owner can move the file into
another project with the FS_IOC_FSSETXATTR ioctl and then write past their own
quota, because the containers share the host's user namespace. Docker's default
profile has defaultAction SCMP_ACT_ERRNO with a long list of ALLOWED syscalls,
and `ioctl` is in one of those lists with no argument condition. In that shape
the rules are ALLOWANCES: an added deny rule for ioctl would not work, because
the unconditional allow wins. So `ioctl` is taken OUT of the unconditional
allow and put back with a condition.

WHAT THE CONDITION DOES NOT CLOSE, AND CANNOT. `SCMP_CMP_NE` compares the FULL
64-BIT REGISTER, and the kernel reads `ioctl`'s request as a 32-bit
`unsigned int`. So a caller who passes `0x1_401c5820` is not equal to
`0x401c5820`, passes the filter, and has the value truncated back to
FS_IOC_FSSETXATTR on the way in. seccomp has no masked not-equal, so there is
no way to express "the low 32 bits are not this" in a profile; the rule below
is the strongest one the format can carry.

This is a residual, not a hole, and the difference is worth stating. It takes
DELIBERATELY CONSTRUCTING an aliased request: every ordinary route to this
ioctl -- `xfs_io`, `chattr`, libc's `ioctl(3)` -- passes the plain value and is
refused. It is on the security checklist as a known residual, and
evals/deterministic/hosted/test_container_template.py asserts the rule that IS
here rather than a stronger one it would be comfortable to claim.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

# Pinned. An unpinned URL means the profile silently changes under the
# deployment the day Docker cuts a release.
MOBY_TAG = "v28.0.4"
URL = f"https://raw.githubusercontent.com/moby/moby/{MOBY_TAG}/profiles/seccomp/default.json"
OUT = Path(__file__).resolve().parent / "seccomp.json"

# FS_IOC_FSSETXATTR = _IOW('X', 32, struct fsxattr)
#   dir  = _IOW               = 1
#   size = sizeof(fsxattr)    = 28   = 0x1c
#   type = 'X'                = 0x58
#   nr   = 32                 = 0x20
#   (1 << 30) | (0x1c << 16) | (0x58 << 8) | 0x20 = 0x401c5820 = 1075599392
FS_IOC_FSSETXATTR = 0x401C5820

COMMENT = (
    "waku: ioctl is allowed EXCEPT FS_IOC_FSSETXATTR (0x401c5820). Without "
    "this, a tenant can move their own file into another XFS project and write "
    "past their disk limit, because the containers share the host's user "
    "namespace. It replaces ioctl's unconditional allow rather than adding a "
    "deny: in a profile whose defaultAction is SCMP_ACT_ERRNO an unconditional "
    "allow always wins."
)


def build(profile: dict) -> dict:
    removed = 0
    for entry in profile["syscalls"]:
        if (entry.get("action") == "SCMP_ACT_ALLOW"
                and "ioctl" in entry.get("names", [])
                and not entry.get("args")):
            entry["names"] = [name for name in entry["names"] if name != "ioctl"]
            removed += 1
    if removed != 1:
        raise SystemExit(
            f"expected `ioctl` in exactly one unconditional allow block of "
            f"moby {MOBY_TAG}'s default profile, found {removed}. The profile's "
            "shape changed; read it before changing this script.")
    profile["syscalls"].append({
        "names": ["ioctl"],
        "action": "SCMP_ACT_ALLOW",
        "args": [{"index": 1, "value": FS_IOC_FSSETXATTR, "op": "SCMP_CMP_NE"}],
        "comment": COMMENT,
        "includes": {},
        "excludes": {},
    })
    return profile


def main() -> int:
    with urllib.request.urlopen(URL, timeout=60) as response:
        profile = json.loads(response.read().decode("utf-8"))
    OUT.write_text(json.dumps(build(profile), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} from moby {MOBY_TAG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
