"""
NetScope RSS Collector

複数 RSS フィードからニュース見出しを取得して dict のリストで返す。
NewsGlobe の fetch_rss を拡張、 feeds.yaml で定義された全フィードを走査。
標準ライブラリのみで動作 (feedparser は将来 ATOM 形式の頑健対応で導入候補)。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree


def _to_iso(text: str | None) -> str:
    """RSS の pubDate / dc:date を ISO8601 風に正規化。 失敗したら空文字"""
    if not text:
        return ""
    text = text.strip()
    # よく見かけるフォーマットを順に試す
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return text  # 最後はそのまま


def _make_id(url: str, title: str) -> str:
    """ URL + title から短い hash ID を作る (重複排除キー)"""
    h = hashlib.sha1(f"{url}::{title}".encode("utf-8")).hexdigest()
    return h[:16]


def fetch_rss(name: str, url: str, category: str, *, timeout: int = 10, max_items: int = 50) -> list[dict]:
    """
    1 つの RSS フィードを取得。 RSS 2.0 / RDF (RSS 1.0) / Atom 風 を試行。
    """
    items: list[dict] = []
    req = Request(url, headers={"User-Agent": "NetScope/1.1 (+https://github.com/Dai-hydrangea)"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        root = ElementTree.fromstring(data)
    except (URLError, HTTPError, ElementTree.ParseError, OSError) as e:
        print(f"  [{name}] fetch error: {e}")
        return items

    # ─── RSS 2.0 ───
    for item in root.findall(".//item"):
        t = item.find("title")
        l = item.find("link")
        d = item.find("pubDate") or item.find("date")
        desc = item.find("description")
        if t is None or not (t.text or "").strip():
            continue
        link = ""
        if l is not None:
            link = (l.text or l.tail or "").strip()
        items.append({
            "id": _make_id(link or t.text, t.text or ""),
            "source": "rss",
            "source_name": name,
            "category": category,
            "title": (t.text or "").strip(),
            "body": (desc.text or "").strip() if desc is not None else "",
            "url": link,
            "published_at": _to_iso(d.text if d is not None else None),
            "score": 0,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

    # ─── RSS 1.0 / RDF ───
    rdf_ns = {"rss": "http://purl.org/rss/1.0/", "dc": "http://purl.org/dc/elements/1.1/"}
    for item in root.findall(".//rss:item", rdf_ns):
        t = item.find("rss:title", rdf_ns)
        l = item.find("rss:link", rdf_ns)
        d = item.find("dc:date", rdf_ns)
        desc = item.find("rss:description", rdf_ns)
        if t is None or not (t.text or "").strip():
            continue
        link = (l.text or "").strip() if l is not None else ""
        items.append({
            "id": _make_id(link or t.text, t.text or ""),
            "source": "rss",
            "source_name": name,
            "category": category,
            "title": (t.text or "").strip(),
            "body": (desc.text or "").strip() if desc is not None else "",
            "url": link,
            "published_at": _to_iso(d.text if d is not None else None),
            "score": 0,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

    # ─── Atom ───
    atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//atom:entry", atom_ns):
        t = entry.find("atom:title", atom_ns)
        l = entry.find("atom:link", atom_ns)
        d = entry.find("atom:published", atom_ns) or entry.find("atom:updated", atom_ns)
        sm = entry.find("atom:summary", atom_ns) or entry.find("atom:content", atom_ns)
        if t is None or not (t.text or "").strip():
            continue
        link = ""
        if l is not None:
            link = (l.get("href") or "").strip()
        items.append({
            "id": _make_id(link or t.text, t.text or ""),
            "source": "rss",
            "source_name": name,
            "category": category,
            "title": (t.text or "").strip(),
            "body": (sm.text or "").strip() if sm is not None else "",
            "url": link,
            "published_at": _to_iso(d.text if d is not None else None),
            "score": 0,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

    # 重複排除 + 上限切り詰め
    seen: set[str] = set()
    unique: list[dict] = []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        unique.append(it)
        if len(unique) >= max_items:
            break

    print(f"  [{name}] {len(unique)} items")
    return unique


def fetch_all(feeds: Iterable[dict], *, timeout: int = 10, max_items: int = 50) -> list[dict]:
    """feeds.yaml の `feeds` リストを走査して全件取得"""
    print(f"[RSS] fetching {len(list(feeds))} feeds...")
    all_items: list[dict] = []
    for feed in feeds:
        name = feed.get("name", "?")
        url = feed.get("url")
        category = feed.get("category", "general")
        if not url:
            continue
        all_items.extend(fetch_rss(name, url, category, timeout=timeout, max_items=max_items))
    print(f"[RSS] total {len(all_items)} items")
    return all_items
