#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord bot wrapper around news_search_scraper.py

- Slash command: /news symbol: RR.L (or PLTR/NVDA/company name)
- Chat shortcut: send '/PLTR' in a channel (requires Message Content Intent)
- Auto-creates a channel webhook named 'news-bot' and passes it to the scraper
- For UK tickers, forwards YAHOO_COOKIES to the scraper

Env:
  DISCORD_TOKEN   = <bot token>   (required)
  YAHOO_COOKIES   = "A1=...; A1S=...; A3=...; GUC=..." (optional but recommended)
  NEWS_SCRIPT     = "news_search_scraper.py" (optional)
"""

import os, re, asyncio, subprocess
import discord
from discord import app_commands
from typing import Optional

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YAHOO_COOKIES = os.getenv("YAHOO_COOKIES", "")
SCRIPT_PATH   = os.getenv("NEWS_SCRIPT", "news_search_scraper.py")

if not DISCORD_TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in env.")

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # enable for '/TICKER' text shortcut

bot  = discord.Client(intents=INTENTS)
tree = app_commands.CommandTree(bot)

VALID_SOURCES = ["auto","proxy","rss","api","yf","html"]

async def _get_or_create_webhook(channel: discord.abc.GuildChannel) -> Optional[discord.Webhook]:
    if not hasattr(channel, "webhooks"):
        return None
    try:
        hooks = await channel.webhooks()
        for h in hooks:
            if h.name == "news-bot":
                return h
        return await channel.create_webhook(name="news-bot")
    except discord.Forbidden:
        return None

def _fix_symbol(s: str) -> str:
    s = s.strip()
    if re.fullmatch(r"nvida", s, re.I): return "NVDA"
    return s

async def _run_scraper(symbol: str, channel: discord.abc.Messageable, *,
                       source: str = "auto", limit: int = 15,
                       enrich: bool = True, fast: bool = True,
                       no_google: bool = True, proxy_first_enrich: bool = True,
                       workers: int = 4, delay: float = 0.4,
                       username: str = "News", force_post: bool = False,
                       loose: bool = False):
    wh = await _get_or_create_webhook(channel)
    if not wh:
        await channel.send("news! 🚨🚀  I need **Manage Webhooks** permission here.")
        return

    args = [
        "python3", SCRIPT_PATH,
        "--symbol", symbol,
        "--source", source,
        "--limit", str(limit),
        "--workers", str(workers),
        "--delay", str(delay),
        "--outfile", f"{symbol.replace('/','_')}_news.csv",
        "--discord-username", username,
        "--discord-webhook", wh.url,
        "--no-enriched-suffix",
        "--fast", "--no-google", "--proxy-first-enrich",
        "--enrich",
        "--state-file", f".posted_{symbol.upper().replace('.','_')}_{channel.id}.json",
    ]
    if loose: args.append("--loose")
    if force_post: args.append("--force-post")

    env = os.environ.copy()
    if YAHOO_COOKIES:
        env["YAHOO_COOKIES"] = YAHOO_COOKIES

    def _run():
        try:
            return subprocess.run(args, env=env, capture_output=True, text=True, timeout=420)
        except subprocess.TimeoutExpired as e:
            return e

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run)

    if isinstance(result, subprocess.TimeoutExpired):
        await channel.send(f"news! 🚨🚀 `{symbol}` timed out after 420s.")
        return
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-12:]
        msg = "```" + "\n".join(tail)[-1800:] + "```" if tail else "(no output)"
        await channel.send(f"news! 🚨🚀 `{symbol}` failed.\n{msg}")

@tree.command(name="news", description="Fetch & post ticker/company news (UK + US).")
@app_commands.describe(
    symbol="Ticker or company (e.g., RR.L, PLTR, Nvidia)",
    source="Source strategy (default auto)",
    limit="Items (default 15)",
    enrich="Summaries + sentiment (default true)",
    fast="Speed mode (default true)",
    no_google="Skip Google top-up (default true)",
    proxy_first_enrich="Use proxy first for enrichment (default true)",
    workers="Parallel enrichment workers (default 4)",
    delay="Per-article delay seconds (default 0.4)",
    force_post="Post even if previously posted",
    loose="Looser relevance filter"
)
@app_commands.choices(source=[app_commands.Choice(name=s, value=s) for s in VALID_SOURCES])
async def slash_news(interaction: discord.Interaction,
                     symbol: str,
                     source: Optional[app_commands.Choice[str]] = None,
                     limit: Optional[int] = 15,
                     enrich: Optional[bool] = True,
                     fast: Optional[bool] = True,
                     no_google: Optional[bool] = True,
                     proxy_first_enrich: Optional[bool] = True,
                     workers: Optional[int] = 4,
                     delay: Optional[float] = 0.4,
                     force_post: Optional[bool] = False,
                     loose: Optional[bool] = False):
    await interaction.response.defer(thinking=True, ephemeral=True)
    symbol = _fix_symbol(symbol)
    src = source.value if source else "auto"
    try:
        await _run_scraper(symbol, interaction.channel, source=src, limit=limit,
                           enrich=enrich, fast=fast, no_google=no_google,
                           proxy_first_enrich=proxy_first_enrich, workers=workers,
                           delay=delay, username="News", force_post=force_post, loose=loose)
        await interaction.followup.send(f"Queued **{symbol}** — results will appear below.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return
    content = message.content.strip()
    if not content.startswith("/") or " " in content: return
    symbol = _fix_symbol(content[1:])
    try: await message.channel.trigger_typing()
    except Exception: pass
    try:
        await _run_scraper(symbol, message.channel, source="auto", limit=15,
                           enrich=True, fast=True, no_google=True, proxy_first_enrich=True,
                           workers=4, delay=0.4, username="News", force_post=False, loose=False)
        try: await message.add_reaction("🚀")
        except Exception: pass
    except Exception as e:
        await message.channel.send(f"news! 🚨🚀 `{symbol}` error: {e}")

@bot.event
async def on_ready():
    try:
        await tree.sync()
        print(f"Logged in as {bot.user}. Slash commands synced.")
    except Exception as e:
        print("Slash sync failed:", e)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
