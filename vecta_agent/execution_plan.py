"""Job validation and normalized inputs for the shared execution lifecycle."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from vecta_agent import api, environment, hooks, postgresql

logger = logging.getLogger("vecta_agent")


def is_legacy_file_job(job: dict[str, Any]) -> bool:
    """Identify ordinary files jobs whose report shape predates pipeline stages."""
    return (
        job.get("backup_type", "files") == "files"
        and job.get("pre_hook") is None
        and job.get("post_hook") is None
        and isinstance(job.get("source"), str)
        and bool(job.get("source"))
        and job.get("source_config") is None
        and isinstance(job.get("destination"), str)
        and bool(job.get("destination"))
    )


@dataclass
class ExecutionPlan:
    backup_type: str
    destination: str
    port: int | None
    source: str | None
    source_config: dict[str, Any] | None
    stdin_name: str | None
    excludes: list[str]
    retention_policy: dict[str, int | None] | None
    pre_selection: Any
    post_selection: Any
    job_env: dict[str, str]
    run_env: dict[str, str]


def build_execution_plan(
    client: api.ApiClient,
    job: dict[str, Any],
    restic_env: dict[str, str],
    run_id: str,
    metadata: dict[str, Any],
    *,
    validate_storage_config: Callable[..., Any],
    resolve_job_env: Callable[..., dict[str, str] | None],
) -> ExecutionPlan | None:
    """Validate the fetched job and prepare normalized local execution inputs."""
    backup_type = job.get("backup_type", "files")
    if backup_type not in {"files", "database"}:
        raise ValueError("backup_type must be files or database.")
    excludes, retention_policy = validate_storage_config(job, backup_type)
    destination = job.get("destination")
    if not isinstance(destination, str) or not destination:
        raise ValueError("destination is required.")
    source_config: dict[str, Any] | None = None
    stdin_name: str | None = None
    if backup_type == "files":
        source = job.get("source")
        if not isinstance(source, str) or not source or job.get("source_config") is not None:
            raise ValueError("Filesystem jobs require a source path and no source_config.")
    else:
        if job.get("source") is not None:
            raise ValueError("Database jobs cannot include a filesystem source.")
        source_config = postgresql.validate_source_config(job.get("source_config"))
        stdin_name = postgresql.stdin_filename(source_config)
        metadata["stdin_filename"] = stdin_name
        source = None

    source_description = (
        source
        if backup_type == "files"
        else f"PostgreSQL {source_config['host']}:{source_config['port']}/{source_config['database']}"
    )
    logger.info("Starting job %s: %s -> %s", job.get("job_id"), source_description, destination)
    catalog = hooks.load_catalog()
    pre_selection = hooks.validate_selection(job.get("pre_hook"), catalog, "pre")
    post_selection = hooks.validate_selection(job.get("post_hook"), catalog, "post")
    job_env = resolve_job_env(client, job["job_id"], job, restic_env, run_id, metadata)
    if job_env is None:
        return None
    run_env = dict(job_env)
    if backup_type == "database":
        if not postgresql.probe_postgresql_support():
            raise postgresql.PostgreSQLConfigError(
                "PostgreSQL backups require Restic with stdin-from-command support and pg_dump 9.0 or newer."
            )
        run_env.update(postgresql.child_environment(source_config))
    if not environment.has_repo_password(job_env):
        job_id = job["job_id"]
        raise ValueError(
            "No repository password configured for this destination. "
            f"Run 'vecta-agent setup {job_id}' on this machine to configure it."
        )
    return ExecutionPlan(
        backup_type=backup_type,
        destination=destination,
        port=job.get("port"),
        source=source,
        source_config=source_config,
        stdin_name=stdin_name,
        excludes=excludes,
        retention_policy=retention_policy,
        pre_selection=pre_selection,
        post_selection=post_selection,
        job_env=job_env,
        run_env=run_env,
    )
