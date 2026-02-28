import json
import random
import re
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

app = FastAPI(title="FB Ads Library HTML Fetcher")

BASE_URL = "https://www.facebook.com/ads/library/"
SEE_AD_DETAILS_TEXT = "See ad details"
SCRIPT_JSON_PATTERN = re.compile(
    r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def build_ads_library_url(country: str, keyword: str) -> str:
    params = {
        "active_status": "active",
        "ad_type": "all",
        "country": country.upper(),
        "is_targeted_country": "false",
        "media_type": "all",
        "q": f'"{keyword}"',
        "search_type": "keyword_exact_phrase",
        "sort_data[direction]": "desc",
        "sort_data[mode]": "total_impressions",
    }
    return f"{BASE_URL}?{urlencode(params)}"


async def count_see_ad_details(page: object) -> int:
    locator = page.locator(f"text={SEE_AD_DETAILS_TEXT}")
    return await locator.count()


async def scroll_until_target_ads(page: object, target_ads: int, max_rounds: int = 120) -> None:
    target = max(1, target_ads)
    stagnant_rounds = 0
    previous_count = -1
    previous_height = -1

    for idx in range(max_rounds):
        current_count = await count_see_ad_details(page)
        if current_count >= target:
            break

        viewport = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
        width = int(viewport.get("w", 1280)) if isinstance(viewport, dict) else 1280
        height = int(viewport.get("h", 720)) if isinstance(viewport, dict) else 720

        x = random.randint(max(1, int(width * 0.2)), max(2, int(width * 0.8)))
        y = random.randint(max(1, int(height * 0.25)), max(2, int(height * 0.85)))
        await page.mouse.move(x, y, steps=random.randint(6, 18))

        scroll_distance = int(height * random.uniform(0.65, 1.05))
        await page.mouse.wheel(0, scroll_distance)
        await page.wait_for_timeout(random.randint(700, 1400))
        if idx % 5 == 4:
            await page.wait_for_timeout(random.randint(1200, 2200))

        current_height = await page.evaluate("() => document.body.scrollHeight")
        at_bottom = await page.evaluate(
            "() => window.scrollY + window.innerHeight >= document.body.scrollHeight - 24"
        )

        if current_count == previous_count and current_height == previous_height and at_bottom:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0

        if stagnant_rounds >= 3:
            break

        previous_count = current_count
        previous_height = current_height

    await page.wait_for_timeout(random.randint(800, 1500))


async def fetch_ads_html(url: str, target_ads: int | None = None) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await page.wait_for_timeout(3_000)
            if isinstance(target_ads, int) and target_ads > 0:
                await scroll_until_target_ads(page, target_ads=target_ads)
            return await page.content()
        finally:
            await browser.close()


def extract_json_script_payloads(html: str) -> list[dict | list]:
    payloads: list[dict | list] = []
    for match in SCRIPT_JSON_PATTERN.finditer(html):
        script_text = match.group(1).strip()
        if not script_text or script_text[0] not in "{[":
            continue
        try:
            payloads.append(json.loads(script_text))
        except json.JSONDecodeError:
            continue
    return payloads


def collect_collated_results(node: object, sink: list[dict]) -> None:
    if isinstance(node, dict):
        collated = node.get("collated_results")
        if isinstance(collated, list):
            for item in collated:
                if isinstance(item, dict):
                    sink.append(item)
        for value in node.values():
            collect_collated_results(value, sink)
        return

    if isinstance(node, list):
        for value in node:
            collect_collated_results(value, sink)


def to_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text_value = value.get("text")
        if isinstance(text_value, str):
            return text_value
    return None


def to_utc_iso(value: object) -> str | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return None


def first_str_value(obj: object, keys: list[str]) -> str | None:
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def looks_like_avatar_url(url: str) -> bool:
    lower = url.lower()
    avatar_markers = [
        "s60x60",
        "s72x72",
        "s80x80",
        "s100x100",
        "dst-jpg_s60x60",
        "dst-jpg_s72x72",
        "dst-jpg_s80x80",
        "dst-jpg_s100x100",
        "t1.30497-1",
        "profile",
    ]
    return any(marker in lower for marker in avatar_markers)


def media_area_score_from_url(url: str) -> int:
    patterns = [
        r"dst-[a-z0-9]+_s(\d{2,4})x(\d{2,4})",
        r"_s(\d{2,4})x(\d{2,4})",
        r"stp=c\d+\.\d+\.(\d{2,4})\.(\d{2,4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url, flags=re.IGNORECASE)
        if match:
            try:
                width = int(match.group(1))
                height = int(match.group(2))
                return width * height
            except ValueError:
                continue
    return 0


def extract_json_image_urls(snapshot: dict, first_card: dict) -> tuple[str | None, str | None]:
    candidates: list[str] = []

    videos = snapshot.get("videos")
    if isinstance(videos, list):
        for video in videos:
            if not isinstance(video, dict):
                continue
            for key in ["video_preview_image_url", "thumbnail_url", "preview_image_url", "resized_image_url"]:
                value = video.get(key)
                if isinstance(value, str) and value:
                    candidates.append(value)

    images = snapshot.get("images")
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict):
                continue
            for key in [
                "resized_image_url",
                "watermarked_resized_image_url",
                "original_image_url",
                "image_url",
                "thumbnail_url",
            ]:
                value = image.get(key)
                if isinstance(value, str) and value:
                    candidates.append(value)

    card_images = first_card.get("images")
    if isinstance(card_images, list):
        for image in card_images:
            if not isinstance(image, dict):
                continue
            for key in [
                "resized_image_url",
                "watermarked_resized_image_url",
                "original_image_url",
                "image_url",
                "thumbnail_url",
            ]:
                value = image.get(key)
                if isinstance(value, str) and value:
                    candidates.append(value)

    for key in [
        "video_preview_image_url",
        "thumbnail_url",
        "preview_image_url",
        "resized_image_url",
        "original_image_url",
        "image_url",
        "link_image_url",
    ]:
        value = first_card.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)

    for key in [
        "video_preview_image_url",
        "thumbnail_url",
        "preview_image_url",
        "resized_image_url",
        "original_image_url",
        "image_url",
        "link_image_url",
    ]:
        value = snapshot.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)

    deduped: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)

    non_avatar = [url for url in deduped if not looks_like_avatar_url(url)]
    if not non_avatar:
        return None, None

    best = max(non_avatar, key=media_area_score_from_url)
    return best, best


