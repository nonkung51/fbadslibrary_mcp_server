import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    from fastmcp import FastMCP  # type: ignore

BASE_URL = "https://www.facebook.com/ads/library/"
DEFAULT_COUNTRY = "US"
SEE_AD_DETAILS_TEXT = "See ad details"
LOW_IMPRESSION_TEXT = "low impression count"
LOW_IMPRESSION_VALUE_SET = {"<100", "< 100"}
LIBRARY_ID_PATTERN = re.compile(r"Library ID:\s*([0-9]+)", re.IGNORECASE)
SCRIPT_JSON_PATTERN = re.compile(
    r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


mcp = FastMCP("fb-ads-library-scraper")


def build_ads_library_url(keyword: str, country: str = DEFAULT_COUNTRY) -> str:
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


async def count_see_ad_details(page: Any) -> int:
    locator = page.locator(f"text={SEE_AD_DETAILS_TEXT}")
    return await locator.count()


async def scroll_until_target_ads(page: Any, target_ads: int, max_rounds: int = 120) -> None:
    target = max(1, target_ads)
    stagnant_rounds = 0
    previous_count = -1

    for _ in range(max_rounds):
        current_count = await count_see_ad_details(page)
        if current_count >= target:
            break

        await page.mouse.wheel(0, 1300)
        await page.wait_for_timeout(900)

        if current_count == previous_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0

        if stagnant_rounds >= 6:
            break

        previous_count = current_count

    await page.wait_for_timeout(1000)


async def fetch_ads_html(url: str, target_ads: int | None = None) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            await page.wait_for_load_state("networkidle", timeout=30_000)
            await page.wait_for_timeout(3000)

            if isinstance(target_ads, int) and target_ads > 0:
                await scroll_until_target_ads(page, target_ads=target_ads)

            return await page.content()
        finally:
            await browser.close()


def extract_json_script_payloads(html: str) -> list[dict[str, Any] | list[Any]]:
    payloads: list[dict[str, Any] | list[Any]] = []

    for match in SCRIPT_JSON_PATTERN.finditer(html):
        script_text = match.group(1).strip()
        if not script_text or script_text[0] not in "[{":
            continue

        try:
            payloads.append(json.loads(script_text))
        except json.JSONDecodeError:
            continue

    return payloads


def collect_collated_results(node: Any, sink: list[dict[str, Any]]) -> None:
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


def to_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        text_value = value.get("text")
        if isinstance(text_value, str):
            return text_value

    return None


def to_utc_iso(value: Any) -> str | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
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


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def contains_class_token(class_attr: Any, token: str) -> bool:
    if isinstance(class_attr, list):
        return token in class_attr
    if isinstance(class_attr, str):
        return token in class_attr.split()
    return False


def text_has_low_impression(text: str) -> bool:
    normalized = normalize_spaces(text).lower()
    return LOW_IMPRESSION_TEXT in normalized


def has_low_impression_span(node: Any) -> bool:
    spans = node.find_all("span")
    for span in spans:
        span_text = span.get_text(" ", strip=True)
        if text_has_low_impression(span_text):
            return True
    return False


def extract_ad_lookup_keys(ad: dict[str, Any]) -> set[str]:
    keys: set[str] = set()

    for field in ("ad_archive_id", "ad_id", "collation_id", "id"):
        value = ad.get(field)
        if value is not None:
            keys.add(str(value))

    snapshot = ad.get("snapshot")
    if isinstance(snapshot, dict):
        for field in ("ad_library_id", "ad_id", "id"):
            value = snapshot.get(field)
            if value is not None:
                keys.add(str(value))

    return keys


def extract_low_impression_flags_from_dom(html: str) -> tuple[dict[str, bool], list[bool]]:
    soup = BeautifulSoup(html, "html.parser")
    flags: dict[str, bool] = {}
    ordered_low_flags: list[bool] = []

    # Primary strategy: anchor from the explicit low-impression span.
    low_spans = soup.find_all("span", string=lambda s: isinstance(s, str) and text_has_low_impression(s))
    for span in low_spans:
        current = span.parent
        while current is not None and hasattr(current, "get_text"):
            current_text = normalize_spaces(current.get_text(" ", strip=True))
            if "Library ID:" in current_text and SEE_AD_DETAILS_TEXT in current_text:
                library_match = LIBRARY_ID_PATTERN.search(current_text)
                if library_match is not None:
                    library_id = library_match.group(1)
                    flags[library_id] = True
                    ordered_low_flags.append(True)
                break
            current = current.parent

    # Secondary strategy: collect cards in DOM order for positional fallback.
    for text_node in soup.find_all(string=lambda s: isinstance(s, str) and "Library ID:" in s):
        current = text_node.parent
        while current is not None and hasattr(current, "get_text"):
            current_text = normalize_spaces(current.get_text(" ", strip=True))
            if "Library ID:" in current_text and SEE_AD_DETAILS_TEXT in current_text:
                library_match = LIBRARY_ID_PATTERN.search(current_text)
                if library_match is not None:
                    library_id = library_match.group(1)
                    low_value = has_low_impression_span(current)
                    if low_value:
                        flags[library_id] = True
                    ordered_low_flags.append(low_value)
                break
            current = current.parent

    return flags, ordered_low_flags


def detect_low_impression_from_payload(ad: dict[str, Any]) -> bool:
    impression_data = ad.get("impressions_with_index")
    if isinstance(impression_data, dict):
        impressions_text = impression_data.get("impressions_text")
        if isinstance(impressions_text, str):
            normalized_text = impressions_text.strip().lower()
            if normalized_text == LOW_IMPRESSION_TEXT:
                return True
            if normalized_text in LOW_IMPRESSION_VALUE_SET:
                return True

        impressions_index = impression_data.get("impressions_index")
        if impressions_index == 0:
            return True

    for key in ("low_impression", "is_low_impression", "has_low_impression"):
        value = ad.get(key)
        if isinstance(value, bool):
            return value

    snapshot = ad.get("snapshot")
    if isinstance(snapshot, dict):
        for key in ("low_impression", "is_low_impression", "has_low_impression"):
            value = snapshot.get(key)
            if isinstance(value, bool):
                return value

    return False


def extract_json_image_urls(snapshot: dict[str, Any], first_card: dict[str, Any]) -> tuple[str | None, str | None]:
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


def normalize_ad(ad: dict[str, Any], low_impression: bool) -> dict[str, Any]:
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
        "low_impression": low_impression,
        "cards_count": len(cards_list),
        "images_count": len(images) if isinstance(images, list) else 0,
        "videos_count": len(videos) if isinstance(videos, list) else 0,
    }


