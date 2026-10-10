import uuid
from types import SimpleNamespace
from typing import cast

from openai import OpenAI

from clients.decisions import (
    DECISIONS_MODEL,
    STRENGTH_LEVELS,
    DecisionsArticleGate,
    DecisionsSignalScorer,
    classification_from_answers,
    is_postable_strength,
    score_from_answer,
)
from db.models import Industry


def _predicate(name, probability):
    return SimpleNamespace(type="predicate", name=name, probability=probability)


def _refusal(name):
    return SimpleNamespace(type="refusal", name=name)


def test_refusal_is_below_the_cutoff_and_blocks_the_article():
    finance_id = uuid.uuid4()
    classification = classification_from_answers(
        [
            _refusal("ai_news"),
            _refusal("ai_workflow"),
            _predicate("industry_finance", 0.99),
        ],
        {"industry_finance": finance_id},
    )
    assert classification.ai_news_probability is None
    assert classification.ai_workflow_probability is None
    assert classification.is_ai_related is False
    assert classification.applies_to_all is False
    assert classification.industry_probabilities == {}


def test_ai_article_with_no_industry_applies_to_all():
    finance_id = uuid.uuid4()
    classification = classification_from_answers(
        [
            _predicate("ai_news", 0.8),
            _predicate("ai_workflow", 0.1),
            _predicate("industry_finance", 0.49),
        ],
        {"industry_finance": finance_id},
    )
    assert classification.is_ai_related is True
    assert classification.applies_to_all is True
    assert classification.industry_probabilities == {}


def test_industry_at_cutoff_is_linked():
    finance_id = uuid.uuid4()
    classification = classification_from_answers(
        [
            _predicate("ai_news", 0.5),
            _predicate("ai_workflow", 0.0),
            _predicate("industry_finance", 0.5),
        ],
        {"industry_finance": finance_id},
    )
    assert classification.is_ai_related is True
    assert classification.applies_to_all is False
    assert classification.industry_probabilities == {finance_id: 0.5}


def test_gate_sends_one_call_with_ai_and_industry_predicates():
    calls = []

    class FakeDecisions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                answers=[
                    _predicate("ai_news", 0.2),
                    _predicate("ai_workflow", 0.1),
                    _predicate("industry_finance", 0.1),
                ]
            )

    class FakeOpenAI:
        decisions = FakeDecisions()

    finance = SimpleNamespace(id=uuid.uuid4(), name="Finance", slug="finance")
    gate = DecisionsArticleGate(client=cast(OpenAI, FakeOpenAI()))
    classification = gate.classify(
        "Markets recap", "No models here.", cast(list[Industry], [finance])
    )

    assert classification.is_ai_related is False
    assert calls[0]["model"] == DECISIONS_MODEL
    assert calls[0]["input"].startswith("Markets recap\n\n")
    assert calls[0]["questions"] == [
        {
            "type": "predicate",
            "name": "ai_news",
            "instructions": "Is this article about AI news?",
        },
        {
            "type": "predicate",
            "name": "ai_workflow",
            "instructions": "Is this article about AI used in a business workflow?",
        },
        {
            "type": "predicate",
            "name": "industry_finance",
            "instructions": "Is this article about Finance?",
        },
    ]


def _level(index, label, probability):
    return SimpleNamespace(value=index, label=label, probability=probability)


def test_score_uses_the_weighted_value_and_the_top_level():
    scored = score_from_answer(
        SimpleNamespace(
            type="score",
            name="strength",
            score=2.4,
            confidence=0.66,
            probabilities=[
                _level(0, "none", 0.0),
                _level(1, "weak", 0.1),
                _level(2, "moderate", 0.4),
                _level(3, "strong", 0.5),
            ],
        )
    )
    assert scored.strength == 2.4
    assert scored.confidence == 0.66
    assert scored.level_label == "strong"
    assert is_postable_strength(scored.strength) is True


def test_tied_probabilities_keep_the_higher_level():
    scored = score_from_answer(
        SimpleNamespace(
            type="score",
            score=1.5,
            confidence=0.4,
            probabilities=[_level(1, "weak", 0.5), _level(2, "moderate", 0.5)],
        )
    )
    assert scored.level_label == "moderate"
    assert is_postable_strength(1.99) is False
    assert is_postable_strength(2) is True


def test_refusal_has_no_strength():
    scored = score_from_answer(SimpleNamespace(type="refusal", name="strength"))
    assert scored.strength is None
    assert scored.confidence is None
    assert scored.level_label is None
    assert is_postable_strength(scored.strength) is False


def test_scorer_sends_one_strength_question():
    calls = []

    class FakeDecisions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                answers=[
                    SimpleNamespace(
                        type="score",
                        name="strength",
                        score=1.1,
                        confidence=0.5,
                        probabilities=[],
                    )
                ]
            )

    scorer = DecisionsSignalScorer(
        client=cast(OpenAI, SimpleNamespace(decisions=FakeDecisions()))
    )
    scored = scorer.score(company_name="Acme", text="Acme is piloting a vendor.")

    assert scored.strength == 1.1
    assert scored.level_label == "weak"
    assert calls[0]["model"] == DECISIONS_MODEL
    assert calls[0]["input"] == "Company: Acme\n\nAcme is piloting a vendor."
    assert calls[0]["questions"] == [
        {
            "type": "score",
            "name": "strength",
            "instructions": (
                "How strong a buying signal is this post that the company "
                "is buying or adopting AI?"
            ),
            "levels": [dict(level) for level in STRENGTH_LEVELS],
        }
    ]
