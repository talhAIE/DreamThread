"""Production Nemotron extraction/reflection and continuity registry."""

from app.services.extraction import ExtractionPipeline
from app.services.indexing import EmbeddingHandler
from app.workers.types import AnalysisPayload, Publication


class AnalysisHandler:
    def __init__(self, settings, provider=None):
        self.pipeline = ExtractionPipeline(settings, provider)

    async def run(self, execution):
        return await self.pipeline.run(execution)


class ContinuityHandler:
    def __init__(self, settings, db=None, provider=None):
        self.settings = settings
        self.db = db
        self.provider = provider

    async def run(self, execution):
        from app.services.continuity import ContinuityEngine

        if self.db:
            async with self.db.tenant_transaction(execution.job["user_id"]) as c:
                engine = ContinuityEngine(self.db, self.settings, self.provider)
                await engine.compare(
                    c,
                    execution.job["user_id"],
                    execution.primary.dream_id,
                    record_call=execution.record_call,
                )
        return Publication(AnalysisPayload(), require_evidence=False)


def default_handlers(settings, db=None):
    return {
        "analysis": AnalysisHandler(settings),
        "embedding": EmbeddingHandler(settings),
        "continuity": ContinuityHandler(settings, db=db),
    }
