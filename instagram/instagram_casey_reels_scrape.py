#!/usr/bin/env python3
"""
Scrape Instagram posts / reels data for Dr. Casey Means (@drcaseyskitchen).

Co-founder of Levels Health, author of "Good Energy".
Confirmed profile: https://www.instagram.com/drcaseyskitchen/  (844K followers, 846 posts)

Seven attempts are made, in order, and EVERY response is printed so the
outcome is verifiable:

  1. /reels/ HTML  -> JSON blobs in <script> tags (_sharedData, etc.)
  2. Picuki mirror with a desktop browser session
  3. Instaloader (anonymous)
  4. Profile HTML -> embedded JSON (_sharedData, edge_owner_to_timeline_media)
  5. web_profile_info API + graphql/query endpoint
  6. /feed/ RSS / Atom
  7. Google webcache

Aggregated findings are written to instagram_casey_reels_results.json.
"""

import json
import re
import sys
import traceback

USERNAME = "drcaseyskitchen"
TIMEOUT = 25

RESULTS = {
    "username": USERNAME,
    "attempts": {},          # step name -> structured outcome
    "posts": [],             # any post/reel records recovered
    "profile": {},           # profile-level fields recovered
}


def banner(text):
    print("\n" + "=" * 74)
    print(text)
    print("=" * 74)


def record(step, **kw):
    RESULTS["attempts"][step] = kw
    return kw


# --------------------------------------------------------------------------
# Shared HTTP helpers
# --------------------------------------------------------------------------
import requests  # noqa: E402

DESKTOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

MOBILE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 "
                   "Mobile/15E148 Safari/604.1"),
}


def show(resp, n=1500):
    """Print a compact, verifiable summary of an HTTP response."""
    print(f"   status      = {resp.status_code}")
    print(f"   final_url   = {resp.url}")
    print(f"   content-type= {resp.headers.get('Content-Type', '?')}")
    print(f"   bytes       = {len(resp.content)}")
    preview = resp.text[:n].replace("\n", " ")
    print(f"   body[:{n}] = {preview}")


# --------------------------------------------------------------------------
# Post-extraction helpers (work on any IG HTML / JSON blob)
# --------------------------------------------------------------------------
import datetime as _dt  # noqa: E402

SHORTCODE_RE = re.compile(r'"shortcode"\s*:\s*"([\w-]{5,})"')
CODE_RE = re.compile(r'"code"\s*:\s*"([\w-]{5,20})"')
JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.DOTALL)


def _ts_to_iso(ts):
    try:
        return _dt.datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"
    except (TypeError, ValueError):
        return None


def _node_to_post(node, source):
    """Classic graphql GraphImage/GraphVideo node -> post record."""
    cap = ""
    edges = node.get("edge_media_to_caption", {}).get("edges", [])
    if edges:
        cap = edges[0].get("node", {}).get("text", "")
    sc = node.get("shortcode", "")
    return {
        "shortcode": sc,
        "url": f"https://www.instagram.com/p/{sc}/" if sc else None,
        "is_video": node.get("is_video"),
        "caption": cap[:300],
        "timestamp": node.get("taken_at_timestamp"),
        "date": _ts_to_iso(node.get("taken_at_timestamp")),
        "likes": node.get("edge_liked_by", {}).get("count")
        or node.get("edge_media_preview_like", {}).get("count"),
        "comments": node.get("edge_media_to_comment", {}).get("count"),
        "video_views": node.get("video_view_count"),
        "source": source,
    }


def _looks_like_media_item(d):
    """A modern IG media item dict has a `code` plus typical media fields."""
    if not isinstance(d, dict):
        return False
    code = d.get("code")
    if not (isinstance(code, str) and 5 <= len(code) <= 20):
        return False
    return any(k in d for k in
               ("caption", "taken_at", "like_count", "play_count",
                "media_type", "image_versions2", "view_count"))


