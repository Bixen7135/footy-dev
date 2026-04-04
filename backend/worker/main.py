import argparse
import time
from datetime import datetime, timedelta

from app.database import create_db_and_tables, session_scope
from app.services import JobOrchestrator


SCHEDULES_MINUTES = {
    "normalize_source_catalog_job": 30,
    "sync_source_to_store_job": 30,
    "cleanup_stale_source_products_job": 24 * 60,
    "aggregate_events_job": 60,
    "recompute_recently_viewed_job": 60,
    "recompute_trending_job": 60,
    "recompute_similarity_job": 24 * 60,
    "build_training_dataset_job": 24 * 60,
    "train_ranker_job": 24 * 60,
    "cleanup_old_events_job": 24 * 60,
}


def run_job(name: str) -> None:
    create_db_and_tables()
    orchestrator = JobOrchestrator()
    with session_scope() as session:
        runners = {
            "bootstrap_exports_job": orchestrator.run_bootstrap_exports_job,
            "normalize_source_catalog_job": orchestrator.run_normalize_source_catalog_job,
            "sync_source_to_store_job": orchestrator.run_sync_source_to_store_job,
            "cleanup_stale_source_products_job": orchestrator.run_cleanup_stale_source_products_job,
            "aggregate_events_job": orchestrator.run_aggregate_events_job,
            "recompute_recently_viewed_job": orchestrator.run_recompute_recently_viewed_job,
            "recompute_trending_job": orchestrator.run_recompute_trending_job,
            "recompute_similarity_job": orchestrator.run_recompute_similarity_job,
            "build_training_dataset_job": orchestrator.run_build_training_dataset_job,
            "train_ranker_job": orchestrator.run_train_ranker_job,
            "cleanup_old_events_job": orchestrator.run_cleanup_old_events_job,
        }
        runner = runners.get(name)
        if not runner:
            raise ValueError(f"Unknown job: {name}")
        run = runner(session)
        print(f"[{datetime.utcnow().isoformat()}] {name}: {run.status}")


def run_scheduler(poll_seconds: int = 30) -> None:
    next_runs = {
        job_name: datetime.utcnow() for job_name in SCHEDULES_MINUTES.keys()
    }

    while True:
        now = datetime.utcnow()
        for job_name, due_at in next_runs.items():
            if now >= due_at:
                try:
                    run_job(job_name)
                except Exception as exc:
                    print(f"[{now.isoformat()}] {job_name} failed: {exc}")
                next_runs[job_name] = now + timedelta(minutes=SCHEDULES_MINUTES[job_name])
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="FOOTY worker")
    parser.add_argument("--job", help="Run one job and exit")
    parser.add_argument("--scheduler", action="store_true", help="Run scheduler loop")
    args = parser.parse_args()

    if args.job:
        run_job(args.job)
        return
    if args.scheduler:
        run_scheduler()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
