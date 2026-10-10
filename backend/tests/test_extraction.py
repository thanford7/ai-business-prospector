from extraction.graph import (
    ExtractedUseCase,
    ExtractionBatch,
    GraphUseCaseExtractor,
    compile_extraction_graph,
)


class FakeStructured:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> ExtractionBatch:
        self.prompts.append(prompt)
        return ExtractionBatch(
            use_cases=[
                ExtractedUseCase(
                    company="Acme",
                    summary="Acme is piloting invoice matching.",
                    evidence_quote="Acme is piloting AI to match invoices.",
                    confidence=0.91,
                )
            ]
        )


class FakeModel:
    def __init__(self) -> None:
        self.structured = FakeStructured()

    def with_structured_output(self, schema):
        assert schema is ExtractionBatch
        return self.structured


def test_extraction_graph_returns_structured_use_cases():
    model = FakeModel()
    extractor = GraphUseCaseExtractor(compile_extraction_graph(model))
    use_cases = extractor.extract(
        title="Acme deploys AI",
        body="Acme is piloting AI to match invoices.",
        industry_name="Finance",
        source_url="https://news.example/acme",
    )
    assert use_cases[0].company == "Acme"
    assert use_cases[0].confidence == 0.91
    assert "Industry: Finance" in model.structured.prompts[0]
    assert "https://news.example/acme" in model.structured.prompts[0]
