#!/usr/bin/env python3
"""
Fetch Instagram profile data for Dr. Casey Means.

Handles attempted: @caseymeans and @drcaseyskitchen

The script tries, in order:
  1. www.instagram.com/<user>/?__a=1&__d=dis            (legacy JSON endpoint)
  2. www.instagram.com/api/v1/users/web_profile_info/   (web profile API)
  3. i.instagram.com/api/v1/users/lookup/               (mobile lookup API)
  4. Raw profile HTML  -> JSON-LD, meta description, og:description
  5. Third-party mirrors: picuki.com, imginn.com

Every response is printed. Findings are aggregated and written to
instagram_casey_means_results.json next to this script.

Note: most of Instagram's unauthenticated endpoints now reject anonymous
requests (HTTP 401 / "useragent mismatch" / login redirect). The script
records exactly what each endpoint returns so the outcome is verifiable.
"""

import json
import re
import sys
import time
from html import unescape

import requests
from bs4 import BeautifulSoup

HANDLES = ["caseymeans", "drcaseyskitchen"]

# Browser-ish headers (desktop) for HTML pages.
HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Headers expected by the web_profile_info API. The X-IG-App-ID is the
# public web app id Instagram's own site sends.
API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-IG-App-ID": "936619743392459",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.instagram.com/",
}

TIMEOUT = 25
RESULTS = {h: {} for h in HANDLES}
RESULTS["_meta"] = {"errors": []}


def banner(text):
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def safe_get(url, headers, label):
    """GET a URL, printing status + a body preview. Returns the Response or None."""
    print(f"\n[GET] {label}")
    print(f"      {url}")
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as exc:
        print(f"      REQUEST FAILED: {exc}")
        RESULTS["_meta"]["errors"].append(f"{label}: {exc}")
        return None
    print(f"      status={resp.status_code}  final_url={resp.url}")
    print(f"      content-type={resp.headers.get('Content-Type', '?')}  "
          f"bytes={len(resp.content)}")
    preview = resp.text[:600].replace("\n", " ")
    print(f"      body[:600]= {preview}")
    return resp


def try_parse_json(resp):
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def summarize_user_node(node):
    """Pull the standard profile fields out of an IG user JSON node."""
    if not isinstance(node, dict):
        return {}
    out = {}
    if "full_name" in node:
        out["full_name"] = node.get("full_name")
    if "username" in node:
        out["username"] = node.get("username")
    if "biography" in node:
        out["biography"] = node.get("biography")
    if "is_verified" in node:
        out["is_verified"] = node.get("is_verified")
    fc = node.get("edge_followed_by") or node.get("follower_count")
    if isinstance(fc, dict):
        out["followers"] = fc.get("count")
    elif fc is not None:
        out["followers"] = fc
    fg = node.get("edge_follow") or node.get("following_count")
    if isinstance(fg, dict):
        out["following"] = fg.get("count")
    elif fg is not None:
        out["following"] = fg
    posts = node.get("edge_owner_to_timeline_media") or node.get("media_count")
    if isinstance(posts, dict):
        out["posts"] = posts.get("count")
    elif posts is not None:
        out["posts"] = posts
    if "external_url" in node:
        out["external_url"] = node.get("external_url")
    if "id" in node:
        out["id"] = node.get("id")
    return out


def step_legacy_a1(handle):
    """Step 1/2: legacy ?__a=1&__d=dis endpoint."""
    url = f"https://www.instagram.com/{handle}/?__a=1&__d=dis"
    resp = safe_get(url, HTML_HEADERS, f"legacy __a=1  @{handle}")
    data = try_parse_json(resp)
    if data is None:
        print("      -> no JSON (endpoint deprecated / returns HTML or 302).")
        return
    node = (data.get("graphql", {}).get("user")
            or data.get("data", {}).get("user")
            or data.get("user"))
    summary = summarize_user_node(node)
    if summary:
        print(f"      -> parsed: {json.dumps(summary)}")
        RESULTS[handle].setdefault("legacy_a1", {}).update(summary)
    else:
        print("      -> JSON returned but no recognizable user node.")
        RESULTS[handle]["legacy_a1_raw_keys"] = list(data.keys())


