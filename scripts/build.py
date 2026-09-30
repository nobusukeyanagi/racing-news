"""Yahoo!フォローの公営競技5テーマを取得し、競技別に最新100件を保持して1ページに表示する。"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

JST = ZoneInfo("Asia/Tokyo")
AGENT = "RacingNewsReader/1.0"
SNAPSHOT_VERSION = 1
MAX_PER_CATEGORY = 100
IMAGE_HOSTS = {"newsatcl-pctr.c.yimg.jp"}

THEMES = [
    ("競輪", "keirin", "https://follow.yahoo.co.jp/themes/0bef3418cd3e109f7707/"),
    ("オートレース", "autorace", "https://follow.yahoo.co.jp/themes/0225b58b93568d981167/"),
    ("ボートレース", "boatrace", "https://follow.yahoo.co.jp/themes/08a589647e213a249875/"),
    ("地方競馬", "local-keiba", "https://follow.yahoo.co.jp/themes/0dd1a07d77824fcd0458/"),
    ("JRA", "jra", "https://follow.yahoo.co.jp/themes/03ebffae693d76102412/"),
]

ALLOWED_ARTICLE_HOSTS = {"news.yahoo.co.jp", "article.yahoo.co.jp"}
NEWS_PATH = re.compile(r"^/(?:expert/)?articles/[A-Za-z0-9_-]+/?$")
ARTICLE_YAHOO_PATH = re.compile(r"^/(?:detail|pickup|feature)/")
FULLWIDTH_ALNUM = {
    code: code - 0xFEE0
    for start, end in ((0xFF10, 0xFF19), (0xFF21, 0xFF3A), (0xFF41, 0xFF5A))
    for code in range(start, end + 1)
}


def clean(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", value or "").strip()


def normalized_url(url: str, base: str = "") -> str | None:
    p = urllib.parse.urlsplit(urllib.parse.urljoin(base, url))
    if p.scheme != "https" or p.hostname not in ALLOWED_ARTICLE_HOSTS or p.port is not None:
        return None
    if p.hostname == "news.yahoo.co.jp" and not NEWS_PATH.fullmatch(p.path):
        return None
    if p.hostname == "article.yahoo.co.jp" and not ARTICLE_YAHOO_PATH.match(p.path):
        return None
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def image_url(url: str, base: str) -> str | None:
    p = urllib.parse.urlsplit(urllib.parse.urljoin(base, url))
    if p.scheme == "https" and p.hostname in IMAGE_HOSTS and p.port is None:
        return urllib.parse.urlunsplit(p)
    return None


class Client:
    def __init__(self):
        self.last = 0.0
        self.cache: dict[str, str] = {}
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _robots(self, origin: str) -> urllib.robotparser.RobotFileParser:
        if origin in self.robots:
            return self.robots[origin]
        parser = urllib.robotparser.RobotFileParser()
        try:
            text = self._raw_get(origin + "/robots.txt", max_bytes=500_000)
        except urllib.error.HTTPError as error:
            if error.code not in (400, 404):
                raise
            text = "User-agent: *\nAllow: /\n"
        parser.parse(text.splitlines())
        self.robots[origin] = parser
        return parser

    def _raw_get(self, url: str, max_bytes: int = 8_000_000) -> str:
        for attempt in range(3):
            time.sleep(max(0, 0.8 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": AGENT,
                    "Accept-Language": "ja",
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    raw = response.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        raise RuntimeError("取得サイズの上限を超えました")
                    return raw.decode("utf-8", "replace")
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
            time.sleep(2 ** (attempt + 1))
        raise RuntimeError("取得できませんでした")

    def get(self, url: str, refresh: bool = False) -> str:
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.hostname not in {"follow.yahoo.co.jp", *ALLOWED_ARTICLE_HOSTS}:
            raise RuntimeError("取得対象外のURL")
        origin = f"{p.scheme}://{p.netloc}"
        if not self._robots(origin).can_fetch(AGENT, url):
            raise RuntimeError(f"robots.txtで取得が許可されていません: {origin}")
        if refresh or url not in self.cache:
            self.cache[url] = self._raw_get(url)
        return self.cache[url]

    def save_image(self, url: str, directory: Path) -> str:
        if not image_url(url, url):
            raise RuntimeError("許可されていない画像URL")
        p = urllib.parse.urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if not self._robots(origin).can_fetch(AGENT, url):
            raise RuntimeError("画像CDNのrobots.txtにより取得不可")
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        time.sleep(max(0, 0.8 - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        with urllib.request.urlopen(request, timeout=25) as response:
            content_type = response.headers.get_content_type()
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(content_type)
            if not ext:
                raise RuntimeError("対応外の画像形式")
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise RuntimeError("画像サイズの上限を超過")
        directory.mkdir(parents=True, exist_ok=True)
        filename = hashlib.sha256(url.encode()).hexdigest()[:24] + ext
        (directory / filename).write_bytes(raw)
        return "images/" + filename


def candidate_title(anchor) -> str:
    for selector in ('[class*="ttl"]', '[class*="title"]', "h2", "h3", "p"):
        node = anchor.select_one(selector)
        if node:
            value = clean(node.get_text(" ", strip=True))
            if 4 <= len(value) <= 300 and "報告" not in value:
                return value
    value = clean(anchor.get_text(" ", strip=True))
    value = re.sub(
        r"\s+[^ ]{2,40}\s*-\s*(?:\d+分前|\d+時間前|\d+日前|\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})$",
        "",
        value,
    )
    return value[:300]


def parse_theme(document: str, theme_url: str, category: str, slug: str) -> list[dict]:
    soup = BeautifulSoup(document, "html.parser")
    items, seen = [], set()
    for anchor in soup.select("a[href]"):
        url = normalized_url(anchor.get("href", ""), theme_url)
        if not url or url in seen:
            continue
        seen.add(url)
        items.append({
            "category": category,
            "category_slug": slug,
            "title": clean_news_title(candidate_title(anchor) or "ニュース"),
            "url": url,
            "publisher": "",
            "published": "",
            "body": [],
            "blocks": [],
            "images": [],
            "note": "",
        })
    if not items:
        raise RuntimeError(f"{category}: ニュース一覧が取得できませんでした")
    return items


def json_articles(value):
    if isinstance(value, dict):
        kind = value.get("@type", [])
        kind = [kind] if isinstance(kind, str) else kind
        if isinstance(kind, list) and any(k in ("NewsArticle", "Article", "ReportageNewsArticle") for k in kind):
            yield value
        for child in value.values():
            yield from json_articles(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_articles(child)


def parse_article(document: str, url: str) -> dict:
    soup = BeautifulSoup(document, "html.parser")
    metadata = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            values = list(json_articles(json.loads(script.string or script.get_text())))
            if values:
                metadata = values[0]
                break
        except (ValueError, TypeError):
            pass

    heading = soup.select_one("article h1, main h1, h1")
    title = clean(str(metadata.get("headline") or (heading.get_text(" ", strip=True) if heading else "")))

    author = metadata.get("author") or metadata.get("publisher") or {}
    if isinstance(author, list):
        author = author[0] if author else {}
    publisher = clean(str(author.get("name", "") if isinstance(author, dict) else author))
    published = clean(str(metadata.get("datePublished", "")))

    containers = soup.select(".article_body") or soup.select('[itemprop="articleBody"]') or soup.select("article section") or soup.select("article")
    blocks, body, images = [], [], []
    for container in containers[:3]:
        for unwanted in container.select("script,style,aside,nav,button,iframe,[class*='related'],[class*='Related'],[class*='advert'],[class*='comment']"):
            unwanted.decompose()
        for element in container.select("img,p,h2,h3,h4"):
            if element.name == "img":
                candidate = image_url(element.get("src") or element.get("data-src") or "", url)
                if candidate and candidate not in {x["url"] for x in images} and len(images) < 6:
                    caption = ""
                    figure = element.find_parent("figure")
                    if figure and figure.find("figcaption"):
                        caption = clean(figure.find("figcaption").get_text(" ", strip=True))
                    image = {"url": candidate, "caption": caption[:300]}
                    images.append(image)
                    blocks.append({"type": "image", **image})
                continue
            if element.find_parent("figcaption"):
                continue
            for br in element.find_all("br"):
                br.replace_with("\n\n")
            text = clean(element.get_text("\n", strip=True)).replace("\n \n", "\n\n")
            if not text:
                continue
            body.append(text)
            blocks.append({"type": "heading" if element.name in ("h2", "h3", "h4") else "text", "text": text})

    if not body and isinstance(metadata.get("articleBody"), str):
        for paragraph in metadata["articleBody"].splitlines():
            paragraph = clean(paragraph)
            if paragraph:
                body.append(paragraph)
                blocks.append({"type": "text", "text": paragraph})

    return {"article_title": title, "publisher": publisher, "published": published, "body": body, "blocks": blocks, "images": images}


def fetch_article(client: Client, item: dict, image_dir: Path) -> dict:
    result = dict(item)
    result.update(body=[], blocks=[], images=[], note="")
    try:
        parsed = parse_article(client.get(item["url"]), item["url"])
        if parsed["article_title"]:
            result["title"] = clean_news_title(parsed["article_title"])
        result["publisher"] = parsed["publisher"]
        result["published"] = parsed["published"]
        for block in parsed["blocks"]:
            if block["type"] in ("text", "heading"):
                result["body"].append(block["text"])
                result["blocks"].append(block)
            elif len(result["images"]) < 6:
                try:
                    saved = client.save_image(block["url"], image_dir)
                    photo = {"src": saved, "caption": block.get("caption", "")}
                    result["images"].append(photo)
                    result["blocks"].append({"type": "image", **photo})
                except Exception as error:
                    print(f"::warning::画像取得失敗 {type(error).__name__}: {error}", file=sys.stderr)
        if not result["body"]:
            result["note"] = "本文を取得できませんでした。元記事をご確認ください。"
    except Exception as error:
        result["note"] = "本文を取得できませんでした。元記事をご確認ください。"
        print(f"::warning::{item['url']}: {type(error).__name__}: {error}", file=sys.stderr)
    return result


def load_snapshot(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {"items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}
    if data.get("format_version") != SNAPSHOT_VERSION:
        return {"items": []}
    return data


def copy_previous_images(previous_site: Path | None, output: Path) -> None:
    if previous_site and (previous_site / "images").is_dir():
        shutil.copytree(previous_site / "images", output / "images", dirs_exist_ok=True)


def merge_category(client: Client, fresh: list[dict], previous: list[dict], image_dir: Path) -> list[dict]:
    previous_by_url = {item.get("url"): item for item in previous if item.get("url")}
    result, seen = [], set()
    for item in fresh:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        if url in previous_by_url:
            kept = dict(previous_by_url[url])
            kept["is_new"] = False
            result.append(kept)
        else:
            fetched = fetch_article(client, item, image_dir)
            fetched["is_new"] = True
            result.append(fetched)
    for item in previous:
        url = item.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        kept = dict(item)
        kept["is_new"] = False
        result.append(kept)
        if len(result) >= MAX_PER_CATEGORY:
            break
    return result[:MAX_PER_CATEGORY]


def format_published(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


TRAILING_TITLE_PAREN = re.compile(r"\s*[（(][^（）()\n]{1,80}[）)]\s*$")
BYLINE_AGENCY = re.compile(
    r"^(?:朝日新聞社|読売新聞社|毎日新聞社|日本経済新聞社|産経新聞社|共同通信社|時事通信社|"
    r"AFP時事|ロイター|Reuters|Full-Count編集部|All Nippon NewsNetwork\(ANN\)|TBSテレビ|"
    r"テレビ朝日|富山テレビ放送|スポニチアネックス|スポーツ報知|日刊スポーツ|"
    r"デイリースポーツ|サンケイスポーツ|東スポWEB|中日スポーツ|netkeiba|netkeirin|"
    r"マクール|BOATCAST|デイリースポーツ online|スポーツニッポン新聞社|"
    r"報知新聞社|日刊スポーツ新聞社|東京スポーツ新聞社|産経デジタル|"
    r"Yahoo!ニュース|Yahoo! JAPAN)$",
    re.I,
)
BYLINE_PERSON = re.compile(
    r"^(?:[一-龥]{2,6}(?:\s|　)[一-龥]{1,6}|"
    r"[A-Z][a-z]+(?:-[A-Za-z]+)?(?:\s+[A-Z][a-z]+(?:-[A-Za-z]+)?){1,3})$"
)
BYLINE_BRACKET = re.compile(r"^[（(【][^）)】\n]{1,80}(?:記者|編集部|通信|新聞|スポーツ|WEB|Web)[^）)】\n]*[）)】]$")
TRAILING_COMPANY = re.compile(
    r"^.{1,80}(?:新聞社|通信社|放送|テレビ|TV|編集部|編集室|デジタル|オンライン|ONLINE|Web|WEB)$",
    re.I,
)

LINK_ONLY_TEXT = re.compile(
    r"^[【［\[]\s*(?:写真|画像|動画|映像|関連記事|関連写真|関連動画|一覧|図解|表|"
    r"一目で|写真で|動画で|画像で|チェック|注目|こちら)\s*[】］\]].*$",
    re.I,
)


def clean_news_title(value: str) -> str:
    """ニュース見出し末尾に併記された媒体・補足の括弧書きを外す。"""
    value = clean(value)
    return TRAILING_TITLE_PAREN.sub("", value).strip()


def clean_body_blocks(blocks: list[dict], publisher: str = "") -> list[dict]:
    """本文末尾に残る執筆者名・執筆社名を落とす。"""
    result = [dict(block) for block in blocks]
    text_indexes = [i for i, block in enumerate(result) if block.get("type") in ("text", "heading")]
    # 末尾側だけを対象にして本文中の人名・社名を誤削除しにくくする。
    for i in reversed(text_indexes[-5:]):
        block = result[i]
        paragraphs = [
            clean(x) for x in re.split(r"\n\s*\n|\n", block.get("text", ""))
            if clean(x) and not LINK_ONLY_TEXT.fullmatch(clean(x))
        ]
        while paragraphs:
            tail = paragraphs[-1].strip()
            normalized = tail.replace("　", " ")
            if (
                tail == publisher
                or BYLINE_AGENCY.fullmatch(tail)
                or BYLINE_PERSON.fullmatch(normalized)
                or BYLINE_BRACKET.fullmatch(tail)
                or TRAILING_COMPANY.fullmatch(tail)
                or re.fullmatch(r"(?:文|取材|撮影|編集)[：:]\s*.{1,50}", tail)
            ):
                paragraphs.pop()
                continue
            break
        if paragraphs:
            block["text"] = "\n\n".join(paragraphs)
            break
        result.pop(i)
    return result


def truncate_title(value: str, maximum: int = 25) -> str:
    value = clean_news_title(str(value))
    return value if len(value) <= maximum else value[:maximum] + "…"


def render(items: list[dict], updated: datetime) -> str:
    esc = html.escape
    def display(value):
        return esc(str(value).translate(FULLWIDTH_ALNUM))

    grouped = {slug: [] for _, slug, _ in THEMES}
    for item in items:
        grouped.setdefault(item["category_slug"], []).append(item)

    nav_groups, sections = [], []
    for category, slug, _ in THEMES:
        links = []
        for index, item in enumerate(grouped.get(slug, [])[:MAX_PER_CATEGORY], 1):
            article_id = f"{slug}-{index}"
            links.append(
                f'<li data-category="{slug}" data-new="{str(bool(item.get("is_new"))).lower()}">'
                f'<a href="#news-{article_id}" title="{esc(item["title"], quote=True)}">{display(truncate_title(item["title"]))}</a></li>'
            )
        nav_groups.append(f'<section class="title-group" data-category="{slug}"><ol>{"".join(links)}</ol></section>')

        for index, item in enumerate(grouped.get(slug, [])[:MAX_PER_CATEGORY], 1):
            article_id = f"{slug}-{index}"
            meta = " / ".join(part for part in [category, item.get("publisher", ""), format_published(item.get("published", ""))] if part)
            new_label = " [NEW]" if item.get("is_new") else ""
            content = []
            cleaned_blocks = clean_body_blocks(item.get("blocks", []), item.get("publisher", ""))
            for block in cleaned_blocks:
                if block["type"] == "image":
                    caption = f'<figcaption>{display(block.get("caption", ""))}</figcaption>' if block.get("caption") else ""
                    content.append(f'<figure><img src="{esc(block["src"], quote=True)}" alt="記事に掲載された写真" loading="lazy" decoding="async">{caption}</figure>')
                else:
                    css = ' class="subheading"' if block["type"] == "heading" else ""
                    for paragraph in block.get("text", "").split("\n\n"):
                        paragraph = clean(paragraph)
                        if paragraph:
                            content.append(f"<p{css}>{display(paragraph)}</p>")
            note = f'<p class="notice">{display(item["note"])}</p>' if item.get("note") else ""
            sections.append(
                f'<article id="news-{article_id}" data-category="{slug}" data-new="{str(bool(item.get("is_new"))).lower()}">'
                f'<h2>{display(clean_news_title(item["title"]))}</h2><p class="meta"><a href="{esc(item["url"], quote=True)}" target="_blank" rel="noopener noreferrer nofollow">{display(meta)}</a>{new_label}</p>{"".join(content)}{note}</article>'
            )

    tabs = "".join(
        f'<button class="race-tab" type="button" data-category="{slug}" aria-pressed="{"true" if i == 0 else "false"}">{display(category)}</button>'
        for i, (category, slug, _) in enumerate(THEMES)
    )
    updated_text = updated.astimezone(JST).strftime("%Y-%m-%d %H:%M")
    first_slug = THEMES[0][1]

    template = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">
<meta name="referrer" content="no-referrer">
<link rel="icon" href="favicon.ico" sizes="any"><link rel="apple-touch-icon" href="apple-touch-icon.png" sizes="180x180">
<title>公営競技ニュース</title>
<style>
*{box-sizing:border-box}html{scroll-behavior:auto}body{margin:0;background:#fff;color:#202020;font-family:system-ui,-apple-system,"Noto Sans JP",sans-serif;font-size:16px;line-height:1.9;overflow-wrap:anywhere}
main{max-width:1440px;margin:0 auto;padding:0 24px 56px}header{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid #bbb;padding:4px 0}.header-row{display:flex;align-items:center;gap:10px 18px;min-height:34px}h1{font-size:1.5rem;line-height:1.3;margin:0;flex:none}h1 a{color:inherit;text-decoration:none}h2{font-size:1.3rem;line-height:1.55;margin:0 0 8px}.feed article h2{color:#14532d}a{color:#174c86;text-underline-offset:3px}a:focus-visible,button:focus-visible{outline:2px solid #14532d;outline-offset:3px}.update-controls{margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;line-height:1.25}.updated,.meta{font-size:.875rem;color:#555}.new-filter{border:0;background:transparent;color:#555;font:inherit;font-size:14px;padding:2px 0;cursor:pointer}.race-tabs{display:flex;align-items:center;gap:14px;white-space:nowrap}.race-tab{border:0;background:transparent;color:#777;padding:0;font:inherit;font-size:14px;cursor:pointer}.race-tab[aria-pressed="true"]{color:#174c86;font-weight:700}.mobile-race-tabs{display:none}
.layout{display:grid;grid-template-columns:minmax(260px,360px) minmax(0,1fr);gap:36px;align-items:start}.feed{padding-top:12px}nav{position:sticky;top:calc(var(--header-height, 42px) + 8px);max-height:calc(100dvh - var(--header-height, 42px) - 16px);overflow:auto;padding-top:12px}.title-group ol{margin:0;padding:0;list-style:none}.title-group li{padding:2px 0}.title-group li[data-new="true"] a{color:#174c86}.title-group a{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}article{border-top:1px solid #bbb;padding:28px 0;scroll-margin-top:calc(var(--header-height, 42px) + 12px)}article p{margin:0 0 18px}.meta{margin:4px 0 14px}.meta a{color:inherit}.subheading{font-weight:700}.notice{padding:10px 14px;border-left:3px solid #999;background:#f5f5f5}figure{margin:0 0 18px}figure img{display:block;width:auto;max-width:300px;height:auto;max-height:300px;object-fit:contain}figcaption{font-size:.8125rem;color:#555;line-height:1.55;margin-top:5px}[hidden]{display:none!important}.menu-toggle,.pull-refresh{display:none}
@media(max-width:700px){body{font-size:17px;line-height:1.8}main{padding:0 16px 36px}header{padding:1px 0;min-height:42px}.header-row{gap:6px 10px}h1{font-size:1.25rem}.desktop-race-tabs{display:none}.updated{font-size:13px}.new-filter{font-size:13px;padding:0}.layout{display:block}.menu-toggle{display:inline-flex;align-items:center;justify-content:center;flex:none;order:3;margin-left:auto;width:32px;height:34px;border:0;background:transparent;color:inherit;padding:4px}.hamburger{display:flex;flex-direction:column;gap:4px}.hamburger span{display:block;width:20px;height:2px;background:currentColor}.layout nav{display:none;position:fixed;top:var(--header-height, 42px);left:0;right:0;z-index:19;max-height:calc(100dvh - var(--header-height, 42px));overflow:auto;padding:10px 16px 14px;background:#fff;border-bottom:1px solid #bbb;box-shadow:0 5px 10px #0002}body.menu-open .layout nav{display:block}.mobile-race-tabs{display:flex;gap:14px;padding:0 0 10px;margin-bottom:8px;border-bottom:1px solid #ddd;overflow-x:auto}.mobile-race-tabs .race-tab{font-size:14px}.title-group a{max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}h2{font-size:20px}figcaption{font-size:14px}.pull-refresh{display:block;position:fixed;top:0;left:50%;z-index:25;padding:3px 14px;background:#14532d;color:#fff;border-radius:0 0 12px 12px;font-size:14px;pointer-events:none;transform:translate(-50%,-110%);transition:transform .15s}.pull-refresh.active{transform:translate(-50%,0)}}
@media print{header{position:static}.menu-toggle,nav,.pull-refresh{display:none!important}main{max-width:none;padding:0}.layout{display:block}}
</style></head><body><div class="pull-refresh" role="status" aria-live="polite">下に引いて更新</div><main id="top">
<header><div class="header-row"><button class="menu-toggle" type="button" aria-label="タイトル一覧を開く" aria-controls="news-nav" aria-expanded="false"><span class="hamburger" aria-hidden="true"><span></span><span></span><span></span></span></button><h1><a href="#top">公営競技ニュース</a></h1><div class="race-tabs desktop-race-tabs" aria-label="競技選択">__TABS__</div><div class="update-controls"><time class="updated" datetime="__ISO__">更新 __UPDATED__</time><button class="new-filter" type="button" aria-pressed="false">すべて表示中</button></div></div></header>
<div class="layout"><nav id="news-nav" aria-label="タイトル一覧"><div class="mobile-race-tabs" aria-label="競技選択">__TABS__</div>__NAV__</nav><div class="feed">__NEWS__</div></div></main>
<script>
let activeCategory = __FIRST__;
const tabs=[...document.querySelectorAll('.race-tab')],filterButton=document.querySelector('.new-filter');
function renderFilter(){const onlyNew=filterButton.getAttribute('aria-pressed')==='true';document.querySelectorAll('[data-category]').forEach(node=>{if(node.classList.contains('race-tab'))return;const categoryMatch=node.dataset.category===activeCategory;const newMatch=!onlyNew||node.dataset.new==='true'||node.classList.contains('title-group');node.hidden=!(categoryMatch&&newMatch)});document.querySelectorAll('.title-group').forEach(group=>{if(group.dataset.category!==activeCategory){group.hidden=true;return}group.hidden=!group.querySelector('li:not([hidden])')})}
function selectCategory(slug){activeCategory=slug;tabs.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.category===slug)));renderFilter();window.scrollTo({top:0,behavior:'auto'})}
tabs.forEach(button=>button.addEventListener('click',()=>selectCategory(button.dataset.category)));filterButton.addEventListener('click',()=>{const onlyNew=filterButton.getAttribute('aria-pressed')!=='true';filterButton.setAttribute('aria-pressed',String(onlyNew));filterButton.textContent=onlyNew?'NEWのみ表示中':'すべて表示中';renderFilter()});
const header=document.querySelector('header'),menuButton=document.querySelector('.menu-toggle'),menu=document.getElementById('news-nav');const updateHeaderHeight=()=>document.documentElement.style.setProperty('--header-height',`${header.getBoundingClientRect().height}px`);updateHeaderHeight();new ResizeObserver(updateHeaderHeight).observe(header);function setMenu(open){document.body.classList.toggle('menu-open',open);menuButton.setAttribute('aria-expanded',String(open));menuButton.setAttribute('aria-label',open?'タイトル一覧を閉じる':'タイトル一覧を開く')}menuButton.addEventListener('click',()=>setMenu(menuButton.getAttribute('aria-expanded')!=='true'));menu.addEventListener('click',event=>{if(event.target.closest('a[href^="#"]'))setMenu(false)});document.addEventListener('keydown',event=>{if(event.key==='Escape')setMenu(false)});document.querySelector('header h1 a').addEventListener('click',()=>setMenu(false));
const pullIndicator=document.querySelector('.pull-refresh');let startY=null,startX=0,pullDistance=0;document.addEventListener('touchstart',event=>{startY=event.touches.length===1&&matchMedia('(max-width:700px)').matches&&window.scrollY<=0&&!document.body.classList.contains('menu-open')?event.touches[0].clientY:null;startX=event.touches[0]?.clientX||0;pullDistance=0},{passive:true});document.addEventListener('touchmove',event=>{if(startY===null||event.touches.length!==1)return;const dy=event.touches[0].clientY-startY;if(dy<=10||Math.abs(event.touches[0].clientX-startX)>dy||window.scrollY>0)return;event.preventDefault();pullDistance=Math.min(dy,120);pullIndicator.classList.add('active');pullIndicator.textContent=pullDistance>=90?'離して更新':'下に引いて更新'},{passive:false});function finishPull(){if(pullDistance>=90){pullIndicator.textContent='更新中…';window.location.reload()}else pullIndicator.classList.remove('active');startY=null;pullDistance=0}document.addEventListener('touchend',finishPull,{passive:true});document.addEventListener('touchcancel',()=>{pullIndicator.classList.remove('active');startY=null;pullDistance=0},{passive:true});renderFilter();
</script></body></html>"""
    return (template.replace("__ISO__", updated.isoformat()).replace("__UPDATED__", updated_text)
            .replace("__TABS__", tabs).replace("__NAV__", "".join(nav_groups))
            .replace("__NEWS__", "".join(sections)).replace("__FIRST__", json.dumps(first_slug)))


