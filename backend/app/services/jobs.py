from sqlalchemy import select, update

from app.db.models import tables
from app.errors import APIError
from app.schemas.jobs import JobView
from app.services.accounts import active_account
from app.services.admission import reserve_resource
from app.services.journal import JournalService, pipeline_version

J, D, ART = tables["app.jobs"], tables["app.dreams"], tables["app.job_artifacts"]
ERRORS = {
    "embedding_dimension_mismatch": "Embedding dimensions do not match server configuration.",
    "embedding_version_conflict": "Change EMBEDDING_VERSION before rebuilding with new settings.",
    "provider_not_configured": "Configure the Nebius API key and the model required by this job.",
    "provider_configuration": "Check model IDs, dimensions and verified input prefixes.",
    "provider_input_too_large": "The entry exceeds the configured model context allowance.",
    "inference_not_implemented": "Analysis inference will be available after the inference phase.",
    "provider_auth_failed": "The inference service configuration needs attention.",
    "invalid_output": "The inference response could not be validated.",
    "source_changed": "Source data changed; submit the current dream for analysis.",
    "lease_expired": "The worker stopped before completing this job.",
    "stage_timeout": "Processing exceeded its stage deadline.",
}


class JobService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    async def owned(self, c, user_id, job_id):
        row = (
            (
                await c.execute(
                    select(J).where(J.c.user_id == user_id, J.c.id == job_id).with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise APIError("not_found", "Job not found", 404)
        return row

    async def eligible(self, c, account, job):
        v = job["input_versions"]
        dream = (
            (
                await c.execute(
                    select(D).where(
                        D.c.id == job["payload"].get("dream_id"),
                        D.c.user_id == job["user_id"],
                    )
                )
            )
            .mappings()
            .one_or_none()
            if job["payload"].get("dream_id")
            else None
        )
        return bool(
            job["kind"] == "analysis"
            and job["status"] == "failed"
            and dream
            and dream["deleted_at"] is None
            and dream["lifecycle"] == "saved"
            and str(dream["current_revision_id"]) == job["payload"].get("revision_id")
            and dream["version"] == v.get("dream_version")
            and account["data_epoch"] == job["data_epoch"]
            and account["memory_version"] == v.get("memory_version")
            and v.get("pipeline_version") == pipeline_version(self.settings)
        )

    async def view(self, c, account, job):
        stages = (
            (
                await c.execute(
                    select(ART.c.stage).where(ART.c.job_id == job["id"]).order_by(ART.c.stage)
                )
            )
            .scalars()
            .all()
        )
        return JobView(
            **{
                key: job[key]
                for key in (
                    "id",
                    "kind",
                    "status",
                    "stage",
                    "attempts",
                    "run_after",
                    "error_code",
                    "created_at",
                    "updated_at",
                )
            },
            max_attempts=self.settings.job_max_attempts,
            submission=job["input_versions"].get("submission", 0),
            error_message=(
                ERRORS.get(job["error_code"], "Processing failed; retry if available.")
                if job["error_code"]
                else None
            ),
            retry_allowed=await self.eligible(c, account, job),
            checkpoint_stages=list(stages),
        )

    async def get(self, identity, job_id):
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = (
                await c.execute(
                    select(tables["app.accounts"]).where(
                        tables["app.accounts"].c.id == identity.user_id
                    )
                )
            ).mappings().one_or_none()
            if account is None:
                raise APIError("account_unavailable", "Account is unavailable", 403)
            job = await self.owned(c, identity.user_id, job_id)
            if account["deletion_state"] != "active" and not (
                account["deletion_state"] == "deleting" and job["kind"] == "journal_delete"
            ):
                raise APIError("account_unavailable", "Account is unavailable", 403)
            return await self.view(c, account, job)

    async def retry(self, identity, job_id, payload, key):
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            # Account lock serializes owner operations; lock source before job as in publication.
            raw = (
                (
                    await c.execute(
                        select(J).where(J.c.user_id == identity.user_id, J.c.id == job_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if raw is None:
                raise APIError("not_found", "Job not found", 404)
            source_id = raw["payload"].get("dream_id")
            if source_id:
                await c.execute(select(D.c.id).where(D.c.id == source_id).with_for_update())
            job = await self.owned(c, identity.user_id, job_id)
            admission = await reserve_resource(
                c,
                identity,
                self.settings,
                route="job.retry",
                key=key,
                body={"job_id": str(job_id), **payload.model_dump(mode="json")},
            )
            if not admission.replayed:
                if (
                    not await self.eligible(c, account, job)
                    or job["attempts"] != payload.expected_attempts
                    or job["input_versions"].get("submission") != payload.expected_submission
                ):
                    raise APIError(
                        "job_not_retryable", "Reload the job or analyze the current dream", 409
                    )
                dream = (await c.execute(select(D).where(D.c.id == source_id))).mappings().one()
                job = await JournalService(self.db, self.settings)._queue(
                    c,
                    identity,
                    account,
                    dream,
                    automatic=False,
                )
                keys = tables["app.idempotency_keys"]
                await c.execute(
                    update(keys)
                    .where(keys.c.route == "job.retry", keys.c.key == key)
                    .values(result_resource_id=job_id)
                )
            return await self.view(c, account, job)
