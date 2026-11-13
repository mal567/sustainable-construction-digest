"""Weekly sustainable construction news digest workflow.

Fetches RSS feeds from construction and sustainability outlets plus the site's
own feed at sustainableconstructionreview.com, uses a Mistral model to pick
the relevant stories and write a market briefing, and emails a branded
digest via Amazon SES. Email sending is skipped until the ``SES_SMTP_PASSWORD``
workspace secret is bound to the deployment.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import mistralai.workflows as workflows
from pydantic import BaseModel

SITE_NAME = "Sustainable Construction Review"
SITE_TAGLINE = "News and insight for the low-carbon built environment"
SITE_FEED = "https://sustainableconstructionreview.com/feed/"
DEFAULT_SENDER = "newsletter@sustainableconstructionreview.com"
DEFAULT_RECIPIENT = "newsletter@sustainableconstructionreview.com"
SUMMARY_MODEL = "mistral-small-latest"
MAX_CANDIDATES = 200


def _google_news_url(query: str) -> str:
    from urllib.parse import quote

    return (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=en-GB&gl=GB&ceid=GB:en"
    )


SOURCES: list[dict[str, str]] = [
    {"name": "Construction Dive", "url": "https://www.constructiondive.com/feeds/news/"},
    {"name": "Construction Enquirer", "url": "https://www.constructionenquirer.com/feed/"},
    {"name": "Construction Manager", "url": "https://www.constructionmanagermagazine.com/feed/"},
    {"name": "edie", "url": "https://www.edie.net/feed/"},
    {"name": "GreenBiz", "url": "https://www.greenbiz.com/feed"},
    {"name": "Building Green", "url": "https://www.buildinggreen.com/rss.xml"},
    {"name": "The Construction Index", "url": "https://www.theconstructionindex.co.uk/news/rss"},
    {"name": "Dezeen", "url": "https://www.dezeen.com/feed/"},
    {"name": "New Civil Engineer", "url": "https://www.newcivilengineer.com/feed/"},
    {"name": "Google News · sustainable construction", "url": _google_news_url("sustainable construction")},
    {"name": "Google News · green building", "url": _google_news_url("green building")},
    {"name": "Google News · low carbon concrete", "url": _google_news_url("low carbon concrete")},
    {"name": "Google News · embodied carbon buildings", "url": _google_news_url("embodied carbon buildings")},
    {"name": "Google News · net zero construction", "url": _google_news_url("net zero construction")},
]

SUSTAINABILITY_KEYWORDS: tuple[str, ...] = (
    "sustainable construction",
    "green building",
    "green building council",
    "net zero",
    "net-zero",
    "embodied carbon",
    "low-carbon",
    "low carbon",
    "decarbonis",
    "decarboniz",
    "passive house",
    "passivhaus",
    "retrofit",
    "energy efficiency",
    "breeam",
    "leed",
    "mass timber",
    "cross-laminated",
    "circular economy",
    "green cement",
    "green steel",
    "building performance",
    "construction emissions",
    "carbon capture",
    "heat pump",
    "insulation",
)

SECTION_NAMES: tuple[str, ...] = (
    "Policy & regulation",
    "Green materials",
    "Net-zero & energy",
    "Technology",
    "Projects & case studies",
    "Industry news",
    "Other",
)


class DigestInput(BaseModel):
    dry_run: bool = False
    lookback_hours: int = 168
    max_articles: int = 15
    max_site_posts: int = 5
    recipient: str = DEFAULT_RECIPIENT
    require_approval: bool = False
    approval_timeout_hours: int = 48


class Article(BaseModel):
    title: str
    url: str
    source: str
    published: datetime | None = None
    summary: str = ""
    keyword_match: bool = False
    from_site: bool = False


class EmailResult(BaseModel):
    sent: bool = False
    status: str = ""


class SelectionDecision(BaseModel):
    title: str
    url: str
    source: str
    keyword_match: bool = False
    included: bool = False
    reason: str = ""


class FetchStats(BaseModel):
    total_entries: int = 0
    duplicates_dropped: int = 0
    undated_or_stale_dropped: int = 0
    over_cap_dropped: int = 0
    candidates: int = 0


class FetchResult(BaseModel):
    articles: list[Article] = []
    stats: FetchStats = FetchStats()


class SiteArticlesResult(BaseModel):
    articles: list[Article] = []
    status: str = ""


class SelectionReport(BaseModel):
    stats: FetchStats = FetchStats()
    selected: list[Article] = []
    decisions: list[SelectionDecision] = []


class SectionAssignment(BaseModel):
    index: int
    section: str
    note: str = ""


class DigestOutput(BaseModel):
    date: str
    article_count: int
    digest_markdown: str
    email_result: EmailResult
    selection: SelectionReport | None = None


def _matches_sustainability(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in SUSTAINABILITY_KEYWORDS)


def _strip_html(text: str) -> str:
    import html as html_module
    import re

    stripped = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html_module.unescape(stripped)).strip()


def _entry_title(entry: Any, url: str) -> str:
    title = _strip_html(str(entry.get("title", "")))
    outlet = entry.get("source", {})
    outlet_name = str(outlet.get("title", "")).strip() if outlet else ""
    suffix = f" - {outlet_name}"
    if outlet_name and title.lower().endswith(suffix.lower()):
        title = title[: -len(suffix)].rstrip()
    return title or url


def _entry_text(entry: Any) -> str:
    parts = [_entry_title(entry, ""), _clean_summary(entry)]
    return " ".join(str(part) for part in parts if part)


def _parse_published(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _clean_summary(entry: Any, limit: int = 400) -> str:
    summary = _strip_html(str(entry.get("summary", "")))
    outlet = entry.get("source", {})
    outlet_name = str(outlet.get("title", "")).strip() if outlet else ""
    if outlet_name:
        summary = summary.replace(outlet_name, "").strip().rstrip("-").strip()
    if len(summary) > limit:
        summary = summary[: limit - 3].rstrip() + "..."
    return summary


def select_articles(
    entries_by_source: list[tuple[str, list[Any]]],
    lookback_hours: int,
    max_candidates: int,
    now: datetime | None = None,
    stats: FetchStats | None = None,
) -> list[Article]:
    """Collect fresh feed entries, dedupe, mark keyword matches, rank by recency."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=lookback_hours)
    seen_urls: set[str] = set()
    articles: list[Article] = []
    total_entries = 0
    duplicates = 0
    undated_or_stale = 0
    for source_name, entries in entries_by_source:
        total_entries += len(entries)
        for entry in entries:
            url = str(entry.get("link", "")).strip()
            text = _entry_text(entry)
            published = _parse_published(entry)
            if not url or url in seen_urls:
                duplicates += 1
                continue
            if published is None or published < cutoff:
                undated_or_stale += 1
                continue
            seen_urls.add(url)
            outlet = entry.get("source", {})
            outlet_name = str(outlet.get("title", "")).strip() if outlet else ""
            articles.append(
                Article(
                    title=_entry_title(entry, url),
                    url=url,
                    source=outlet_name or source_name,
                    published=published,
                    summary=_clean_summary(entry),
                    keyword_match=_matches_sustainability(text),
                )
            )
    articles.sort(key=lambda a: a.published, reverse=True)
    selected = articles[:max_candidates]
    if stats is not None:
        stats.total_entries = total_entries
        stats.duplicates_dropped = duplicates
        stats.undated_or_stale_dropped = undated_or_stale
        stats.over_cap_dropped = max(0, len(articles) - len(selected))
        stats.candidates = len(selected)
    return selected


