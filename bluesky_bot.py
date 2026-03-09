import feedparser
import asyncio
import os
import json
import httpx
import random
import time
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
BSKY_HANDLE     = os.environ.get("BSKY_HANDLE", "")      # e.g. yourname.bsky.social
BSKY_APP_PASS   = os.environ.get("BSKY_APP_PASS", "")    # e.g. xxxx-xxxx-xxxx-xxxx

BSKY_API        = "https://bsky.social/xrpc"
POSTED_FILE     = "bluesky_posted_links.json"

# ============================================================
# RSS FEEDS
# ============================================================
FEEDS = {
    "tech": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.feedburner.com/venturebeat/SZYF",
    ],
    "realme": [
        "https://www.gsmarena.com/rss-news-reviews.php3",
        "https://www.91mobiles.com/feed",
        "https://gadgets360.com/rss/news",
    ]
}

REALME_KEYWORDS = ["realme", "Realme", "REALME"]

NICHE_STYLE = {
    "tech": {
        "emoji": "💻",
        "hashtags": ["#Tech", "#TechNews", "#Innovation", "#Startups"],
        "tone": "engaging and insightful tech industry news"
    },
    "realme": {
        "emoji": "📱",
        "hashtags": ["#Realme", "#Smartphone", "#Android", "#TechReview"],
        "tone": "exciting Realme smartphone and product news for tech enthusiasts"
    }
}

# ============================================================
# HELPERS
# ============================================================
def get_posted_links():
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r") as f:
            return json.load(f)
    return []

def save_posted_links(links):
    with open(POSTED_FILE, "w") as f:
        json.dump(links, f)

# ============================================================
# BLUESKY AUTH — get session token
# ============================================================
async def bsky_login(client: httpx.AsyncClient):
    """Login to Bluesky and return access token + DID."""
    response = await client.post(
        f"{BSKY_API}/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_APP_PASS}
    )
    response.raise_for_status()
    data = response.json()
    return data["accessJwt"], data["did"]

# ============================================================
# BLUESKY — upload image blob
# ============================================================
async def bsky_upload_image(client: httpx.AsyncClient, token: str, image_bytes: bytes):
    """Upload image to Bluesky and return blob reference."""
    response = await client.post(
        f"{BSKY_API}/com.atproto.repo.uploadBlob",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "image/jpeg"
        },
        content=image_bytes
    )
    response.raise_for_status()
    return response.json()["blob"]

