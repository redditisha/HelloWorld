// Sheet layout contract. local/sync.py mirrors these column orders — change
// both together.
//
//   _sources         written by the PC (local sync), read by the collector
//   _health          written by the collector, read by the PC
//   YYYY-MM-DD tabs  one per publish date (IST), append-only, written by the
//                    collector; the PC deletes them after archiving (~60 days)

export const TZ = "Asia/Kolkata";

export const SOURCES_TAB = "_sources";
export const SOURCE_COLUMNS = [
  "id", "name", "rss_url", "language", "country", "region", "category_group",
  "primary_category", "secondary_category", "source_type", "priority", "active",
];

export const HEALTH_TAB = "_health";
export const HEALTH_COLUMNS = [
  "source_id", "last_checked_at", "status", "items_in_feed", "new_articles",
  "last_success_at", "last_failure_at", "last_error",
];

export const ARTICLE_COLUMNS = [
  "id", "published_at", "fetched_at", "source_id", "source_name", "language",
  "title", "url", "description", "author", "image_url", "guid",
  "category_group", "category", "topic",
];

export const DATE_TAB = /^\d{4}-\d{2}-\d{2}$/;

/** IST calendar date (YYYY-MM-DD) of a timestamp. */
export function istDate(ms) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" })
    .format(new Date(ms));
}
