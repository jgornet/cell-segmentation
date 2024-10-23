import os
import ssl

from celery import Celery

use_ssl = {
    "ssl_keyfile": "/etc/redis/tls/redis.key",
    "ssl_certfile": "/etc/redis/tls/redis.crt",
    "ssl_ca_certs": "/etc/redis/tls/ca.crt",
    "ssl_cert_reqs": ssl.CERT_REQUIRED,
}

REDIS_URL = f"rediss://:@{os.environ['REDIS_HOST']}:{os.environ['REDIS_PORT']}/0"

celery = Celery(
    "tasks",
    broker=REDIS_URL,
    broker_use_ssl=use_ssl,
    backend=REDIS_URL,
    redis_backend_use_ssl=use_ssl,
    task_compression="gzip",
    worker_cancel_long_running_tasks_on_connection_loss=True,  # restart tasks on connection loss
    task_track_started=True,  # by default does not report this granularly
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_extended=True,
)
celery.conf.broker_transport_options = {"visibility_timeout": 24 * 60 * 60}