def step_web_profile_info(handle):
    """Step 3/4: /api/v1/users/web_profile_info/."""
    url = ("https://www.instagram.com/api/v1/users/web_profile_info/"
           f"?username={handle}")
    resp = safe_get(url, API_HEADERS, f"web_profile_info  @{handle}")
    data = try_parse_json(resp)
    if data is None:
        print("      -> no JSON (likely 401 / login wall).")
        return
    node = data.get("data", {}).get("user") if isinstance(data, dict) else None
    summary = summarize_user_node(node)
    if summary:
        print(f"      -> parsed: {json.dumps(summary)}")
        RESULTS[handle].setdefault("web_profile_info", {}).update(summary)
    else:
        print(f"      -> JSON returned, no user node. keys={list(data.keys())}")


def step_mobile_lookup(handle):
    """Step 5: i.instagram.com mobile lookup endpoint."""
    url = f"https://i.instagram.com/api/v1/users/lookup/?q={handle}"
    headers = dict(API_HEADERS)
    headers["User-Agent"] = "Instagram 155.0.0.37.107 Android"
    resp = safe_get(url, headers, f"mobile lookup  @{handle}")
    data = try_parse_json(resp)
    if data is not None:
        print(f"      -> JSON: {json.dumps(data)[:400]}")
        RESULTS[handle]["mobile_lookup"] = data
    else:
        print("      -> no JSON (this endpoint normally needs a signed POST).")


def extract_from_html(html, handle):
    """Step 6/7: pull JSON-LD, meta description, og:description from raw HTML."""
    soup = BeautifulSoup(html, "lxml")
    found = {}

    # JSON-LD structured data
    ld_blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            ld_blocks.append(json.loads(tag.string or "{}"))
        except (ValueError, TypeError):
            continue
    if ld_blocks:
        print(f"      JSON-LD blocks found: {len(ld_blocks)}")
        for blk in ld_blocks:
            print(f"        {json.dumps(blk)[:500]}")
        found["json_ld"] = ld_blocks

    # meta description + og:description
    for sel in [("name", "description"), ("property", "og:description"),
                ("property", "og:title"), ("property", "og:image")]:
        tag = soup.find("meta", attrs={sel[0]: sel[1]})
        if tag and tag.get("content"):
            content = unescape(tag["content"])
            print(f"      <meta {sel[0]}='{sel[1]}'> = {content}")
            found[sel[1]] = content

    # Follower/post counts are usually inside og:description, e.g.
    # "12.3M Followers, 345 Following, 1,234 Posts - Casey Means ..."
    desc = found.get("og:description") or found.get("description") or ""
    m = re.search(
        r"([\d.,]+[KMB]?)\s+Followers?,\s+([\d.,]+[KMB]?)\s+Following,"
        r"\s+([\d.,]+[KMB]?)\s+Posts?",
        desc, re.IGNORECASE)
    if m:
        found["followers_text"] = m.group(1)
        found["following_text"] = m.group(2)
        found["posts_text"] = m.group(3)
        print(f"      -> parsed counts from meta: followers={m.group(1)} "
              f"following={m.group(2)} posts={m.group(3)}")

    # Inline shared-data / profile JSON sometimes still embedded
    m2 = re.search(r'"edge_followed_by":\{"count":(\d+)\}', html)
    if m2:
        found["followers_inline"] = int(m2.group(1))
        print(f"      -> inline edge_followed_by count={m2.group(1)}")
    m3 = re.search(r'"biography":"(.*?)","', html)
    if m3:
        found["biography_inline"] = m3.group(1)

    return found


