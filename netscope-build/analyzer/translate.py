"""
Gemini 2.0 Flash でニュースタイトルを日本語訳。

設計:
- 日本語 (ひらがな / カタカナ / 漢字 含む) なタイトルは翻訳せず title_ja = title でコピー
- 非日本語は Gemini に batch (20 件単位) で投げて JSON 配列で受ける
- 失敗時は原題を title_ja に入れて諦める (球儀に乗らないだけ)
- API キー: 環境変数 GEMINI_API_KEY (Google AI Studio で発行)
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from storage.db import Storage


GEMINI_MODEL = os.environ.get("NETSCOPE_GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

# ひらがな / カタカナ / 漢字 のいずれかが含まれていたら日本語扱い
JA_RE = re.compile(r"[぀-ヿ一-鿿]")

BATCH_SIZE = 20
TIMEOUT_SEC = 40


def detect_lang(text: str) -> str:
    if not text:
        return "ja"  # 空は翻訳不要
    return "ja" if JA_RE.search(text) else "en"


def _gemini_translate(titles: list[str], api_key: str, retries: int = 3) -> list[str] | None:
    """20 件以内のタイトルを日本語訳。 503/429 は exp backoff で retry。 失敗時 None"""
    prompt = (
        "あなたはプロの翻訳者です。 次のニュース・論文タイトルを自然で簡潔な日本語に訳してください。\n"
        "- 固有名詞 (会社名 / 製品名 / 人名 / 地名) は一般的なカタカナ表記または英字のまま\n"
        "- 学術論文 (arXiv) なら技術用語を保つ\n"
        "- 訳文だけを JSON 配列として返す (入力と同じ長さ、 同じ順序)\n\n"
        f"入力:\n{json.dumps(titles, ensure_ascii=False)}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
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
                print(f"[Translate] HTTP {e.code}, retry in {wait}s ({attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            msg = e.read().decode("utf-8", errors="replace")[:200]
            print(f"[Translate] HTTP {e.code}: {msg}")
            return None
        except Exception as e:
            print(f"[Translate] error: {e}")
            return None
    if payload is None:
        return None

    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
    except Exception as e:
        print(f"[Translate] parse error: {e}, payload={payload}")
        return None

    if not isinstance(result, list) or len(result) != len(titles):
        print(f"[Translate] ⚠️ length mismatch: got {len(result) if isinstance(result, list) else '?'}, want {len(titles)}")
        return None
    return [str(s) for s in result]


def translate_untranslated(storage: Storage, api_key: str, max_items: int = 5000) -> int:
    """DB から未翻訳の記事を取得 → 日本語タイトル化 → DB に書き戻す。 翻訳件数を返す。
    内部で 500 件ずつページネーションして全件処理する"""
    total_done = 0
    page_size = 500
    while total_done < max_items:
        rows = storage.get_untranslated(limit=page_size)
        if not rows:
            break
        before = total_done
        total_done += _translate_page(storage, rows, api_key)
        if total_done == before:
            # 何も進まなかったら infinite loop 防止
            break
    return total_done


def _translate_page(storage: Storage, rows: list[dict], api_key: str) -> int:
    """1 ページ (500 件まで) を翻訳して書き戻す"""
    if not rows:
        return 0

    # 日本語タイトルは即セルフコピー、 それ以外は Gemini 行き
    self_copy: dict[str, tuple[str, str]] = {}
    to_translate: list[dict] = []
    for r in rows:
        title = r["title"] or ""
        if detect_lang(title) == "ja":
            self_copy[r["id"]] = (title, "ja")
        else:
            to_translate.append(r)

    if self_copy:
        storage.update_translations(self_copy)
        print(f"[Translate] {len(self_copy)} JP rows self-copied")

    if not to_translate:
        return len(self_copy)

    translated_total = 0
    for start in range(0, len(to_translate), BATCH_SIZE):
        chunk = to_translate[start:start + BATCH_SIZE]
        titles = [r["title"] for r in chunk]
        ja_list = _gemini_translate(titles, api_key)
        if ja_list is None:
            # フォールバック: 原題のまま title_ja に入れる (再試行可能にしたいので lang のみ記録)
            print(f"[Translate] fallback: {len(chunk)} titles kept original")
            continue
        updates: dict[str, tuple[str, str]] = {}
        for r, ja in zip(chunk, ja_list):
            updates[r["id"]] = (ja, "en")
        storage.update_translations(updates)
        translated_total += len(updates)
        print(f"[Translate] {len(updates)} EN→JA done (batch {start // BATCH_SIZE + 1})")

    return len(self_copy) + translated_total
