"""
Gemini で各球儀 (カテゴリ) の DAILY BRIEF + クラスタラベルを生成。

設計:
- 球儀 1 個ごとに 1 回の Gemini 呼び出し (8 globes × 8 calls/refresh)
- 入力: カテゴリ名 + 上位 30 キーワード + 代表記事タイトル 15 件
- 出力: { summary: "3-5 行のブリーフ", clusters: [{name, keywords}] }
- 失敗時は globe にフィールド追加せず (UI 側で省略表示)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


GEMINI_MODEL = os.environ.get("NETSCOPE_GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)
TIMEOUT_SEC = 40


def _gemini_summarize(label: str, keywords: list[str], titles: list[str],
                       api_key: str, retries: int = 3) -> dict | None:
    """1 球儀分の summary + clusters を生成。 失敗時 None"""
    # ⭐ 圧縮プロンプト: instruction を最短に、 形式は schema で示す
    prompt = (
        f"カテゴリ: {label}\n"
        f"JSON: {{\"summary\":\"3-5行の日本語(各50-70字)、具体的な出来事/主体/数字\","
        f"\"clusters\":[{{\"name\":\"15字以内\",\"keywords\":[\"入力KWのみ\"]}}](3-6個)}}\n"
        f"KW: {json.dumps(keywords, ensure_ascii=False)}\n"
        f"T: {json.dumps(titles, ensure_ascii=False)}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.3,
        },
    }
    data = json.dumps(body).encode("utf-8")
    url = f"{GEMINI_URL}?key={api_key}"

    payload = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                payload = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt + 1 < retries:
                wait = 2 ** attempt
                print(f"[Summarize/{label}] HTTP {e.code}, retry in {wait}s")
                time.sleep(wait)
                continue
            msg = e.read().decode("utf-8", errors="replace")[:200]
            print(f"[Summarize/{label}] HTTP {e.code}: {msg}")
            return None
        except Exception as e:
            print(f"[Summarize/{label}] error: {e}")
            return None
    if payload is None:
        return None

    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
    except Exception as e:
        print(f"[Summarize/{label}] parse error: {e}")
        return None

    if not isinstance(result, dict):
        return None
    summary = str(result.get("summary", "")).strip()
    clusters_raw = result.get("clusters", [])
    clusters = []
    kw_set = set(keywords)
    if isinstance(clusters_raw, list):
        for c in clusters_raw:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name", "")).strip()
            kws = [str(k).strip() for k in c.get("keywords", [])
                   if isinstance(k, str) and k.strip() in kw_set]
            if name and kws:
                clusters.append({"name": name, "keywords": kws})
    if not summary and not clusters:
        return None
    return {"summary": summary, "clusters": clusters}


def _globe_hash(label: str, nodes: list[dict]) -> str:
    """球儀の content fingerprint。 同じなら API スキップ判定に使う"""
    sig = label + "|" + "|".join(f"{n.get('text','')}:{n.get('count',0)}" for n in nodes[:30])
    return hashlib.md5(sig.encode("utf-8")).hexdigest()[:12]


def _load_cache(cache_path: Path) -> dict:
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text())
    except Exception:
        return {}


def _save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def make_summarizer(api_key: str, cache_path: Path | None = None):
    """export_orrery に渡す callable を作る。 content-hash キャッシュで Gemini call をスキップ可能"""
    if cache_path is None:
        cache_path = Path(__file__).resolve().parent.parent / "data" / "summary_cache.json"
    cache = _load_cache(cache_path)
    api_calls = [0]
    cache_hits = [0]

    def summarizer(globe: dict) -> dict:
        label = globe.get("label", "")
        nodes = globe.get("nodes", [])
        gid = globe.get("id", label)
        hash_now = _globe_hash(label, nodes)
        cached = cache.get(gid)
        if cached and cached.get("hash") == hash_now and cached.get("summary"):
            cache_hits[0] += 1
            print(f"[Summarize/{label}] 🟢 cache hit (no API call)")
            return {"summary": cached["summary"], "clusters": cached.get("clusters", [])}

        # ⭐ 圧縮: top 20 KW + 10 titles に絞る (前 30 + 15)
        keywords = [n["text"] for n in nodes[:20]]
        titles: list[str] = []
        seen = set()
        for n in nodes[:10]:
            for a in n.get("articles", [])[:2]:
                t = a.get("title")
                if t and t not in seen:
                    titles.append(t)
                    seen.add(t)
                if len(titles) >= 10:
                    break
            if len(titles) >= 10:
                break
        if not keywords or not titles:
            return {}
        result = _gemini_summarize(label, keywords, titles, api_key)
        if result is None:
            return {}
        api_calls[0] += 1
        cache[gid] = {"hash": hash_now, **result}
        _save_cache(cache_path, cache)
        print(f"[Summarize/{label}] summary {len(result['summary'])} chars, "
              f"{len(result['clusters'])} clusters")
        return result

    # 内部 stats を見せたい時用
    summarizer.stats = lambda: {"api_calls": api_calls[0], "cache_hits": cache_hits[0]}
    return summarizer