def build_headlines_md(articles: list[Article]) -> str:
    lines = [f"- [{a.title}]({a.url}) — {a.source}" for a in articles]
    return "\n".join(lines)


def build_site_posts_md(posts: list[Article]) -> str:
    lines = []
    for post in posts:
        line = f"- [{post.title}]({post.url})"
        if post.summary:
            line += f" — {post.summary}"
        lines.append(line)
    return "\n".join(lines)


def build_sections_prompt(articles: list[Article]) -> str:
    lines = []
    for index, article in enumerate(articles):
        lines.append(f"{index}. [{article.source}] {article.title}: {article.summary}")
    joined = "\n".join(lines)
    section_list = ", ".join(f'"{name}"' for name in SECTION_NAMES)
    return (
        "Below are this week's sustainable construction and green building news items.\n"
        f"Assign each item to exactly ONE section from: {section_list}.\n"
        "Choose the item's dominant angle, not every theme it touches: government policy, "
        'building standards, and regulation to "Policy & regulation"; materials innovation '
        '(low-carbon concrete, cement, steel, timber, insulation, recycled materials) to '
        '"Green materials"; energy performance, decarbonisation, and building services to '
        '"Net-zero & energy"; construction products, digital tools, and monitoring to '
        '"Technology"; completed or announced buildings and infrastructure to '
        '"Projects & case studies"; business, investment, and people moves to '
        '"Industry news". Use "Other" only when nothing fits.\n'
        "Also write a short note (at most 15 words) for each item describing its significance "
        "for sustainable construction.\n"
        'Respond with strict JSON: {"assignments": [{"index": 0, "section": "Green materials", "note": "..."}]}.\n\n'
        f"{joined}"
    )


