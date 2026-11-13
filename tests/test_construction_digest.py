from datetime import UTC, datetime, timedelta

import pytest

from worker.workflows import construction_digest
from worker.workflows.construction_digest import (
    Article,
    DigestInput,
    SitePost,
    build_headlines_md,
    build_relevance_prompt,
    build_briefing_prompt,
    build_sections_prompt,
    build_site_posts_md,
    render_digest,
    select_articles,
)


def _entry(title: str, link: str, summary: str = "", published=None):
    entry = {"title": title, "link": link, "summary": summary}
    if published is not None:
        entry["published_parsed"] = published.timetuple()[:6]
    return entry


class TestSelectArticles:
    def test_marks_keyword_matches_and_keeps_all_fresh_items(self):
        now = datetime.now(UTC)
        entries = [
            _entry("Low-carbon cement wins award", "https://x.com/1", published=now),
            _entry("Office tower opens downtown", "https://x.com/2", published=now),
            _entry("Passivhaus scheme completes", "https://x.com/3", published=now),
        ]
        articles = select_articles([("Test", entries)], lookback_hours=168, max_candidates=10, now=now)
        assert len(articles) == 3
        by_url = {a.url: a for a in articles}
        assert by_url["https://x.com/1"].keyword_match is True
        assert by_url["https://x.com/2"].keyword_match is False
        assert by_url["https://x.com/3"].keyword_match is True

    def test_deduplicates_by_url(self):
        now = datetime.now(UTC)
        entries = [
            _entry("Green steel plant opens", "https://x.com/1", published=now),
            _entry("Green steel plant opens (syndicated)", "https://x.com/1", published=now),
        ]
        articles = select_articles([("Test", entries)], lookback_hours=168, max_candidates=10, now=now)
        assert len(articles) == 1

    def test_drops_entries_older_than_lookback(self):
        now = datetime.now(UTC)
        entries = [
            _entry("Fresh retrofit news", "https://x.com/fresh", published=now - timedelta(hours=2)),
            _entry("Stale retrofit news", "https://x.com/stale", published=now - timedelta(hours=200)),
        ]
        articles = select_articles([("Test", entries)], lookback_hours=168, max_candidates=10, now=now)
        assert [a.url for a in articles] == ["https://x.com/fresh"]

    def test_drops_undated_entries(self):
        now = datetime.now(UTC)
        entries = [
            _entry("Undated green building news", "https://x.com/undated"),
            _entry("Dated green building news", "https://x.com/dated", published=now - timedelta(hours=1)),
        ]
        articles = select_articles([("Test", entries)], lookback_hours=168, max_candidates=10, now=now)
        assert [a.url for a in articles] == ["https://x.com/dated"]

    def test_sorts_by_recency_and_caps_candidates(self):
        now = datetime.now(UTC)
        entries = [
            _entry(f"Green item {i}", f"https://x.com/{i}", published=now - timedelta(hours=i))
            for i in range(5)
        ]
        articles = select_articles([("Test", entries)], lookback_hours=168, max_candidates=3, now=now)
        assert [a.url for a in articles] == ["https://x.com/0", "https://x.com/1", "https://x.com/2"]

    def test_cap_ranks_by_recency_regardless_of_keywords(self):
        now = datetime.now(UTC)
        entries = [
            _entry(f"General item {i}", f"https://x.com/t{i}", published=now - timedelta(hours=i))
            for i in range(3)
        ]
        entries.append(_entry("Embodied carbon study", "https://x.com/carbon", published=now - timedelta(hours=10)))
        articles = select_articles([("Test", entries)], lookback_hours=168, max_candidates=2, now=now)
        assert [a.url for a in articles] == ["https://x.com/t0", "https://x.com/t1"]
        assert all(not a.keyword_match for a in articles)


