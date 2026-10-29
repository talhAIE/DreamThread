"""Production registry. Phase 6 will replace the unavailable inference stage."""

from app.workers.types import WorkFailure


class AnalysisHandler:
    async def run(self, execution):
        if await execution.load_checkpoint("prepared") is None:
            await execution.checkpoint(
                "prepared",
                {
                    "revision_id": str(execution.primary.revision_id),
                    "content_hash": execution.primary.content_hash,
                },
            )
        # Deliberate terminal state, not a placeholder interpretation or fake success.
        raise WorkFailure("inference_not_implemented")


def default_handlers():
    return {"analysis": AnalysisHandler()}
