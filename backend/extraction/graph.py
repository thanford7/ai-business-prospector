"""LangGraph graph that extracts company AI use cases from one article."""

import os
from typing import Any, Protocol

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from clients.decisions import MAX_ARTICLE_CHARS

DEFAULT_EXTRACTION_MODEL = "openai:gpt-6-luna"


class ExtractedUseCase(BaseModel):
    company: str = Field(description="Company the article names as using or buying AI")
    summary: str = Field(
        description="What that company is doing with AI, in one or two sentences"
    )
    evidence_quote: str = Field(
        description="A short quote from the article that supports the use case"
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description="Confidence from 0 to 1 that the company and use case were identified correctly",
    )


class ExtractionBatch(BaseModel):
    use_cases: list[ExtractedUseCase] = Field(default_factory=list)


class ExtractionState(TypedDict):
    title: str
    body: str
    industry_name: str
    source_url: str
    use_cases: list[dict]


def extraction_model_name() -> str:
    return os.environ.get("EXTRACTION_MODEL", DEFAULT_EXTRACTION_MODEL)


class StructuredOutputModel(Protocol):
    def with_structured_output(self, schema: type[BaseModel]) -> Any: ...


def compile_extraction_graph(model: StructuredOutputModel) -> Any:
    """Compile the extract node around a chat model that supports structured output."""
    from langgraph.graph import END, START, StateGraph

    structured = model.with_structured_output(ExtractionBatch)

    def extract(state: ExtractionState) -> dict:
        prompt = (
            f"Industry: {state['industry_name']}\n"
            f"Source: {state['source_url']}\n"
            f"Title: {state['title']}\n\n"
            f"{state['body'][:MAX_ARTICLE_CHARS]}\n\n"
            "Extract AI use cases for companies this article actually names in the industry above. "
            "Include a company only when the article describes that company using, buying, piloting, "
            "or evaluating AI. Return an empty list when it names none."
        )
        batch = structured.invoke(prompt)
        if isinstance(batch, dict):
            batch = ExtractionBatch.model_validate(batch)
        return {"use_cases": [item.model_dump() for item in batch.use_cases]}

    builder = StateGraph(ExtractionState)
    builder.add_node("extract", extract)
    builder.add_edge(START, "extract")
    builder.add_edge("extract", END)
    return builder.compile()


def build_extraction_graph(model_name: str | None = None) -> Any:
    from langchain.chat_models import init_chat_model

    model = init_chat_model(model_name or extraction_model_name())
    return compile_extraction_graph(model)


class GraphUseCaseExtractor:
    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def extract(
        self, *, title: str, body: str, industry_name: str, source_url: str
    ) -> list[ExtractedUseCase]:
        result = self._graph.invoke(
            {
                "title": title,
                "body": body,
                "industry_name": industry_name,
                "source_url": source_url,
                "use_cases": [],
            }
        )
        return [ExtractedUseCase.model_validate(item) for item in result["use_cases"]]