def build(output: Path, previous_site: Path | None = None, from_snapshot: Path | None = None) -> None:
    now = datetime.now(JST)
    previous_snapshot = load_snapshot((previous_site / "snapshot.json") if previous_site else None)
    previous_items = previous_snapshot.get("items", [])
    output.mkdir(parents=True, exist_ok=True)
    copy_previous_images(previous_site, output)

    if from_snapshot:
        snapshot = load_snapshot(from_snapshot)
        items = snapshot.get("items", [])
        if not items:
            raise RuntimeError("保存済みニュースがありません")
        now = datetime.fromisoformat(snapshot["updated"])
    else:
        client = Client()
        items = []
        for category, slug, theme_url in THEMES:
            previous_category = [x for x in previous_items if x.get("category_slug") == slug]
            try:
                fresh = parse_theme(client.get(theme_url, refresh=True), theme_url, category, slug)
                print(f"{category}: 一覧 {len(fresh)}件", flush=True)
                merged = merge_category(client, fresh, previous_category, output / "images")
                if not merged:
                    raise RuntimeError("0件")
            except Exception as error:
                if previous_category:
                    print(f"::warning::{category}の更新失敗。前回データを維持します: {type(error).__name__}: {error}", file=sys.stderr)
                    merged = [dict(x, is_new=False) for x in previous_category[:MAX_PER_CATEGORY]]
                else:
                    raise
            items.extend(merged)

    if not items:
        raise RuntimeError("ニュースを1件も生成できませんでした")
    (output / "index.html").write_text(render(items, now), encoding="utf-8")
    (output / "snapshot.json").write_text(json.dumps({"format_version": SNAPSHOT_VERSION, "updated": now.isoformat(), "items": items}, ensure_ascii=False), encoding="utf-8")
    for icon in ("favicon.ico", "apple-touch-icon.png"):
        source = Path(__file__).resolve().parents[1] / icon
        if source.is_file():
            shutil.copyfile(source, output / icon)
    (output / "robots.txt").write_text("User-agent: *\nDisallow:\n", encoding="utf-8")
    (output / ".nojekyll").touch()
    print("生成完了", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="site")
    parser.add_argument("--previous-site", type=Path)
    parser.add_argument("--from-snapshot", type=Path)
    parser.add_argument("--empty", action="store_true")
    args = parser.parse_args()
    output = Path(args.output)
    if args.empty:
        output.mkdir(parents=True, exist_ok=True)
        (output / "index.html").write_text(render([], datetime.now(JST)), encoding="utf-8")
        return
    build(output, args.previous_site, args.from_snapshot)


if __name__ == "__main__":
    main()