def normalize_ad(ad: dict) -> dict:
    snapshot = ad.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}

    cards = snapshot.get("cards")
    cards_list = cards if isinstance(cards, list) else []
    first_card = cards_list[0] if cards_list and isinstance(cards_list[0], dict) else {}

    images = snapshot.get("images")
    videos = snapshot.get("videos")

    page_name = ad.get("page_name")
    if not isinstance(page_name, str):
        page_name = snapshot.get("page_name")

    body_text = to_text(snapshot.get("body")) or to_text(first_card.get("body"))
    link_url = snapshot.get("link_url")
    if not isinstance(link_url, str):
        link_url = first_card.get("link_url")
        if not isinstance(link_url, str):
            link_url = None

    platforms = ad.get("publisher_platform")
    publisher_platforms = platforms if isinstance(platforms, list) else []
    image_url, thumbnail_url = extract_json_image_urls(snapshot, first_card)

    return {
        "ad_archive_id": ad.get("ad_archive_id"),
        "ad_id": ad.get("ad_id"),
        "page_name": page_name,
        "is_active": ad.get("is_active"),
        "start_date_unix": ad.get("start_date"),
        "start_date_utc": to_utc_iso(ad.get("start_date")),
        "end_date_unix": ad.get("end_date"),
        "end_date_utc": to_utc_iso(ad.get("end_date")),
        "publisher_platforms": publisher_platforms,
        "cta_text": snapshot.get("cta_text"),
        "cta_type": snapshot.get("cta_type"),
        "title": to_text(snapshot.get("title")),
        "byline": to_text(snapshot.get("byline")),
        "body_text": body_text,
        "link_url": link_url,
        "image_url": image_url,
        "thumbnail_url": thumbnail_url,
        "cards_count": len(cards_list),
        "images_count": len(images) if isinstance(images, list) else 0,
        "videos_count": len(videos) if isinstance(videos, list) else 0,
    }


def extract_ads_from_html(html: str) -> list[dict]:
    raw_ads: list[dict] = []
    for payload in extract_json_script_payloads(html):
        collect_collated_results(payload, raw_ads)

    deduped_ads: list[dict] = []
    seen_ids: set[str] = set()

    for ad in raw_ads:
        key_value = ad.get("ad_archive_id") or ad.get("ad_id") or ad.get("collation_id")
        key = str(key_value) if key_value is not None else None

        if key is not None:
            if key in seen_ids:
                continue
            seen_ids.add(key)

        deduped_ads.append(normalize_ad(ad))

    return deduped_ads


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def contains_class_token(class_attr: object, token: str) -> bool:
    if isinstance(class_attr, list):
        return token in class_attr
    if isinstance(class_attr, str):
        return token in class_attr.split()
    return False


