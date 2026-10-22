"""What search engines and crawlers are offered: the landing page only, with
its metadata; the terminal, API and sign-in page are never indexed, and AI
training crawlers are refused everywhere."""
import asyncio
import json

import httpx

from src.api import webapp


def _get(path, cookies=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     cookies=cookies) as c:
            return await c.get(path)
    return asyncio.run(go())


def test_robots_lets_search_engines_read_the_landing_page_only():
    text = _get("/robots.txt").text
    general = text.split("User-agent: *")[1]
    assert "Allow: /$" in general and "Disallow: /\n" in general and "Sitemap: https://verquoai.com/sitemap.xml" in text
    training = text.split("User-agent: *")[0]
    assert "User-agent: GPTBot" in training and "User-agent: CCBot" in training and "Disallow: /" in training
    assert "Googlebot" not in text and "Bingbot" not in text        # search engines follow the general rule


def test_sitemap_lists_the_landing_page():
    res = _get("/sitemap.xml")
    assert res.headers["content-type"].startswith("application/xml")
    assert "<loc>https://verquoai.com/</loc>" in res.text and "noindex" not in res.headers.get("x-robots-tag", "")


def test_only_the_landing_page_is_indexable():
    assert "x-robots-tag" not in _get("/").headers
    for path in ("/login", "/app", "/api/status", "/demo/start", "/app/stock/INFY"):
        assert _get(path).headers.get("x-robots-tag") == "noindex, nofollow", path


def test_landing_page_carries_search_and_share_metadata():
    html = _get("/").text
    assert '<link rel="canonical" href="https://verquoai.com/">' in html
    assert 'property="og:image" content="https://verquoai.com/static/img/og-card.png"' in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    ld = json.loads(html.split('<script type="application/ld+json">')[1].split("</script>")[0])
    assert {g["@type"] for g in ld["@graph"]} == {"Organization", "WebSite", "SoftwareApplication"}
    assert 'content="noindex, follow"' in _get("/login").text      # the sign-in view of the same page


def test_ai_training_crawlers_are_refused_but_may_read_robots():
    async def go(path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)"}) as c:
            return await c.get(path)
    assert asyncio.run(go("/")).status_code == 403
    assert asyncio.run(go("/static/js/landing.js")).status_code == 403
    assert asyncio.run(go("/robots.txt")).status_code == 200
    googlebot = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

    async def google():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="https://testserver",
                                     headers={"User-Agent": googlebot}) as c:
            return await c.get("/")
    assert asyncio.run(google()).status_code == 200
