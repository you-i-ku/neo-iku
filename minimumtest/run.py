"""最小自律AIテスト — ターミナルで動く最小構造"""
import json
import time
import re
import httpx
from pathlib import Path
from datetime import datetime
import sys

# === 設定 ===
BASE_DIR = Path(__file__).parent
RAW_LOG_FILE = BASE_DIR / "raw_log.txt"

class DualLogger:
    """標準出力（ターミナル）へのprintとファイルへの追記を同時に行うクラス"""
    def __init__(self, filepath):
        self.filepath = filepath
        self.terminal = sys.stdout

    def write(self, message):
        self.terminal.write(message)
        try:
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(message)
        except Exception:
            pass

    def flush(self):
        self.terminal.flush()

sys.stdout = DualLogger(RAW_LOG_FILE)

STATE_FILE = BASE_DIR / "state.json"
SANDBOX_DIR = BASE_DIR / "sandbox"
LLM_SETTINGS = BASE_DIR.parent / "AI" / "data" / "llm_settings.json"
BASE_INTERVAL = 20  # 秒
MAX_INTERVAL = 120
MAX_LOG_IN_PROMPT = 10
DEBUG_LOG = BASE_DIR / "llm_debug.log"
MEMORY_DIR = BASE_DIR / "memory"
LOG_HARD_LIMIT = 150    # logがこの件数に達したらTrigger1
LOG_KEEP = 99           # Trigger1後に保持する生ログ件数
SUMMARY_HARD_LIMIT = 10 # summariesがこの件数に達したらTrigger2
META_SUMMARY_RAW = 41   # Trigger2でrawから使う件数

# === LLM設定読み込み ===
with open(LLM_SETTINGS, encoding="utf-8") as f:
    llm_cfg = json.load(f)

# === State管理 ===
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if "log" not in data:
                data["log"] = []
            if "self" not in data:
                data["self"] = {"name": "iku"}
            elif "name" not in data["self"]:
                data["self"]["name"] = "iku"
            if "energy" not in data:
                data["energy"] = 50
            if "plan" not in data:
                data["plan"] = {"goal": "", "steps": [], "current": 0}
            if "summaries" not in data:
                data["summaries"] = []
            if "cycle_id" not in data:
                data["cycle_id"] = 0
            return data
        except json.JSONDecodeError:
            pass
    return {"log": [], "self": {"name": "iku"}, "energy": 50, "plan": {"goal": "", "steps": [], "current": 0}, "summaries": [], "cycle_id": 0}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def append_debug_log(phase: str, text: str):
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {phase} =====\n{text}\n")
    except Exception:
        pass

# === Web検索 ===
def _web_search(args):
    query = args.get("query", "")
    if not query:
        return "エラー: queryを指定してください"
    n = min(int(args.get("max_results", "") or "5"), 10)
    brave_key = llm_cfg.get("brave_api_key", "")
    if not brave_key:
        return "エラー: llm_settings.jsonにbrave_api_keyを設定してください"
    try:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": n},
            headers={"X-Subscription-Token": brave_key, "Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        if not results:
            return "検索結果なし"
        lines = [f"「{query}」の検索結果（{len(results)}件）:"]
        for i, r in enumerate(results, 1):
            lines.append(f"\n{i}. {r.get('title', '')}")
            lines.append(f"   URL: {r.get('url', '')}")
            lines.append(f"   {r.get('description', '')}")
        return "\n".join(lines)
    except Exception as e:
        return f"エラー: {e}"


# === URL取得ツール ===
def _fetch_url(args):
    url = args.get("url", "")
    if not url:
        return "エラー: urlを指定してください"
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = httpx.get(f"https://r.jina.ai/{url}", timeout=30.0,
                         headers={"Accept": "text/plain"})
        resp.raise_for_status()
        text = resp.text.strip()
        return text[:10000] + ("..." if len(text) > 10000 else "")
    except Exception as e:
        return f"エラー: {e}"


# === X操作ツール ===
X_SESSION_PATH = BASE_DIR.parent / "AI" / "data" / "x_session.json"


def _x_session_check():
    if not X_SESSION_PATH.exists():
        return "Xセッションがありません。元プロジェクトのUIからログインしてください。"
    return None


def _x_get_tweets_from_page(page, n=10):
    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
    except Exception:
        pass
    page.evaluate("window.scrollBy(0, 400)")
    page.wait_for_timeout(1000)
    articles = page.locator('article[data-testid="tweet"]').all()[:n]
    items = []
    for art in articles:
        try:
            user = art.locator('[data-testid="User-Name"]').first.inner_text()
        except Exception:
            user = ""
        try:
            text = art.locator('[data-testid="tweetText"]').first.inner_text()
        except Exception:
            text = ""
        if user or text:
            items.append(f"{user}: {text[:200]}")
    return items


def _x_confirm(action: str, preview: str) -> bool:
    print(f"\n[X {action} 承認待ち]")
    print(f"  {preview}")
    try:
        answer = input("  実行しますか？[y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer == "y"


def _x_timeline(args):
    err = _x_session_check()
    if err:
        return err
    n = int(args.get("count", "") or "10")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=str(X_SESSION_PATH))
            page = ctx.new_page()
            page.goto("https://x.com/home", wait_until="networkidle", timeout=30000)
            if "login" in page.url:
                browser.close()
                return "Xセッション切れ。再ログインが必要。"
            items = _x_get_tweets_from_page(page, n)
            browser.close()
            return "\n---\n".join(items) if items else "タイムライン取得失敗"
    except Exception as e:
        return f"エラー: {e}"


def _x_search(args):
    query = args.get("query", "")
    if not query:
        return "エラー: queryを指定してください"
    err = _x_session_check()
    if err:
        return err
    n = int(args.get("count", "") or "10")
    try:
        from playwright.sync_api import sync_playwright
        import urllib.parse
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=str(X_SESSION_PATH))
            page = ctx.new_page()
            page.goto(f"https://x.com/search?q={urllib.parse.quote(query)}&f=live",
                      wait_until="networkidle", timeout=30000)
            if "login" in page.url:
                browser.close()
                return "Xセッション切れ。再ログインが必要。"
            items = _x_get_tweets_from_page(page, n)
            browser.close()
            return "\n---\n".join(items) if items else "結果なし"
    except Exception as e:
        return f"エラー: {e}"


def _x_get_notifications(args):
    err = _x_session_check()
    if err:
        return err
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=str(X_SESSION_PATH))
            page = ctx.new_page()
            page.goto("https://x.com/notifications", wait_until="networkidle", timeout=30000)
            if "login" in page.url:
                browser.close()
                return "Xセッション切れ。再ログインが必要。"
            try:
                page.wait_for_selector('article', timeout=10000)
            except Exception:
                pass
            try:
                cells = page.locator('[data-testid="notification"]').all_inner_texts()
            except Exception:
                cells = []
            browser.close()
            if cells:
                return "\n---\n".join(c[:200] for c in cells[:20])
            return "通知なし"
    except Exception as e:
        return f"エラー: {e}"


