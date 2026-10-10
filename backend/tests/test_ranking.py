from datetime import UTC, datetime, timedelta

from jobs.ranking import prospect_rank_score


def test_recent_multi_source_company_outranks_an_older_single_mention():
    now = datetime(2026, 10, 9, tzinfo=UTC)
    recent = prospect_rank_score(
        last_seen_at=now - timedelta(days=1),
        distinct_source_count=3,
        confidence=0.9,
        now=now,
    )
    older = prospect_rank_score(
        last_seen_at=now - timedelta(days=60),
        distinct_source_count=1,
        confidence=0.9,
        now=now,
    )
    assert recent > older
    assert 0 < recent <= 1
    assert 0 < older < recent