def step_raw_html(handle):
    """Step 6/7: fetch raw profile HTML."""
    url = f"https://www.instagram.com/{handle}/"
    resp = safe_get(url, HTML_HEADERS, f"raw profile HTML  @{handle}")
    if resp is None or not resp.text:
        return
    found = extract_from_html(resp.text, handle)
    if found:
        RESULTS[handle]["raw_html"] = found
    else:
        print("      -> no structured data / meta tags recovered "
              "(likely a login-walled HTML shell).")


# ----- third-party mirrors -------------------------------------------------

def extract_picuki(html):
    soup = BeautifulSoup(html, "lxml")
    out = {}
    # Picuki renders counts in <div class="profile-info"> with labels.
    for box in soup.select(".profile-info .stats span, .profile-statistics li"):
        txt = box.get_text(" ", strip=True)
        if txt:
            out.setdefault("stat_lines", []).append(txt)
    # Generic "Followers" number search
    m = re.search(r"([\d.,]+[KMB]?)\s*Followers", html, re.IGNORECASE)
    if m:
        out["followers_text"] = m.group(1)
    posts = []
    for art in soup.select(".box-photo, .photo, article")[:12]:
        link = art.find("a", href=True)
        cap = art.select_one(".photo-description, .description, figcaption")
        date = art.select_one(".time, time, .photo-time")
        if link or cap:
            posts.append({
                "url": link["href"] if link else None,
                "caption": cap.get_text(" ", strip=True) if cap else None,
                "date": date.get_text(" ", strip=True) if date else None,
            })
    if posts:
        out["recent_posts"] = posts
    return out


def extract_imginn(html):
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for box in soup.select(".userinfo, .profile, .info"):
        txt = box.get_text(" ", strip=True)
        if txt:
            out.setdefault("info_text", []).append(txt[:300])
    m = re.search(r"([\d.,]+[KMB]?)\s*Followers", html, re.IGNORECASE)
    if m:
        out["followers_text"] = m.group(1)
    posts = []
    for art in soup.select(".item, .post, article")[:12]:
        link = art.find("a", href=True)
        cap = art.select_one(".desc, .caption, figcaption, p")
        time_tag = art.find("time")
        if link or cap:
            posts.append({
                "url": link["href"] if link else None,
                "caption": cap.get_text(" ", strip=True) if cap else None,
                "date": (time_tag.get("datetime") or time_tag.get_text(strip=True))
                        if time_tag else None,
            })
    if posts:
        out["recent_posts"] = posts
    return out


def step_third_party(handle):
    mirrors = [
        (f"https://www.picuki.com/profile/{handle}", "picuki", extract_picuki),
        (f"https://imginn.com/{handle}/", "imginn", extract_imginn),
    ]
    for url, name, extractor in mirrors:
        resp = safe_get(url, HTML_HEADERS, f"{name} mirror  @{handle}")
        if resp is None or resp.status_code >= 400 or not resp.text:
            print(f"      -> {name}: no usable HTML (status "
                  f"{resp.status_code if resp else 'n/a'}).")
            continue
        try:
            found = extractor(resp.text)
        except Exception as exc:  # noqa: BLE001 - mirror markup is unstable
            print(f"      -> {name}: parse error: {exc}")
            continue
        if found:
            print(f"      -> {name} extracted: {json.dumps(found)[:600]}")
            RESULTS[handle].setdefault(name, {}).update(found)
        else:
            print(f"      -> {name}: page loaded but no profile data matched.")


def main():
    for handle in HANDLES:
        banner(f"INSTAGRAM HANDLE: @{handle}")
        step_legacy_a1(handle)
        step_web_profile_info(handle)
        step_mobile_lookup(handle)
        step_raw_html(handle)
        step_third_party(handle)
        time.sleep(1)  # be polite between handles

    banner("AGGREGATED FINDINGS")
    print(json.dumps(RESULTS, indent=2, default=str))

    out_path = "/home/user/output/instagram_casey_means_results.json"
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(RESULTS, fh, indent=2, default=str)
        print(f"\nResults written to {out_path}")
    except OSError as exc:
        print(f"\nCould not write results file: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
