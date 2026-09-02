"""Runtime-only proxy endpoint overrides.

Harbor job and trial configs preserve the proxy URL used when an experiment was
created.  That value is useful provenance, but a local proxy port is ephemeral:
an in-place resume may need to bind a different free port.  ``run.sh`` exports
the current endpoint through ``WORKBUDDY_RUNTIME_PROXY_URL`` so runtime clients
can rebind without rewriting the recorded Harbor config or lock.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


RUNTIME_PROXY_URL_ENV = "WORKBUDDY_RUNTIME_PROXY_URL"


def runtime_proxy_url(
    recorded_url: str = "",
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the current proxy endpoint, falling back to recorded provenance."""

    values = os.environ if environ is None else environ
    override = str(values.get(RUNTIME_PROXY_URL_ENV) or "").strip()
    return override or str(recorded_url or "").strip()