def extract_ads_from_html(html: str) -> list[dict[str, Any]]:
    raw_ads: list[dict[str, Any]] = []
    low_impression_flags, ordered_low_flags = extract_low_impression_flags_from_dom(html)

    for payload in extract_json_script_payloads(html):
        collect_collated_results(payload, raw_ads)

    deduped_ads: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for index, ad in enumerate(raw_ads):
        key_value = ad.get("ad_archive_id") or ad.get("ad_id") or ad.get("collation_id")
        key = str(key_value) if key_value is not None else None

        if key is not None:
            if key in seen_ids:
                continue
            seen_ids.add(key)

        low_impression = detect_low_impression_from_payload(ad)

        lookup_keys = extract_ad_lookup_keys(ad)
        if any(low_impression_flags.get(candidate_key, False) for candidate_key in lookup_keys):
            low_impression = True
        elif index < len(ordered_low_flags) and ordered_low_flags[index]:
            low_impression = True

        deduped_ads.append(normalize_ad(ad, low_impression=low_impression))

    return deduped_ads


def validate_keywords(value: str) -> str:
    keywords = value.strip()
    if not keywords:
        raise ValueError("keywords is required")
    return keywords


def validate_limit(value: int) -> int:
    if value < 1 or value > 200:
        raise ValueError("limit must be between 1 and 200")
    return value


@mcp.tool()
async def search_ads(keywords: str, limit: int = 20) -> dict[str, Any]:
    """Search Facebook Ads Library ads by keywords.

    Args:
        keywords: Search keywords.
        limit: Max ads to return (1-200).
    """
    normalized_keywords = validate_keywords(keywords)
    normalized_limit = validate_limit(limit)

    url = build_ads_library_url(keyword=normalized_keywords, country=DEFAULT_COUNTRY)
    html = await fetch_ads_html(url, target_ads=normalized_limit)
    ads = extract_ads_from_html(html)

    return {
        "query": {
            "keywords": normalized_keywords,
            "limit": normalized_limit,
            "country": DEFAULT_COUNTRY,
            "url": url,
        },
        "total_ads_found": len(ads),
        "returned_ads": min(normalized_limit, len(ads)),
        "ads": ads[:normalized_limit],
    }


# ASGI app for `uvicorn main:app`
app = mcp.streamable_http_app()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
