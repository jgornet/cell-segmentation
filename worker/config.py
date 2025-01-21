import os
import ssl

from celery import Celery


RABBITMQ_URL = os.environ["RABBITMQ_URL"]
REDIS_URL = os.environ["REDIS_URL"]

celery = Celery(
    "tasks",
    broker=RABBITMQ_URL,
    backend=REDIS_URL,
    task_compression="gzip",
    task_track_started=True,
    task_acks_late=False,
    task_acks_on_failure_or_timeout=True,
    worker_cancel_long_running_tasks_on_connection_loss=False,
    worker_prefetch_multiplier=1,
    result_extended=True,
    broker_connection_max_retries=None,
    broker_connection_timeout=72 * 60 * 60,
    task_time_limit=None,
    task_soft_time_limit=None,
    worker_max_tasks_per_child=1,
)

celery.conf.broker_transport_options = {
    "visibility_timeout": 24 * 60 * 60,
    "heartbeat": 1800
}