class TestRendering:
    def test_build_headlines_md(self):
        articles = [
            Article(title="Cement breakthrough", url="https://x.com/1", source="edie"),
            Article(title="Retrofit funding doubled", url="https://x.com/2", source="Construction Enquirer"),
        ]
        markdown = build_headlines_md(articles)
        assert markdown == (
            "- [Cement breakthrough](https://x.com/1) — edie\n"
            "- [Retrofit funding doubled](https://x.com/2) — Construction Enquirer"
        )

    def test_build_site_posts_md_includes_excerpts(self):
        posts = [
            SitePost(title="Timber towers", url="https://site.com/a", excerpt="Mass timber rises."),
            SitePost(title="Policy watch", url="https://site.com/b"),
        ]
        markdown = build_site_posts_md(posts)
        assert markdown == (
            "- [Timber towers](https://site.com/a) — Mass timber rises.\n"
            "- [Policy watch](https://site.com/b)"
        )

    def test_render_digest_structure_without_assignments(self):
        articles = [Article(title="Cement breakthrough", url="https://x.com/1", source="edie")]
        markdown = render_digest(articles, "A quiet week in green building.", "09 November 2026")
        assert markdown.startswith("# Sustainable Construction Review — Weekly Digest — 09 November 2026")
        assert "## Market Briefing" in markdown
        assert "A quiet week in green building." in markdown
        assert markdown.index("## Market Briefing") < markdown.index("## Other")
        assert "[Cement breakthrough](https://x.com/1)" in markdown

    def test_render_digest_features_site_posts_after_briefing(self):
        articles = [Article(title="Cement breakthrough", url="https://x.com/1", source="edie")]
        posts = [SitePost(title="Our latest analysis", url="https://site.com/a")]
        markdown = render_digest(articles, "Briefing.", "09 November 2026", [], posts)
        assert "## From Sustainable Construction Review" in markdown
        assert "[Our latest analysis](https://site.com/a)" in markdown
        assert markdown.index("## Market Briefing") < markdown.index("## From Sustainable Construction Review")
        assert markdown.index("## From Sustainable Construction Review") < markdown.index("## Other")

    def test_render_digest_groups_by_section(self):
        articles = [
            Article(title="Building standards tightened", url="https://x.com/1", source="edie"),
            Article(title="Low-carbon concrete plant", url="https://x.com/2", source="Construction Dive"),
            Article(title="Net-zero tower completes", url="https://x.com/3", source="Dezeen"),
        ]
        assignments = [
            construction_digest.SectionAssignment(index=0, section="Policy & regulation", note="Part L"),
            construction_digest.SectionAssignment(index=1, section="Green materials", note="cement"),
            construction_digest.SectionAssignment(index=2, section="Projects & case studies", note="tower"),
        ]
        markdown = render_digest(articles, "Briefing.", "09 November 2026", assignments)
        assert "## Policy & regulation" in markdown
        assert "## Green materials" in markdown
        assert "## Projects & case studies" in markdown
        assert "## Technology" not in markdown
        assert "[Building standards tightened](https://x.com/1) — edie: Part L" in markdown
        assert markdown.index("## Market Briefing") < markdown.index("## Policy & regulation")

    def test_render_digest_unknown_section_goes_to_other(self):
        articles = [Article(title="Odd item", url="https://x.com/1", source="S")]
        assignments = [construction_digest.SectionAssignment(index=0, section="Sports", note="")]
        markdown = render_digest(articles, "Briefing.", "09 November 2026", assignments)
        assert "## Other" in markdown
        assert "[Odd item](https://x.com/1)" in markdown

    def test_briefing_prompt_contains_articles_and_urls(self):
        articles = [
            Article(
                title="Embodied carbon rules tighten",
                url="https://x.com/1",
                source="edie",
                published=datetime(2026, 11, 9, 7, 0, tzinfo=UTC),
                summary="New Part Z proposals published.",
            )
        ]
        prompt = build_briefing_prompt(articles, "09 November 2026")
        assert "Embodied carbon rules tighten" in prompt
        assert "edie" in prompt
        assert "2026-11-09 07:00 UTC" in prompt
        assert "URL: https://x.com/1" in prompt
        assert "policy" in prompt
        assert "materials innovation" in prompt

    def test_sections_prompt_lists_sections_and_items(self):
        articles = [Article(title="Low-carbon concrete plant", url="https://x.com/1", source="edie", summary="20% less")]
        prompt = build_sections_prompt(articles)
        assert '"Green materials"' in prompt and '"Other"' in prompt
        assert "0. [edie] Low-carbon concrete plant" in prompt
        assert '"assignments"' in prompt


