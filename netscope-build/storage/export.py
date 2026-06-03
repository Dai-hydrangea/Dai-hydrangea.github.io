"""
NetScope JSON エクスポート

⭐ NewsGlobe ロジック踏襲: 各 globe = 1 カテゴリ。
   ノード = キーワード (タイトルから抽出)、 サイズ = 出現頻度、 エッジ = 共起。
   キーワードをクリックすると、 そのキーワードを含む記事一覧が見える。

orrery.json フォーマット:
{
  "version": "1.1",
  "generated_at": ISO8601,
  "globes": [
    {
      "id": "politics", "label": "POLITICS", "color": "#ff4136",
      "node_count": 60, "total_articles": 234,
      "nodes": [{ text, count, articles: [...top8], pos:[x,y,z] }],
      "edges": [{ a, b, weight }]
    }
  ]
}
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from storage.db import Storage


# カテゴリ → ラベル / 色
CATEGORY_META = {
    "general":       {"label": "GENERAL",       "color": "#00d4ff"},
    "politics":      {"label": "POLITICS",      "color": "#ff4136"},
    "economy":       {"label": "ECONOMY",       "color": "#ffdc00"},
    "tech":          {"label": "TECH",          "color": "#00ff9f"},
    "society":       {"label": "SOCIETY",       "color": "#b88aff"},
    "international": {"label": "INTERNATIONAL", "color": "#ff851b"},
    "culture":       {"label": "CULTURE",       "color": "#ff7eb9"},
    "sports":        {"label": "SPORTS",        "color": "#7fdbff"},
    "academic":      {"label": "ACADEMIC",      "color": "#9be7c4"},
}

# NewsGlobe と同じ stop words
STOP_WORDS = {
    "について", "による", "として", "ている", "された", "される",
    "など", "ため", "この", "その", "また", "さらに", "めぐり",
    "めぐる", "ことで", "において", "に関して", "よると",
    "以上", "以下", "以内", "以前", "以降", "最大", "最小", "最高", "最低",
    "報道", "発表", "報告", "関係", "問題", "状況", "対応", "実施",
    "予定", "確認", "影響", "可能", "必要", "重要", "開始", "終了",
}

MAX_KEYWORDS_PER_GLOBE = 110       # ⭐ ノード数 80→110 (球数減らして 1 球あたり密度 UP)
MAX_KEYWORD_EDGES = 180
TOP_ARTICLES_PER_KEYWORD = 8

# ⭐ 球の物理半径 (キーワード表面配置半径)。 buildGlobe の sphere mesh と完全一致
GLOBE_RADIUS = 170


def _extract_keywords(text: str) -> list[str]:
    """NewsGlobe と同じ抽出ロジック: カタカナ 3+ / 漢字 2-6"""
    found: set[str] = set()
    for m in re.finditer(r"[ァ-ヴー]{3,}", text):
        found.add(m.group())
    for m in re.finditer(r"[一-龯]{2,6}", text):
        w = m.group()
        if w not in STOP_WORDS:
            found.add(w)
    return list(found)


def _spring_layout(n: int, edges: list[tuple[int, int, float]],
                   *, radius: float = 90.0, iterations: int = 200) -> list[list[float]]:
    """球面スプリングレイアウト (NewsGlobe そのまま)"""
    if n == 0:
        return []
    pts: list[list[float]] = []
    for i in range(n):
        phi = math.acos(1 - 2 * (i + 0.5) / n)
        theta = math.pi * (1 + math.sqrt(5)) * i
        pts.append([
            math.sin(phi) * math.cos(theta),
            math.cos(phi),
            math.sin(phi) * math.sin(theta),
        ])

    if not edges:
        return [[p[0] * radius, p[1] * radius, p[2] * radius] for p in pts]

    max_w = max((w for _, _, w in edges), default=1.0)
    adj: dict[tuple[int, int], float] = {}
    for a, b, w in edges:
        if 0 <= a < n and 0 <= b < n:
            key = (min(a, b), max(a, b))
            adj[key] = w / max_w

    K_ATTR = 0.16
    K_REP = 0.020   # ⭐ 斥力さらに強化 (0.012 → 0.020) でキーワード同士の距離を最大化

    for step in range(iterations):
        temp = 1.0 - step / iterations
        forces = [[0.0, 0.0, 0.0] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                dx = pts[j][0] - pts[i][0]
                dy = pts[j][1] - pts[i][1]
                dz = pts[j][2] - pts[i][2]
                dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                if dist < 1e-6:
                    continue
                ux, uy, uz = dx / dist, dy / dist, dz / dist
                rep = K_REP / (dist * dist)
                for c, u in enumerate((ux, uy, uz)):
                    forces[i][c] -= u * rep
                    forces[j][c] += u * rep
                key = (min(i, j), max(i, j))
                if key in adj:
                    attr = K_ATTR * adj[key] * dist
                    for c, u in enumerate((ux, uy, uz)):
                        forces[i][c] += u * attr
                        forces[j][c] -= u * attr

        for i in range(n):
            nx, ny, nz = pts[i]
            fx, fy, fz = forces[i]
            dot = fx * nx + fy * ny + fz * nz
            tx, ty, tz = fx - dot * nx, fy - dot * ny, fz - dot * nz
            pts[i][0] += tx * temp
            pts[i][1] += ty * temp
            pts[i][2] += tz * temp
            d = math.sqrt(pts[i][0] ** 2 + pts[i][1] ** 2 + pts[i][2] ** 2)
            if d > 0:
                pts[i] = [pts[i][k] / d for k in range(3)]

    return [[p[0] * radius, p[1] * radius, p[2] * radius] for p in pts]


def _title_for_index(c: dict) -> str:
    """キーワード抽出 / 表示用のメインタイトル。 title_ja があればそれ、 無ければ原題"""
    return (c.get("title_ja") or c.get("title") or "")


def _build_globe(category: str, contents: list[dict]) -> dict | None:
    """1 カテゴリ → 1 球儀 (NewsGlobe 互換構造)"""
    if not contents:
        return None
    meta = CATEGORY_META.get(category, {"label": category.upper(), "color": "#00d4ff"})

    # ① キーワードカウント (NewsGlobe と同じ重み: カタカナ x2、 漢字 x1)
    counter: Counter[str] = Counter()
    for c in contents:
        title = _title_for_index(c)
        for m in re.finditer(r"[ァ-ヴー]{3,}", title):
            counter[m.group()] += 2
        for m in re.finditer(r"[一-龯]{2,6}", title):
            w = m.group()
            if w not in STOP_WORDS:
                counter[w] += 1

    top_keywords = counter.most_common(MAX_KEYWORDS_PER_GLOBE)
    if not top_keywords:
        return None
    top_set = {w for w, _ in top_keywords}

    # ② 各キーワードの代表記事 (タップ時に右パネルで表示する)
    word_articles: dict[str, list[dict]] = defaultdict(list)
    for c in contents:
        title_ja = _title_for_index(c)
        title_orig = c.get("title") or ""
        kws_in_title = [k for k in _extract_keywords(title_ja) if k in top_set]
        for kw in kws_in_title:
            word_articles[kw].append({
                "title": title_ja,
                "title_orig": title_orig if (title_orig and title_orig != title_ja) else None,
                "url": c.get("url", ""),
                "summary": (c.get("summary") or c.get("body", "") or "")[:200],
                "source_name": c.get("source_name", ""),
                "published_at": c.get("published_at", ""),
                "score": c.get("score", 0),
            })
    # 各キーワードの記事は score 降順 → top N 取る
    for kw in word_articles:
        word_articles[kw].sort(key=lambda a: a.get("score", 0), reverse=True)

    # ③ 共起ペア (同一タイトル内のキーワード組合せ)
    pair_counter: Counter[tuple[str, str]] = Counter()
    for c in contents:
        title = _title_for_index(c)
        kws = sorted(set(k for k in _extract_keywords(title) if k in top_set))
        for i in range(len(kws)):
            for j in range(i + 1, len(kws)):
                pair_counter[(kws[i], kws[j])] += 1

    # ④ keyword → index map
    word_idx = {w: i for i, (w, _) in enumerate(top_keywords)}

    edges_raw: list[tuple[int, int, float]] = []
    for (a, b), w in pair_counter.most_common(MAX_KEYWORD_EDGES):
        if w >= 1:
            edges_raw.append((word_idx[a], word_idx[b], float(w)))

    # ⑤ spring layout (GLOBE_RADIUS で配置、 buildGlobe sphere と完全一致)
    positions = _spring_layout(len(top_keywords), edges_raw, radius=float(GLOBE_RADIUS))

    # ⑥ JSON 構造
    nodes = []
    for i, (w, count) in enumerate(top_keywords):
        articles_all = word_articles.get(w, [])
        en_count = sum(1 for a in articles_all if a.get("title_orig"))
        en_ratio = en_count / max(1, len(articles_all))
        nodes.append({
            "text": w,
            "count": count,
            "articles": articles_all[:TOP_ARTICLES_PER_KEYWORD],
            "pos": positions[i] if i < len(positions) else [0, 0, 0],
            "en_ratio": round(en_ratio, 2),
        })

    edges = [
        {"a": top_keywords[a][0], "b": top_keywords[b][0], "weight": w}
        for a, b, w in edges_raw
    ]

    return {
        "id": category,
        "label": meta["label"],
        "color": meta["color"],
        "node_count": len(nodes),
        "total_articles": len(contents),
        "nodes": nodes,
        "edges": edges,
    }


def export_orrery(storage: Storage, output_path: str | Path,
                  *, retain_days: int = 7, summarizer=None) -> dict:
    """DB から retain_days 以内の記事を読んで orrery.json を生成。
    summarizer (callable) を渡すと各 globe に summary / clusters を追加"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    all_items = storage.get_recent(days=retain_days)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for it in all_items:
        by_cat[it.get("category") or "general"].append(it)

    ordered_cats = [c for c in CATEGORY_META if c in by_cat] + \
                   [c for c in by_cat if c not in CATEGORY_META]

    globes = []
    for cat in ordered_cats:
        g = _build_globe(cat, by_cat[cat])
        if g:
            globes.append(g)

    # ─── DAILY BRIEF + クラスタラベル (任意) ───
    if summarizer is not None:
        for g in globes:
            try:
                extra = summarizer(g) or {}
            except Exception as e:
                print(f"[Export] summarizer error on {g.get('label')}: {e}")
                extra = {}
            if extra.get("summary"):
                g["summary"] = extra["summary"]
            if extra.get("clusters"):
                g["clusters"] = extra["clusters"]

    payload = {
        "version": "1.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_articles": len(all_items),
        "total_keywords": sum(g["node_count"] for g in globes),
        "globes": globes,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[Export] orrery.json: {len(globes)} globes, "
          f"{payload['total_keywords']} keywords, {len(all_items)} articles → {output_path}")
    return payload
