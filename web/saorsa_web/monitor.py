import logging
import time

logger = logging.getLogger(__name__)


def run_monitor(interval=60):
    """Background status monitor loop.

    Polls deployment status on the given interval. Currently a no-op stub.
    """
    logger.info("Starting monitor with interval=%ds", interval)
    while True:
        logger.debug("Monitor tick")
        # TODO: poll deployment statuses and update DB
        time.sleep(interval)