class TestSectionAssignmentParsing:
    def test_parses_valid_assignments(self):
        raw = '{"assignments": [{"index": 0, "section": "Green materials", "note": "cement"}, {"index": 1, "section": "Sports", "note": "x"}]}'
        parsed = construction_digest._parse_section_assignments(raw, 2)
        assert parsed[0].section == "Green materials"
        assert parsed[0].note == "cement"
        assert parsed[1].section == "Other"

    def test_rejects_out_of_range_and_duplicate_indices(self):
        raw = '{"assignments": [{"index": 5, "section": "Rates"}, {"index": 0, "section": "Technology"}, {"index": 0, "section": "Other"}]}'
        parsed = construction_digest._parse_section_assignments(raw, 1)
        assert [p.index for p in parsed] == [0]
        assert parsed[0].section == "Technology"

    def test_invalid_json_returns_empty(self):
        assert construction_digest._parse_section_assignments("not json", 3) == []

    async def test_categorize_activity_falls_back_on_model_failure(self, monkeypatch):
        articles = [Article(title="A", url="https://x.com/1", source="S")]

        async def broken(prompt: str) -> str:
            raise RuntimeError("model down")

        monkeypatch.setattr(construction_digest, "_categorize", broken)
        assert await construction_digest.categorize_articles(articles) == []

    def test_relevance_prompt_numbers_items(self):
        articles = [
            Article(title="Property prices rise", url="https://x.com/1", source="Construction Dive"),
            Article(title="Retrofit grant scheme opens", url="https://x.com/2", source="edie"),
        ]
        prompt = build_relevance_prompt(articles)
        assert "0. Property prices rise (Construction Dive)" in prompt
        assert "1. Retrofit grant scheme opens (edie)" in prompt
        assert "strict JSON" in prompt


class TestParseRelevantIndices:
    def test_parses_json_with_surrounding_prose(self):
        raw = 'Here you go:\n{"relevant": [0, 2, 5]}\nDone.'
        assert construction_digest._parse_relevant_indices(raw, total=6) == [0, 2, 5]

    def test_drops_out_of_range_and_non_int(self):
        raw = '{"relevant": [0, 9, "x", -1, 1]}'
        assert construction_digest._parse_relevant_indices(raw, total=2) == [0, 1]

    def test_raises_without_json(self):
        with pytest.raises(ValueError):
            construction_digest._parse_relevant_indices("no json here", total=2)