def _parse_section_assignments(raw: str, total: int) -> list[SectionAssignment]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    assignments: list[SectionAssignment] = []
    seen: set[int] = set()
    for item in data.get("assignments", []) if isinstance(data, dict) else []:
        try:
            index = int(item["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if index < 0 or index >= total or index in seen:
            continue
        seen.add(index)
        section = str(item.get("section", ""))
        if section not in SECTION_NAMES:
            section = "Other"
        note = str(item.get("note", "")).strip()
        assignments.append(SectionAssignment(index=index, section=section, note=note))
    return assignments


async def _categorize(articles: list[Article]) -> list[SectionAssignment]:
    from mistralai.workflows.client import get_mistral_client

    client = get_mistral_client()
    response = await client.chat.complete_async(
        model=SUMMARY_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a precise news classifier. You respond with strict "
                    "JSON only, no prose."
                ),
            },
            {"role": "user", "content": build_sections_prompt(articles)},
        ],
        temperature=0.0,
        max_tokens=1400,
        response_format={"type": "json_object"},
    )
    return _parse_section_assignments(_extract_digest_text(response), len(articles))


def render_digest(
    articles: list[Article],
    briefing: str,
    date_str: str,
    assignments: list[SectionAssignment] | None = None,
    site_posts: list[Article] | None = None,
) -> str:
    parts = [
        f"# {SITE_NAME} — Weekly Digest — {date_str}",
        f"## Market Briefing\n\n{briefing.strip()}",
    ]
    if site_posts:
        parts.append(f"## From {SITE_NAME}\n\n" + build_site_posts_md(site_posts))
    by_section: dict[str, list[tuple[Article, str]]] = {name: [] for name in SECTION_NAMES}
    assigned_urls: set[str] = set()
    if assignments:
        for assignment in assignments:
            if 0 <= assignment.index < len(articles):
                article = articles[assignment.index]
                if article.url in assigned_urls:
                    continue
                assigned_urls.add(article.url)
                section = assignment.section if assignment.section in SECTION_NAMES else "Other"
                by_section[section].append((article, assignment.note))
    for article in articles:
        if article.from_site or article.url in assigned_urls:
            continue
        by_section["Other"].append((article, ""))
    for name in SECTION_NAMES:
        items = by_section[name]
        if not items:
            continue
        lines = [f"- [{a.title}]({a.url}) — {a.source}" + (f": {note}" if note else "") for a, note in items]
        parts.append(f"## {name}\n\n" + "\n".join(lines))
    return "\n\n".join(parts) + "\n"


def _fetch_feed(url: str) -> bytes:
    import httpx

    response = httpx.get(
        url,
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "sustainable-construction-digest/0.1"},
    )
    response.raise_for_status()
    return response.content


def _parse_feed(content: bytes) -> list[Any]:
    import feedparser

    return list(feedparser.parse(content).entries)


def _extract_digest_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        raise ValueError("model response contained no choices")
    content = choices[0].message.content
    if not content:
        raise ValueError("model response contained no content")
    return str(content)


async def _complete_model_call(prompt: str, max_tokens: int = 800) -> str:
    from mistralai.workflows.client import get_mistral_client

    client = get_mistral_client()
    response = await client.chat.complete_async(
        model=SUMMARY_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a sustainable construction industry analyst. You write "
                    "concise, factual briefings for architects, contractors, developers, "
                    "and sustainability leads in the built environment. Write plain "
                    "markdown. Cite sources with inline markdown links [outlet](url). "
                    "No preamble."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=max_tokens,
    )
    return _extract_digest_text(response)


