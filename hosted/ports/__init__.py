"""The four replaceable seams, and only these four.

Each is here because the design names a replacement for it: IdentityVerifier
(Supabase JWKS now, any JWT issuer later, design 4.5), ControlStore (SQLite
now, shared Postgres at more than one VM, 4.2), TenantRuntime (Docker now, the
in-process pool if memory binds, 4.4) and Upstream (Anthropic now).

Nothing else in hosted/ is a seam. A fifth Protocol here means somebody is
abstracting over a choice that has not been made twice.
"""

from hosted.ports.control import ControlStore, Tenant
from hosted.ports.identity import Identity, IdentityVerifier
from hosted.ports.runtime import RunningContainer, TenantRuntime
from hosted.ports.upstream import Upstream

__all__ = ["ControlStore", "Identity", "IdentityVerifier", "RunningContainer",
           "Tenant", "TenantRuntime", "Upstream"]
