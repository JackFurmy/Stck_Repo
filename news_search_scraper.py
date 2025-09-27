#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
News scraper (Yahoo Finance focused) + enrichment + Discord posting.

Features
- Sources: auto | html | rss | api | yf | proxy
- UK-friendly: supports Yahoo consent via cookies or proxy mirror
- Enrichment: readability-lxml extraction + VADER sentiment + short summary
- Fast: tuned requests sessions, thread pool enrichment, optional on-disk cache
- Discord: pretty embeds with summaries & sentiment, per-channel de-dup
- CSV: overwrite same file name with --no-enriched-suffix

Environment (optional):
  YAHOO_COOKIES="A1=...; A1S=...; A3=...; GUC=..."
  DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
"""

import os, re, sys, csv, io, json, time, math, html, argparse, threading
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
import feedparser

# Optional deps
try:
    from readability import Document
except Exception:
    Document = None

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
except Exception:
    SentimentIntensityAnalyzer = None

# --------- HTTP session tuning ----------
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:
    Retry = None

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Connection": "keep-alive",
}

def _tune_session(s: requests.Session):
    if Retry:
        retry = Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    else:
        adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(HEADERS)
    return s

_session_main = _tune_session(requests.Session())
_thread_local = threading.local()

def get_session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = _tune_session(requests.Session())
        setattr(_thread_local, "session", s)
    return s

def http_get(url, *, timeout=25, cookies=None, allow_redirects=True):
    s = get_session()
    return s.get(url, timeout=timeout, cookies=cookies, allow_redirects=allow_redirects)

# --------- utils ----------
def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def clip(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n-1] + "…"

def parse_timeago(ts: datetime) -> str:
    if not ts:
        return ""
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:  return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:  return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 24:   return f"{hrs}h ago"
    days = hrs // 24
    return f"{days} day ago" if days == 1 else f"{days} days ago"

def is_international_symbol(symbol: str) -> bool:
    return "." in symbol or "-" in symbol  # e.g., RR.L, SAP.DE, BTC-USD

def clean_text(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t

def hostname(u: str) -> str:
    try:
        return urlparse(u).hostname or ""
    except Exception:
        return ""

# --------- args ----------
ap = argparse.ArgumentParser()
g = ap.add_mutually_exclusive_group(required=True)
g.add_argument("--symbol", help="Ticker symbol, e.g. RR.L")
g.add_argument("--query", help="Free-text query, e.g. 'Rolls-Royce'")

ap.add_argument("--limit", type=int, default=15)
ap.add_argument("--outfile", default="news.csv")
ap.add_argument("--source", choices=["auto","html","rss","api","yf","proxy"], default="auto")
ap.add_argument("--strict", action="store_true", help="Only include very relevant items")
ap.add_argument("--loose", action="store_true", help="Looser relevance gate")
ap.add_argument("--debug", action="store_true")

ap.add_argument("--enrich", action="store_true")
ap.add_argument("--delay", type=float, default=0.4)
ap.add_argument("--workers", type=int, default=4)

# Discord
ap.add_argument("--discord-webhook", default=os.environ.get("DISCORD_WEBHOOK_URL"))
ap.add_argument("--discord-username", default="News")
ap.add_argument("--discord-avatar", default=None)
ap.add_argument("--discord-batch", type=int, default=6)
ap.add_argument("--discord-thread", default=None)
ap.add_argument("--force-post", action="store_true")
ap.add_argument("--state-file", default=".posted_news.json")

# Cookies + speed flags
ap.add_argument("--yahoo-cookies", default=os.environ.get("YAHOO_COOKIES"))
ap.add_argument("--no-google", action="store_true", help="Skip Google News top-up")
ap.add_argument("--fast", action="store_true", help="Prefer fast sources")
ap.add_argument("--proxy-first-enrich", action="store_true", help="Use proxy for enrichment first")

# Cache
ap.add_argument("--cache-file", default=".news_cache.json")
ap.add_argument("--cache-ttl", type=int, default=86400, help="seconds")
ap.add_argument("--no-enriched-suffix", action="store_true", help="Write enriched rows to the exact outfile name")

args = ap.parse_args()

# --------- cache -----------
_CACHE = {}
_CACHE_PATH = args.cache_file
_CACHE_TTL = max(1, int(args.cache_ttl))

def _cache_load():
    global _CACHE
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            _CACHE = json.load(f)
    except Exception:
        _CACHE = {}

def _cache_save():
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f)
    except Exception:
        pass

def _cache_get(url):
    try:
        ent = _CACHE.get(url)
        if not ent: return None
        if (time.time() - ent.get("ts", 0)) > _CACHE_TTL:
            return None
        return ent.get("data")
    except Exception:
        return None

def _cache_put(url, data):
    try:
        _CACHE[url] = {"ts": time.time(), "data": data}
    except Exception:
        pass

_cache_load()

# --------- Discord posting ----------
def _discord_post(webhook, payload):
    r = requests.post(webhook, json=payload, timeout=25)
    r.raise_for_status()

def _discord_publish(rows, webhook, symbol, username, avatar, batch=6, state_file=".posted_news.json", thread_id=None, force=False):
    posted = {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            posted = json.load(f)
    except Exception:
        posted = {}

    sent = 0
    embeds = []

    # Header as requested
    content = "news! 🚨🚀"

    for row in rows:
        url = row.get("url")
        if not url:
            continue
        if (not force) and posted.get(url):
            continue

        title = clip(row.get("title",""), 240)
        pub = row.get("publisher") or hostname(url)
        ts  = row.get("published_at") or now_utc_iso()
        sumtxt = clip(row.get("summary") or "", 1000)
        sent_label = row.get("sentiment_label") or ""
        sent_score = row.get("sentiment", "")
        footer = f"{pub} • {parse_timeago(_parse_iso(ts))}" if ts else pub

        embed = {
            "title": title or url,
            "url": url,
            "description": sumtxt if sumtxt else None,
            "footer": {"text": f"{footer}  {sent_label} {sent_score}" if sent_label else footer},
        }
        embeds.append(embed)

        # send in batches
        if len(embeds) >= batch:
            payload = {
                "content": content,
                "username": username,
                "avatar_url": avatar,
                "embeds": embeds[:10]  # Discord hard limit
            }
            if thread_id:
                payload["thread_id"] = thread_id
            _discord_post(webhook, payload)
            sent += len(embeds)
            embeds = []
            for it in rows:
                if it.get("url"):
                    posted[it["url"]] = now_utc_iso()

    # flush remainder
    if embeds:
        payload = {
            "content": content,
            "username": username,
            "avatar_url": avatar,
            "embeds": embeds[:10]
        }
        if thread_id:
            payload["thread_id"] = thread_id
        _discord_post(webhook, payload)
        sent += len(embeds)

    # update state
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(posted, f)
    except Exception:
        pass

    return sent

# --------- parsing helpers ----------
def _parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception:
        return None

def extract_items_from_yahoo_html(html_text, base_url):
    soup = BeautifulSoup(html_text, "lxml")
    # Try new panel first
    tp = soup.select_one("#tabpanel-news")
    items = []
    def push(a, title, publisher=None, when=None):
        if not a: return
        href = a.get("href")
        if not href: return
        url = href if href.startswith("http") else f"https://{urlparse(base_url).hostname}{href}"
        items.append({"title": clean_text(title), "url": url, "publisher": publisher, "when": when})
    if tp:
        for sec in tp.select('[data-testid="storyitem"]'):
            a = sec.select_one("a.subtle-link")
            h3 = sec.select_one("h3")
            foot = sec.select_one(".publishing")
            push(a, h3.get_text(" ", strip=True) if h3 else a.get("title",""),
                 publisher=foot.get_text(" ", strip=True).split("•")[0].strip() if foot else None,
                 when=foot.get_text(" ", strip=True).split("•")[-1].strip() if foot and "•" in foot.get_text() else None)
    # Fallback: generic links
    if not items:
        for a in soup.select("a[href]"):
            t = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
            if not t: continue
            if "finance.yahoo.com" in base_url and "/news/" not in a.get("href","") and "qsp-recent-news" not in (a.get("data-ylk") or ""):
                continue
            push(a, t)
    # Clean duplicates
    seen = set(); out = []
    for it in items:
        u = it["url"]
        if u in seen: continue
        seen.add(u); out.append(it)
    return out

# --------- sources ----------
Y_DOMAINS = [
    "https://finance.yahoo.com",
    "https://uk.finance.yahoo.com",
    "https://sg.finance.yahoo.com",
    "https://m.finance.yahoo.com",
]

def fetch_via_html(symbol, limit=15, cookies=None, debug=False):
    urls = [f"/quote/{symbol}/latest-news", f"/quote/{symbol}/news", f"/quote/{symbol}/press-releases", f"/quote/{symbol}/"]
    rows = []
    for dom in Y_DOMAINS:
        for path in urls:
            url = f"{dom}{path}"
            r = http_get(url, cookies=cookies)
            txt = r.text or ""
            if r.status_code >= 500:
                if debug: print(f"[debug] HTTP {r.status_code} on {url}")
                continue
            if "consent" in txt.lower() and "guce" in txt.lower():
                if debug: print(f"[debug] consent wall on {url}, trying next domain")
                continue
            items = extract_items_from_yahoo_html(txt, url)
            rows.extend(items)
            if len(rows) >= limit: break
        if len(rows) >= limit: break
    return rows[:limit]

def fetch_via_proxy(symbol, limit=15, debug=False):
    # r.jina.ai mirrors raw HTML as text; easier to parse without JS/consent
    bases = [
        f"https://r.jina.ai/http://finance.yahoo.com/quote/{symbol}/latest-news",
        f"https://r.jina.ai/http://finance.yahoo.com/quote/{symbol}/news",
        f"https://r.jina.ai/http://finance.yahoo.com/quote/{symbol}/press-releases",
        f"https://r.jina.ai/http://sg.finance.yahoo.com/quote/{symbol}/latest-news",
        f"https://r.jina.ai/http://sg.finance.yahoo.com/quote/{symbol}/news",
        f"https://r.jina.ai/http://sg.finance.yahoo.com/quote/{symbol}/press-releases",
        f"https://r.jina.ai/http://m.finance.yahoo.com/quote/{symbol}/latest-news",
        f"https://r.jina.ai/http://m.finance.yahoo.com/quote/{symbol}/news",
        f"https://r.jina.ai/http://m.finance.yahoo.com/quote/{symbol}/press-releases",
    ]
    rows = []
    for u in bases:
        r = http_get(u)
        if r.status_code in (451, 422):
            if debug: print(f"[debug] proxy HTTP {r.status_code} on {u}")
            continue
        soup = BeautifulSoup(r.text or "", "lxml")
        for a in soup.select("a[href]"):
            href = a.get("href","")
            title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
            if not title or not href.startswith("http"):
                continue
            # skip chrome junk/text nav
            if re.search(r"(Privacy|cookie|partners|navigation|main content|right column)", title, re.I):
                continue
            rows.append({"title": title, "url": href})
        if len(rows) >= limit:
            break
    # de-dupe
    out, seen = [], set()
    for it in rows:
        u = it["url"]
        if u in seen: continue
        seen.add(u); out.append(it)
        if len(out) >= limit: break
    if debug: print(f"[debug] proxy rows: {len(out)}")
    return out

def fetch_via_rss(symbol_or_query, limit=15, debug=False):
    rows = []
    # Google News RSS (reliable, fast)
    q = quote_plus(symbol_or_query)
    rss = [
        f"https://news.google.com/rss/search?q={q}+when:7d&hl=en-GB&gl=GB&ceid=GB:en",
        f"https://news.google.com/rss/search?q={q}+when:14d&hl=en-US&gl=US&ceid=US:en",
    ]
    for url in rss:
        feed = feedparser.parse(url)
        for e in feed.entries[:limit]:
            title = clean_text(e.get("title",""))
            link  = e.get("link")
            pub   = clean_text((e.get("source") or {}).get("title") or (e.get("publisher") or ""))
            when  = e.get("published") or e.get("updated")
            rows.append({"title": title, "url": link, "publisher": pub, "when": when})
    # de-dupe
    out, seen = [], set()
    for it in rows:
        u = it["url"]; t = it["title"]
        key = (u or "")[:200]
        if key in seen: continue
        seen.add(key); out.append(it)
        if len(out) >= limit: break
    if debug: print(f"[debug] RSS rows: {len(out)}")
    return out

def fetch_via_api(symbol_or_query, limit=15, debug=False):
    # Yahoo search API (can 429; keep best-effort)
    rows = []
    base = "https://query2.finance.yahoo.com/v1/finance/search"
    for region in ("GB","US"):
        try:
            r = http_get(f"{base}?q={quote_plus(symbol_or_query)}&lang=en-GB&region={region}")
            if r.status_code == 429:
                if debug: print(f"[debug] API HTTP 429 for {symbol_or_query} {region}")
                continue
            data = r.json()
            # Pull news if present
            for n in (data.get("news") or [])[:limit]:
                title = clean_text(n.get("title") or "")
                link  = n.get("link")
                if not title or not link: continue
                rows.append({"title": title, "url": link, "publisher": n.get("publisher")})
        except Exception:
            pass
    if debug: print(f"[debug] API rows: {len(rows)}")
    # de-dupe
    out, seen = [], set()
    for it in rows:
        u = it["url"]
        if u in seen: continue
        seen.add(u); out.append(it)
        if len(out) >= limit: break
    return out

def fetch_via_yf(symbol, limit=15, debug=False):
    try:
        import yfinance as yf
    except Exception:
        return []
    rows = []
    try:
        tk = yf.Ticker(symbol)
        # yfinance: .news or .get_news()
        news = getattr(tk, "news", None)
        if callable(news):
            news = news()
        for n in (news or [])[:limit]:
            title = clean_text(n.get("title") or "")
            link  = n.get("link") or n.get("url")
            if not title or not link: continue
            rows.append({"title": title, "url": link, "publisher": n.get("publisher")})
    except Exception:
        pass
    if debug: print(f"[debug] yfinance rows: {len(rows)}")
    return rows[:limit]

# --------- relevance ----------
def get_company_aliases(symbol_or_query: str):
    s = symbol_or_query
    aliases = [s]
    # Very simple expansions (can be extended)
    if s.upper() == "RR.L":
        aliases += ["Rolls-Royce", "Rolls Royce", "Rolls-Royce Holdings"]
    return list(dict.fromkeys(aliases))

def is_relevant(title: str, aliases, loose=False):
    title = (title or "").lower()
    for a in aliases:
        if a.lower() in title:
            return True
    if loose:
        # Allow symbol-only or partial matches like "RR"
        if re.search(r"\b[A-Z]{2,5}\b", (title.upper())):
            return True
    return False

# --------- enrichment ----------
def _jina_fetch(url):
    # Try both http and https on mirror
    for base in ("http://", "https://"):
        ju = f"https://r.jina.ai/{base}{urlparse(url).hostname}{urlparse(url).path}"
        try:
            r = http_get(ju, timeout=20)
            if r.ok and len(r.text) > 500:
                return r.text
        except Exception:
            continue
    return ""

def _extract_from_jina(url):
    txt = _jina_fetch(url)
    if not txt: return "", ""
    soup = BeautifulSoup(txt, "lxml")
    # remove nav/footers
    for sel in ["header","footer","nav",".consent",".cookie",".banner"]:
        for el in soup.select(sel):
            el.decompose()
    body = clean_text(soup.get_text(" ", strip=True))
    if not body: return "", ""
    # summarise: first 2-3 sentences
    sents = re.split(r"(?<=[.!?])\s+", body)
    summary = " ".join(sents[:3]).strip()
    return body, summary

def _extract_readability(url, cookies=None):
    try:
        r = http_get(url, cookies=cookies, timeout=25)
        if not r.ok: return "", ""
        html_doc = r.text
        if Document:
            doc = Document(html_doc)
            summary_html = doc.summary(html_partial=True)
            soup = BeautifulSoup(summary_html, "lxml")
            body = clean_text(soup.get_text(" ", strip=True))
            sents = re.split(r"(?<=[.!?])\s+", body)
            return body, " ".join(sents[:3]).strip()
        else:
            soup = BeautifulSoup(html_doc, "lxml")
            body = clean_text(soup.get_text(" ", strip=True))
            sents = re.split(r"(?<=[.!?])\s+", body)
            return body, " ".join(sents[:3]).strip()
    except Exception:
        return "", ""

def sentiment_label(score):
    if score >= 0.25: return "🟢 Positive"
    if score <= -0.25: return "🔴 Negative"
    return "🟡 Neutral"

def extract_article(url, cookies=None):
    # cache?
    cached = _cache_get(url)
    if cached: return cached

    # proxy-first if asked
    if args.proxy_first_enrich:
        body, summary = _extract_from_jina(url)
        if body:
            data = {"canonical_url": url, "article_text": body, "summary": summary, "word_count": len(body.split())}
            _cache_put(url, data)
            return data

    # direct try
    body, summary = _extract_readability(url, cookies=cookies)
    data = {"canonical_url": url, "article_text": body, "summary": summary, "word_count": len(body.split())}
    _cache_put(url, data)
    return data

# --------- pipeline ----------
def enrich_rows(rows, cookies=None, delay=0.4, workers=4, debug=False):
    if not rows: return []
    out = [dict(r) for r in rows]

    analyzer = SentimentIntensityAnalyzer() if SentimentIntensityAnalyzer else None

    # Threaded enrichment
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def job(r):
        time.sleep(max(0.0, delay))
        dat = extract_article(r.get("url"), cookies=cookies)
        summary = dat.get("summary") or ""
        text = dat.get("article_text") or ""
        sent, label = "", ""
        if analyzer and (summary or text):
            sc = analyzer.polarity_scores((summary or text))
            sent = round(sc["compound"], 3)
            label = sentiment_label(sent)
        r2 = dict(r)
        r2["summary"] = summary
        r2["sentiment"] = sent
        r2["sentiment_label"] = label
        r2["article_text"] = text
        r2["enriched_at"] = now_utc_iso()
        return r2

    out2 = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = [ex.submit(job, r) for r in out]
        for f in as_completed(futs):
            try:
                out2.append(f.result())
            except Exception as e:
                if debug: print("[debug] enrich error:", e)
    # preserve input order roughly
    url_to_row = {r["url"]: r for r in out2 if r.get("url")}
    final = []
    for r in out:
        u = r.get("url")
        final.append(url_to_row.get(u, r))
    return final

def relevance_filter(rows, aliases, loose=False):
    out = []
    for r in rows:
        if is_relevant(r.get("title",""), aliases, loose=loose):
            out.append(r)
    return out

# --------- main ----------
def main():
    symbol = args.symbol or args.query
    aliases = get_company_aliases(symbol)
    if args.debug: print(f"[debug] aliases: {aliases}")

    # cookies
    cookies = None
    if args.yahoo_cookies:
        # requests accepts dict; parse simple "k=v; k2=v2" to dict
        cookies = {}
        for kv in re.split(r";\s*", args.yahoo_cookies.strip()):
            if "=" in kv:
                k,v = kv.split("=",1)
                cookies[k.strip()] = v.strip()

    # choose sources
    def try_rss():
        return fetch_via_rss(symbol, args.limit, debug=args.debug)
    def try_html():
        return fetch_via_html(symbol, args.limit, cookies=cookies, debug=args.debug)
    def try_api():
        return fetch_via_api(symbol, args.limit, debug=args.debug)
    def try_yf():
        return fetch_via_yf(symbol, args.limit, debug=args.debug)
    def try_proxy():
        return fetch_via_proxy(symbol, args.limit, debug=args.debug)

    if args.source == "rss":
        rows = try_rss()
    elif args.source == "html":
        rows = try_html()
    elif args.source == "api":
        rows = try_api()
    elif args.source == "yf":
        rows = try_yf()
    elif args.source == "proxy":
        rows = try_proxy()
    else:
        # auto
        if args.fast:
            if is_international_symbol(symbol):
                rows = try_api() or try_proxy() or try_rss() or try_html() or try_yf()
            else:
                rows = try_api() or try_rss() or try_proxy() or try_html() or try_yf()
        else:
            if is_international_symbol(symbol):
                rows = try_api() or try_html() or try_rss() or try_proxy() or try_yf()
            else:
                rows = try_rss() or try_api() or try_html() or try_proxy() or try_yf()

    rows = rows or []
    if not args.loose:
        rows = relevance_filter(rows, aliases, loose=False)
        if args.debug: print(f"[debug] rows after relevance filter: {len(rows)}")
    else:
        if args.debug: print(f"[debug] rows (loose): {len(rows)}")

    # Top up with Google News if needed
    if (not args.no_google) and len(rows) < args.limit:
        add = fetch_via_rss(symbol, args.limit - len(rows), debug=args.debug)
        # de-dupe
        have = set(r["url"] for r in rows if r.get("url"))
        for r in add:
            if r.get("url") not in have:
                rows.append(r)
                have.add(r.get("url"))
        if args.debug: print(f"[debug] topped up with Google News, total rows: {len(rows)}")

    if not rows:
        print("No relevant news items found for {}.".format(symbol))
        _cache_save()
        return

    # Enrich?
    if args.enrich:
        rows = enrich_rows(rows, cookies=cookies, delay=max(args.delay, 0.2), workers=max(args.workers, 3), debug=args.debug)

    # write CSV
    outfile = args.outfile
    if args.enrich and (not args.no_enriched_suffix):
        root, ext = os.path.splitext(outfile)
        outfile = f"{root}_enriched{ext or '.csv'}"
    fields = ["title","url","publisher","when","summary","sentiment","sentiment_label","article_text","enriched_at"]
    try:
        with open(outfile, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k,"") for k in fields})
        print(f"Wrote {len(rows)} row(s) -> {outfile}")
    except Exception as e:
        print("Failed to write CSV:", e, file=sys.stderr)

    # Discord
    if args.discord_webhook:
        posted = _discord_publish(
            rows,
            webhook=args.discord_webhook,
            symbol=symbol,
            username=args.discord_username,
            avatar=args.discord_avatar,
            batch=max(1, int(args.discord_batch)),
            state_file=args.state_file,
            thread_id=args.discord_thread,
            force=args.force_post,
        )
        print(f"Posted {posted} embed(s) to Discord.")

    _cache_save()

if __name__ == "__main__":
    main()
