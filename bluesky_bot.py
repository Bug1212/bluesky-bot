import feedparser
import asyncio
import os
import json
import httpx
import random
import re
import time
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
BSKY_HANDLE     = os.environ.get("BSKY_HANDLE", "")
BSKY_APP_PASS   = os.environ.get("BSKY_APP_PASS", "")
HF_API_KEY      = os.environ.get("HF_API_KEY", "")   # Free at huggingface.co — add to .env

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

def clean_prompt(prompt: str) -> str:
    """Sanitize prompt — remove special chars, limit to 100 chars."""
    prompt = re.sub(r"[^a-zA-Z0-9 .,]", " ", prompt)
    prompt = re.sub(r"\s+", " ", prompt).strip()
    return prompt[:100]

# ============================================================
# BLUESKY AUTH
# ============================================================
async def bsky_login(client: httpx.AsyncClient):
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
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    record = {
        "$type": "app.bsky.feed.post",
        "text": text[:300],
        "createdAt": now,
        "langs": ["en"]
    }

    if image_bytes:
        try:
            blob = await bsky_upload_image(client, token, image_bytes)
            record["embed"] = {
                "$type": "app.bsky.embed.images",
                "images": [{"image": blob, "alt": "AI generated news image"}]
            }
        except Exception as e:
            print(f"⚠️ Image upload error: {e}")

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
    print("🧵 Posting as thread...")

    uri, cid = await bsky_post(client, token, did, main_text, image_bytes=image_bytes)
    print("✅ Main post done!")

    root_ref = {"root": {"uri": uri, "cid": cid}, "parent": {"uri": uri, "cid": cid}}
    parent_uri, parent_cid = uri, cid

    for i, reply_text in enumerate(replies):
        await asyncio.sleep(2)

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
                    "3. 'image_prompt': short vivid image prompt under 80 chars. "
                    "Plain English words only. No special characters or quotes. "
                    "End with 'digital art 4k'.\n"
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
        prompt = data.get("image_prompt", f"{niche} technology news digital art 4k")
    except Exception:
        main   = f"{style['emoji']} {title[:240]}"
        thread = []
        prompt = f"{niche} technology concept digital art 4k"

    return main, thread, prompt

# ============================================================
# IMAGE — Method 1: Hugging Face SDXL (FREE, best quality)
# Get free key: https://huggingface.co/settings/tokens
# Add to .env: HF_API_KEY=hf_xxxxxxxxxxxx
# ============================================================
async def generate_image_hf(prompt: str):
    if not HF_API_KEY:
        print("   ⏭️  Skipping HF (no HF_API_KEY in .env)")
        return None

    url     = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
    headers = {"Authorization": f"Bearer {HF_API_KEY}"}
    payload = {"inputs": clean_prompt(prompt)}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            print("   🤗 Trying Hugging Face (SDXL)...")
            response = await client.post(url, headers=headers, json=payload)

            if response.status_code == 503:
                print("   ⏳ Model loading, waiting 20s and retrying...")
                await asyncio.sleep(20)
                response = await client.post(url, headers=headers, json=payload)

            if response.status_code == 200 and len(response.content) > 5000:
                print("   ✅ Hugging Face success!")
                return response.content

            print(f"   ⚠️ HF failed: status={response.status_code}")
    except Exception as e:
        print(f"   ⚠️ HF error: {e}")
    return None

# ============================================================
# IMAGE — Method 2: Pollinations with Flux model (no key needed)
# ============================================================
async def generate_image_pollinations(prompt: str):
    safe    = clean_prompt(prompt)
    encoded = safe.replace(" ", "%20").replace(",", "%2C")
    seed    = random.randint(1, 99999)
    # Use flux model which is more reliable than default
    url     = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=576&nologo=true&seed={seed}&model=flux"

    try:
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
            print("   🌸 Trying Pollinations (Flux)...")
            response = await client.get(url)
            size = len(response.content)
            if response.status_code == 200 and size > 5000:
                print("   ✅ Pollinations success!")
                return response.content
            print(f"   ⚠️ Pollinations failed: status={response.status_code}, size={size}")
    except Exception as e:
        print(f"   ⚠️ Pollinations error: {e}")
    return None

# ============================================================
# IMAGE — Method 3: Picsum placeholder (always works, no AI)
# ============================================================
async def generate_image_placeholder():
    seed = random.randint(1, 500)
    url  = f"https://picsum.photos/seed/{seed}/1200/675"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            print("   🖼️  Trying Picsum placeholder fallback...")
            response = await client.get(url)
            if response.status_code == 200 and len(response.content) > 5000:
                print("   ✅ Placeholder image ready!")
                return response.content
    except Exception as e:
        print(f"   ⚠️ Placeholder error: {e}")
    return None

# ============================================================
# IMAGE — Orchestrator: tries all methods in order
# ============================================================
async def generate_image(prompt: str):
    print(f"   📝 Clean prompt: {clean_prompt(prompt)}")

    # 1. Best quality: Hugging Face (needs free HF_API_KEY in .env)
    image = await generate_image_hf(prompt)
    if image:
        return image

    # 2. Fallback: Pollinations Flux
    image = await generate_image_pollinations(prompt)
    if image:
        return image

    # 3. Last resort: random photo placeholder
    image = await generate_image_placeholder()
    if image:
        return image

    print("   ❌ All image methods failed — posting text only")
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

    if not HF_API_KEY:
        print("⚠️  Tip: Add HF_API_KEY to .env for AI-generated images.")
        print("   Free key at: https://huggingface.co/settings/tokens\n")

    posted_links     = get_posted_links()
    articles_to_post = []

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

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            token, did = await bsky_login(client)
            print(f"✅ Logged in to Bluesky as {BSKY_HANDLE}")
        except Exception as e:
            print(f"❌ Bluesky login failed: {e}")
            return

        for niche, article in articles_to_post[:2]:
            title       = article.title
            link        = article.link
            description = getattr(article, "summary", title)
            style       = NICHE_STYLE[niche]

            print(f"\n📰 [{niche.upper()}] {title}")

            try:
                main_post, thread, image_prompt = generate_content(title, description, niche)
                print("✅ Content generated")
            except Exception as e:
                print(f"⚠️ Groq error: {e}")
                main_post    = f"{style['emoji']} {title[:240]}"
                thread       = []
                image_prompt = f"{niche} technology digital art 4k"

            print("🖼️  Generating image...")
            image_bytes = await generate_image(image_prompt)
            print("✅ Image ready!" if image_bytes else "⚠️ No image — text only")

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
