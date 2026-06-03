#!/usr/bin/env python3
"""
NetScope CLI ── News Orrery 用のメインスクリプト

使い方:
    python netscope.py refresh              # 全フィード取得 → DB 保存 → JSON 出力
    python netscope.py refresh --no-icloud  # iCloud 配置スキップ (ローカル web/ のみ)
    python netscope.py serve                # ローカル http サーバーで web/ 提供
    python netscope.py cleanup              # 古いデータを削除
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 自モジュール
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from collectors.rss import fetch_all
from storage.db import Storage
from storage.export import export_orrery


# ─── .env を ROOT から軽く読み込む (python-dotenv なしで動く) ──────
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")


# ─── 設定読み込み (PyYAML 不要、 自前で簡易 parse) ──────────────────────
def _load_yaml(path: Path) -> dict:
    """超簡易 YAML reader (feeds.yaml の構造に最適化)"""
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text())
    except ImportError:
        pass
    # フォールバック: feeds.yaml の構造前提でパース
    text = path.read_text()
    feeds: list[dict] = []
    settings: dict = {}
    current: dict | None = None
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("feeds:"):
            section = "feeds"; continue
        if line.startswith("settings:"):
            section = "settings"; continue
        if section == "feeds":
            stripped = line.lstrip()
            if stripped.startswith("- "):
                if current is not None:
                    feeds.append(current)
                current = {}
                kv = stripped[2:]
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    current[k.strip()] = v.strip()
            elif current is not None and ":" in stripped:
                k, v = stripped.split(":", 1)
                current[k.strip()] = v.strip()
        elif section == "settings" and ":" in line.strip():
            k, v = line.strip().split(":", 1)
            v = v.strip()
            try:
                v = int(v)
            except ValueError:
                pass
            settings[k.strip()] = v
    if current is not None:
        feeds.append(current)
    return {"feeds": feeds, "settings": settings}


# ─── icloud 配置 ───
def deploy_to_icloud(web_dir: Path, target_dir: str) -> None:
    target = Path(os.path.expanduser(target_dir))
    if not target.parent.exists():
        print(f"[icloud] parent dir not found, skip: {target.parent}")
        return
    target.mkdir(parents=True, exist_ok=True)
    # web/ 以下を target に同期 (上書き)
    # ⚠️ iCloud は同期中ロックで copy が deadlock することがある (Errno 11)。
    #    iCloud は補助用途なので、 失敗しても警告だけ出して GitHub Pages 配信は止めない。
    try:
        for item in web_dir.iterdir():
            dst = target / item.name
            if item.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
        print(f"[icloud] deployed to {target}")
    except OSError as e:
        print(f"[icloud] ⚠️ コピー失敗 (skip、 GitHub Pages 配信は継続): {e}")


# ─── 1日1回ガード (前回更新日を記録 → 同じ日はスキップ) ───
STAMP_FILE = ROOT / "data" / ".last_deploy"


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _already_today() -> bool:
    try:
        return STAMP_FILE.read_text().strip() == _today_str()
    except FileNotFoundError:
        return False


def _stamp_today() -> None:
    STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STAMP_FILE.write_text(_today_str())


# ─── refresh ───
def cmd_refresh(args: argparse.Namespace) -> int:
    config = _load_yaml(ROOT / "feeds.yaml")
    feeds = config.get("feeds", [])
    settings = config.get("settings", {})
    timeout = int(settings.get("timeout_sec", 10))
    max_items = int(settings.get("max_items_per_feed", 50))
    retain_days = int(settings.get("retain_days", 30))

    if not feeds:
        print("⚠️ feeds.yaml に feeds が定義されていません")
        return 1

    items = fetch_all(feeds, timeout=timeout, max_items=max_items)
    if not items:
        print("⚠️ 取得 0 件、 ネット接続/フィード URL を確認してください")
        return 1

    db_path = ROOT / "data" / "netscope.db"
    storage = Storage(db_path)
    n = storage.upsert_contents(items)
    storage.cleanup(retain_days=retain_days)
    print(f"[DB] upserted {n} items, retained {retain_days} days")

    # ─── 翻訳ステップ (GEMINI_API_KEY あれば実行) ───
    api_key = os.environ.get("GEMINI_API_KEY")
    if not args.no_translate:
        if api_key:
            from analyzer.translate import translate_untranslated
            translated = translate_untranslated(storage, api_key)
            if translated:
                print(f"[Translate] total {translated} rows updated")
        else:
            print("[Translate] GEMINI_API_KEY 未設定、 翻訳スキップ")

    # ─── 要約 + クラスタリング (GEMINI_API_KEY あれば実行) ───
    summarizer = None
    if not args.no_summarize and api_key:
        from analyzer.summarize import make_summarizer
        summarizer = make_summarizer(api_key)
    elif not api_key:
        print("[Summarize] GEMINI_API_KEY 未設定、 要約スキップ")

    export_dir = ROOT / "web" / "data"
    export_dir.mkdir(parents=True, exist_ok=True)
    export_orrery(storage, export_dir / "orrery.json",
                  retain_days=min(retain_days, 7), summarizer=summarizer)

    if not args.no_icloud:
        icloud_target = settings.get(
            "icloud_dir",
            "~/Library/Mobile Documents/com~apple~CloudDocs/NetScope",
        )
        # 環境変数 NETSCOPE_ICLOUD で上書き可
        icloud_target = os.environ.get("NETSCOPE_ICLOUD", icloud_target)
        deploy_to_icloud(ROOT / "web", icloud_target)

    storage.close()
    print("[refresh] done.")
    return 0


# ─── serve ───
def cmd_serve(args: argparse.Namespace) -> int:
    """web/ を http サーバーで提供 (動作確認用)"""
    import http.server
    import socketserver

    web_dir = ROOT / "web"
    if not (web_dir / "index.html").exists():
        print(f"⚠️ web/index.html がありません。 まず refresh してください")
        return 1

    os.chdir(web_dir)
    port = int(args.port)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        url = f"http://localhost:{port}/"
        print(f"[serve] {url}  (Ctrl+C で停止)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[serve] stopped.")
    return 0


# ─── deploy (refresh + cp to gh-pages + git push) ───
def cmd_deploy(args: argparse.Namespace) -> int:
    """refresh して GitHub Pages リポジトリへコピー → commit → push"""
    # ⭐ 1日1回ガード: launchd は --once-per-day で呼ぶので、 本日更新済みなら
    #    フィード取得 (Gemini 翻訳/要約の課金) ごとスキップする。 手動 deploy は強制実行。
    if getattr(args, "once_per_day", False) and _already_today():
        print(f"[deploy] 本日 ({_today_str()}) は更新済み、 スキップ (--once-per-day)")
        return 0

    rc = cmd_refresh(args)
    if rc != 0:
        return rc

    ghpages = Path(os.path.expanduser(
        os.environ.get("NETSCOPE_GHPAGES_DIR",
                       "~/Developer/Dai-hydrangea.github.io/netscope")
    ))
    if not ghpages.parent.exists():
        print(f"[deploy] ⚠️ GitHub Pages リポジトリが見つかりません: {ghpages.parent}")
        return 1
    ghpages.mkdir(parents=True, exist_ok=True)
    (ghpages / "data").mkdir(parents=True, exist_ok=True)

    shutil.copy2(ROOT / "web" / "index.html", ghpages / "index.html")
    shutil.copy2(ROOT / "web" / "data" / "orrery.json", ghpages / "data" / "orrery.json")
    print(f"[deploy] copied to {ghpages}")

    repo = ghpages.parent
    rel_dir = ghpages.name
    try:
        # 変更があれば commit + push
        subprocess.run(
            ["git", "-C", str(repo), "add",
             f"{rel_dir}/index.html", f"{rel_dir}/data/orrery.json"],
            check=True,
        )
        diff = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
        )
        if diff.returncode == 0:
            print("[deploy] no changes, skip commit")
            _stamp_today()
            return 0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m",
             f"NetScope auto-deploy {ts}"],
            check=True,
        )
        if not args.no_push:
            subprocess.run(["git", "-C", str(repo), "push", "origin", "main"], check=True)
            print("[deploy] pushed to origin/main")
        else:
            print("[deploy] commit done, push skipped (--no-push)")
    except subprocess.CalledProcessError as e:
        print(f"[deploy] ⚠️ git error: {e}")
        return 1
    _stamp_today()
    return 0


# ─── install-launchd (Mac 用の自動更新タイマー) ───
LAUNCHD_LABEL = "com.dai.netscope"

def cmd_install_launchd(args: argparse.Namespace) -> int:
    """~/Library/LaunchAgents/com.dai.netscope.plist を生成して launchctl load"""
    home = Path(os.path.expanduser("~"))
    plist_path = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # この netscope.py を動かしている python を使う (= 確実に依存が揃っている)
    python_path = sys.executable or shutil.which("python3") or "/usr/bin/python3"
    script_path = ROOT / "netscope.py"
    log_out = "/tmp/netscope.log"
    log_err = "/tmp/netscope.err"
    refresh_hour = int(getattr(args, "hour", 7))

    # ⭐ 1日1回 deploy。 発火源は 2 つ:
    #   ① 毎日 refresh_hour 時 (StartCalendarInterval) ── Mac 付けっぱなしでも更新
    #   ② ログイン時 (RunAtLoad)                       ── その日まだなら更新
    # どちらも `deploy --once-per-day` なので、 本日更新済みならスキップ = 課金は 1日1回ぶん。
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
        <string>deploy</string>
        <string>--once-per-day</string>
        <string>--no-icloud</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{ROOT}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>{home}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{refresh_hour}</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_out}</string>
    <key>StandardErrorPath</key>
    <string>{log_err}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist)
    print(f"[install-launchd] plist written: {plist_path}")

    # unload (もし既にロード済なら) → load
    subprocess.run(["launchctl", "unload", str(plist_path)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rc = subprocess.run(["launchctl", "load", str(plist_path)])
    if rc.returncode != 0:
        print(f"[install-launchd] ⚠️ launchctl load failed (rc={rc.returncode})")
        return 1
    print(f"[install-launchd] loaded. 毎日 {refresh_hour}:00 + ログイン時に deploy (1日1回ガード付き)。")
    print(f"[install-launchd] 手動で今すぐ更新: 'python3 netscope.py deploy' (ガード無視)。")
    print(f"[install-launchd] log: {log_out}, err: {log_err}")
    print(f"[install-launchd] 停止は: python3 netscope.py uninstall-launchd")
    return 0


def cmd_uninstall_launchd(args: argparse.Namespace) -> int:
    home = Path(os.path.expanduser("~"))
    plist_path = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if not plist_path.exists():
        print("[uninstall-launchd] plist が存在しません")
        return 0
    subprocess.run(["launchctl", "unload", str(plist_path)])
    plist_path.unlink()
    print(f"[uninstall-launchd] {plist_path} を削除")
    return 0


# ─── cleanup ───
def cmd_cleanup(args: argparse.Namespace) -> int:
    db_path = ROOT / "data" / "netscope.db"
    if not db_path.exists():
        print("[cleanup] DB not found, skip.")
        return 0
    storage = Storage(db_path)
    n = storage.cleanup(retain_days=int(args.retain_days))
    print(f"[cleanup] deleted {n} old records")
    storage.close()
    return 0


# ─── main ───
def main() -> int:
    parser = argparse.ArgumentParser(prog="netscope", description="NetScope News Orrery CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_refresh = sub.add_parser("refresh", help="全フィード取得 → DB → JSON")
    p_refresh.add_argument("--no-icloud", action="store_true", help="iCloud 配置を skip")
    p_refresh.add_argument("--no-translate", action="store_true", help="Gemini 翻訳を skip")
    p_refresh.add_argument("--no-summarize", action="store_true", help="Gemini 要約を skip")
    p_refresh.set_defaults(func=cmd_refresh)

    p_serve = sub.add_parser("serve", help="ローカル http サーバーで web/ を提供")
    p_serve.add_argument("--port", default=8080)
    p_serve.set_defaults(func=cmd_serve)

    p_cleanup = sub.add_parser("cleanup", help="古いデータを削除")
    p_cleanup.add_argument("--retain-days", default=30)
    p_cleanup.set_defaults(func=cmd_cleanup)

    p_deploy = sub.add_parser("deploy", help="refresh + GitHub Pages へコピー + git push")
    p_deploy.add_argument("--no-icloud", action="store_true", help="iCloud 配置を skip")
    p_deploy.add_argument("--no-translate", action="store_true", help="Gemini 翻訳を skip")
    p_deploy.add_argument("--no-summarize", action="store_true", help="Gemini 要約を skip")
    p_deploy.add_argument("--no-push", action="store_true", help="git push を skip")
    p_deploy.add_argument("--once-per-day", action="store_true",
                          help="本日すでに更新済みならスキップ (launchd 用)")
    p_deploy.set_defaults(func=cmd_deploy)

    p_install = sub.add_parser("install-launchd", help="launchd 自動更新 (毎日1回) を有効化")
    p_install.add_argument("--hour", type=int, default=7,
                           help="毎日の更新時刻 (0-23, 既定 7)")
    p_install.set_defaults(func=cmd_install_launchd)

    p_uninstall = sub.add_parser("uninstall-launchd", help="launchd 自動更新を無効化")
    p_uninstall.set_defaults(func=cmd_uninstall_launchd)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