def _modern_item_to_post(d, source):
    cap = d.get("caption")
    if isinstance(cap, dict):
        cap = cap.get("text", "")
    elif not isinstance(cap, str):
        cap = ""
    code = d.get("code", "")
    # media_type: 1=image, 2=video, 8=carousel; product_type 'clips' => reel
    is_reel = d.get("product_type") == "clips"
    is_video = d.get("media_type") == 2 or is_reel
    return {
        "shortcode": code,
        "url": (f"https://www.instagram.com/reel/{code}/" if is_reel
                else f"https://www.instagram.com/p/{code}/") if code else None,
        "is_video": is_video,
        "is_reel": is_reel,
        "caption": (cap or "")[:300],
        "timestamp": d.get("taken_at"),
        "date": _ts_to_iso(d.get("taken_at")),
        "likes": d.get("like_count"),
        "comments": d.get("comment_count"),
        "video_views": d.get("play_count") or d.get("view_count")
        or d.get("ig_play_count"),
        "source": source,
    }


def _walk_json(obj, found, source, depth=0):
    """Recursively walk a parsed JSON object collecting media items."""
    if depth > 40:
        return
    if isinstance(obj, dict):
        if _looks_like_media_item(obj):
            found.append(_modern_item_to_post(obj, source))
        # classic graphql node
        if obj.get("__typename") in ("GraphImage", "GraphVideo", "GraphSidecar") \
                and obj.get("shortcode"):
            found.append(_node_to_post(obj, source))
        for v in obj.values():
            _walk_json(v, found, source, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json(v, found, source, depth + 1)


def harvest_posts_from_text(text, source):
    """Find post/reel records inside arbitrary IG HTML or a JSON string."""
    posts = []

    # 1) Walk every <script type="application/json"> blob (modern IG).
    for blob in JSON_SCRIPT_RE.findall(text):
        blob = blob.strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        _walk_json(data, posts, source)

    # 2) If the whole text is itself JSON, walk it directly.
    if not posts:
        try:
            _walk_json(json.loads(text), posts, source)
        except ValueError:
            pass

    # 3) Classic inline graphql edges.
    if not posts:
        for m in re.finditer(
            r'\{"node":\{"__typename":"Graph(?:Image|Video|Sidecar)".*?\}\}',
            text,
        ):
            try:
                node = json.loads(m.group(0)).get("node", {})
            except ValueError:
                continue
            posts.append(_node_to_post(node, source))

    # 4) Last-resort: loose shortcode / code hits.
    #    Real IG shortcodes are ~11 chars and almost always carry an
    #    uppercase letter or digit; reject locale codes (en_US) and other
    #    lowercase tokens that would otherwise be false positives.
    def _plausible_shortcode(s):
        if not (8 <= len(s) <= 15):
            return False
        if re.fullmatch(r"[a-z]{2}_[A-Z]{2}", s):
            return False
        return any(c.isupper() or c.isdigit() for c in s)

    if not posts:
        seen = set()
        for sc in SHORTCODE_RE.findall(text) + CODE_RE.findall(text):
            if sc in seen or not _plausible_shortcode(sc):
                continue
            seen.add(sc)
            posts.append({"shortcode": sc,
                          "url": f"https://www.instagram.com/p/{sc}/",
                          "source": source + "_loose"})

    # de-dupe within this harvest
    uniq, seen = [], set()
    for p in posts:
        sc = p.get("shortcode")
        key = sc or id(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def add_posts(posts):
    existing = {p.get("shortcode") for p in RESULTS["posts"]}
    added = 0
    for p in posts:
        sc = p.get("shortcode")
        if sc and sc not in existing:
            RESULTS["posts"].append(p)
            existing.add(sc)
            added += 1
    return added


# ==========================================================================
# STEP 1 — /reels/ HTML, look for embedded JSON blobs
# ==========================================================================
def step1_reels_html():
    banner("STEP 1  -  fetch /reels/ HTML and parse embedded JSON")
    url = f"https://www.instagram.com/{USERNAME}/reels/"
    try:
        r = requests.get(url, headers=DESKTOP_HEADERS, timeout=TIMEOUT)
    except Exception as exc:
        print(f"   REQUEST FAILED: {exc}")
        return record("1_reels_html", error=str(exc))
    show(r)

    out = {"status": r.status_code, "blobs": []}

    for pat, label in [
        (r'window\._sharedData\s*=\s*({.*?});</script>', "_sharedData"),
        (r'window\.__additionalDataLoaded\s*\([^,]+,\s*({.*?})\);', "__additionalDataLoaded"),
        (r'<script type="application/json"[^>]*>(.*?)</script>', "application/json"),
    ]:
        hits = re.findall(pat, r.text, re.DOTALL)
        if hits:
            print(f"   found {len(hits)} '{label}' blob(s)")
            out["blobs"].append({"type": label, "count": len(hits)})

    posts = harvest_posts_from_text(r.text, "step1_reels_html")
    out["posts_found"] = len(posts)
    out["posts_added"] = add_posts(posts)
    print(f"   posts/reels harvested from /reels/ HTML: {len(posts)}")
    return record("1_reels_html", **out)


# ==========================================================================
# STEP 2 — Picuki mirror
# ==========================================================================
def step2_picuki():
    banner("STEP 2  -  Picuki mirror (desktop browser session)")
    from bs4 import BeautifulSoup

    session = requests.Session()
    url = f"https://www.picuki.com/profile/{USERNAME}"
    try:
        r = session.get(url, headers=DESKTOP_HEADERS, timeout=TIMEOUT)
    except Exception as exc:
        print(f"   REQUEST FAILED: {exc}")
        return record("2_picuki", error=str(exc))
    show(r, n=5000)

    out = {"status": r.status_code}
    if r.status_code == 200 and r.text:
        soup = BeautifulSoup(r.text, "html.parser")
        m = re.search(r"([\d.,]+[KMB]?)\s*Followers", r.text, re.IGNORECASE)
        if m:
            out["followers_text"] = m.group(1)
        posts = []
        for art in soup.select(".box-photo, .photo, article")[:30]:
            link = art.find("a", href=True)
            cap = art.select_one(".photo-description, .description, figcaption")
            posts.append({
                "url": link["href"] if link else None,
                "caption": cap.get_text(" ", strip=True)[:300] if cap else None,
                "source": "step2_picuki",
            })
        posts = [p for p in posts if p["url"] or p["caption"]]
        out["posts_found"] = len(posts)
        print(f"   Picuki posts parsed: {len(posts)}")
    return record("2_picuki", **out)


# ==========================================================================
# STEP 3 — Instaloader (anonymous)
# ==========================================================================
def step3_instaloader():
    banner("STEP 3  -  Instaloader (anonymous, no login)")
    out = {}
    try:
        import instaloader
    except ImportError as exc:
        print(f"   instaloader not importable: {exc}")
        return record("3_instaloader", error=str(exc))

    try:
        L = instaloader.Instaloader(quiet=True, download_pictures=False,
                                    download_videos=False, save_metadata=False)
        profile = instaloader.Profile.from_username(L.context, USERNAME)
        out["followers"] = profile.followers
        out["mediacount"] = profile.mediacount
        out["biography"] = profile.biography
        out["full_name"] = profile.full_name
        print(f"   Followers : {profile.followers}")
        print(f"   Posts     : {profile.mediacount}")
        print(f"   Bio       : {profile.biography}")
        RESULTS["profile"].update({k: out[k] for k in
                                   ("followers", "mediacount", "biography", "full_name")})

        harvested = []
        for i, post in enumerate(profile.get_posts()):
            rec = {
                "shortcode": post.shortcode,
                "url": f"https://www.instagram.com/p/{post.shortcode}/",
                "date": str(post.date_utc),
                "caption": (post.caption or "")[:300],
                "likes": post.likes,
                "comments": post.comments,
                "is_video": post.is_video,
                "video_views": post.video_view_count if post.is_video else None,
                "source": "step3_instaloader",
            }
            harvested.append(rec)
            print(f"   [{i}] {rec['shortcode']} {rec['date']} likes={rec['likes']}")
            if i >= 29:        # cap at 30 to avoid rate limiting
                break
        out["posts_found"] = len(harvested)
        out["posts_added"] = add_posts(harvested)
    except Exception as exc:
        print(f"   instaloader failed: {exc}")
        out["error"] = f"{type(exc).__name__}: {exc}"
    return record("3_instaloader", **out)


# ==========================================================================
# STEP 4 — Profile HTML embedded JSON
# ==========================================================================
def step4_profile_html():
    banner("STEP 4  -  profile HTML, embedded JSON (_sharedData / timeline)")
    url = f"https://www.instagram.com/{USERNAME}/"
    try:
        r = requests.get(url, headers=MOBILE_HEADERS, timeout=TIMEOUT)
    except Exception as exc:
        print(f"   REQUEST FAILED: {exc}")
        return record("4_profile_html", error=str(exc))
    show(r)

    out = {"status": r.status_code}

    # og:description usually carries the public follower/post counts.
    m = re.search(r'<meta property="og:description" content="([^"]+)"', r.text)
    if m:
        out["og_description"] = m.group(1)
        print(f"   og:description = {m.group(1)}")
        cm = re.search(r"([\d.,]+[KMB]?)\s+Followers?,\s+([\d.,]+[KMB]?)\s+"
                       r"Following,\s+([\d.,]+[KMB]?)\s+Posts?", m.group(1), re.I)
        if cm:
            RESULTS["profile"].update({
                "followers_text": cm.group(1),
                "following_text": cm.group(2),
                "posts_text": cm.group(3),
            })
            print(f"   parsed counts -> followers={cm.group(1)} "
                  f"following={cm.group(2)} posts={cm.group(3)}")

    shared = re.findall(r'window\._sharedData\s*=\s*({.*?});</script>', r.text, re.DOTALL)
    out["_sharedData_found"] = bool(shared)
    print(f"   window._sharedData present: {bool(shared)}")

    timeline = re.findall(r'"edge_owner_to_timeline_media":\s*({.*?})\s*,\s*'
                          r'"edge_saved_media"', r.text, re.DOTALL)
    out["timeline_media_found"] = bool(timeline)
    print(f"   edge_owner_to_timeline_media present: {bool(timeline)}")

    posts = harvest_posts_from_text(r.text, "step4_profile_html")
    out["posts_found"] = len(posts)
    out["posts_added"] = add_posts(posts)
    print(f"   posts/reels harvested from profile HTML: {len(posts)}")
    return record("4_profile_html", **out)


# ==========================================================================
# STEP 5 — web_profile_info API + graphql/query
# ==========================================================================
def step5_api():
    banner("STEP 5  -  web_profile_info API + graphql/query")
    import time
    api_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "x-ig-app-id": "936619743392459",
        "Accept": "*/*",
        "Referer": f"https://www.instagram.com/{USERNAME}/",
        "X-Requested-With": "XMLHttpRequest",
    }
    out = {}

    # Warm up a session so Instagram sets csrftoken / mid cookies first.
    session = requests.Session()
    try:
        session.get("https://www.instagram.com/", headers=DESKTOP_HEADERS,
                     timeout=TIMEOUT)
        print(f"   session cookies after warm-up: {list(session.cookies.keys())}")
    except Exception as exc:
        print(f"   warm-up request failed: {exc}")

    # 5a) web_profile_info — also yields the profile id + first media page.
    url = (f"https://www.instagram.com/api/v1/users/web_profile_info/"
           f"?username={USERNAME}")
    profile_id = None
    try:
        r = session.get(url, headers=api_headers, timeout=TIMEOUT)
        # one polite retry if rate-limited
        if r.status_code == 429:
            print("   429 -> waiting 8s and retrying once")
            time.sleep(8)
            r = session.get(url, headers=api_headers, timeout=TIMEOUT)
        show(r, n=2000)
        out["web_profile_info_status"] = r.status_code
        profile_id = None
        try:
            data = r.json()
            user = data.get("data", {}).get("user") or {}
            if user:
                profile_id = user.get("id")
                RESULTS["profile"].update({
                    "id": user.get("id"),
                    "full_name": user.get("full_name"),
                    "biography": user.get("biography"),
                    "followers": user.get("edge_followed_by", {}).get("count"),
                    "posts": user.get("edge_owner_to_timeline_media", {}).get("count"),
                })
                print(f"   profile id   = {profile_id}")
                edges = user.get("edge_owner_to_timeline_media", {}).get("edges", [])
                harvested = [_node_to_post(e.get("node", {}), "step5_web_profile_info")
                             for e in edges]
                out["posts_from_web_profile_info"] = len(harvested)
                out["posts_added"] = add_posts(harvested)
                print(f"   media edges in response: {len(edges)}")
        except ValueError:
            print("   (response was not JSON)")
    except Exception as exc:
        print(f"   web_profile_info FAILED: {exc}")
        out["web_profile_info_error"] = str(exc)
        profile_id = None

    # 5b) graphql/query for the timeline media page.
    pid = RESULTS["profile"].get("id") or profile_id
    if pid:
        gql = "https://www.instagram.com/graphql/query/"
        params = {
            "query_hash": "8c2a529969ee035a5063f2fc8602a0fd",
            "variables": json.dumps({"id": str(pid), "first": 12}),
        }
        try:
            r = requests.get(gql, headers=api_headers, params=params, timeout=TIMEOUT)
            show(r, n=2000)
            out["graphql_status"] = r.status_code
            try:
                data = r.json()
                media = (data.get("data", {})
                         .get("user", {})
                         .get("edge_owner_to_timeline_media", {}))
                edges = media.get("edges", [])
                harvested = [_node_to_post(e.get("node", {}), "step5_graphql")
                             for e in edges]
                out["posts_from_graphql"] = len(harvested)
                out["graphql_posts_added"] = add_posts(harvested)
                print(f"   graphql media edges: {len(edges)}")
            except ValueError:
                print("   (graphql response was not JSON)")
        except Exception as exc:
            print(f"   graphql/query FAILED: {exc}")
            out["graphql_error"] = str(exc)
    else:
        print("   no profile id available -> skipping graphql/query")
        out["graphql_skipped"] = "no profile id"
    return record("5_api", **out)


# ==========================================================================
# STEP 6 — /feed/ RSS / Atom
# ==========================================================================
def step6_feed():
    banner("STEP 6  -  /feed/ RSS / Atom")
    url = f"https://www.instagram.com/{USERNAME}/feed/"
    try:
        r = requests.get(url, headers=DESKTOP_HEADERS, timeout=TIMEOUT)
    except Exception as exc:
        print(f"   REQUEST FAILED: {exc}")
        return record("6_feed", error=str(exc))
    show(r)
    ct = r.headers.get("Content-Type", "")
    is_feed = "xml" in ct.lower() or r.text.lstrip()[:5].lower() in ("<?xml", "<rss ", "<feed")
    print(f"   looks like an RSS/Atom feed: {is_feed}")
    return record("6_feed", status=r.status_code, content_type=ct, is_feed=is_feed)


# ==========================================================================
# STEP 7 — Google webcache
# ==========================================================================
def step7_webcache():
    banner("STEP 7  -  Google webcache")
    url = ("https://webcache.googleusercontent.com/search?q=cache:"
           f"instagram.com/{USERNAME}")
    try:
        r = requests.get(url, headers=DESKTOP_HEADERS, timeout=TIMEOUT)
    except Exception as exc:
        print(f"   REQUEST FAILED: {exc}")
        return record("7_webcache", error=str(exc))
    show(r)
    out = {"status": r.status_code}
    if r.status_code == 200:
        posts = harvest_posts_from_text(r.text, "step7_webcache")
        out["posts_found"] = len(posts)
        out["posts_added"] = add_posts(posts)
    return record("7_webcache", **out)


# ==========================================================================
def main():
    for step in (step1_reels_html, step2_picuki, step3_instaloader,
                 step4_profile_html, step5_api, step6_feed, step7_webcache):
        try:
            step()
        except Exception:
            print("   UNEXPECTED ERROR:")
            traceback.print_exc()

    banner("AGGREGATED RESULTS")
    RESULTS["total_posts_recovered"] = len(RESULTS["posts"])
    print(json.dumps(RESULTS, indent=2, default=str))

    out_path = "/home/user/output/instagram_casey_reels_results.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(RESULTS, fh, indent=2, default=str)
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    main()