def find_card_container_from_see_node(see_node: object) -> object | None:
    current = getattr(see_node, "parent", None)
    while current is not None and hasattr(current, "get_text"):
        text = normalize_spaces(current.get_text(" ", strip=True))
        classes = current.get("class", [])
        if (
            "Library ID:" in text
            and "Started running on" in text
            and contains_class_token(classes, "x1plvlek")
        ):
            return current
        current = getattr(current, "parent", None)
    return None


def extract_library_id(card: object) -> str | None:
    for value in card.stripped_strings:
        text = normalize_spaces(str(value))
        match = re.search(r"Library ID:\s*([0-9]+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract_started_on(card: object) -> str | None:
    for value in card.stripped_strings:
        text = normalize_spaces(str(value))
        match = re.search(r"Started running on\s+(.+)$", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract_status(card: object) -> str | None:
    values = [normalize_spaces(str(v)) for v in card.stripped_strings]
    library_idx = None
    for idx, text in enumerate(values):
        if "Library ID:" in text:
            library_idx = idx
            break

    if library_idx is None:
        library_idx = len(values)

    for text in values[:library_idx]:
        lower = text.lower()
        if lower == "active":
            return "Active"
        if lower == "inactive":
            return "Inactive"
    return None


def extract_platform_icons_count(card: object) -> int:
    platforms_label = card.find(string=lambda s: isinstance(s, str) and normalize_spaces(s) == "Platforms")
    if platforms_label is None:
        return 0

    parent = platforms_label.parent
    if parent is None or not hasattr(parent, "find_all"):
        return 0

    icon_count = 0
    for node in parent.find_all("div"):
        style = node.get("style", "")
        if isinstance(style, str) and "mask-image:" in style:
            icon_count += 1
    return icon_count


def extract_page_name_from_card(card: object) -> str | None:
    sponsored = card.find(string=lambda s: isinstance(s, str) and normalize_spaces(s).lower() == "sponsored")
    if sponsored is None:
        return None

    for anchor in sponsored.find_all_previous("a"):
        if card not in anchor.parents:
            continue
        href = anchor.get("href", "")
        text = normalize_spaces(anchor.get_text(" ", strip=True))
        if not text:
            continue
        if "facebook.com" in href and "l.facebook.com/l.php" not in href and "/ads/library" not in href:
            return text

    return None


def looks_like_noise_text(text: str) -> bool:
    if not text:
        return True

    lower = text.lower()
    if lower in {
        "active",
        "inactive",
        "platforms",
        "see ad details",
        "open drop-down",
        "sponsored",
        "this ad has multiple versions",
    }:
        return True

    if text.startswith("Library ID:") or text.startswith("Started running on "):
        return True

    if len(text) < 18:
        return True

    if lower.startswith("http://") or lower.startswith("https://"):
        return True

    if text.isupper() and len(text) < 50:
        return True

    return False


def extract_body_text_from_card(card: object) -> str | None:
    values = [normalize_spaces(str(v)) for v in card.stripped_strings]
    sponsored_idx = 0
    for idx, text in enumerate(values):
        if text.lower() == "sponsored":
            sponsored_idx = idx + 1
            break

    for text in values[sponsored_idx:]:
        if looks_like_noise_text(text):
            continue
        return text

    for text in values:
        if looks_like_noise_text(text):
            continue
        return text

    return None


def extract_cta_from_card(card: object) -> str | None:
    allowed = {
        "shop now",
        "learn more",
        "sign up",
        "book now",
        "download",
        "apply now",
        "watch more",
        "get quote",
        "contact us",
        "subscribe",
        "play game",
        "order now",
        "listen now",
        "get offer",
        "donate now",
        "send message",
    }
    for value in card.stripped_strings:
        text = normalize_spaces(str(value))
        if text.lower() in allowed:
            return text
    return None


def unwrap_facebook_redirect_url(url: str) -> str:
    if not url:
        return url
    if "l.facebook.com/l.php" not in url:
        return url

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    target = params.get("u", [None])[0]
    if isinstance(target, str) and target:
        return unquote(target)
    return url


def extract_link_url_from_card(card: object) -> str | None:
    for anchor in card.find_all("a", href=True):
        href = anchor.get("href", "")
        if not href:
            continue
        if href.startswith("https://l.facebook.com/l.php"):
            return unwrap_facebook_redirect_url(href)
        if href.startswith("http") and "facebook.com/ads/library" not in href:
            return href
    return None


def extract_image_urls_from_card(card: object) -> tuple[str | None, str | None]:
    sources: list[str] = []
    seen: set[str] = set()

    for img in card.find_all("img", src=True):
        src = img.get("src", "")
        if not isinstance(src, str) or not src.startswith("http"):
            continue
        if src in seen:
            continue
        seen.add(src)
        sources.append(src)

    for video in card.find_all("video"):
        poster = video.get("poster", "")
        if not isinstance(poster, str) or not poster.startswith("http"):
            continue
        if poster in seen:
            continue
        seen.add(poster)
        sources.append(poster)

    for node in card.find_all(style=True):
        style_value = node.get("style", "")
        if not isinstance(style_value, str):
            continue
        for match in re.finditer(r"url\((?:\"|')?(https?://[^)\"']+)(?:\"|')?\)", style_value):
            src = match.group(1)
            if src in seen:
                continue
            seen.add(src)
            sources.append(src)

    if not sources:
        return None, None

    non_avatar = [src for src in sources if not looks_like_avatar_url(src)]
    if not non_avatar:
        return None, None

    best = max(non_avatar, key=media_area_score_from_url)
    return best, best


def extract_ads_from_dom(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    see_nodes = soup.find_all(
        string=lambda s: isinstance(s, str) and normalize_spaces(s).lower() == SEE_AD_DETAILS_TEXT.lower()
    )

    records: list[dict] = []
    seen_keys: set[str] = set()

    for see_node in see_nodes:
        card = find_card_container_from_see_node(see_node)
        if card is None:
            continue

        library_id = extract_library_id(card)
        key = library_id if library_id is not None else str(id(card))
        if key in seen_keys:
            continue
        seen_keys.add(key)

        image_url, thumbnail_url = extract_image_urls_from_card(card)
        record = {
            "library_id": library_id,
            "status": extract_status(card),
            "started_running_on": extract_started_on(card),
            "platform_icons_count": extract_platform_icons_count(card),
            "page_name": extract_page_name_from_card(card),
            "body_text": extract_body_text_from_card(card),
            "cta_text": extract_cta_from_card(card),
            "link_url": extract_link_url_from_card(card),
            "image_url": image_url,
            "thumbnail_url": thumbnail_url,
        }
        records.append(record)

    return records


@app.get("/ads-library-html", response_class=Response)
async def fetch_ads_library_html(
    country: str = Query(default="US", min_length=2, max_length=2),
    keyword: str = Query(default="dogs", min_length=1),
    limit: int = Query(default=20, ge=1, le=500),
) -> Response:
    url = build_ads_library_url(country=country, keyword=keyword)

    try:
        html = await fetch_ads_html(url, target_ads=limit)
        return Response(content=html, media_type="text/html")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch page HTML: {exc}") from exc


@app.get("/ads-library-data", response_class=JSONResponse)
async def fetch_ads_library_data(
    country: str = Query(default="US", min_length=2, max_length=2),
    keyword: str = Query(default="dogs", min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    url = build_ads_library_url(country=country, keyword=keyword)

    try:
        html = await fetch_ads_html(url, target_ads=limit)
        ads = extract_ads_from_html(html)
        return {
            "url": url,
            "total_ads": len(ads),
            "returned_ads": min(limit, len(ads)),
            "ads": ads[:limit],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to extract ads data: {exc}") from exc


@app.get("/ads-library-data-dom", response_class=JSONResponse)
async def fetch_ads_library_data_dom(
    country: str = Query(default="US", min_length=2, max_length=2),
    keyword: str = Query(default="dogs", min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    url = build_ads_library_url(country=country, keyword=keyword)

    try:
        html = await fetch_ads_html(url, target_ads=limit)
        ads = extract_ads_from_dom(html)
        return {
            "url": url,
            "strategy": "dom-see-ad-details-backward",
            "total_ads": len(ads),
            "returned_ads": min(limit, len(ads)),
            "ads": ads[:limit],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to extract ads data from DOM: {exc}") from exc


@app.get("/", response_class=JSONResponse)
def root() -> dict[str, str]:
    return {
        "message": (
            "Use /ads-library-html?country=US&keyword=dogs&limit=20 for raw HTML "
            "or /ads-library-data?country=US&keyword=dogs&limit=20 for JSON payload parsing "
            "or /ads-library-data-dom?country=US&keyword=dogs&limit=20 for DOM backward parsing."
        )
    }
