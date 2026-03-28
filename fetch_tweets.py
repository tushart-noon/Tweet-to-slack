#!/usr/bin/env python3
"""
Fetch tweets from specified Twitter/X users via RSSHub and post new ones to Slack.
Tracks already-posted tweets in posted_tweets.json to avoid duplicates.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests

# --- Configuration ---
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
CONFIG_FILE = Path(__file__).parent / "config.json"
STATE_FILE = Path(__file__).parent / "posted_tweets.json"
MAX_TWEET_AGE_HOURS = 24  # Only post tweets from the last 24 hours


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_posted_tweets():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"posted_ids": []}


def save_posted_tweets(state):
    # Keep only last 500 IDs to prevent unbounded growth
    state["posted_ids"] = state["posted_ids"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_user_feed(handle, rsshub_instances):
    """Try multiple RSSHub instances to fetch a user's tweet feed."""
    for instance in rsshub_instances:
        url = f"{instance}/twitter/user/{handle}"
        try:
            print(f"  Trying {url}...")
            resp = requests.get(url, timeout=15, headers={"User-Agent": "TweetToSlack/1.0"})
            if resp.status_code == 200:
                feed = feedparser.parse(resp.text)
                if feed.entries:
                    print(f"  Got {len(feed.entries)} entries from {instance}")
                    return feed.entries
        except requests.RequestException as e:
            print(f"  Failed: {e}")
            continue
    print(f"  All instances failed for @{handle}")
    return []


def extract_tweet_id(entry):
    """Extract a unique ID from a feed entry."""
    # RSSHub typically uses the tweet URL as the link
    link = entry.get("link", "")
    if "/status/" in link:
        return link.split("/status/")[-1].split("?")[0]
    # Fallback to entry id or link
    return entry.get("id", link)


def is_recent(entry, max_age_hours):
    """Check if a feed entry is within the max age window."""
    published = entry.get("published_parsed")
    if not published:
        return True  # If no date, include it to be safe
    entry_time = datetime(*published[:6], tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return entry_time > cutoff


def format_slack_message(entry, handle):
    """Format a tweet as a nice Slack message."""
    link = entry.get("link", "")
    title = entry.get("title", "New tweet")

    # Clean up HTML from the summary if present
    summary = entry.get("summary", title)

    # Truncate if too long
    if len(summary) > 500:
        summary = summary[:497] + "..."

    return {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*New tweet from <https://x.com/{handle}|@{handle}>* :bird:\n\n{summary}"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":heart: Like & RT on X"},
                        "url": link,
                        "action_id": "open_tweet"
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
    instances = config["rsshub_instances"]
    new_tweets_count = 0

    print(f"Checking tweets for: {', '.join(handles)}")
    print(f"Using {len(instances)} RSSHub instances")
    print(f"Previously posted: {len(state['posted_ids'])} tweets")

    for handle in handles:
        print(f"\nFetching @{handle}...")
        entries = fetch_user_feed(handle, instances)

        for entry in entries:
            tweet_id = extract_tweet_id(entry)

            if tweet_id in state["posted_ids"]:
                continue

            if not is_recent(entry, MAX_TWEET_AGE_HOURS):
                continue

            print(f"  New tweet found: {tweet_id}")
            message = format_slack_message(entry, handle)

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