# ============================================================
# BLUESKY — create a post
# ============================================================
async def bsky_post(client: httpx.AsyncClient, token: str, did: str,
                    text: str, image_bytes=None, reply_ref=None):
    """Create a single Bluesky post, optionally with image and reply."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    record = {
        "$type": "app.bsky.feed.post",
        "text": text[:300],
        "createdAt": now,
        "langs": ["en"]
    }

    # Attach image if provided
    if image_bytes:
        try:
            blob = await bsky_upload_image(client, token, image_bytes)
            record["embed"] = {
                "$type": "app.bsky.embed.images",
                "images": [{"image": blob, "alt": "AI generated news image"}]
            }
        except Exception as e:
            print(f"⚠️ Image upload error: {e}")

    # Thread reply reference
    if reply_ref:
        record["reply"] = reply_ref

    response = await client.post(
        f"{BSKY_API}/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "repo": did,
            "collection": "app.bsky.feed.post",
            "record": record
        }
    )
    response.raise_for_status()
    data = response.json()
    return data["uri"], data["cid"]

# ============================================================
# BLUESKY — post thread
# ============================================================
async def bsky_post_thread(client: httpx.AsyncClient, token: str, did: str,
                            main_text: str, replies: list,
                            link: str, hashtags: list, image_bytes=None):
    """Post main + thread replies on Bluesky."""
    print("🧵 Posting as thread...")

    # Main post with image
    uri, cid = await bsky_post(client, token, did, main_text, image_bytes=image_bytes)
    print(f"✅ Main post done!")

    root_ref = {"root": {"uri": uri, "cid": cid}, "parent": {"uri": uri, "cid": cid}}
    parent_uri, parent_cid = uri, cid

    for i, reply_text in enumerate(replies):
        await asyncio.sleep(2)

        # Last reply gets link + hashtags
        if i == len(replies) - 1:
            tags = " ".join(hashtags)
            reply_text = f"{reply_text}\n\n🔗 {link}\n\n{tags}"

        reply_ref = {
            "root": {"uri": root_ref["root"]["uri"], "cid": root_ref["root"]["cid"]},
            "parent": {"uri": parent_uri, "cid": parent_cid}
        }

        try:
            parent_uri, parent_cid = await bsky_post(
                client, token, did, reply_text, reply_ref=reply_ref
            )
            print(f"✅ Thread reply {i+1}/{len(replies)} posted!")
        except Exception as e:
            print(f"⚠️ Thread reply {i+1} failed: {e}")

# ============================================================
# GROQ — generate post content
# ============================================================
def generate_content(title: str, description: str, niche: str):
    """Returns main_post text, thread replies, and image prompt."""
    client = Groq(api_key=GROQ_API_KEY)
    style  = NICHE_STYLE[niche]

    response = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are a viral social media content creator specializing in {style['tone']}. "
                    "Always respond in valid JSON only with these fields:\n"
                    "1. 'main_post': a punchy hook post under 250 chars. "
                    "Start with an emoji. Make it curiosity-driven. No hashtags.\n"
                    "2. 'thread': list of exactly 3 follow-up posts that tell the full story. "
                    "Each under 280 chars. Add 1-2 emojis per post. No hashtags.\n"
                    "3. 'image_prompt': vivid image generation prompt matching the article. "
                    "End with 'digital art, 4k, cinematic lighting'.\n"
                    "Respond with JSON only. No extra text."
                )
            },
            {
                "role": "user",
                "content": f"Title: {title}\n\nDescription: {description}"
            }
        ],
        model="llama-3.3-70b-versatile",
        temperature=0.8,
        max_tokens=600,
    )

    raw = response.choices[0].message.content.strip()

    try:
        raw    = raw.replace("```json", "").replace("```", "").strip()
        data   = json.loads(raw)
        main   = data.get("main_post", f"{style['emoji']} {title[:240]}")
        thread = data.get("thread", [])
        prompt = data.get("image_prompt", f"{niche} technology news, digital art, 4k")
    except Exception:
        main   = f"{style['emoji']} {title[:240]}"
        thread = []
        prompt = f"{niche} technology concept, digital art, 4k, cinematic lighting"

    return main, thread, prompt

# ============================================================
# IMAGE — Pollinations.AI (free)
# ============================================================
async def generate_image(prompt: str):
    try:
        encoded  = prompt.replace(" ", "%20").replace(",", "%2C")
        url      = f"https://image.pollinations.ai/prompt/{encoded}?width=1200&height=675&nologo=true"

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, follow_redirects=True)
            if response.status_code == 200:
                return response.content
    except Exception as e:
        print(f"⚠️ Image error: {e}")
    return None

# ============================================================
# MAIN
# ============================================================
async def run_bluesky_bot():
    print("🦋 Bluesky Bot Starting...")
    print("=" * 50)

    if not BSKY_HANDLE or not BSKY_APP_PASS:
        print("❌ Bluesky credentials missing! Check your .env file.")
        return

    posted_links     = get_posted_links()
    articles_to_post = []

    # Collect 1 fresh article per niche
    for niche, feed_urls in FEEDS.items():
        found = False
        for feed_url in feed_urls:
            if found:
                break
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:15]:
                    link = entry.link
                    text = entry.title + getattr(entry, "summary", "")

                    if niche == "realme":
                        if not any(kw in text for kw in REALME_KEYWORDS):
                            continue

                    if link not in posted_links:
                        articles_to_post.append((niche, entry))
                        found = True
                        break
            except Exception as e:
                print(f"⚠️ Feed error {feed_url}: {e}")

    if not articles_to_post:
        print("🟡 No new articles found.")
        return

    # Login to Bluesky once
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            token, did = await bsky_login(client)
            print(f"✅ Logged in to Bluesky as {BSKY_HANDLE}")
        except Exception as e:
            print(f"❌ Bluesky login failed: {e}")
            return

        # Post max 2 articles
        for niche, article in articles_to_post[:2]:
            title       = article.title
            link        = article.link
            description = getattr(article, "summary", title)
            style       = NICHE_STYLE[niche]

            print(f"\n📰 [{niche.upper()}] {title}")

            # 1. Generate content
            try:
                main_post, thread, image_prompt = generate_content(title, description, niche)
                print("✅ Content generated")
            except Exception as e:
                print(f"⚠️ Groq error: {e}")
                main_post    = f"{style['emoji']} {title[:240]}"
                thread       = []
                image_prompt = f"{niche} technology, digital art, 4k"

            # 2. Generate image
            print("🖼️  Generating image...")
            image_bytes = await generate_image(image_prompt)
            print("✅ Image ready!" if image_bytes else "⚠️ No image — text only")

            # 3. Mix: randomly post as thread or single post
            try:
                if thread and random.choice([True, False]):
                    await bsky_post_thread(
                        client, token, did,
                        main_post, thread,
                        link, style["hashtags"],
                        image_bytes
                    )
                else:
                    tags      = " ".join(style["hashtags"])
                    full_post = f"{main_post}\n\n🔗 {link}\n\n{tags}"
                    await bsky_post(client, token, did, full_post, image_bytes=image_bytes)
                    print("✅ Single post done!")
            except Exception as e:
                print(f"⚠️ Post failed: {e}")

            posted_links.append(link)
            await asyncio.sleep(3)

    posted_links = posted_links[-100:]
    save_posted_links(posted_links)
    print("\n✅ Bluesky bot finished!")

# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    asyncio.run(run_bluesky_bot())