class TestSitePosts:
    def test_parse_site_posts_extracts_fields(self):
        payload = [
            {
                "title": {"rendered": "<p>Timber towers rise</p>"},
                "link": "https://sustainableconstructionreview.com/timber-towers",
                "date_gmt": "2026-11-09T10:30:00",
                "excerpt": {"rendered": "<p>Mass timber &amp; low-carbon design.</p>"},
            }
        ]
        posts = construction_digest._parse_site_posts(payload, 5)
        assert len(posts) == 1
        assert posts[0].title == "Timber towers rise"
        assert posts[0].url == "https://sustainableconstructionreview.com/timber-towers"
        assert posts[0].published == datetime(2026, 11, 9, 10, 30, tzinfo=UTC)
        assert posts[0].excerpt == "Mass timber & low-carbon design."

    def test_parse_site_posts_skips_items_without_links(self):
        payload = [
            {"title": {"rendered": "No link"}, "link": ""},
            {"title": {"rendered": "Fine"}, "link": "https://site.com/fine"},
        ]
        posts = construction_digest._parse_site_posts(payload, 5)
        assert [p.url for p in posts] == ["https://site.com/fine"]

    def test_parse_site_posts_handles_non_list(self):
        assert construction_digest._parse_site_posts({"error": 401}, 5) == []

    def test_wp_headers_include_token(self, monkeypatch):
        monkeypatch.setenv("WP_API_TOKEN", "secret-token")
        monkeypatch.delenv("WP_API_USER", raising=False)
        monkeypatch.delenv("WP_API_APP_PASSWORD", raising=False)
        headers = construction_digest._wp_headers()
        assert headers["X-Digest-Token"] == "secret-token"
        assert "Authorization" not in headers

    def test_wp_headers_include_basic_auth(self, monkeypatch):
        import base64

        monkeypatch.delenv("WP_API_TOKEN", raising=False)
        monkeypatch.setenv("WP_API_USER", "digest-agent")
        monkeypatch.setenv("WP_API_APP_PASSWORD", "app-pass")
        headers = construction_digest._wp_headers()
        expected = base64.b64encode(b"digest-agent:app-pass").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    async def test_fetch_site_posts_activity_survives_failure(self, monkeypatch):
        def broken(base_url: str, per_page: int):
            raise RuntimeError("401 unauthorized")

        monkeypatch.setattr(construction_digest, "_fetch_site_posts", broken)
        result = await construction_digest.fetch_site_posts(5)
        assert result.posts == []
        assert "unavailable" in result.status

    async def test_fetch_site_posts_activity_returns_posts(self, monkeypatch):
        def fake_fetch(base_url: str, per_page: int):
            return [SitePost(title="Post", url="https://site.com/post")]

        monkeypatch.setattr(construction_digest, "_fetch_site_posts", fake_fetch)
        result = await construction_digest.fetch_site_posts(5)
        assert result.status == "fetched 1 posts"
        assert result.posts[0].url == "https://site.com/post"


class TestSmtpSettings:
    def test_returns_none_without_password(self, monkeypatch):
        monkeypatch.delenv("SES_SMTP_PASSWORD", raising=False)
        assert construction_digest._smtp_settings("test@example.com") is None

    def test_builds_settings_from_environment(self, monkeypatch):
        monkeypatch.setenv("SES_SMTP_PASSWORD", "secret-password")
        monkeypatch.delenv("SES_SMTP_HOST", raising=False)
        monkeypatch.delenv("SES_SMTP_PORT", raising=False)
        monkeypatch.delenv("SES_CONFIGURATION_SET", raising=False)
        monkeypatch.delenv("SES_LIST_UNSUBSCRIBE_URL", raising=False)
        settings = construction_digest._smtp_settings("test@example.com")
        assert settings == {
            "host": "email-smtp.us-east-1.amazonaws.com",
            "port": "587",
            "user": construction_digest.DEFAULT_SENDER,
            "password": "secret-password",
            "sender": construction_digest.DEFAULT_SENDER,
            "sender_name": construction_digest.SITE_NAME,
            "configuration_set": "",
            "list_unsubscribe_url": "",
        }


