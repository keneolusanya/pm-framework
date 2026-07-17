"""Day 4 Block 4: RSS feed — the zero-friction alert channel for anyone else.

Each flagged opportunity is appended as an item to a static RSS 2.0 file served
alongside the dashboard. No account or registration on either end: anyone can
subscribe with any reader, or route it into Discord/Slack/email. This is what
the README points to for "how would someone else get these alerts."
"""
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

FEED_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "feed.xml"
MAX_ITEMS = 100
TITLE = "pm-framework — cross-platform spread opportunities"
LINK = "https://example.com/"  # replaced with the real dashboard URL on Day 6


def _new_feed() -> ET.Element:
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = TITLE
    ET.SubElement(ch, "link").text = LINK
    ET.SubElement(ch, "description").text = (
        "Verified-equivalent prediction-market contracts whose prices diverge "
        "enough to clear fees.")
    return rss


def _load() -> ET.Element:
    if FEED_PATH.exists():
        try:
            return ET.parse(FEED_PATH).getroot()
        except ET.ParseError:
            pass
    return _new_feed()


def append_opportunity(opp: dict) -> None:
    rss = _load()
    channel = rss.find("channel")

    item = ET.Element("item")
    ET.SubElement(item, "title").text = (
        f"{opp['question'][:70]} — spread {opp['spread']:.3f}")
    ET.SubElement(item, "description").text = (
        f"{opp['platform_a']} YES {opp['price_a']:.2f} vs "
        f"{opp['platform_b']} YES {opp['price_b']:.2f}; "
        f"net edge {opp.get('net_edge', 0):.3f} after fees.")
    guid = f"{opp['market_a']}::{opp['market_b']}::{datetime.now(timezone.utc).isoformat()}"
    g = ET.SubElement(item, "guid")
    g.text = guid
    g.set("isPermaLink", "false")
    ET.SubElement(item, "pubDate").text = format_datetime(datetime.now(timezone.utc))

    # Newest first; cap the feed length.
    first_item = channel.find("item")
    if first_item is not None:
        channel.insert(list(channel).index(first_item), item)
    else:
        channel.append(item)
    items = channel.findall("item")
    for extra in items[MAX_ITEMS:]:
        channel.remove(extra)

    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(rss).write(FEED_PATH, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    # Smoke test: append a sample item and report the feed size.
    append_opportunity({
        "market_a": "demo_a", "market_b": "demo_b",
        "question": "Sample: will X happen by year end?",
        "platform_a": "polymarket", "platform_b": "kalshi",
        "price_a": 0.42, "price_b": 0.55, "spread": 0.13, "net_edge": 0.09,
    })
    n = len(ET.parse(FEED_PATH).getroot().find("channel").findall("item"))
    print(f"feed now has {n} item(s) at {FEED_PATH}")
