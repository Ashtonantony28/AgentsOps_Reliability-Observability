from celery import Celery
from celery.schedules import crontab

from app.config import settings


celery = Celery(
    "agentops",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.tasks.anomaly_tasks",
        "app.tasks.alert_tasks",
        "app.tasks.evaluation_tasks",
        "app.tasks.report_tasks",
        "app.tasks.policy_tasks",
    ],
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    # batch policy checks get their own worker, so a backlog of them never delays alerting
    task_routes={"app.tasks.policy_tasks.*": {"queue": "policy"}},
)

celery.conf.beat_schedule = {
    "anomaly-detection-every-5min": {
        "task": "app.tasks.anomaly_tasks.detect_anomalies",
        "schedule": 300.0,
    },
    "evaluate-alert-rules-every-minute": {
        "task": "app.tasks.alert_tasks.evaluate_alert_rules",
        "schedule": 60.0,
    },
    "weekly-digest-monday-8am": {
        "task": "app.tasks.report_tasks.send_weekly_digest",
        "schedule": crontab(day_of_week="mon", hour=8, minute=0),
    },
    "scheduled-evaluations": {
        "task": "app.tasks.evaluation_tasks.run_scheduled_evaluations",
        "schedule": 300.0,
    },
}
