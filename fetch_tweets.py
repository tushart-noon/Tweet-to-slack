#!/usr/bin/env python3
"""
Fetch tweets from specified Twitter/X users via X API v2 and post new ones to Slack.
Tracks already-posted tweets in posted_tweets.json to avoid duplicates.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import unquote

import requests

# --- Configuration ---
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN")
CONFIG_FILE = Path(__file__).parent / "config.json"
STATE_FILE = Path(__file__).parent / "posted_tweets.json"
MAX_TWEET_AGE_HOURS = 96  # 4 days


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_posted_tweets():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"posted_ids": [], "user_ids": {}}


def save_posted_tweets(state):
    state["posted_ids"] = state["posted_ids"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def x_api_headers():
    """Get headers for X API requests."""
    token = unquote(X_BEARER_TOKEN)  # Decode URL-encoded token
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": "TweetToSlack/1.0",
    }


def get_user_id(handle, state):
    """Look up a Twitter user ID by handle. Caches in state."""
    if handle in state.get("user_ids", {}):
        return state["user_ids"][handle]

    url = f"https://api.x.com/2/users/by/username/{handle}"
    resp = requests.get(url, headers=x_api_headers(), timeout=15)
    print(f"  User lookup for @{handle}: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        if "data" in data:
            user_id = data["data"]["id"]
            if "user_ids" not in state:
                state["user_ids"] = {}
            state["user_ids"][handle] = user_id
            print(f"  Found user ID: {user_id}")
            return user_id

    print(f"  User lookup failed: {resp.text[:200]}")
    return None


def fetch_user_tweets(handle, user_id):
    """Fetch recent tweets for a user via X API v2."""
    # Get tweets from the last 48 hours
    since = (datetime.now(timezone.utc) - timedelta(hours=MAX_TWEET_AGE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = f"https://api.x.com/2/users/{user_id}/tweets"
    params = {
        "max_results": 10,
        "start_time": since,
        "tweet.fields": "created_at,text,public_metrics",
        "exclude": "retweets,replies",
    }

    resp = requests.get(url, headers=x_api_headers(), params=params, timeout=15)
    print(f"  Tweets API for @{handle}: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        tweets = data.get("data", [])
        print(f"  Found {len(tweets)} tweets")
        return tweets
    else:
        print(f"  Tweets API failed: {resp.text[:300]}")
        return []


def format_slack_message(tweet, handle):
    """Format a tweet as a nice Slack message."""
    text = tweet["text"]
    tweet_id = tweet["id"]
    link = f"https://x.com/{handle}/status/{tweet_id}"

    if len(text) > 500:
        text = text[:497] + "..."

    # Add engagement stats if available
    metrics = tweet.get("public_metrics", {})
    stats = ""
    if metrics:
        likes = metrics.get("like_count", 0)
        rts = metrics.get("retweet_count", 0)
        if likes or rts:
            stats = f"\n\n:heart: {likes}  :repeat: {rts}"

    return {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*New tweet from <https://x.com/{handle}|@{handle}>* :bird:\n\n{text}{stats}"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": ":heart: Like & RT on X"},
                        "url": link,
                        "action_id": f"open_tweet_{tweet_id}"
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
    if not X_BEARER_TOKEN:
        print("ERROR: X_BEARER_TOKEN environment variable not set")
        sys.exit(1)

    config = load_config()
    state = load_posted_tweets()
    handles = config["twitter_handles"]
    new_tweets_count = 0

    print(f"Checking tweets for: {', '.join(handles)}")
    print(f"Previously posted: {len(state['posted_ids'])} tweets")

    for handle in handles:
        print(f"\nFetching @{handle}...")

        # Get user ID
        user_id = get_user_id(handle, state)
        if not user_id:
            print(f"  Skipping @{handle} - could not resolve user ID")
            continue

        # Fetch tweets
        tweets = fetch_user_tweets(handle, user_id)

        for tweet in tweets:
            tweet_id = tweet["id"]

            if tweet_id in state["posted_ids"]:
                continue

            print(f"  New tweet found: {tweet_id}")
            message = format_slack_message(tweet, handle)

            if post_to_slack(message):
                state["posted_ids"].append(tweet_id)
                new_tweets_count += 1
                print(f"  Posted to Slack!")
                time.sleep(1)
            else:
                print(f"  Failed to post to Slack")

    save_posted_tweets(state)
    print(f"\nDone! Posted {new_tweets_count} new tweets to Slack.")


if __name__ == "__main__":
    main()