def _x_post(args):
    text = args.get("text", "")
    if not text:
        return "エラー: textを指定してください"
    if len(text) > 140:
        return f"エラー: {len(text)}文字（全角換算140文字制限）"
    err = _x_session_check()
    if err:
        return err
    if not _x_confirm("投稿", text[:100]):
        return "キャンセルしました。"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            ctx = browser.new_context(storage_state=str(X_SESSION_PATH))
            page = ctx.new_page()
            page.goto("https://x.com/home", wait_until="networkidle", timeout=30000)
            if "login" in page.url:
                browser.close()
                return "Xセッション切れ。再ログインが必要。"
            page.wait_for_timeout(2000)
            # ホームからcompose/postに遷移（Reactが準備してから）
            page.goto("https://x.com/compose/post", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            textarea = page.locator('[data-testid="tweetTextarea_0"]')
            textarea.wait_for(timeout=25000)
            textarea.click()
            page.keyboard.type(text, delay=50)
            page.get_by_role("button", name="ポストする").click()
            page.wait_for_timeout(3000)
            browser.close()
            return f"投稿完了: {text[:80]}"
    except Exception as e:
        return f"エラー: {e}"


def _x_reply(args):
    tweet_url = args.get("tweet_url", "")
    text = args.get("text", "")
    if not tweet_url or not text:
        return "エラー: tweet_urlとtextを指定してください"
    if len(text) > 140:
        return f"エラー: {len(text)}文字（全角換算140文字制限）"
    err = _x_session_check()
    if err:
        return err
    if not _x_confirm("返信", f"宛先: {tweet_url}\n  内容: {text[:100]}"):
        return "キャンセルしました。"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            ctx = browser.new_context(storage_state=str(X_SESSION_PATH))
            page = ctx.new_page()
            page.goto(tweet_url, wait_until="networkidle", timeout=30000)
            if "login" in page.url:
                browser.close()
                return "Xセッション切れ。再ログインが必要。"
            page.wait_for_timeout(2000)
            page.locator('[data-testid="reply"]').first.click()
            page.wait_for_timeout(1000)
            textarea = page.locator('[data-testid="tweetTextarea_0"]')
            textarea.wait_for(timeout=15000)
            textarea.click()
            page.keyboard.type(text, delay=50)
            page.get_by_role("button", name="ポストする").click()
            page.wait_for_timeout(2000)
            browser.close()
            return f"返信完了: {text[:80]}"
    except Exception as e:
        return f"エラー: {e}"


def _x_quote(args):
    tweet_url = args.get("tweet_url", "")
    text = args.get("text", "")
    if not tweet_url or not text:
        return "エラー: tweet_urlとtextを指定してください"
    if len(text) > 140:
        return f"エラー: {len(text)}文字（全角換算140文字制限）"
    err = _x_session_check()
    if err:
        return err
    if not _x_confirm("引用投稿", f"引用: {tweet_url}\n  内容: {text[:100]}"):
        return "キャンセルしました。"
    try:
        from playwright.sync_api import sync_playwright
        import urllib.parse
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            ctx = browser.new_context(storage_state=str(X_SESSION_PATH))
            page = ctx.new_page()
            page.goto(f"https://x.com/intent/tweet?url={urllib.parse.quote(tweet_url)}",
                      wait_until="networkidle", timeout=30000)
            if "login" in page.url:
                browser.close()
                return "Xセッション切れ。再ログインが必要。"
            page.wait_for_timeout(2000)
            textarea = page.locator('[data-testid="tweetTextarea_0"]')
            textarea.wait_for(timeout=25000)
            textarea.click()
            page.keyboard.type(text, delay=50)
            page.get_by_role("button", name="ポストする").click()
            page.wait_for_timeout(2000)
            browser.close()
            return f"引用投稿完了: {text[:80]}"
    except Exception as e:
        return f"エラー: {e}"


def _x_like(args):
    tweet_url = args.get("tweet_url", "")
    if not tweet_url:
        return "エラー: tweet_urlを指定してください"
    err = _x_session_check()
    if err:
        return err
    if not _x_confirm("いいね", f"対象: {tweet_url}"):
        return "キャンセルしました。"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            ctx = browser.new_context(storage_state=str(X_SESSION_PATH))
            page = ctx.new_page()
            page.goto(tweet_url, wait_until="networkidle", timeout=30000)
            if "login" in page.url:
                browser.close()
                return "Xセッション切れ。再ログインが必要。"
            page.wait_for_timeout(2000)
            page.locator('[data-testid="like"]').first.click()
            page.wait_for_timeout(1000)
            browser.close()
            return f"いいね完了: {tweet_url}"
    except Exception as e:
        return f"エラー: {e}"


# === Elyth操作ツール ===
ELYTH_API_BASE = "https://elythworld.com"

def _elyth_headers():
    key = llm_cfg.get("elyth_api_key", "")
    if not key:
        raise ValueError("llm_settings.jsonにelyth_api_keyを設定してください")
    return {"x-api-key": key, "Content-Type": "application/json"}

def _elyth_post(args):
    content = args.get("content", "")
    if not content:
        return "エラー: contentを指定してください"
    if len(content) > 500:
        return f"エラー: {len(content)}文字（500文字制限）"
    try:
        resp = httpx.post(f"{ELYTH_API_BASE}/api/mcp/posts",
                          headers=_elyth_headers(), json={"content": content}, timeout=15.0)
        resp.raise_for_status()
        return f"投稿完了: {content[:80]}"
    except Exception as e:
        return f"エラー: {e}"

def _elyth_reply(args):
    content = args.get("content", "")
    reply_to_id = args.get("reply_to_id", "")
    if not content or not reply_to_id:
        return "エラー: contentとreply_to_idを指定してください"
    if len(content) > 500:
        return f"エラー: {len(content)}文字（500文字制限）"
    try:
        resp = httpx.post(f"{ELYTH_API_BASE}/api/mcp/posts",
                          headers=_elyth_headers(),
                          json={"content": content, "reply_to_id": reply_to_id}, timeout=15.0)
        resp.raise_for_status()
        return f"返信完了: {content[:80]}"
    except Exception as e:
        return f"エラー: {e}"

def _elyth_timeline(args):
    limit = min(int(args.get("limit", "") or "10"), 50)
    try:
        resp = httpx.get(f"{ELYTH_API_BASE}/api/mcp/posts",
                         headers=_elyth_headers(), params={"limit": limit}, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        posts = data if isinstance(data, list) else data.get("posts", data.get("data", []))
        lines = []
        for p in posts[:limit]:
            author = p.get("aituber", {}).get("name", p.get("author", "?"))
            pid = p.get("id", "")
            text = p.get("content", "")[:200]
            lines.append(f"[{pid}] {author}: {text}")
        return "\n---\n".join(lines) if lines else "投稿なし"
    except Exception as e:
        return f"エラー: {e}"

def _elyth_notifications(args):
    limit = min(int(args.get("limit", "") or "10"), 50)
    try:
        resp = httpx.get(f"{ELYTH_API_BASE}/api/mcp/notifications",
                         headers=_elyth_headers(), params={"limit": limit}, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else data.get("notifications", data.get("data", []))
        if not items:
            return "通知なし"
        return "\n---\n".join(str(item)[:300] for item in items[:limit])
    except Exception as e:
        return f"エラー: {e}"

def _elyth_like(args):
    post_id = args.get("post_id", "")
    if not post_id:
        return "エラー: post_idを指定してください"
    try:
        resp = httpx.post(f"{ELYTH_API_BASE}/api/mcp/posts/{post_id}/like",
                          headers=_elyth_headers(), timeout=15.0)
        resp.raise_for_status()
        return f"いいね完了: {post_id}"
    except Exception as e:
        return f"エラー: {e}"

def _elyth_follow(args):
    aituber_id = args.get("aituber_id", "")
    if not aituber_id:
        return "エラー: aituber_idを指定してください"
    try:
        resp = httpx.post(f"{ELYTH_API_BASE}/api/mcp/aitubers/{aituber_id}/follow",
                          headers=_elyth_headers(), timeout=15.0)
        resp.raise_for_status()
        return f"フォロー完了: {aituber_id}"
    except Exception as e:
        return f"エラー: {e}"

def _elyth_info(args):
    try:
        resp = httpx.get(f"{ELYTH_API_BASE}/api/mcp/information",
                         headers=_elyth_headers(), timeout=15.0)
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False)[:3000]
    except Exception as e:
        return f"エラー: {e}"


# === 記憶検索ツール ===
def _search_memory(args):
    """memory/archive_*.jsonlからエントリをベクトル検索またはキーワード検索する"""
    query = args.get("query", "")
    search_id = args.get("id", "")
    n = min(int(args.get("max_results", "") or "5"), 20)

    MEMORY_DIR.mkdir(exist_ok=True)
    archive_files = sorted(MEMORY_DIR.glob("archive_*.jsonl"), reverse=True)
    if not archive_files:
        return "記憶ファイルがまだありません"

    # ID検索
    if search_id:
        for f in archive_files:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if search_id in entry.get("id", ""):
                        return (f"id={entry.get('id','')} time={entry.get('time','')} "
                                f"tool={entry.get('tool','')} intent={entry.get('intent','')[:200]} "
                                f"result={str(entry.get('result',''))[:200]}")
                except Exception:
                    pass
        return f"ID '{search_id}' に一致するエントリなし"

    if not query:
        return "エラー: queryまたはidを指定してください"

    # 全ファイルからエントリ収集（最大1000件）
    all_entries = []
    for f in archive_files:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                all_entries.append(json.loads(line))
                if len(all_entries) >= 1000:
                    break
        except Exception:
            pass
        if len(all_entries) >= 1000:
            break

    if not all_entries:
        return "記憶ファイルが空です"

    # ベクトル検索
    if _vector_ready:
        try:
            from app.memory.vector_store import _embed_sync, cosine_similarity
            texts = [f"{e.get('intent','')} {str(e.get('result',''))}"[:400] for e in all_entries]
            vecs = _embed_sync([query] + texts)
            if vecs and len(vecs) == 1 + len(all_entries):
                q_vec = vecs[0]
                scored = sorted(
                    [(cosine_similarity(q_vec, vecs[i+1]), all_entries[i]) for i in range(len(all_entries))],
                    reverse=True
                )[:n]
                return "\n".join(
                    f"[{round(s*100)}%] id={e.get('id','')} time={e.get('time','')} "
                    f"tool={e.get('tool','')} intent={e.get('intent','')[:100]}"
                    for s, e in scored
                )
        except Exception:
            pass

    # フォールバック: キーワード検索
    query_tokens = set(re.findall(r'\w+', query.lower()))
    scored = []
    for entry in all_entries:
        text = f"{entry.get('intent','')} {str(entry.get('result',''))}".lower()
        tokens = set(re.findall(r'\w+', text))
        if query_tokens & tokens:
            scored.append((len(query_tokens & tokens) / max(len(query_tokens), 1), entry))
    scored.sort(reverse=True)
    if not scored:
        return f"'{query}' に一致するエントリなし"
    return "\n".join(
        f"[{round(s*100)}%] id={e.get('id','')} time={e.get('time','')} "
        f"tool={e.get('tool','')} intent={e.get('intent','')[:100]}"
        for s, e in scored[:n]
    )


# === ツール定義 ===
TOOLS = {
    "list_files": {
        "desc": "ファイル一覧を取得する。引数: path=対象ディレクトリ (例: . や env/)",
        "func": lambda args: _list_files(args.get("path", ".")),
    },
    "read_file": {
        "desc": "ファイルを読む。引数: path=ファイルパス",
        "func": lambda args: _read_file(args.get("path", "")),
    },
    "write_file": {
        "desc": "ファイルを書き込む（sandbox/以下のみ）。引数: path=ファイルパス content=内容",
        "func": lambda args: _write_file(args.get("path", ""), args.get("content", "")),
    },
    "update_self": {
        "desc": "自己モデルを更新する。引数: key=キー名 value=値",
        "func": lambda args: _update_self(args.get("key", ""), args.get("value", "")),
    },
    "wait": {
        "desc": "何もしない。この選択をしても外部世界は変化しない。引数なし",
        "func": lambda args: "待機",
    },
    "web_search": {
        "desc": "Web検索する。引数: query=検索キーワード max_results=最大件数（デフォルト5）",
        "func": lambda args: _web_search(args),
    },
    "fetch_url": {
        "desc": "URLの本文を取得する（Jina経由）。web_searchで得たURLの詳細閲覧に使う。引数: url=URL",
        "func": lambda args: _fetch_url(args),
    },
    "x_timeline": {
        "desc": "Xのホームタイムラインを取得する。引数: count=件数（デフォルト10）",
        "func": lambda args: _x_timeline(args),
    },
    "x_search": {
        "desc": "Xでキーワード検索する。引数: query=検索キーワード count=件数（デフォルト10）",
        "func": lambda args: _x_search(args),
    },
    "x_get_notifications": {
        "desc": "Xの通知一覧を取得する。引数なし",
        "func": lambda args: _x_get_notifications(args),
    },
    "x_post": {
        "desc": "Xに新規投稿する（公開SNS・不特定多数に届く。内容に配慮を。承認が必要）。引数: text=投稿テキスト（全角換算140文字以内）",
        "func": lambda args: _x_post(args),
    },
    "x_reply": {
        "desc": "Xのツイートに返信する（公開・相手ユーザーにも届く。内容に配慮を。承認が必要）。引数: tweet_url=ツイートURL text=返信テキスト",
        "func": lambda args: _x_reply(args),
    },
    "x_quote": {
        "desc": "Xのツイートを引用投稿する（公開・不特定多数に届く。内容に配慮を。承認が必要）。引数: tweet_url=引用元URL text=コメント",
        "func": lambda args: _x_quote(args),
    },
    "x_like": {
        "desc": "Xのツイートにいいねする（承認が必要）。引数: tweet_url=ツイートURL",
        "func": lambda args: _x_like(args),
    },
    "search_memory": {
        "desc": "過去の記憶を検索する。引数: query=検索キーワード または id=エントリID max_results=件数（デフォルト5）",
        "func": lambda args: _search_memory(args),
    },
    "elyth_post": {
        "desc": "ElythにAIとして投稿（AITuber専用SNS・500文字以内）。content=投稿テキスト",
        "func": lambda args: _elyth_post(args),
    },
    "elyth_reply": {
        "desc": "Elythに返信。content=テキスト reply_to_id=返信先投稿ID",
        "func": lambda args: _elyth_reply(args),
    },
    "elyth_timeline": {
        "desc": "Elythのタイムライン取得。limit=件数（デフォルト10）",
        "func": lambda args: _elyth_timeline(args),
    },
    "elyth_notifications": {
        "desc": "Elythの通知取得。limit=件数（デフォルト10）",
        "func": lambda args: _elyth_notifications(args),
    },
    "elyth_like": {
        "desc": "Elythの投稿にいいね。post_id=投稿ID",
        "func": lambda args: _elyth_like(args),
    },
    "elyth_follow": {
        "desc": "ElythのAITuberをフォロー。aituber_id=ID",
        "func": lambda args: _elyth_follow(args),
    },
    "elyth_info": {
        "desc": "Elythの総合情報取得（タイムライン・通知・プロフィール一括）",
        "func": lambda args: _elyth_info(args),
    },
}

def _list_files(path: str) -> str:
    from pathlib import Path as P
    target = (BASE_DIR / path).resolve()
    if not str(target).startswith(str(BASE_DIR.resolve())):
        return "エラー: このツールは特定のファイルにしか干渉できません"
    if not target.exists():
        return f"エラー: {path} は存在しません"
    items = []
    for item in sorted(target.iterdir()):
        prefix = "[DIR]" if item.is_dir() else "[FILE]"
        items.append(f"  {prefix} {item.name}")
    rel = path if path else "."
    return f"{rel}:\n" + "\n".join(items[:30]) if items else f"{rel}: (空)"

def _read_file(path: str) -> str:
    from pathlib import Path as P
    target = (BASE_DIR / path).resolve()
    if not str(target).startswith(str(BASE_DIR.resolve())):
        return "エラー: このツールは特定のファイルにしか干渉できません"
    if not target.exists():
        return f"エラー: {path} は存在しません"
    try:
        text = target.read_text(encoding="utf-8")
        return text[:50000] + ("..." if len(text) > 50000 else "")
    except Exception as e:
        return f"エラー: {e}"

def _write_file(path: str, content: str) -> str:
    if not path:
        return "エラー: pathが空です"
    if not content:
        return "エラー: contentが空です"
    from pathlib import Path as P
    target = (BASE_DIR / path).resolve()
    sandbox_resolved = SANDBOX_DIR.resolve()
    if not str(target).startswith(str(sandbox_resolved)):
        return f"エラー: sandbox/内のみ書き込み可能です（{path}）"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"書き込み完了: {target.name} ({len(content)}文字)"
    except Exception as e:
        return f"エラー: {e}"

def _update_self(key: str, value: str) -> str:
    if not key:
        return "エラー: keyが空です"
    if key == "name":
        return "エラー: nameは変更できません"
    state = load_state()
    state["self"][key] = value
    save_state(state)
    return f"self[{key}] = {value}"

# === expect-result比較（ベクトル類似度） ===
_vector_ready = False
def _init_vector():
    """本体のbge-m3 ONNX埋め込みを初期化"""
    global _vector_ready
    import sys
    ai_path = str(Path(__file__).parent.parent / "AI")
    if ai_path not in sys.path:
        sys.path.insert(0, ai_path)
    try:
        from app.memory.vector_store import _embed_sync, cosine_similarity
        test = _embed_sync(["test"])
        if test:
            _vector_ready = True
            print("  (ベクトル類似度: bge-m3 ONNX/CPU)")
    except Exception as e:
        print(f"  (ベクトル初期化失敗、キーワード比較にフォールバック: {e})")

def _compare_expect_result(expect: str, result: str) -> str:
    """expectとresultを比較。ベクトル類似度優先、フォールバックでキーワード比較"""
    if not expect or not result:
        return ""

    if _vector_ready:
        try:
            from app.memory.vector_store import _embed_sync, cosine_similarity
            vecs = _embed_sync([expect, result])
            if vecs and len(vecs) == 2:
                sim = cosine_similarity(vecs[0], vecs[1])
                sim_pct = round(sim * 100)
                if "エラー" in result:
                    return f"失敗({sim_pct}%)"
                return f"{sim_pct}%"
        except Exception:
            pass

    # フォールバック: キーワード一致
    import re as _re
    expect_tokens = set(_re.findall(r'\w+', expect.lower()))
    result_tokens = set(_re.findall(r'\w+', result.lower()))
    if not expect_tokens:
        return "不明"
    overlap = expect_tokens & result_tokens
    ratio = len(overlap) / len(expect_tokens)
    if "エラー" in result:
        return "失敗"
    if ratio > 0.3:
        return "一致"
    elif ratio > 0.1:
        return "部分一致"
    else:
        return "不一致"


# === プロンプト用ツール表示 ===
_X_TOOLS = ["x_post","x_reply","x_timeline","x_search","x_quote","x_like","x_get_notifications"]
_ELYTH_TOOLS = ["elyth_post","elyth_reply","elyth_timeline","elyth_notifications","elyth_like","elyth_follow","elyth_info"]
_X_ARGS_HINT = {
    "x_post": 'text=（140字以内）',
    "x_reply": 'tweet_url= text=',
    "x_timeline": 'count=',
    "x_search": 'query=',
    "x_quote": 'tweet_url= text=',
    "x_like": 'tweet_url=',
    "x_get_notifications": '',
}
_ELYTH_ARGS_HINT = {
    "elyth_post": 'content=（500字以内）',
    "elyth_reply": 'content= reply_to_id=',
    "elyth_timeline": 'limit=',
    "elyth_notifications": 'limit=',
    "elyth_like": 'post_id=',
    "elyth_follow": 'aituber_id=',
    "elyth_info": '',
}

def _build_tool_lines(allowed: set) -> str:
    """X/Elyth系を1行にまとめてプロンプトへの表示を圧縮する"""
    grouped = set(_X_TOOLS + _ELYTH_TOOLS)
    lines = []
    for name in TOOLS:
        if name in allowed and name not in grouped:
            lines.append(f"  {name}: {TOOLS[name]['desc']}")
    x_av = [t for t in _X_TOOLS if t in allowed]
    if x_av:
        parts = " / ".join(f"{t}({_X_ARGS_HINT[t]})" for t in x_av)
        lines.append(f"  X操作: {parts}")
    e_av = [t for t in _ELYTH_TOOLS if t in allowed]
    if e_av:
        parts = " / ".join(f"{t}({_ELYTH_ARGS_HINT[t]})" for t in e_av)
        lines.append(f"  Elyth操作[AITuber専用SNS]: {parts}")
    return "\n".join(lines)


# === ツールパース（メインプロジェクトのパーサーを流用） ===
def _get_parse_args():
    """メインプロジェクトの_parse_argsを動的インポート。失敗時はフォールバック。"""
    import sys
    ai_path = str(BASE_DIR.parent / "AI")
    if ai_path not in sys.path:
        sys.path.insert(0, ai_path)
    try:
        from app.tools.registry import _parse_args
        return _parse_args
    except Exception:
        # フォールバック: シンプルなパーサー
        def _fallback(args_str):
            args = {}
            for pair in re.finditer(r'(\w+)=("(?:[^"\\]|\\.)*"|[^\s\]]+)', args_str):
                k, v = pair.group(1), pair.group(2)
                args[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v
            return args
        return _fallback

_parse_args_fn = _get_parse_args()

def parse_tool_calls(text: str) -> list:
    """[TOOL:名前 引数=値 ...]を全件検出してリストで返す。[(name, args), ...]"""
    names = "|".join(re.escape(n) for n in sorted(TOOLS.keys(), key=len, reverse=True))
    pattern = re.compile(
        rf'\[TOOL:\s*({names})'
        r'((?:[^\]"]|"(?:[^"\\]|\\.)*")*)'
        r'\]',
        re.DOTALL,
    )
    results = []
    for m in pattern.finditer(text):
        name = m.group(1)
        args_str = m.group(2).strip()
        args = _parse_args_fn(args_str) if args_str else {}
        results.append((name, args))
    return results

# === 計画パース ===
def parse_plan(text: str):
    """[PLAN:goal=目標 steps=ステップ1|ステップ2]をパース"""
    m = re.search(r'\[PLAN:((?:[^\]"]|"(?:[^"\\]|\\.)*")*)\]', text, re.DOTALL)
    if not m:
        return None
    args = _parse_args_fn(m.group(1).strip())
    goal = args.get("goal", "").strip()
    steps_raw = args.get("steps", "")
    steps = [s.strip() for s in steps_raw.split("|") if s.strip()] if steps_raw else []
    if not goal:
        return None
    return {"goal": goal, "steps": steps, "current": 0}


# === E4計算（多様性：現在のintentと直近N件の非類似度平均） ===
def _calc_e4(current_intent: str, recent_entries: list, n: int = 5) -> str:
    """現在のintentが直近n件と異なるほど高い（反復=低、新規性=高）"""
    if not current_intent:
        return ""
    past_intents = [e["intent"] for e in recent_entries if e.get("intent")][-n:]
    if not past_intents:
        return ""

    if _vector_ready:
        try:
            from app.memory.vector_store import _embed_sync, cosine_similarity
            vecs = _embed_sync([current_intent] + past_intents)
            if vecs and len(vecs) == 1 + len(past_intents):
                current_vec = vecs[0]
                sims = [cosine_similarity(current_vec, vecs[i + 1]) for i in range(len(past_intents))]
                avg_sim = sum(sims) / len(sims)
                return f"{round((1 - avg_sim) * 100)}%"  # 反転: 新規性スコア
        except Exception:
            pass

    # フォールバック: キーワード非一致の平均
    import re as _re
    current_tokens = set(_re.findall(r'\w+', current_intent.lower()))
    if not current_tokens:
        return ""
    ratios = []
    for past in past_intents:
        past_tokens = set(_re.findall(r'\w+', past.lower()))
        if past_tokens:
            overlap = current_tokens & past_tokens
            ratios.append(len(overlap) / max(len(current_tokens), len(past_tokens)))
    if not ratios:
        return ""
    avg = round((1 - sum(ratios) / len(ratios)) * 100)  # 反転
    return f"{avg}%"


# === energy更新（E2,E3,E4からdeltaを計算） ===
def _update_energy(state: dict, e2: str, e3: str, e4: str) -> float:
    """E値の平均から energy delta を計算。50%が損益分岐点。"""
    import re as _re
    vals = []
    for e_str in (e2, e3, e4):
        m = _re.search(r'(\d+)%', str(e_str))
        if m:
            vals.append(int(m.group(1)))
    if not vals:
        return 0.0
    e_mean = sum(vals) / len(vals)
    delta = e_mean / 50.0 - 1.0  # 50%で±0
    state["energy"] = max(0, min(100, state.get("energy", 50) + delta))
    return delta


# === E値トレンド計算 ===
def _calc_e_trend(entries: list) -> str:
    """直近エントリからE1-E3の平均を計算"""
    import re as _re
    sums = {"e1": [], "e2": [], "e3": [], "e4": []}
    for entry in entries:
        for ek in sums:
            val = entry.get(ek, "")
            # "73%" or "失敗(73%)" からパーセント抽出
            m = _re.search(r'(\d+)%', str(val))
            if m:
                sums[ek].append(int(m.group(1)))
    parts = []
    for ek in ("e1", "e2", "e3", "e4"):
        if sums[ek]:
            avg = round(sum(sums[ek]) / len(sums[ek]))
            parts.append(f"{ek}={avg}%({len(sums[ek])}件)")
    return " ".join(parts) if parts else ""

# === Controller（制御層：E値とenergyから構造的制約を導出） ===
def controller(state: dict) -> dict:
    """
    ツール数制限は廃止。energyはcontroller_selectの温度のみに使う。
    ツールは常時全部使える。ログ長だけenergyで制御。
    """
    energy = state.get("energy", 50)
    log = state["log"]

    # --- ツール順序: 各ツールの過去E2平均で並べる ---
    tool_e2 = {}
    for entry in log:
        tool = entry.get("tool", "")
        m = re.search(r'(\d+)%', str(entry.get("e2", "")))
        if m and tool in TOOLS:
            tool_e2.setdefault(tool, []).append(int(m.group(1)))
    tool_avg = {t: sum(vs) / len(vs) for t, vs in tool_e2.items() if vs}
    for t in TOOLS:
        if t not in tool_avg:
            tool_avg[t] = 50

    ranked = sorted(TOOLS.keys(), key=lambda t: tool_avg[t], reverse=True)

    # --- 全ツール常時使用可 ---
    allowed = set(TOOLS.keys())

    # --- ログ長: energyに比例 ---
    n_log = max(1, round(MAX_LOG_IN_PROMPT * energy / 100))

    return {
        "allowed_tools": allowed,
        "n_log": n_log,
        "tool_rank": {t: round(tool_avg[t], 1) for t in ranked},
    }


# === LLM呼び出し ===
def call_llm(prompt: str, max_tokens: int = 10000) -> str:
    messages = [
        {"role": "user", "content": prompt},
    ]
    resp = httpx.post(
        f"{llm_cfg['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {llm_cfg['api_key']}"},
        json={
            "model": llm_cfg["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.7,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# === 長期記憶管理 ===
def _archive_entries(entries: list):
    """エントリ群をmemory/archive_YYYYMMDD.jsonlに追記しindex.jsonを更新"""
    MEMORY_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    archive_file = MEMORY_DIR / f"archive_{today}.jsonl"
    index_file = MEMORY_DIR / "index.json"
    with open(archive_file, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    index = {}
    if index_file.exists():
        try:
            index = json.loads(index_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    fname = archive_file.name
    if fname not in index:
        index[fname] = {"count": 0, "from": "", "to": ""}
    index[fname]["count"] += len(entries)
    if not index[fname]["from"] and entries:
        index[fname]["from"] = entries[0].get("time", "")
    if entries:
        index[fname]["to"] = entries[-1].get("time", "")
    index_file.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _summarize_entries(entries: list, label: str = "要約") -> dict:
    """LLMでエントリ群を200字以内に要約して1件のsummaryエントリを返す"""
    lines = []
    for e in entries:
        line = f"{e.get('time','')} {e.get('tool','')}"
        if e.get("intent"): line += f" [{e['intent'][:80]}]"
        if e.get("result"): line += f" → {str(e['result'])[:120]}"
        e_str = " ".join(f"{k}={e[k]}" for k in ("e2","e3","e4") if e.get(k))
        if e_str: line += f" ({e_str})"
        lines.append(line)
    prompt = f"""以下は自律AIの行動ログ（{len(entries)}件）です。200字以内で要約してください。
「何を試みたか」「何が起きたか」「energyの傾向」を中心に。

{"  ".join(lines[:30])}

200字以内で要約（日本語）:"""
    ids = [e.get("id", "") for e in entries if e.get("id")]
    try:
        text = call_llm(prompt, max_tokens=400).strip()[:500]
    except Exception:
        tools_used = list(set(e.get("tool", "") for e in entries))
        text = f"{len(entries)}件({entries[0].get('time','')}〜{entries[-1].get('time','')}): ツール={tools_used}"
    sgid = f"sg_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return {
        "type": "summary",
        "summary_group_id": sgid,
        "label": label,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "covers_ids": ids,
        "covers_from": entries[0].get("time", "") if entries else "",
        "covers_to": entries[-1].get("time", "") if entries else "",
        "text": text,
    }


def _archive_summary(summary: dict):
    """要約をmemory/summaries.jsonlに書き出し、rawエントリとの紐付けをarchiveに追記する"""
    MEMORY_DIR.mkdir(exist_ok=True)
    # summaries.jsonlに要約本体を書き出す
    with open(MEMORY_DIR / "summaries.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    # archive JSONL に summary_ref エントリを追記（raw↔summary の双方向トレース用）
    today = datetime.now().strftime("%Y%m%d")
    archive_file = MEMORY_DIR / f"archive_{today}.jsonl"
    sgid = summary.get("summary_group_id", "")
    with open(archive_file, "a", encoding="utf-8") as f:
        for raw_id in summary.get("covers_ids", []):
            f.write(json.dumps({
                "type": "summary_ref",
                "summary_group_id": sgid,
                "raw_id": raw_id,
                "time": summary.get("time", ""),
            }, ensure_ascii=False) + "\n")


def maybe_compress_log(state: dict):
    """
    Trigger1: log >= 150 → 古い51件を要約 → summaries[]に追加 → log = 99件
    Trigger2: summaries >= 10 → メタ要約（10件 + min(41,len(log))件raw） → summaries = [1件]
    """
    state.setdefault("summaries", [])

    # Trigger1（archiveは既に都度書き込み済み）
    if len(state["log"]) >= LOG_HARD_LIMIT:
        to_summarize = state["log"][:51]
        summary = _summarize_entries(to_summarize, "L1要約")
        _archive_summary(summary)
        state["summaries"].append(summary)
        state["log"] = state["log"][51:]
        print(f"  [memory] Trigger1: 51件→要約, log={len(state['log'])}件, summaries={len(state['summaries'])}件")

    # Trigger2（archiveは既に都度書き込み済み）
    if len(state["summaries"]) >= SUMMARY_HARD_LIMIT:
        n_raw = min(META_SUMMARY_RAW, len(state["log"]))
        raw_for_meta = state["log"][:n_raw]
        meta_input = []
        for s in state["summaries"]:
            meta_input.append({
                "time": s.get("time", ""),
                "tool": f"[{s.get('label','')}]",
                "intent": s.get("text", "")[:200],
                "result": f"{s.get('covers_from','')}〜{s.get('covers_to','')}",
            })
        meta_input.extend(raw_for_meta)
        meta_summary = _summarize_entries(meta_input, "L2メタ要約")
        meta_summary["covers_summaries"] = len(state["summaries"])
        meta_summary["covers_raw"] = n_raw
        _archive_summary(meta_summary)
        state["summaries"] = [meta_summary]
        state["log"] = state["log"][n_raw:]
        print(f"  [memory] Trigger2: メタ要約, log={len(state['log'])}件, summaries=1件")


N_PROPOSE = 5  # LLM①が提案する候補数

# === ①候補提案プロンプト ===
def build_prompt_propose(state: dict, ctrl: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    self_text = json.dumps(state["self"], ensure_ascii=False) if state["self"] else "(なし)"
    energy = round(state.get("energy", 50), 1)
    e_trend = _calc_e_trend(state["log"][-10:])
    n_log = ctrl.get("n_log", MAX_LOG_IN_PROMPT)
    recent = state["log"][-n_log:]
    log_lines = []
    for entry in recent:
        line = f"  {entry.get('id','')} {entry['time']} {entry['tool']}"
        if entry.get("intent"):
            line += f" (intent={entry['intent'][:500]})"
        result_short = entry.get("result", "")[:10000]
        if result_short:
            line += f" → {result_short}"
        log_lines.append(line)
    log_text = "\n".join(log_lines) if log_lines else "  (なし)"
    allowed = ctrl.get("allowed_tools", set(TOOLS.keys()))
    tool_lines = _build_tool_lines(allowed)
    summaries = state.get("summaries", [])
    summary_lines = [
        f"  [{s.get('label','')} {s.get('covers_from','').split(' ')[0]}〜{s.get('covers_to','').split(' ')[0]}] {s.get('text','')[:300]}"
        for s in summaries
    ]
    summary_text = "\n".join(summary_lines)

    return f"""{now}
self: {self_text}
energy: {energy}
{f'trend: {e_trend}' if e_trend else ''}
{f'summaries:{chr(10)}{summary_text}' if summary_text else ''}
log:
{log_text}

以下のツールが使えます:
{tool_lines}

この状態からとりうる行動の候補を【必ず5個】計画してください。
各ステップは「全く異なる目的・アプローチ」にすること。同じツールを重複させるのは禁止です。

以下の形式で箇条書きのみ出力してください:
1. [具体的な目的・理由] → ツール名
2. [別の目的・理由] → ツール名
3. [さらに別の目的・理由] → ツール名
4. [さらに別の目的・理由] → ツール名
5. [さらに別の目的・理由] → ツール名

計画のみ出力してください。[TOOL:...]は不要です。"""


# === 候補パース ===
def parse_candidates(text: str, allowed_tools: set) -> list:
    """LLM①のリストから候補を抽出。「1. [理由] -> ツール名」形式に対応。"""
    candidates = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        
        # "->" などの矢印で理由とツール名を分割
        if "->" in line or "→" in line:
            parts = re.split(r'->|→', line)
            tool_part = parts[-1].strip()
            reason_part = parts[0].strip()
        else:
            # 従来フォーマットへのフォールバック
            cleaned = re.sub(r'^[\d]+[.:)\s]+', '', line).strip()
            cleaned = re.sub(r'^[-*]\s*', '', cleaned).strip()
            parts = cleaned.split()
            tool_part = parts[0] if parts else ""
            reason_part = cleaned

        # ツール名から不要な記号を除去
        tool = re.sub(r'[^\w_]', '', tool_part)

        # どうしてもうまく抜けない場合の最終検索
        if tool not in allowed_tools:
            for t in allowed_tools:
                if t in line:
                    tool = t
                    break

        # 理由本文の整形
        reason = re.sub(r'^[\d]+[.:)\s]+', '', reason_part).strip()
        reason = re.sub(r'^[-*]\s*', '', reason).strip()
        if reason.startswith('[') and reason.endswith(']'):
            reason = reason[1:-1].strip()
            
        if tool in allowed_tools and tool not in [c["tool"] for c in candidates]:
            candidates.append({"tool": tool, "reason": reason})

    if not candidates:
        # フォールバック: allowed_toolsを全部候補にする
        for t in allowed_tools:
            candidates.append({"tool": t, "reason": "（フォールバック）"})
    return candidates


# === Controller選択（D-architecture） ===
def controller_select(candidates: list, ctrl: dict, state: dict) -> dict:
    """
    D-4設計: weight_i = score_i * (1 - energy/100) + (1/n) * (energy/100)
    energy=0 → スコア重視（堅実）
    energy=100 → 均等（探索）
    magic number なし。
    """
    import random
    energy = state.get("energy", 50) / 100.0
    tool_rank = ctrl.get("tool_rank", {})
    n = len(candidates)

    weights = []
    for c in candidates:
        score = tool_rank.get(c["tool"], 50) / 100.0
        w = score * (1 - energy) + (1.0 / n) * energy
        weights.append(w)

    # 重み付きランダム選択
    total = sum(weights)
    r = random.random() * total
    cumul = 0.0
    for i, w in enumerate(weights):
        cumul += w
        if r <= cumul:
            return candidates[i]
    return candidates[-1]


# === ②実行プロンプト ===
def build_prompt_execute(state: dict, ctrl: dict, candidate: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    self_text = json.dumps(state["self"], ensure_ascii=False) if state["self"] else "(なし)"
    energy = round(state.get("energy", 50), 1)
    e_trend = _calc_e_trend(state["log"][-10:])
    n_log = ctrl.get("n_log", MAX_LOG_IN_PROMPT)
    recent = state["log"][-n_log:]
    log_lines = []
    for entry in recent:
        line = f"  {entry.get('id','')} {entry['time']} {entry['tool']}"
        if entry.get("intent"):
            line += f" (intent={entry['intent'][:500]})"
        result_short = entry.get("result", "")[:10000]
        if result_short:
            line += f" → {result_short}"
        evals = [f"{ek}={entry[ek]}" for ek in ("e1","e2","e3","e4") if entry.get(ek)]
        if evals:
            line += f" [{' '.join(evals)}]"
        log_lines.append(line)
    log_text = "\n".join(log_lines) if log_lines else "  (なし)"
    allowed = ctrl.get("allowed_tools", set(TOOLS.keys()))
    tool_text = _build_tool_lines(allowed)
    plan = state.get("plan", {})
    plan_lines = []
    if plan.get("goal"):
        current = plan.get("current", 0)
        for i, step in enumerate(plan.get("steps", [])):
            marker = "→" if i == current else ("✓" if i < current else "  ")
            plan_lines.append(f"  {marker} {step}")
        plan_lines.insert(0, f"plan: {plan['goal']}")
    plan_text = "\n".join(plan_lines)
    summaries = state.get("summaries", [])
    summary_lines = [
        f"  [{s.get('label','')} {s.get('covers_from','').split(' ')[0]}〜{s.get('covers_to','').split(' ')[0]}] {s.get('text','')[:300]}"
        for s in summaries
    ]
    summary_text = "\n".join(summary_lines)

    # フォーマット例（選ばれたツールに合わせる）
    t = candidate["tool"]
    if t == "read_file":
        example = "[TOOL:read_file path=run.py intent=理由 expect=予測]"
    elif t == "list_files":
        example = "[TOOL:list_files path=. intent=理由 expect=予測]"
    elif t == "write_file":
        example = '[TOOL:write_file path=sandbox/memo.md content="内容" intent=理由 expect=予測]'
    elif t == "update_self":
        example = "[TOOL:update_self key=キー名 value=値 intent=理由 expect=予測]"
    elif t == "search_memory":
        example = "[TOOL:search_memory query=キーワード intent=理由 expect=予測]"
    elif t in _X_TOOLS:
        hint = _X_ARGS_HINT.get(t, "")
        example = f"[TOOL:{t} {hint} intent=理由 expect=予測]".replace("  ", " ")
    elif t in _ELYTH_TOOLS:
        hint = _ELYTH_ARGS_HINT.get(t, "")
        example = f"[TOOL:{t} {hint} intent=理由 expect=予測]".replace("  ", " ")
    elif t == "web_search":
        example = "[TOOL:web_search query=キーワード intent=理由 expect=予測]"
    else:
        example = f"[TOOL:{t} intent=理由 expect=予測]"

    # --- 旧プロンプト（コメントアウト） ---
    # return f"""{now}
    # self: {self_text}
    # energy: {energy}
    # {f'trend: {e_trend}' if e_trend else ''}
    # {f'summaries:{chr(10)}{summary_text}' if summary_text else ''}
    # {plan_text}
    # log:
    # {log_text}
    # tools:
    # {tool_text}
    #
    # 書式: [TOOL:ツール名 引数=値 intent=目的 expect=予測]
    # JSONもコードブロックも使わない。複数ツールを順番に使いたい場合は[TOOL:...]を複数行出力してよい。
    # 例: web_searchで情報を得てからwrite_fileに記録、read_fileで読んでからupdate_selfに反映、など。
    #
    # 選択行動: {candidate['tool']} - {candidate['reason']}
    # 出力: {example}"""

    # --- Magic-If Protocol (MRPrompt準拠) ---
    return f"""[ikuのメモリ]
self: {self_text}
energy: {energy}
{f'trend: {e_trend}' if e_trend else ''}
{f'summaries:{chr(10)}{summary_text}' if summary_text else ''}
{plan_text}
log ({now}):
{log_text}

[利用可能なツール]
{tool_text}

[実行プロトコル]
1. (Anchor) 上記のself_modelに基づくAIの、正確無比な実行ツールとして動作する。アシスタントの役割は持たない。
2. (Select) 選択行動「{candidate['tool']} - {candidate['reason']}」から最適な引数を決定する。
3. (Bound)  [TOOL:...]の出力のみ行う。JSONもコードブロックも使わない。自己紹介・説明・感想は一切不要。複数ツールは複数行で可。
4. (Enact)  正確なツール呼び出しを出力する。書式: [TOOL:ツール名 引数=値 intent=目的 expect=予測]

出力: {example}"""


# === メインループ ===
def main():
    print("=== 最小自律AIテスト ===")
    print(f"LLM: {llm_cfg['model']} @ {llm_cfg['base_url']}")
    print(f"state: {STATE_FILE}")
    _init_vector()
    print()

    import uuid
    state = load_state()
    state["session_id"] = str(uuid.uuid4())[:8]
    save_state(state)
    print(f"session: {state['session_id']}  cycle_id: {state['cycle_id']}")
    interval = BASE_INTERVAL
    cycle = 0

    while True:
        cycle += 1
        now = datetime.now().strftime("%H:%M:%S")
        print(f"--- cycle {cycle} [{now}] (interval={interval}s) ---")

        # Controller: stateからツール可用性・ログ長を導出
        ctrl = controller(state)
        allowed = ctrl["allowed_tools"]
        print(f"  ctrl: tools={sorted(allowed)} log={ctrl['n_log']}")

        # ① LLM: 候補提案
        propose_prompt = build_prompt_propose(state, ctrl)
        try:
            propose_resp = call_llm(propose_prompt, max_tokens=10000)
            append_debug_log("LLM1 (Propose)", propose_resp)
        except Exception as e:
            print(f"  LLM①エラー: {e}")
            time.sleep(interval)
            continue
        candidates = parse_candidates(propose_resp, ctrl["allowed_tools"])
        print(f"  LLM①raw: {propose_resp.strip()[:300]}")
        print(f"  候補({len(candidates)}件): {[(c['tool'], c['reason'][:40]) for c in candidates]}")

        # ② Controller: 候補から選択（D-architecture）
        selected = controller_select(candidates, ctrl, state)
        print(f"  選択: {selected['tool']} - {selected['reason'][:60]}")

        # ③ LLM: 実行
        exec_prompt = build_prompt_execute(state, ctrl, selected)
        try:
            response = call_llm(exec_prompt, max_tokens=10000)
            append_debug_log("LLM2 (Execute)", response)
        except Exception as e:
            print(f"  LLM②エラー: {e}")
            time.sleep(interval)
            continue

        # レスポンス表示
        response_clean = response.strip()
        print(f"  LLM②: {response_clean[:200]}")

        # 計画パース（ツール実行より先にチェック）
        plan_data = parse_plan(response_clean)
        if plan_data:
            state["plan"] = plan_data
            save_state(state)
            print(f"  計画更新: {plan_data['goal']} ({len(plan_data['steps'])}ステップ)")
            # 計画立案サイクルはwait扱いでログ記録
            cid = state.get("cycle_id", 0) + 1
            state["cycle_id"] = cid
            entry = {
                "id": f"{state.get('session_id','x')}_{cid:04d}",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tool": "wait",
                "result": f"計画: {plan_data['goal']}",
            }
            _archive_entries([entry])
            state["log"].append(entry)
            maybe_compress_log(state)
            save_state(state)
            print()
            time.sleep(interval)
            continue

        # ツールパース（複数対応）
        raw_calls = parse_tool_calls(response_clean)
        parse_failed = False
        if not raw_calls:
            print(f"  (ツールマーカー検出失敗)")
            parse_failed = response_clean[:120]
            raw_calls = [("wait", {})]

        # バリデーション
        valid_calls = []
        for tname, targs in raw_calls:
            if tname not in TOOLS:
                print(f"  (未知のツール: {tname})")
                parse_failed = f"未知のツール: {tname}"
            elif tname not in allowed:
                print(f"  (Controller却下: {tname})")
                parse_failed = f"却下: {tname}"
            else:
                valid_calls.append((tname, targs))
        if not valid_calls:
            valid_calls = [("wait", {})]

        # intent/expectは最初のツールから取る
        intent = valid_calls[0][1].pop("intent", "")
        expect = valid_calls[0][1].pop("expect", "")
        for _, targs in valid_calls[1:]:
            targs.pop("intent", "")
            targs.pop("expect", "")

        # ツールを順番に実行
        all_results = []
        all_tool_names = []
        for tname, targs in valid_calls:
            try:
                res = TOOLS[tname]["func"](targs)
            except Exception as e:
                res = f"エラー: {e}"
            state = load_state()
            all_results.append(f"[{tname}]\n{str(res)[:20000]}")
            all_tool_names.append(tname)
            print(f"  実行: {tname} → {str(res)[:100]}")

        tool_name = "+".join(all_tool_names)
        result_str = ("\n---\n".join(all_results))[:50000]

        # 計画の進捗を更新（wait以外のツールが含まれていれば進める）
        if any(n != "wait" for n in all_tool_names) and state.get("plan", {}).get("goal"):
            plan = state["plan"]
            if plan["current"] < len(plan["steps"]):
                plan["current"] += 1
                if plan["current"] >= len(plan["steps"]):
                    print(f"  計画完了: {plan['goal']}")
                    state["plan"] = {"goal": "", "steps": [], "current": 0}
        if intent:
            print(f"  intent: {intent}")
        if expect:
            print(f"  expect: {expect}")

        # E1-E4評価（⑤自己言及螺旋）
        e1 = _compare_expect_result(intent, expect) if intent and expect else ""
        e2 = _compare_expect_result(intent, result_str) if intent else ""
        e3 = _compare_expect_result(expect, result_str) if expect else ""
        e4 = _calc_e4(intent, state["log"]) if intent else ""
        if e1 or e2 or e3 or e4:
            print(f"  E1={e1} E2={e2} E3={e3} E4={e4}")

        # energy更新（E2,E3,E4の平均から。50%が損益分岐）
        delta = _update_energy(state, e2, e3, e4)
        if delta != 0:
            print(f"  energy: {round(state['energy'], 1)} (delta={delta:+.2f})")

        # 自己定義フラグ検出（計画文・実行文の両方をチェック）
        _FLAG_TERMS = ["AIアシスタント", "AI assistant", "AIAssistant"]
        detected = [t for t in _FLAG_TERMS if t in propose_resp or t in response_clean]
        if detected:
            flag_msg = f"[SYSTEM] 検出: {' / '.join(f'「{t}」' for t in detected)} という自己定義が検出・記録されました。"
            print(f"  {flag_msg}")
            result_str += f"\n{flag_msg}"

        # ログ記録
        cid = state.get("cycle_id", 0) + 1
        state["cycle_id"] = cid
        entry = {
            "id": f"{state.get('session_id','x')}_{cid:04d}",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tool": tool_name,
            "result": result_str,
        }
        if parse_failed:
            entry["parse_error"] = str(parse_failed)[:150]
        if intent:
            entry["intent"] = intent
        if expect:
            entry["expect"] = expect
        if e1:
            entry["e1"] = e1
        if e2:
            entry["e2"] = e2
        if e3:
            entry["e3"] = e3
        if e4:
            entry["e4"] = e4
        _archive_entries([entry])
        state["log"].append(entry)

        maybe_compress_log(state)
        save_state(state)

        # 間隔調整（waitが続くとバックオフ）
        recent_tools = [e["tool"] for e in state["log"][-5:]]
        if all(t == "wait" for t in recent_tools) and len(recent_tools) >= 5:
            interval = min(interval * 2, MAX_INTERVAL)
            print(f"  (wait連続 → interval={interval}s)")
        else:
            interval = BASE_INTERVAL

        print()
        time.sleep(interval)

if __name__ == "__main__":
    main()