def build_relevance_prompt(articles: list[Article]) -> str:
    lines = []
    for index, article in enumerate(articles):
        snippet = article.summary[:200] if article.summary else ""
        lines.append(f"{index}. {article.title} ({article.source}) {snippet}".rstrip())
    joined = "\n".join(lines)
    return (
        "You curate news for a sustainable construction and green building "
        "professional. Below are numbered news headlines from the last week. Select "
        "every item relevant to their work: low-carbon and green building materials, "
        "embodied and operational carbon, net-zero buildings, energy efficiency and "
        "retrofit, building standards and certification (BREEAM, LEED, Passivhaus), "
        "sustainable construction technology, circular economy in construction, and "
        "policy or regulation that materially affects sustainable building. Exclude "
        "items with no connection (general property prices, unrelated infrastructure "
        "politics, mining and energy markets, interior design) unless they materially "
        "affect sustainable construction. Return strict JSON: "
        '{"relevant": [<indices>]}. Indices must be integers from the list.\n\n'
        f"{joined}"
    )


def _parse_relevant_indices(raw: str, total: int) -> list[int]:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"model returned no JSON object: {raw[:200]}")
    data = json.loads(raw[start : end + 1])
    indices = data.get("relevant", [])
    result = []
    for index in indices:
        if isinstance(index, int) and 0 <= index < total:
            result.append(index)
    return sorted(set(result))


async def _classify_relevance(articles: list[Article]) -> list[int]:
    from mistralai.workflows.client import get_mistral_client

    client = get_mistral_client()
    response = await client.chat.complete_async(
        model=SUMMARY_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a precise news classifier. You respond with strict "
                    "JSON only, no prose."
                ),
            },
            {"role": "user", "content": build_relevance_prompt(articles)},
        ],
        temperature=0.0,
        max_tokens=800,
        response_format={"type": "json_object"},
    )
    return _parse_relevant_indices(_extract_digest_text(response), len(articles))


def build_briefing_prompt(articles: list[Article], date_str: str) -> str:
    lines = []
    for article in articles:
        published = article.published.strftime("%Y-%m-%d %H:%M UTC") if article.published else "unknown date"
        lines.append(f"- {article.title} ({article.source}, {published}): {article.summary} URL: {article.url}")
    joined = "\n".join(lines)
    return (
        f"Below are sustainable construction and green building news items from the last week ({date_str}). "
        "Write a market briefing as markdown, structured exactly as:\n"
        "1. Two or three paragraphs of prose covering the most significant developments: "
        "policy and building standards changes, materials innovation and carbon reduction, "
        "energy performance, notable projects, investment, and industry moves. Cite sources "
        "inline as markdown links like [edie](https://...) using the item URLs.\n"
        "2. A bold heading **Key developments** followed by 4-6 short bullet points, one fact each.\n"
        "Use only facts present in the items; never invent figures or events. At most 350 words.\n\n"
        f"{joined}"
    )


