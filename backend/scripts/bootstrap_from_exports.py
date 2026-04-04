from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.bootstrap import ExportBootstrapService
from app.config import get_settings
from app.database import create_db_and_tables, session_scope
from app.services import JobOrchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time bootstrap from exports/*.csv into working tables")
    parser.add_argument(
        "--skip-post-jobs",
        action="store_true",
        help="Skip normalize/sync jobs after bootstrap",
    )
    args = parser.parse_args()

    settings = get_settings()
    exports_dir = Path(settings.exports_dir)
    report_dir = Path(settings.bootstrap_report_dir)

    create_db_and_tables()
    service = ExportBootstrapService(exports_dir=exports_dir, report_dir=report_dir)
    orchestrator = JobOrchestrator()

    with session_scope() as session:
        report = service.bootstrap(session)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report.get("ok") and not args.skip_post_jobs:
            normalize_run = orchestrator.run_normalize_source_catalog_job(session)
            sync_run = orchestrator.run_sync_source_to_store_job(session)
            print(
                json.dumps(
                    {
                        "post_jobs": {
                            "normalize_source_catalog_job": normalize_run.status,
                            "sync_source_to_store_job": sync_run.status,
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
