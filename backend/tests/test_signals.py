import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select

from clients.decisions import SignalScore
from clients.x import XPost
from db.models import Company, CompanyOrigin, Signal, SignalSource
from jobs.seed import seed_industries
from jobs.signals import run_signal_job

NOW = datetime(2026, 10, 9, tzinfo=UTC)
POSTED = datetime(2026, 10, 8, tzinfo=UTC)


class RecordingSearch:
    def __init__(self, posts_by_query):
        self.posts_by_query = posts_by_query
        self.calls = []

    def search_recent(self, query, limit):
        self.calls.append((query, limit))
        posts = self.posts_by_query.get(query, [])
        if isinstance(posts, Exception):
            raise posts
        return posts[:limit]


class RecordingScorer:
    def __init__(self, scores_by_text):
        self.scores_by_text = scores_by_text
        self.calls = []

    def score(self, *, company_name, text):
        self.calls.append((company_name, text))
        value = self.scores_by_text[text]
        if isinstance(value, Exception):
            raise value
        return value


def _company(
    industry_id, name, *, rank, handle=None, active=True, origin=CompanyOrigin.news
):
    return Company(
        industry_id=industry_id,
        name=name,
        normalized_name=name.lower(),
        x_handle=handle,
        origin=origin,
        mention_count=1,
        distinct_source_count=1,
        rank_score=rank,
        is_active=active,
    )


def _post(post_id, text):
    return XPost(
        id=post_id,
        text=text,
        author="reporter",
        url=f"https://x.com/reporter/status/{post_id}",
        posted_at=POSTED,
        metadata={"lang": "en"},
    )


def test_signal_job_scores_top_companies_and_stores_postable_rows(session):
    finance = next(
        industry for industry in seed_industries(session) if industry.slug == "finance"
    )
    for existing in session.scalars(
        select(Company).where(Company.industry_id == finance.id)
    ):
        existing.is_active = False
    suffix = uuid.uuid4().hex[:6]
    acme = _company(finance.id, f"Acme {suffix}", rank=0.9, handle="acme")
    globex = _company(finance.id, f"Globex {suffix}", rank=0.2, handle="globex")
    paused = _company(
        finance.id, f"Paused {suffix}", rank=0.99, handle="paused", active=False
    )
    manual = _company(
        finance.id,
        f"Manual {suffix}",
        rank=None,
        handle="manualco",
        origin=CompanyOrigin.manual,
    )
    session.add_all([acme, globex, paused, manual])
    session.flush()
    session.add(
        Signal(
            company_id=acme.id,
            industry_id=finance.id,
            source=SignalSource.x,
            external_id=f"{suffix}-already",
            text="Already scored.",
            author="reporter",
            item_url=f"https://x.com/reporter/status/{suffix}-already",
            posted_at=POSTED,
            strength=3.0,
            confidence=0.9,
            level_label="strong",
            fetched_at=NOW,
        )
    )
    session.flush()

    acme_query = f'("Acme {suffix}" OR @acme) (AI OR pilot OR RFP OR evaluating OR implementing OR vendor) -is:retweet lang:en'
    globex_query = (
        f'("Globex {suffix}" OR @globex) (AI OR pilot OR RFP OR evaluating OR implementing OR vendor) '
        "-is:retweet lang:en"
    )
    search = RecordingSearch(
        {
            acme_query: [
                _post(f"{suffix}-already", "Already scored."),
                _post(f"{suffix}-rfp", "Acme published an AI vendor RFP."),
                _post(f"{suffix}-aside", "Someone mentioned Acme."),
                _post(f"{suffix}-refused", "Unclear post."),
            ],
            globex_query: [
                _post(f"{suffix}-pilot", "Globex is piloting forecasting."),
                _post(f"{suffix}-shared", "Shared post."),
            ],
        }
    )
    scorer = RecordingScorer(
        {
            "Acme published an AI vendor RFP.": SignalScore(2.4, 0.8, "strong"),
            "Someone mentioned Acme.": SignalScore(0.4, 0.7, "none"),
            "Unclear post.": SignalScore(None, None, None),
            "Globex is piloting forecasting.": SignalScore(2.0, 0.6, "moderate"),
            "Shared post.": RuntimeError("decisions down"),
        }
    )

    stats = run_signal_job(
        session,
        finance.id,
        searcher=search,
        scorer=scorer,
        max_companies=2,
        max_posts=4,
        now=NOW,
    )

    assert [call[0] for call in search.calls] == [acme_query, globex_query]
    assert all(limit == 4 for _, limit in search.calls)
    assert "Paused" not in " ".join(call[0] for call in search.calls)
    assert stats.companies_queried == 2
    assert stats.posts_read == 6
    assert stats.skipped_existing == 1
    assert stats.scored == 4
    assert stats.postable == 2
    assert stats.errors == [f"score {suffix}-shared: decisions down"]
    assert [text for _, text in scorer.calls] == [
        "Acme published an AI vendor RFP.",
        "Someone mentioned Acme.",
        "Unclear post.",
        "Globex is piloting forecasting.",
        "Shared post.",
    ]
    assert "Already scored." not in [text for _, text in scorer.calls]

    stored = {
        signal.external_id: signal
        for signal in session.scalars(
            select(Signal).where(Signal.industry_id == finance.id)
        ).all()
        if signal.external_id.startswith(f"{suffix}-")
    }
    assert set(stored) == {
        f"{suffix}-already",
        f"{suffix}-rfp",
        f"{suffix}-aside",
        f"{suffix}-refused",
        f"{suffix}-pilot",
    }
    assert stored[f"{suffix}-rfp"].source == SignalSource.x
    assert stored[f"{suffix}-rfp"].company_id == acme.id
    assert stored[f"{suffix}-rfp"].strength == 2.4
    assert stored[f"{suffix}-rfp"].level_label == "strong"
    assert (
        stored[f"{suffix}-rfp"].item_url
        == f"https://x.com/reporter/status/{suffix}-rfp"
    )
    assert stored[f"{suffix}-rfp"].posted_at == POSTED
    assert stored[f"{suffix}-rfp"].source_metadata == {"lang": "en"}
    assert stored[f"{suffix}-aside"].strength == 0.4
    assert stored[f"{suffix}-refused"].strength is None
    assert stored[f"{suffix}-refused"].confidence is None
    assert stored[f"{suffix}-refused"].level_label is None
    assert stored[f"{suffix}-pilot"].strength == 2.0

    again = RecordingSearch(
        {acme_query: [_post(f"{suffix}-rfp", "Acme published an AI vendor RFP.")]}
    )
    again_scorer = RecordingScorer({})
    second = run_signal_job(
        session,
        finance.id,
        searcher=again,
        scorer=again_scorer,
        max_companies=1,
        max_posts=4,
        now=NOW,
    )
    assert second.skipped_existing == 1
    assert second.scored == 0
    assert again_scorer.calls == []
    assert (
        len(
            session.scalars(
                select(Signal).where(Signal.external_id == f"{suffix}-rfp")
            ).all()
        )
        == 1
    )