def select_site_articles(
    entries: list[Any],
    lookback_hours: int,
    max_posts: int,
    now: datetime | None = None,
) -> list[Article]:
    """Turn recent entries from the site's own feed into featured site posts."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=lookback_hours)
    articles: list[Article] = []
    for entry in entries:
        url = str(entry.get("link", "")).strip()
        published = _parse_published(entry)
        if not url or published is None or published < cutoff:
            continue
        articles.append(
            Article(
                title=_entry_title(entry, url),
                url=url,
                source=SITE_NAME,
                published=published,
                summary=_clean_summary(entry, limit=200),
                from_site=True,
            )
        )
    articles.sort(key=lambda a: a.published, reverse=True)
    return articles[:max_posts]


def _smtp_settings(recipient: str) -> dict[str, str] | None:
    password = os.environ.get("SES_SMTP_PASSWORD", "").strip()
    if password:
        defaults = {
            "host": "email-smtp.us-east-1.amazonaws.com",
            "user": DEFAULT_SENDER,
            "sender": DEFAULT_SENDER,
        }
    else:
        password = os.environ.get("ETIK_SMTP_PASSWORD", "").strip()
        if not password:
            return None
        defaults = {
            "host": "mail.infomaniak.com",
            "user": "info@clearbris.com",
            "sender": "info@clearbris.com",
        }
    return {
        "host": os.environ.get("SES_SMTP_HOST", defaults["host"]),
        "port": os.environ.get("SES_SMTP_PORT", "587"),
        "user": os.environ.get("SES_SMTP_USER", defaults["user"]),
        "password": password,
        "sender": os.environ.get("SES_SMTP_FROM", defaults["sender"]),
        "sender_name": os.environ.get("SES_SENDER_NAME", SITE_NAME),
        "configuration_set": os.environ.get("SES_CONFIGURATION_SET", "").strip(),
        "list_unsubscribe_url": os.environ.get("SES_LIST_UNSUBSCRIBE_URL", "").strip(),
    }


def _digest_plain_text(body_markdown: str) -> str:
    import re

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", body_markdown)


def _digest_html(body_markdown: str) -> str:
    import markdown

    body = markdown.markdown(body_markdown, extensions=["sane_lists"])
    return (
        "<!DOCTYPE html>"
        "<html>"
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<style>"
        "body { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;"
        " color: #1a1a1a; line-height: 1.55; margin: 0; padding: 16px;"
        " background-color: #f2f4f1; }"
        ".digest { max-width: 640px; margin: 0 auto; background-color: #ffffff;"
        " padding: 24px 28px; border-radius: 8px; }"
        ".brand { border-bottom: 3px solid #2e7d32; padding-bottom: 12px; margin-bottom: 20px; }"
        ".brand .site { font-size: 20px; font-weight: 700; color: #2e7d32; margin: 0; }"
        ".brand .tagline { font-size: 12px; color: #666666; margin: 4px 0 0; }"
        "h1 { font-size: 21px; margin: 0 0 16px; }"
        "h2 { font-size: 17px; margin: 28px 0 10px; padding-bottom: 6px;"
        " border-bottom: 1px solid #dddddd; }"
        "h3 { font-size: 15px; margin: 20px 0 8px; }"
        "h4 { font-size: 14px; margin: 18px 0 6px; color: #444444; }"
        "ul { padding-left: 20px; margin: 8px 0; }"
        "li { margin-bottom: 10px; }"
        "p { margin: 8px 0; }"
        "a { color: #2e7d32; text-decoration: underline; }"
        "hr { border: none; border-top: 1px solid #dddddd; margin: 20px 0; }"
        ".footer { font-size: 12px; color: #777777; margin-top: 24px;"
        " border-top: 1px solid #dddddd; padding-top: 12px; }"
        ".footer a { color: #777777; }"
        "</style>"
        "</head>"
        "<body>"
        f'<div class="digest"><div class="brand"><p class="site">{SITE_NAME}</p>'
        f'<p class="tagline">{SITE_TAGLINE}</p></div>{body}'
        '<div class="footer">You are receiving this because you subscribed to '
        f"{SITE_NAME}.</div></div>"
        "</body>"
        "</html>"
    )


def _send_email(settings: dict[str, str], recipient: str, subject: str, body_markdown: str) -> None:
    import smtplib
    from email.message import EmailMessage
    from email.utils import formataddr

    message = EmailMessage()
    message["From"] = formataddr((settings["sender_name"], settings["sender"]))
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(_digest_plain_text(body_markdown))
    message.add_alternative(_digest_html(body_markdown), subtype="html")
    if settings.get("configuration_set"):
        message["X-SES-CONFIGURATION-SET"] = settings["configuration_set"]
    if settings.get("list_unsubscribe_url"):
        message["List-Unsubscribe"] = f"<{settings['list_unsubscribe_url']}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    port = int(settings["port"])
    with smtplib.SMTP(settings["host"], port, timeout=30) as server:
        server.starttls()
        server.login(settings["user"], settings["password"])
        server.send_message(message)


@workflows.activity(start_to_close_timeout=timedelta(seconds=240), retry_policy_max_attempts=3)
async def fetch_articles(lookback_hours: int, max_candidates: int = MAX_CANDIDATES) -> FetchResult:
    """Fetch all configured feeds and return fresh candidate articles with fetch stats."""
    entries_by_source: list[tuple[str, list[Any]]] = []
    for source in SOURCES:
        try:
            entries_by_source.append((source["name"], _parse_feed(_fetch_feed(source["url"]))))
        except Exception as error:
            print(f"feed fetch failed for {source['name']}: {error}")
    stats = FetchStats()
    articles = select_articles(entries_by_source, lookback_hours, max_candidates, stats=stats)
    return FetchResult(articles=articles, stats=stats)


@workflows.activity(start_to_close_timeout=timedelta(seconds=60), retry_policy_max_attempts=1)
async def fetch_site_articles(max_posts: int, lookback_hours: int) -> SiteArticlesResult:
    """Fetch recent posts from the site's own RSS feed; failures never break the run."""
    try:
        entries = _parse_feed(_fetch_feed(SITE_FEED))
        posts = select_site_articles(entries, lookback_hours, max_posts)
        return SiteArticlesResult(articles=posts, status=f"fetched {len(posts)} posts")
    except Exception as error:
        print(f"site feed fetch failed: {error}")
        return SiteArticlesResult(articles=[], status=f"unavailable: {error}")


