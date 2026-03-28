#!/usr/bin/env python3
"""
Fetch tweets from specified Twitter/X users and post new ones to Slack.
Uses multiple methods: X syndication API, Nitter RSS, and RSSHub as fallbacks.
Tracks already-posted tweets in posted_tweets.json to avoid duplicates.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html import unescape

import feedparser
import requests

# --- Configuration ---
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
CONFIG_FILE = Path(__file__).parent / "config.json"
STATE_FILE = Path(__file__).parent / "posted_tweets.json"
MAX_TWEET_AGE_HOURS = 48  # Post tweets from the last 48 hours


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_posted_tweets():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"posted_ids": []}


def save_posted_tweets(state):
    state["posted_ids"] = state["posted_ids"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def strip_html(text):
    """Remove HTML tags from text."""
    clean = re.sub(r'<[^>]+>', '', text)
    return unescape(clean).strip()


def fetch_via_syndication(handle):
    """Fetch tweets using X's syndication/embed timeline endpoint."""
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        print(f"  Trying syndication API...")
        resp = requests.get(url, timeout=15, headers=headers)
        if resp.status_code == 200 and resp.text:
            # Parse tweet data from the HTML response
            tweets = []
            # Find tweet links and text content
            tweet_pattern = re.findall(
                r'<a[^>]*href="https://twitter\.com/[^/]+/status/(\d+)"[^>]*>.*?</a>',
                resp.text, re.DOTALL
            )
            # Extract tweet text blocks
            text_blocks = re.findall(
                r'<p[^>]*class="[^"]*timeline-Tweet-text[^"]*"[^>]*>(.*?)</p>',
                resp.text, re.DOTALL
            )

            seen_ids = set()
            for i, tweet_id in enumerate(tweet_pattern):
                if tweet_id not in seen_ids:
                    seen_ids.add(tweet_id)
                    text = strip_html(text_blocks[i]) if i < len(text_blocks) else ""
                    tweets.append({
                        "id": tweet_id,
                        "text": text,
                        "link": f"https://x.com/{handle}/status/{tweet_id}",
                    })

            if tweets:
                print(f"  Got {len(tweets)} tweets from syndication API")
                return tweets
    except Exception as e:
        print(f"  Syndication failed: {e}")
    return []


def fetch_via_rss(handle, instances):
    """Try multiple RSSHub/Nitter instances to fetch a user's tweet feed."""
    for instance in instances:
        url = f"{instance}/twitter/user/{handle}"
        try:
            print(f"  Trying {url}...")
            resp = requests.get(url, timeout=15, headers={"User-Agent": "TweetToSlack/1.0"})
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                if feed.entries:
                    print(f"  Got {len(feed.entries)} entries from {instance}")
                    tweets = []
                    for entry in feed.entries:
                        link = entry.get("link", "")
                        tweet_id = link.split("/status/")[-1].split("?")[0] if "/status/" in link else entry.get("id", link)
                        text = strip_html(entry.get("summary", entry.get("title", "")))
                        tweets.append({
                            "id": tweet_id,
                            "text": text,
                            "link": link,
                        })
                    return tweets
        except requests.RequestException as e:
            print(f"  Failed: {e}")
            continue
    return []


def fetch_via_nitter(handle):
    """Try Nitter instances for RSS feeds."""
    nitter_instances = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.woodland.cafe",
    ]
    for instance in nitter_instances:
        url = f"{instance}/{handle}/rss"
        try:
            print(f"  Trying {url}...")
            resp = requests.get(url, timeout=15, headers={"User-Agent": "TweetToSlack/1.0"})
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                if feed.entries:
                    print(f"  Got {len(feed.entries)} entries from Nitter")
                    tweets = []
                    for entry in feed.entries:
                        link = entry.get("link", "").replace(instance, "https://x.com")
                        tweet_id = link.split("/status/")[-1].split("#")[0] if "/status/" in link else entry.get("id", "")
                        text = strip_html(entry.get("title", entry.get("summary", "")))
                        tweets.append({
                            "id": tweet_id,
                            "text": text,
                            "link": link,
                        })
                    return tweets
        except requests.RequestException as e:
            print(f"  Failed: {e}")
            continue
    return []


def fetch_user_tweets(handle, rsshub_instances):
    """Try all methods to fetch tweets for a user."""
    # Method 1: X Syndication API
    tweets = fetch_via_syndication(handle)
    if tweets:
        return tweets

    # Method 2: Nitter RSS
    tweets = fetch_via_nitter(handle)
    if tweets:
        return tweets

    # Method 3: RSSHub instances
    tweets = fetch_via_rss(handle, rsshub_instances)
    if tweets:
        return tweets

    print(f"  All methods failed for @{handle}")
    return []


def format_slack_message(tweet, handle):
    """Format a tweet as a nice Slack message."""
    text = tweet["text"]
    if len(text) > 500:
        text = text[:497] + "..."

    return {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*New tweet from <https://x.com/{handle}|@{handle}>* :bird:\n\n{text}"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":heart: Like & RT on X"},
                        "url": tweet["link"],
                        "action_id": f"open_tweet_{tweet['id']}"
                    }
                ]
            },
            {"type": "divider"}
        ]
    }


def post_to_slack(message):
    """Send a message to Slack via webhook."""
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json=message,
        headers={"Content-Type": "application/json"},
        timeout=10
    )
    if resp.status_code != 200:
        print(f"  Slack error: {resp.status_code} - {resp.text}")
        return False
    return True


def main():
    if not SLACK_WEBHOOK_URL:
        print("ERROR: SLACK_WEBHOOK_URL environment variable not set")
        sys.exit(1)

    config = load_config()
    state = load_posted_tweets()
    handles = config["twitter_handles"]
    rsshub_instances = config.get("rsshub_instances", [])
    new_tweets_count = 0

    print(f"Checking tweets for: {', '.join(handles)}")
    print(f"Previously posted: {len(state['posted_ids'])} tweets")

    for handle in handles:
        print(f"\nFetching @{handle}...")
        tweets = fetch_user_tweets(handle, rsshub_instances)

        for tweet in tweets:
            tweet_id = tweet["id"]

            if tweet_id in state["posted_ids"]:
                continue

            if not tweet["text"]:
                continue

            print(f"  New tweet found: {tweet_id}")
            message = format_slack_message(tweet, handle)

            if post_to_slack(message):
                state["posted_ids"].append(tweet_id)
                new_tweets_count += 1
                print(f"  Posted to Slack!")
                time.sleep(1)  # Rate limit courtesy
            else:
                print(f"  Failed to post to Slack")

    save_posted_tweets(state)
    print(f"\nDone! Posted {new_tweets_count} new tweets to Slack.")


if __name__ == "__main__":
    main()
