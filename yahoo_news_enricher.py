#!/usr/bin/env python3
# Read a CSV (from the scraper), add canonical_url, article_text, summary, word_count, sentiment.

import csv
import re
import sys
import time
import requests
from bs4 import BeautifulSoup

# Optional (recommended) deps:
try:
    from readability import Document
except Exception:
    Document = None
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
except Exception:
    SentimentIntensityAnalyzer = None

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.8"}
session = requests.Session(); session.headers.update(HEADERS)

def canonical_from_soup(soup, fallback):
    link = soup.find("link", rel=lambda v: v and "canonical" in v.lower())
    if link and link.get("href"):
        return link["href"]
    og = soup.find("meta", property="og:url")
    if og and og.get("content"):
        return og["content"]
    return fallback

def fetch_and_extract(url, timeout=25):
    if Document is None:
        return {"canonical_url": url, "article_text": "", "summary": "", "word_count": 0}
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return {"canonical_url": url, "article_text": "", "summary": "", "word_count": 0}
    soup = BeautifulSoup(r.text, "lxml")
    canonical = canonical_from_soup(soup, r.url)
    try:
        doc = Document(r.text)
        main_text = BeautifulSoup(doc.summary(), "lxml").get_text(" ", strip=True)
    except Exception:
        main_text = ""
    if len(main_text) < 400:
        paras = [p.get_text(" ", strip=True) for p in soup.select("article p")]
        if paras:
            main_text = " ".join(paras)
    main_text = re.sub(r"\s+", " ", main_text or "").strip()
    if not main_text:
        return {"canonical_url": canonical, "article_text": "", "summary": "", "word_count": 0}
    sents = re.split(r"(?<=[.!?])\s+", main_text)
    summary = " ".join(sents[:3])[:800]
    return {
        "canonical_url": canonical,
        "article_text": main_text[:8000],
        "summary": summary,
        "word_count": len(main_text.split()),
    }

def add_sentiment(rows):
    if SentimentIntensityAnalyzer is None:
        for r in rows:
            r["sentiment"] = 0.0
        return rows
    ana = SentimentIntensityAnalyzer()
    for r in rows:
        txt = r.get("summary") or r.get("article_text") or ""
        r["sentiment"] = round(ana.polarity_scores(txt)["compound"], 4) if txt else 0.0
    return rows

def main(inp, outp, delay=0.7):
    with open(inp, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("Input CSV is empty.", file=sys.stderr)
        sys.exit(1)

    # Build field order
    keys = set().union(*[r.keys() for r in rows]) | {"canonical_url","article_text","summary","word_count","sentiment"}
    order = [k for k in ("symbol","title","url","canonical_url","source","time_ago","published_iso","summary","article_text","word_count","sentiment") if k in keys]
    order += [k for k in sorted(keys) if k not in order]

    out_rows = []
    for i, r in enumerate(rows, 1):
        info = fetch_and_extract(r.get("url",""))
        r.update(info)
        out_rows.append(r)
        if delay > 0:
            time.sleep(delay)
        print(f"[enrich {i}/{len(rows)}] {r.get('title','')[:60]}…")

    out_rows = add_sentiment(out_rows)

    with open(outp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=order)
        w.writeheader()
        for r in out_rows:
            w.writerow(r)
    print(f"Enriched -> {outp}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 yahoo_news_enricher.py <input_csv> <output_csv> [delay_seconds]", file=sys.stderr)
        sys.exit(1)
    delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.7
    main(sys.argv[1], sys.argv[2], delay)