class TestEmailFormatting:
    def test_plain_text_expands_links(self):
        body = "- [Title](https://example.com/a) — Source"
        plain = construction_digest._digest_plain_text(body)
        assert "[Title](https://example.com/a)" not in plain
        assert "Title (https://example.com/a) — Source" in plain

    def test_html_renders_links_and_headings(self):
        body = "# Digest\n\n## Headlines\n\n- [Title](https://example.com/a) — Source\n\n## Summary\n\nText."
        html = construction_digest._digest_html(body)
        assert '<a href="https://example.com/a">Title</a>' in html
        assert "<h1" in html and "<h2" in html
        assert "##" not in html.replace("</h2>", "")
        assert "Sustainable Construction Review" in html

    def test_html_is_wrapped_document(self):
        html = construction_digest._digest_html("# Digest")
        assert html.startswith("<!DOCTYPE html>")
        assert 'name="viewport"' in html
        assert '<div class="digest">' in html

    def test_send_email_sets_ses_headers(self, monkeypatch):
        monkeypatch.setenv("SES_SMTP_PASSWORD", "secret-password")
        monkeypatch.setenv("SES_CONFIGURATION_SET", "newsletter-tracking")
        monkeypatch.setenv("SES_LIST_UNSUBSCRIBE_URL", "https://sustainableconstructionreview.com/unsubscribe")
        captured = {}

        class FakeServer:
            def __init__(self, host, port, timeout=None):
                captured["host"] = host
                captured["port"] = port

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self):
                captured["starttls"] = True

            def login(self, user, password):
                captured["user"] = user

            def send_message(self, message):
                captured["message"] = message

        monkeypatch.setattr("smtplib.SMTP", FakeServer)
        settings = construction_digest._smtp_settings("test@example.com")
        construction_digest._send_email(settings, "test@example.com", "Subject", "# Digest")
        message = captured["message"]
        assert message["X-SES-CONFIGURATION-SET"] == "newsletter-tracking"
        assert message["List-Unsubscribe"] == "<https://sustainableconstructionreview.com/unsubscribe>"
        assert message["From"].startswith("Sustainable Construction Review")
        assert captured["starttls"] is True


class TestFetchArticlesActivity:
    async def test_fetches_configured_sources(self, monkeypatch):
        now = datetime.now(UTC)
        by_url = {
            "https://www.edie.net/feed/": [
                _entry(
                    "Low-carbon cement wins award",
                    "https://edie.net/cement",
                    summary="A new low-carbon cement cuts emissions.",
                    published=now - timedelta(hours=3),
                ),
            ],
            "https://www.dezeen.com/feed/": [
                _entry(
                    "Timber tower completes",
                    "https://dezeen.com/timber",
                    published=now - timedelta(hours=5),
                ),
            ],
        }
        current_url = [""]

        def tracking_fetch(url: str) -> bytes:
            current_url[0] = url
            return b"<rss></rss>"

        def parse(content: bytes):
            return by_url.get(current_url[0], [])

        monkeypatch.setattr(construction_digest, "_fetch_feed", tracking_fetch)
        monkeypatch.setattr(construction_digest, "_parse_feed", parse)

        result = await construction_digest.fetch_articles(168)
        assert len(result.articles) == 2
        assert "Dezeen" in {a.source for a in result.articles}
        assert result.stats.total_entries >= len(result.articles)
        assert result.stats.candidates == len(result.articles)

    async def test_failed_feed_does_not_break_the_run(self, monkeypatch):
        def broken_fetch(url: str) -> bytes:
            raise RuntimeError("connection refused")

        monkeypatch.setattr(construction_digest, "_fetch_feed", broken_fetch)
        result = await construction_digest.fetch_articles(168)
        assert result.articles == []


