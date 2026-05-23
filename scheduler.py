"""
Long-running scheduler that generates the brief daily at 07:00 Europe/London.

    python scheduler.py

This stays in the foreground and fires `main.run()` on schedule. It is an
alternative to a system crontab entry (see the README for the crontab option,
which is usually the better choice for an always-on server).

Set RUN_ON_START=1 to also generate a brief immediately when the scheduler
boots (handy for testing; off by default so restarts don't burn API calls).
"""

import os
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import main

LONDON = ZoneInfo("Europe/London")


def job():
    try:
        main.run()
    except SystemExit:
        # main.run() exits if the API key is missing; keep the scheduler alive.
        main.logger.error("Run aborted; scheduler still active.")
    except Exception as exc:  # noqa: BLE001
        main.logger.error("Scheduled run failed: %s", exc)


def start():
    scheduler = BlockingScheduler(timezone=LONDON)
    # Weekdays only would be day_of_week="mon-fri"; daily is fine per the spec.
    scheduler.add_job(job, CronTrigger(hour=7, minute=0, timezone=LONDON))

    if os.getenv("RUN_ON_START") == "1":
        main.logger.info("RUN_ON_START=1 set; generating a brief now.")
        job()

    main.logger.info("Scheduler started. Next brief: daily at 07:00 Europe/London. Ctrl-C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        main.logger.info("Scheduler stopped.")


if __name__ == "__main__":
    start()
