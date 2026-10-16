"""Process supervisor helpers for APEX live traders.

VPS primary path is NSSM (Windows) / systemd with ``Restart=always``.
This module is the in-process fallback: if ``main_loop`` returns or raises
unexpectedly, restart it within ``SUPERVISOR_RESTART_DELAY_SEC`` (capped at
``SUPERVISOR_RESTART_MAX_DELAY_SEC`` so a kill/restart cycle stays under ~60s).
"""

from __future__ import annotations

import time
from typing import Any, Callable

# Match install_service.bat AppThrottle (5s) and stay well under the 60s SLA.
SUPERVISOR_RESTART_DELAY_SEC: float = 5.0
SUPERVISOR_RESTART_MAX_DELAY_SEC: float = 60.0


def run_supervised(
    main_fn: Callable[[], Any],
    *,
    log_fn: Callable[..., Any] | None = None,
    restart_delay_sec: float = SUPERVISOR_RESTART_DELAY_SEC,
    max_delay_sec: float = SUPERVISOR_RESTART_MAX_DELAY_SEC,
) -> None:
    """
    Run ``main_fn``; on unexpected exit/exception, sleep and re-enter.

    ``KeyboardInterrupt`` / ``SystemExit`` propagate (clean stop). Hard kills
    (taskkill / OOM) are handled by the outer NSSM or systemd unit.
    """
    delay = float(restart_delay_sec)
    max_d = float(max_delay_sec)
    while True:
        try:
            main_fn()
            # Clean return from main (should be rare) — treat as stop.
            if log_fn is not None:
                log_fn("info", "[SUPERVISOR] main returned — exiting")
            return
        except KeyboardInterrupt:
            if log_fn is not None:
                log_fn("info", "[SUPERVISOR] interrupted — exiting")
            raise
        except SystemExit:
            raise
        except BaseException as e:  # noqa: BLE001 — must survive any crash
            if log_fn is not None:
                try:
                    log_fn(
                        "critical",
                        "[SUPERVISOR] crash — restarting",
                        error=str(e),
                        restart_in_sec=round(delay, 1),
                    )
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(delay)
            delay = min(max_d, max(float(restart_delay_sec), delay * 2.0))