class TestFilterRelevantActivity:
    async def test_merges_keyword_matches_and_model_picks(self, monkeypatch):
        now = datetime.now(UTC)
        articles = [
            Article(title="Retrofit grants open", url="https://x.com/1", source="S", published=now, keyword_match=True),
            Article(title="Office rents rise", url="https://x.com/2", source="S", published=now - timedelta(hours=1)),
            Article(title="Green steel demand grows", url="https://x.com/3", source="S", published=now - timedelta(hours=2)),
            Article(title="Mining merger", url="https://x.com/4", source="S", published=now - timedelta(hours=3)),
        ]

        async def fake_classify(candidates):
            return [0, 1]

        monkeypatch.setattr(construction_digest, "_classify_relevance", fake_classify)
        report = await construction_digest.filter_relevant_articles(
            construction_digest.FetchResult(articles=articles), 15
        )
        assert [a.url for a in report.selected] == ["https://x.com/1", "https://x.com/2", "https://x.com/3"]
        assert report.selected[0].keyword_match is True
        reasons = {d.url: (d.included, d.reason) for d in report.decisions}
        assert reasons["https://x.com/1"] == (True, "keyword match")
        assert reasons["https://x.com/2"] == (True, "model relevance")
        assert reasons["https://x.com/3"] == (True, "model relevance")
        assert reasons["https://x.com/4"] == (False, "model: not relevant")

    async def test_caps_at_max_articles(self, monkeypatch):
        now = datetime.now(UTC)
        articles = [
            Article(title=f"Item {i}", url=f"https://x.com/{i}", source="S", published=now, keyword_match=True)
            for i in range(5)
        ]

        async def fake_classify(candidates):
            return []

        monkeypatch.setattr(construction_digest, "_classify_relevance", fake_classify)
        report = await construction_digest.filter_relevant_articles(
            construction_digest.FetchResult(articles=articles), 3
        )
        assert len(report.selected) == 3
        cuts = [d for d in report.decisions if not d.included]
        assert len(cuts) == 2
        assert all(d.reason == "cut: beyond max_articles cap" for d in cuts)

    async def test_classification_failure_falls_back_to_keyword_matches(self, monkeypatch):
        now = datetime.now(UTC)
        articles = [
            Article(title="Retrofit grants open", url="https://x.com/1", source="S", published=now, keyword_match=True),
            Article(title="Office rents rise", url="https://x.com/2", source="S", published=now),
        ]

        async def broken(candidates):
            raise RuntimeError("model down")

        monkeypatch.setattr(construction_digest, "_classify_relevance", broken)
        report = await construction_digest.filter_relevant_articles(
            construction_digest.FetchResult(articles=articles), 15
        )
        assert [a.url for a in report.selected] == ["https://x.com/1"]
        reasons = {d.url: (d.included, d.reason) for d in report.decisions}
        assert reasons["https://x.com/2"] == (False, "cut: classifier unavailable")


class TestMarketBriefingActivity:
    async def test_returns_model_output(self, monkeypatch):
        articles = [Article(title="Retrofit grants open", url="https://x.com/1", source="edie")]

        async def fake_model(prompt: str, max_tokens: int = 800) -> str:
            return "Retrofit funding dominates this week."

        monkeypatch.setattr(construction_digest, "_complete_model_call", fake_model)
        result = await construction_digest.market_briefing(articles, "09 November 2026")
        assert result == "Retrofit funding dominates this week."

    async def test_empty_articles_skip_model(self, monkeypatch):
        async def boom(prompt: str, max_tokens: int = 800) -> str:
            raise AssertionError("model must not be called for empty input")

        monkeypatch.setattr(construction_digest, "_complete_model_call", boom)
        assert await construction_digest.market_briefing([], "09 November 2026") == ""


class TestSendEmailActivity:
    async def test_skips_without_password(self, monkeypatch):
        monkeypatch.delenv("SES_SMTP_PASSWORD", raising=False)
        result = await construction_digest.send_digest_email(
            "test@example.com", "Subject", "# Digest"
        )
        assert result.sent is False
        assert "not configured" in result.status

    async def test_sends_with_password(self, monkeypatch):
        monkeypatch.setenv("SES_SMTP_PASSWORD", "secret-password")
        captured = {}

        def fake_send(settings, recipient, subject, body):
            captured.update(recipient=recipient, subject=subject, body=body)

        monkeypatch.setattr(construction_digest, "_send_email", fake_send)
        result = await construction_digest.send_digest_email(
            "test@example.com", "Sustainable Construction Review Weekly Digest", "# Digest"
        )
        assert result.sent is True
        assert captured["recipient"] == "test@example.com"
        assert captured["subject"] == "Sustainable Construction Review Weekly Digest"


class TestDigestInputDefaults:
    def test_weekly_defaults(self):
        data = DigestInput()
        assert data.lookback_hours == 168
        assert data.max_articles == 15
        assert data.max_site_posts == 5
        assert data.require_approval is False
        assert data.dry_run is False
