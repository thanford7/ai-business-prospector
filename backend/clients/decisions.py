"""Decisions API relevance gate for crawled articles."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from openai import OpenAI
from openai.types.decision_create_params import (
    Question,
    QuestionQuestionParamPredicate,
    QuestionQuestionParamScore,
    QuestionQuestionParamScoreLevel,
)

from db.models import Industry

DECISIONS_MODEL = "gpt-6-luna"
PREDICATE_CUTOFF = 0.5
MAX_ARTICLE_CHARS = 20_000
POST_STRENGTH_CUTOFF = 2.0
STRENGTH_LEVELS: tuple[QuestionQuestionParamScoreLevel, ...] = (
    {
        "label": "none",
        "description": "Not evidence the company is buying or adopting AI.",
    },
    {"label": "weak", "description": "A passing mention, not a buying motion."},
    {"label": "moderate", "description": "Evaluating, piloting, or implementing."},
    {
        "label": "strong",
        "description": "An active buy, such as an RFP, a vendor selection, or a rollout.",
    },
)


@dataclass(frozen=True)
class ArticleClassification:
    ai_news_probability: float | None
    ai_workflow_probability: float | None
    is_ai_related: bool
    applies_to_all: bool
    industry_probabilities: dict[uuid.UUID, float] = field(default_factory=dict)


def industry_question_name(industry: Industry) -> str:
    return f"industry_{industry.slug}"


def article_decision_input(title: str, body: str) -> str:
    return f"{title}\n\n{body[:MAX_ARTICLE_CHARS]}"


def classification_from_answers(
    answers: Sequence[object],
    industry_ids_by_question: dict[str, uuid.UUID],
) -> ArticleClassification:
    """Map one decisions.create response onto the article gate.

    A refusal, or a missing answer, is below the cutoff. Industry rows are kept
    only for an AI-related article, and only when that industry's predicate is
    at least 0.5. An AI-related article that clears no industry applies to every industry.
    """
    by_name = {getattr(answer, "name", None): answer for answer in answers}
    ai_news = _predicate_probability(by_name.get("ai_news"))
    ai_workflow = _predicate_probability(by_name.get("ai_workflow"))
    is_ai_related = _clears_cutoff(ai_news) or _clears_cutoff(ai_workflow)
    industry_probabilities: dict[uuid.UUID, float] = {}
    if is_ai_related:
        for name, industry_id in industry_ids_by_question.items():
            probability = _predicate_probability(by_name.get(name))
            if probability is not None and probability >= PREDICATE_CUTOFF:
                industry_probabilities[industry_id] = probability
    return ArticleClassification(
        ai_news_probability=ai_news,
        ai_workflow_probability=ai_workflow,
        is_ai_related=is_ai_related,
        applies_to_all=is_ai_related and not industry_probabilities,
        industry_probabilities=industry_probabilities,
    )


class DecisionsArticleGate:
    """One decisions.create call per article, covering the AI gate and every active industry."""

    def __init__(self, client: OpenAI | None = None) -> None:
        self._client = client or OpenAI()

    def classify(
        self, title: str, body: str, industries: Sequence[Industry]
    ) -> ArticleClassification:
        industry_ids_by_question = {
            industry_question_name(industry): industry.id for industry in industries
        }
        questions: list[Question] = [
            QuestionQuestionParamPredicate(
                type="predicate",
                name="ai_news",
                instructions="Is this article about AI news?",
            ),
            QuestionQuestionParamPredicate(
                type="predicate",
                name="ai_workflow",
                instructions="Is this article about AI used in a business workflow?",
            ),
        ]
        for industry in industries:
            questions.append(
                QuestionQuestionParamPredicate(
                    type="predicate",
                    name=industry_question_name(industry),
                    instructions=f"Is this article about {industry.name}?",
                )
            )
        decision = self._client.decisions.create(
            model=DECISIONS_MODEL,
            input=article_decision_input(title, body),
            questions=questions,
        )
        return classification_from_answers(decision.answers, industry_ids_by_question)


def _predicate_probability(answer: object) -> float | None:
    if answer is None or getattr(answer, "type", None) != "predicate":
        return None
    value = getattr(answer, "probability", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clears_cutoff(probability: float | None) -> bool:
    return probability is not None and probability >= PREDICATE_CUTOFF


@dataclass(frozen=True)
class SignalScore:
    strength: float | None
    confidence: float | None
    level_label: str | None


def signal_decision_input(company_name: str, text: str) -> str:
    return f"Company: {company_name}\n\n{text}"


def score_from_answer(answer: object) -> SignalScore:
    """Map the strength answer onto stored signal fields.

    A refusal, or any answer that is not a score, is stored with null strength
    and is not posted. The level label is the highest-probability level. When
    probabilities are missing, the label is the level nearest the score.
    """
    if answer is None or getattr(answer, "type", None) != "score":
        return SignalScore(strength=None, confidence=None, level_label=None)
    raw_score = getattr(answer, "score", None)
    strength = _bounded_float(raw_score, upper=float(len(STRENGTH_LEVELS) - 1))
    confidence = _bounded_float(getattr(answer, "confidence", None), upper=1.0)
    label = _top_level_label(getattr(answer, "probabilities", None), strength)
    return SignalScore(strength=strength, confidence=confidence, level_label=label)


def is_postable_strength(strength: float | None) -> bool:
    return strength is not None and strength >= POST_STRENGTH_CUTOFF


class DecisionsSignalScorer:
    """One decisions.create call per X post, with a single strength score question."""

    def __init__(self, client: OpenAI | None = None) -> None:
        self._client = client or OpenAI()

    def score(self, *, company_name: str, text: str) -> SignalScore:
        decision = self._client.decisions.create(
            model=DECISIONS_MODEL,
            input=signal_decision_input(company_name, text),
            questions=[
                QuestionQuestionParamScore(
                    type="score",
                    name="strength",
                    instructions=(
                        "How strong a buying signal is this post that the company "
                        "is buying or adopting AI?"
                    ),
                    levels=list(STRENGTH_LEVELS),
                )
            ],
        )
        by_name = {getattr(answer, "name", None): answer for answer in decision.answers}
        return score_from_answer(by_name.get("strength"))


def _bounded_float(value: object, *, upper: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number != number:
        return None
    return max(0.0, min(upper, number))


def _top_level_label(probabilities: object, strength: float | None) -> str | None:
    best_label: str | None = None
    best_probability = -1.0
    best_index = -1
    items = (
        probabilities
        if isinstance(probabilities, Sequence)
        and not isinstance(probabilities, (str, bytes))
        else []
    )
    for item in items:
        index = _level_index(item)
        probability = _bounded_float(_item_field(item, "probability"), upper=1.0)
        label = _item_field(item, "label")
        if (
            index is None
            or probability is None
            or not isinstance(label, str)
            or not label.strip()
        ):
            continue
        if probability > best_probability or (
            probability == best_probability and index > best_index
        ):
            best_probability = probability
            best_index = index
            best_label = label.strip()
    if best_label is not None:
        return best_label
    if strength is None:
        return None
    index = min(len(STRENGTH_LEVELS) - 1, max(0, int(strength + 0.5)))
    return STRENGTH_LEVELS[index]["label"]


def _level_index(item: object) -> int | None:
    value = _item_field(item, "value")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        index = int(value)
    except ValueError:
        return None
    if index < 0 or index >= len(STRENGTH_LEVELS):
        return None
    return index


def _item_field(item: object, name: str) -> object:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)
