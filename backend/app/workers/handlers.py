"""Production single-entry Nemotron extraction/reflection registry."""

from app.services.extraction import ExtractionPipeline


class AnalysisHandler:
    def __init__(self, settings, provider=None):
        self.pipeline = ExtractionPipeline(settings, provider)

    async def run(self, execution):
        return await self.pipeline.run(execution)


def default_handlers(settings):
    return {"analysis": AnalysisHandler(settings)}
