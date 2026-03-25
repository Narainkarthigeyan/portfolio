import os
import re
import mimetypes
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://narain-karthigeyan.base44.app/"
OUT_DIR = "site-clone"

session = requests.Session()
session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
    }
)

visited_pages = set()
downloaded_assets = set()


def is_same_domain(url: str) -> bool:
    return urlparse(url).netloc == urlparse(BASE_URL).netloc


def normalize_url(url: str) -> str:
    url, _ = urldefrag(url)
    if not url:
        return ""
    p = urlparse(url)
    clean = p._replace(query="", fragment="")
    return clean.geturl()


def url_to_local_path(url: str, is_page: bool) -> str:
    p = urlparse(url)
    path = p.path

    if not path or path.endswith("/"):
        path = path + "index.html"

    if is_page and not os.path.splitext(path)[1]:
        if not path.endswith("/"):
            path = path + "/"
        path = path + "index.html"

    if path.startswith("/"):
        path = path[1:]

    return os.path.join(OUT_DIR, path.replace("/", os.sep))


def ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def should_treat_as_page(url: str, content_type: str) -> bool:
    p = urlparse(url)
    ext = os.path.splitext(p.path)[1].lower()
    if ext in {".html", ".htm"}:
        return True
    if ext:
        return False
    return "text/html" in (content_type or "")


def extract_css_urls(css_text: str):
    # url(...) patterns
    urls = re.findall(r"url\(([^)]+)\)", css_text)
    cleaned = []
    for item in urls:
        item = item.strip().strip('"\'')
        if not item or item.startswith("data:"):
            continue
        cleaned.append(item)
    return cleaned


def fetch_asset(url: str):
    url = normalize_url(url)
    if not url or not is_same_domain(url) or url in downloaded_assets:
        return

    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
    except Exception:
        return

    content_type = r.headers.get("Content-Type", "")
    if should_treat_as_page(url, content_type):
        # handled by page crawler
        return

    local_path = url_to_local_path(url, is_page=False)
    ensure_parent(local_path)
    with open(local_path, "wb") as f:
        f.write(r.content)

    downloaded_assets.add(url)

    # parse CSS for nested assets
    if "text/css" in content_type or local_path.lower().endswith(".css"):
        try:
            text = r.text
            for ref in extract_css_urls(text):
                abs_ref = urljoin(url, ref)
                if is_same_domain(abs_ref):
                    fetch_asset(abs_ref)
        except Exception:
            pass


def rewrite_html_links(soup: BeautifulSoup, page_url: str):
    attr_map = {
        "a": ["href"],
        "link": ["href"],
        "script": ["src"],
        "img": ["src", "srcset"],
        "source": ["src", "srcset"],
        "video": ["src", "poster"],
        "audio": ["src"],
        "iframe": ["src"],
    }

    for tag, attrs in attr_map.items():
        for node in soup.find_all(tag):
            for attr in attrs:
                val = node.get(attr)
                if not val:
                    continue

                if attr == "srcset":
                    parts = []
                    for part in val.split(","):
                        chunk = part.strip().split()
                        if not chunk:
                            continue
                        raw_url = chunk[0]
                        abs_url = urljoin(page_url, raw_url)
                        if is_same_domain(abs_url):
                            local = "/" + os.path.relpath(
                                url_to_local_path(abs_url, is_page=False), OUT_DIR
                            ).replace(os.sep, "/")
                            chunk[0] = local
                        parts.append(" ".join(chunk))
                    node[attr] = ", ".join(parts)
                    continue

                if val.startswith("data:") or val.startswith("mailto:") or val.startswith("tel:"):
                    continue

                abs_url = urljoin(page_url, val)
                if not is_same_domain(abs_url):
                    continue

                normalized = normalize_url(abs_url)
                is_page = val.endswith("/") or os.path.splitext(urlparse(normalized).path)[1] in {"", ".html", ".htm"}
                local_target = url_to_local_path(normalized, is_page=is_page)
                rel = "/" + os.path.relpath(local_target, OUT_DIR).replace(os.sep, "/")
                node[attr] = rel


def crawl_page(url: str):
    url = normalize_url(url)
    if not url or not is_same_domain(url) or url in visited_pages:
        return

    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
    except Exception:
        return

    content_type = r.headers.get("Content-Type", "")
    if not should_treat_as_page(url, content_type):
        fetch_asset(url)
        return

    visited_pages.add(url)

    soup = BeautifulSoup(r.text, "html.parser")

    to_visit_pages = set()
    to_fetch_assets = set()

    for node in soup.find_all(["a", "link", "script", "img", "source", "video", "audio", "iframe"]):
        for attr in ["href", "src", "poster"]:
            val = node.get(attr)
            if not val:
                continue
            if val.startswith("data:") or val.startswith("mailto:") or val.startswith("tel:"):
                continue
            abs_url = normalize_url(urljoin(url, val))
            if not abs_url or not is_same_domain(abs_url):
                continue
            ext = os.path.splitext(urlparse(abs_url).path)[1].lower()
            if attr == "href" and ext in {"", ".html", ".htm"}:
                to_visit_pages.add(abs_url)
            elif attr == "href" and node.name == "a" and ext in {"", ".html", ".htm"}:
                to_visit_pages.add(abs_url)
            else:
                to_fetch_assets.add(abs_url)

        srcset = node.get("srcset")
        if srcset:
            for part in srcset.split(","):
                raw = part.strip().split()
                if not raw:
                    continue
                abs_url = normalize_url(urljoin(url, raw[0]))
                if abs_url and is_same_domain(abs_url):
                    to_fetch_assets.add(abs_url)

    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        for css_url in extract_css_urls(css_text):
            abs_css = normalize_url(urljoin(url, css_url))
            if abs_css and is_same_domain(abs_css):
                to_fetch_assets.add(abs_css)

    rewrite_html_links(soup, url)

    local_page = url_to_local_path(url, is_page=True)
    ensure_parent(local_page)
    with open(local_page, "w", encoding="utf-8") as f:
        f.write(str(soup))

    for asset_url in sorted(to_fetch_assets):
        fetch_asset(asset_url)

    for page_url in sorted(to_visit_pages):
        crawl_page(page_url)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    crawl_page(BASE_URL)
    print(f"Mirrored pages: {len(visited_pages)}")
    print(f"Downloaded assets: {len(downloaded_assets)}")
    print(f"Output folder: {os.path.abspath(OUT_DIR)}")


if __name__ == "__main__":
    main()