@workflows.activity(start_to_close_timeout=timedelta(seconds=240), retry_policy_max_attempts=2)
async def filter_relevant_articles(fetch_result: FetchResult, max_articles: int) -> SelectionReport:
    """Keep keyword matches plus model-selected relevant items, with per-article decisions."""
    articles = fetch_result.articles
    decisions: list[SelectionDecision] = []

    def decision_for(article: Article, included: bool, reason: str) -> SelectionDecision:
        return SelectionDecision(
            title=article.title,
            url=article.url,
            source=article.source,
            keyword_match=article.keyword_match,
            included=included,
            reason=reason,
        )

    keyword_matches = [a for a in articles if a.keyword_match]
    candidates = [a for a in articles if not a.keyword_match]
    for article in keyword_matches:
        decisions.append(decision_for(article, True, "keyword match"))
    if candidates:
        try:
            indices = await _classify_relevance(candidates)
        except Exception as error:
            print(f"relevance classification failed, keeping keyword matches only: {error}")
            for article in candidates:
                decisions.append(decision_for(article, False, "cut: classifier unavailable"))
            selected = keyword_matches[:max_articles]
            return SelectionReport(stats=fetch_result.stats, selected=selected, decisions=decisions)
        model_picks = [candidates[i] for i in indices]
        picked = {candidates[i].url for i in indices}
        for article in candidates:
            if article.url in picked:
                decisions.append(decision_for(article, True, "model relevance"))
            else:
                decisions.append(decision_for(article, False, "model: not relevant"))
    else:
        model_picks = []
    selected = keyword_matches + model_picks
    selected.sort(key=lambda a: a.published or datetime.min.replace(tzinfo=UTC), reverse=True)
    kept = selected[:max_articles]
    kept_urls = {a.url for a in kept}
    for article in selected:
        if article.url not in kept_urls:
            decisions.append(decision_for(article, False, "cut: beyond max_articles cap"))
    return SelectionReport(stats=fetch_result.stats, selected=kept, decisions=decisions)


@workflows.activity(start_to_close_timeout=timedelta(seconds=240), retry_policy_max_attempts=2)
async def market_briefing(articles: list[Article], date_str: str) -> str:
    """Write the hyperlinked market briefing that leads the digest."""
    if not articles:
        return ""
    return await _complete_model_call(build_briefing_prompt(articles, date_str), max_tokens=1000)


@workflows.activity(start_to_close_timeout=timedelta(seconds=240), retry_policy_max_attempts=2)
async def categorize_articles(articles: list[Article]) -> list[SectionAssignment]:
    """Assign each selected article to exactly one digest section."""
    if not articles:
        return []
    try:
        return await _categorize(articles)
    except Exception as error:
        print(f"section categorization failed, falling back to Other: {error}")
        return []


@workflows.activity(start_to_close_timeout=timedelta(seconds=10))
async def today_str() -> str:
    """Current UTC date, as a deterministic activity for the workflow sandbox."""
    return datetime.now(UTC).strftime("%d %B %Y")


@workflows.activity(start_to_close_timeout=timedelta(seconds=90), retry_policy_max_attempts=2)
async def send_digest_email(recipient: str, subject: str, body_markdown: str) -> EmailResult:
    """Send the digest via Amazon SES SMTP; skipped when no SMTP password is configured."""
    settings = _smtp_settings(recipient)
    if settings is None:
        print("SES_SMTP_PASSWORD is not set; skipping email delivery")
        return EmailResult(sent=False, status="skipped: SES_SMTP_PASSWORD not configured")
    _send_email(settings, recipient, subject, body_markdown)
    return EmailResult(sent=True, status=f"sent to {recipient}")


