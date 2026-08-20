"""Free headline snippets per ticker via Google News RSS search. No API key required."""

import urllib.parse

import feedparser

BASE = "https://news.google.com/rss/search"


def get_recent_headlines(query: str, limit: int = 5) -> list[dict]:
    url = f"{BASE}?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        parsed = feedparser.parse(url)
    except Exception:
        return []

    headlines = []
    for entry in parsed.entries[:limit]:
        headlines.append(
            {
                "title": entry.get("title", ""),
                "published": entry.get("published", ""),
                "source": entry.get("source", {}).get("title", ""),
            }
        )
    return headlines