def test_signal_job_includes_an_unranked_manual_company_when_cap_allows(session):
    finance = next(
        industry for industry in seed_industries(session) if industry.slug == "finance"
    )
    for existing in session.scalars(
        select(Company).where(Company.industry_id == finance.id)
    ):
        existing.is_active = False
    suffix = uuid.uuid4().hex[:6]
    manual = _company(
        finance.id,
        f"Added {suffix}",
        rank=None,
        handle=None,
        origin=CompanyOrigin.manual,
    )
    session.add(manual)
    session.flush()
    query = (
        f'"Added {suffix}" (AI OR pilot OR RFP OR evaluating OR implementing OR vendor) '
        "-is:retweet lang:en"
    )
    search = RecordingSearch(
        {query: [_post(f"{suffix}-m1", "Added is evaluating AI.")]}
    )
    scorer = RecordingScorer(
        {"Added is evaluating AI.": SignalScore(2.1, 0.5, "moderate")}
    )
    stats = run_signal_job(
        session,
        finance.id,
        searcher=search,
        scorer=scorer,
        max_companies=1,
        max_posts=1,
        now=NOW,
    )
    assert search.calls == [(query, 1)]
    assert stats.postable == 1


def test_signal_job_rejects_a_negative_cap(session):
    finance = next(
        industry for industry in seed_industries(session) if industry.slug == "finance"
    )
    try:
        run_signal_job(
            session,
            finance.id,
            searcher=SimpleNamespace(),
            scorer=SimpleNamespace(),
            max_posts=-1,
        )
    except ValueError as exc:
        assert "max_posts" in str(exc)
    else:
        raise AssertionError("expected ValueError")