@workflows.workflow.define(
    name="sustainable-construction-digest",
    workflow_display_name="Sustainable Construction Digest",
    workflow_description="Weekly digest of sustainable construction news plus site posts, sent via Amazon SES after optional morning review approval.",
)
class SustainableConstructionDigestWorkflow:
    def __init__(self) -> None:
        self.approved = False
        self.rejected = False
        self.pending_digest = ""
        self.selection_report: SelectionReport | None = None

    @workflows.workflow.signal(name="approve")
    async def approve(self) -> None:
        self.approved = True

    @workflows.workflow.signal(name="reject")
    async def reject(self) -> None:
        self.rejected = True

    @workflows.workflow.query(name="pending_digest")
    def pending_digest_query(self) -> str:
        return self.pending_digest

    @workflows.workflow.query(name="selection_report")
    def selection_report_query(self) -> str:
        return self.selection_report.model_dump_json() if self.selection_report else ""

    async def _await_review(self, timeout_hours: int) -> EmailResult:
        import asyncio

        from mistralai.workflows import workflow as workflow_ns

        try:
            await workflow_ns.wait_condition(
                lambda: self.approved or self.rejected,
                timeout=timedelta(hours=timeout_hours),
            )
        except asyncio.TimeoutError:
            return EmailResult(sent=False, status="skipped: approval timed out")
        if self.rejected and not self.approved:
            return EmailResult(sent=False, status="skipped: rejected by reviewer")
        return EmailResult(sent=False, status="approved")

    @workflows.workflow.entrypoint
    async def run(self, input: DigestInput) -> DigestOutput:
        date_str = await today_str()
        fetch_result = await fetch_articles(input.lookback_hours)
        site_posts_result = await fetch_site_articles(input.max_site_posts, input.lookback_hours)
        if not fetch_result.articles and not site_posts_result.articles:
            empty_digest = (
                f"# {SITE_NAME} — Weekly Digest — {date_str}\n\n"
                f"No news candidates found in the last {input.lookback_hours} hours.\n"
            )
            email_result = EmailResult(sent=False, status="skipped: no articles")
            if not input.dry_run:
                email_result = await send_digest_email(
                    input.recipient,
                    f"{SITE_NAME} Weekly Digest — {date_str} (no news)",
                    empty_digest,
                )
            return DigestOutput(
                date=date_str,
                article_count=0,
                digest_markdown=empty_digest,
                email_result=email_result,
            )

        report = await filter_relevant_articles(fetch_result, input.max_articles)
        articles = report.selected
        self.selection_report = report
        if not articles and not site_posts_result.articles:
            empty_digest = (
                f"# {SITE_NAME} — Weekly Digest — {date_str}\n\n"
                f"No relevant news found in the last {input.lookback_hours} hours.\n"
            )
            email_result = EmailResult(sent=False, status="skipped: no relevant articles")
            if not input.dry_run:
                email_result = await send_digest_email(
                    input.recipient,
                    f"{SITE_NAME} Weekly Digest — {date_str} (no news)",
                    empty_digest,
                )
            return DigestOutput(
                date=date_str,
                article_count=0,
                digest_markdown=empty_digest,
                email_result=email_result,
            )

        briefing = await market_briefing(articles, date_str)
        assignments = await categorize_articles(articles)
        digest_markdown = render_digest(
            articles,
            briefing,
            date_str,
            assignments,
            site_posts_result.articles,
        )
        self.pending_digest = digest_markdown
        if input.dry_run:
            email_result = EmailResult(sent=False, status="skipped: dry run")
        elif input.require_approval:
            review = await self._await_review(input.approval_timeout_hours)
            if review.status != "approved":
                email_result = review
            else:
                email_result = await send_digest_email(
                    input.recipient,
                    f"{SITE_NAME} Weekly Digest — {date_str}",
                    digest_markdown,
                )
        else:
            email_result = await send_digest_email(
                input.recipient,
                f"{SITE_NAME} Weekly Digest — {date_str}",
                digest_markdown,
            )
        return DigestOutput(
            date=date_str,
            article_count=len(articles),
            digest_markdown=digest_markdown,
            selection=report,
            email_result=email_result,
        )
