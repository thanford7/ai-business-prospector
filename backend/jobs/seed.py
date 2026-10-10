"""Seed industries the news job can crawl before Slack configuration exists."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Industry

SEED_INDUSTRIES = (
    {
        "name": "Finance",
        "slug": "finance",
        "search_query": "AI use cases in finance",
    },
    {
        "name": "Consumer packaged goods",
        "slug": "consumer-packaged-goods",
        "search_query": "AI use cases in consumer packaged goods",
    },
)


def seed_industries(session: Session) -> list[Industry]:
    """Insert Finance and consumer packaged goods when those slugs are missing.

    An industry a person has already edited is left as stored, including its search query.
    """
    seeded: list[Industry] = []
    for spec in SEED_INDUSTRIES:
        industry = session.scalar(select(Industry).where(Industry.slug == spec["slug"]))
        if industry is None:
            industry = Industry(
                name=spec["name"],
                slug=spec["slug"],
                search_query=spec["search_query"],
                is_active=True,
            )
            session.add(industry)
        seeded.append(industry)
    session.flush()
    return seeded
