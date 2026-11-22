from __future__ import annotations

from bot.models import clean_html, make_article_id, normalize_url


def test_normalize_url_removes_utm_ref_fragment_and_trailing_slash() -> None:
    url_a = "https://Example.com/news/ai-breakthrough/?utm_source=rss&utm_medium=feed&ref=home#comments"
    url_b = "https://example.com/news/ai-breakthrough"

    assert normalize_url(url_a) == url_b
    assert make_article_id(url_a) == make_article_id(url_b)


def test_normalize_url_preserves_non_tracking_query_params() -> None:
    url_a = "https://news.site/article?id=42&utm_campaign=daily&page=2"
    url_b = "https://news.site/article?id=42&page=2"

    assert normalize_url(url_a) == url_b
    assert make_article_id(url_a) == make_article_id(url_b)


def test_clean_html_strips_tags_and_unescapes_entities() -> None:
    raw = "<p>Novo chip <b>ARM64</b> &amp; Linux &quot;otimizado&quot;</p>  \n  <br/>"
    assert clean_html(raw) == 'Novo chip ARM64 & Linux "otimizado"'
    assert clean_html("   ") is None
    assert clean_html(None) is None
