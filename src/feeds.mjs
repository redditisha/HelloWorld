// Feed fetching + normalization (ported from the app's former src/lib/rss).
import crypto from "node:crypto";
import Parser from "rss-parser";

// Plain browser headers. A custom UA token or an rss-specific Accept header
// trips some publisher WAFs (e.g. Gadgets 360 403s on `Accept: application/rss+xml`).
const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
  Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9,kn;q=0.8,hi;q=0.7",
};
const FETCH_TIMEOUT_MS = 20000;
const IST_OFFSET_MS = 5.5 * 3600_000;
const FUTURE_TOLERANCE_MS = 10 * 60_000;

const TRACKING_PARAMS = [
  "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
  "cmpid", "CMP", "ncid", "at_medium", "at_campaign", "fbclid", "gclid",
];

const parser = new Parser({
  customFields: {
    item: [
      ["media:content", "media:content"],
      ["media:thumbnail", "media:thumbnail"],
      ["content:encoded", "content:encoded"],
      ["dc:creator", "dc:creator"],
    ],
  },
});

/**
 * Make real-world feed XML parseable by a strict parser: strip a BOM and
 * XML-illegal control characters, and drop the optional channel <image>
 * block, which publishers (e.g. Ee Dina) often emit with an unclosed <link>.
 */
function sanitizeXml(xml) {
  return xml
    .replace(/^﻿/, "")
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "")
    .replace(/<image>[\s\S]*?<\/image>/gi, "");
}

export async function fetchFeed(url) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(url, { headers: BROWSER_HEADERS, redirect: "follow", signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await parser.parseString(sanitizeXml(await res.text()));
  } finally {
    clearTimeout(timer);
  }
}

/** Strip tracking params + fragment, lowercase host, drop trailing slash. */
function normalizeUrl(raw) {
  try {
    const u = new URL(raw.trim());
    u.hash = "";
    u.hostname = u.hostname.toLowerCase();
    TRACKING_PARAMS.forEach((p) => u.searchParams.delete(p));
    const s = u.toString();
    return s.endsWith("/") ? s.slice(0, -1) : s;
  } catch {
    return raw.trim();
  }
}

const sha1 = (s) => crypto.createHash("sha1").update(s).digest("hex");

function stripHtml(s) {
  if (!s) return "";
  return s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim().slice(0, 800);
}

/** Best-effort image extraction across the many RSS/Atom conventions. */
function extractImage(item) {
  if (item.enclosure?.url && /^https?:/i.test(item.enclosure.url)) return item.enclosure.url;
  const media = item["media:content"] || item["media:thumbnail"];
  if (media) {
    const node = Array.isArray(media) ? media[0] : media;
    const url = node?.$?.url || node?.url;
    if (url) return url;
  }
  const html = item["content:encoded"] || item.content;
  const m = html && html.match(/<img[^>]+src=["']([^"']+)["']/i);
  return m ? m[1] : "";
}

/**
 * One feed item -> article, or null if malformed. The id is stable across
 * runs: sha1(source id | dedup key), where the dedup key is the normalized
 * URL, then guid, then a title hash.
 */
export function normalizeItem(item, source) {
  const title = (item.title || "").trim();
  const link = (item.link || item.guid || "").trim();
  if (!title || !link) return null;

  const url = normalizeUrl(link);
  const guid = (item.guid || "").trim();
  const dedupKey = url || guid || `t:${sha1(title)}`;

  const d = new Date(item.isoDate || item.pubDate || "");
  let publishedMs = isNaN(d.getTime()) ? null : d.getTime();
  // Some Indian feeds (e.g. Daijiworld) stamp IST clock times as GMT, which
  // puts articles 5½ hours in the future. Shift those back; if still in the
  // future, use the fetch time.
  const now = Date.now();
  if (publishedMs !== null && publishedMs > now + FUTURE_TOLERANCE_MS) {
    const shifted = publishedMs - IST_OFFSET_MS;
    publishedMs = shifted <= now + FUTURE_TOLERANCE_MS ? shifted : now;
  }
  const author = item.creator || item.author || item["dc:creator"] || "";

  return {
    id: sha1(`${source.id}|${dedupKey}`).slice(0, 16),
    publishedMs,
    source_id: source.id,
    source_name: source.name,
    language: source.language || "en",
    title: title.slice(0, 1000),
    url,
    description: stripHtml(item.contentSnippet || item.summary || item.content),
    author: typeof author === "string" ? author : "",
    image_url: extractImage(item),
    guid,
    category_group: source.category_group || "",
    category: source.primary_category || "",
    topic: source.secondary_category || "",
  };
}
