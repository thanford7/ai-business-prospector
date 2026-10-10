"""Slack mrkdwn helpers for digests and modals."""


def flat(value: str) -> str:
    return " ".join(value.split())


def escape_mrkdwn(value: str) -> str:
    return flat(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_link(url: str, label: str) -> str:
    safe_url = url.replace("<", "").replace(">", "").replace("|", "").strip()
    safe_label = escape_mrkdwn(label)[:40] or "link"
    if not safe_url:
        return safe_label
    return f"<{safe_url}|{safe_label}>"


def header_block(text: str) -> dict:
    return {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": (flat(text) or "Prospector")[:150],
            "emoji": False,
        },
    }


def section_block(lines: list[str], *, empty: str) -> dict:
    body = "\n".join(lines).strip() if lines else empty
    if not body:
        body = empty or " "
    kept = lines[:]
    while len(body) > 2900 and len(kept) > 1:
        kept = kept[:-1]
        body = "\n".join(kept) + "\n…"
    if len(body) > 2900:
        body = body[:2899] + "…"
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def divider_block() -> dict:
    return {"type": "divider"}
