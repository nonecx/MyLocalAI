# -*- coding: utf-8 -*-
"""Web lifespan scheduler ownership tests.

The monitor and platform schedulers are process-local daemon threads.  A multi-worker
uvicorn deployment must start them only in the single-worker case so every worker does
not run the same subscription scan.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestWebLifespanSchedulers(unittest.TestCase):
    def _run_lifespan(self, workers: int, platform_started: bool):
        import mylocalai.web.app as webapp

        async def exercise():
            async with webapp._lifespan(webapp.app):
                pass

        scheduler = mock.Mock()
        with mock.patch.object(webapp, "_WORKERS", workers), \
                mock.patch("mylocalai.web.monitor_api.start_scheduler") as monitor_start, \
                mock.patch("mylocalai.web.monitor_api.stop_scheduler") as monitor_stop, \
                mock.patch("mylocalai.web.platform_api.start_scheduler_if_enabled",
                            return_value=platform_started) as platform_start, \
                mock.patch("mylocalai.web.platform_api.get_scheduler",
                            return_value=scheduler) as platform_get:
            asyncio.run(exercise())
        return monitor_start, monitor_stop, platform_start, platform_get, scheduler

    def test_multiworker_does_not_start_or_stop_process_local_schedulers(self):
        """APP_WORKERS>1 must not duplicate monitor or platform scans."""
        monitor_start, monitor_stop, platform_start, platform_get, scheduler = \
            self._run_lifespan(workers=2, platform_started=True)
        monitor_start.assert_not_called()
        monitor_stop.assert_not_called()
        platform_start.assert_not_called()
        platform_get.assert_not_called()
        scheduler.stop.assert_not_called()

    def test_single_worker_stops_only_schedulers_started_by_lifespan(self):
        """Single worker owns both schedulers and stops a platform scheduler it started."""
        monitor_start, monitor_stop, platform_start, platform_get, scheduler = \
            self._run_lifespan(workers=1, platform_started=True)
        monitor_start.assert_called_once_with()
        monitor_stop.assert_called_once_with()
        platform_start.assert_called_once_with()
        platform_get.assert_called_once_with()
        scheduler.stop.assert_called_once_with()

    def test_single_worker_does_not_construct_platform_scheduler_when_disabled(self):
        """Disabled platform scheduling must not construct or stop a lazy scheduler."""
        monitor_start, monitor_stop, platform_start, platform_get, scheduler = \
            self._run_lifespan(workers=1, platform_started=False)
        monitor_start.assert_called_once_with()
        monitor_stop.assert_called_once_with()
        platform_start.assert_called_once_with()
        platform_get.assert_not_called()
        scheduler.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
