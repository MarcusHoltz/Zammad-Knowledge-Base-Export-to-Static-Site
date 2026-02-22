#!/usr/bin/env python3
"""
Export a Zammad Knowledge Base to Markdown, plus users/orgs/roles/groups to YAML.
All config is read from environment variables — set them in docker-compose.yml.

Zammad API quirks baked into this script:
  - Category titles do NOT exist on the category endpoint. They only appear
    inside the assets payload of answer responses, under the key
    KnowledgeBaseCategoryTranslation. We therefore do a full prefetch pass
    over every answer before writing a single file, so folder names are resolved.
  - The tags[] field on every answer object is always empty in the API — tags
    are stored in a separate polymorphic table and must be fetched via:
    GET /api/v1/tags?object=KnowledgeBaseAnswer&o_id={id}
  - Answer bodies are not returned by the metadata call. A second call with
    ?include_contents={translation_id} is required to get the HTML body.
  - Attachment URLs (/api/v1/attachments/{id}) require the same auth token.
    Content-Disposition headers on attachment responses are RFC 6266 encoded
    (e.g. filename*=UTF-8''image.png) and cannot be used as filenames.
"""

import os
import re
import sys
import time
import logging
import requests
import yaml
from pathlib import Path
from markdownify import markdownify
from slugify import slugify

# ---- Config -------------------------------------------------------------------------------------------------------------------
BASE_URL    = os.environ.get("ZAMMAD_URL",    "").rstrip("/")
TOKEN       = os.environ.get("ZAMMAD_TOKEN",  "")
KB_ID       = int(os.environ.get("ZAMMAD_KB_ID", "1"))
RATE_LIMIT  = float(os.environ.get("RATE_LIMIT", "0.1"))
FRONTMATTER = os.environ.get("FRONTMATTER", "true").lower() == "true"
MD_HEADING  = os.environ.get("MD_HEADING_STYLE", "ATX")
OUTPUT_DIR  = Path("/output")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("kb-export")

if not BASE_URL or not TOKEN:
    sys.exit("Set ZAMMAD_URL and ZAMMAD_TOKEN in docker-compose.yml")
if "your-zammad" in BASE_URL or "your_api_token" in TOKEN:
    sys.exit("Replace the placeholder values in docker-compose.yml before running")


# ---- HTTP ---------------------------------------------------------------------------------------------------------------------
session = requests.Session()
session.headers["Authorization"] = f"Token token={TOKEN}"


def api_get(path, params=None):
    """
    Make an authenticated GET request to /api/v1{path}.

    Exits immediately on 401/403 — these are configuration errors that cannot
    be retried. All other HTTP errors are raised and handled by the caller.
    Note: the 403 message is intentionally generic because this function is
    used for both KB endpoints (need knowledge_base.reader) and org endpoints
    (need admin or agent role) — a specific message would be wrong half the time.
    """
    resp = session.get(f"{BASE_URL}/api/v1{path}", params=params or {})
    time.sleep(RATE_LIMIT)  # be a polite client; rate limit after every call
    if resp.status_code == 401:
        sys.exit("Authentication failed — check your ZAMMAD_TOKEN")
    if resp.status_code == 403:
        sys.exit(f"Permission denied on {path} — check the token's role permissions in Zammad")
    resp.raise_for_status()
    return resp.json()


def fetch_all_pages(path, label="records"):
    """
    Collect all records from a paginated Zammad list endpoint.

    Zammad paginates with ?page=N&per_page=N. We stop when a page returns
    fewer records than requested, which signals the last page.
    ?expand=true asks Zammad to resolve integer ID references into their
    human-readable names inline (e.g. organization name alongside org_id).
    Returns an empty list and logs a warning on any network failure so
    callers can continue exporting other data rather than crashing entirely.
    """
    records, page = [], 1
    try:
        while True:
            batch = api_get(path, {"page": page, "per_page": 500, "expand": "true"})
            if not isinstance(batch, list) or not batch:
                break
            records.extend(batch)
            if len(batch) < 500:
                break  # last page — fewer records than the page size means no more pages
            page += 1
    except Exception as e:
        log.warning("Failed to fetch %s (got %d so far): %s", label, len(records), e)
    return records


# ---- Utilities -------------------------------------------------------------------------------------------------------------------
def to_md(html):
    """Convert Zammad's answer body HTML to clean Markdown."""
    if not html:
        return ""
    result = markdownify(html, heading_style=MD_HEADING, bullets="-",
                         strip=["script", "style"], newline_style="backslash")
    # markdownify can leave runs of 3+ blank lines around block elements; collapse them
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def frontmatter(fields):
    """
    Render a YAML frontmatter block from a dict, skipping None values.

    None means "not applicable for this article" (e.g. no internal_at on a
    draft). False is intentionally kept — promoted: false is meaningful and
    templates need it to filter reliably. Empty lists are excluded via
    `value or None` at the call site so tags/category don't appear as [].
    """
    clean = {k: v for k, v in fields.items() if v is not None}
    return "---\n" + yaml.dump(clean, allow_unicode=True, default_flow_style=False, sort_keys=False) + "---"


def slug(text):
    """URL-safe slug, max 80 chars. Falls back to 'untitled' if text is empty or all symbols."""
    return slugify(str(text), max_length=80, separator="-") or "untitled"


def write_md(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("  wrote %s", path.relative_to(OUTPUT_DIR))


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    log.info("  wrote %s", path.relative_to(OUTPUT_DIR))


# ---- Category caches and title resolution ----------------------------------------------------------------------------
#
# The Zammad category endpoint returns {id, parent_id, translation_ids, answer_ids,
# child_ids} — translation_ids are ints with no titles attached.
# Titles only appear in answer response assets under KnowledgeBaseCategoryTranslation.
# Strategy: cache every category API response and every title seen in any answer
# asset, then resolve titles from cache when building folder names.
#
_cat_cache:   dict[int, dict] = {}  # raw category API responses, keyed by category id
_cat_titles:  dict[int, str]  = {}  # category translation_id -> title string
_answer_cache: dict[int, dict] = {}  # answer step-1 responses, keyed by answer id


def cache_cat_titles(assets):
    """
    Pull any KnowledgeBaseCategoryTranslation entries from an answer's assets
    dict and store them. Called every time we fetch an answer so that by the
    time we need a category's folder name, its title is already known.
    """
    for tid, trans in assets.get("KnowledgeBaseCategoryTranslation", {}).items():
        if trans.get("title"):
            _cat_titles[int(tid)] = trans["title"]


def fetch_category(cat_id):
    """Fetch and cache a category by ID. Raises on network failure — callers handle it."""
    if cat_id not in _cat_cache:
        _cat_cache[cat_id] = api_get(f"/knowledge_bases/{KB_ID}/categories/{cat_id}")
    return _cat_cache[cat_id]


def category_title(cat):
    """
    Return the best available title for a category dict.
    Tries each translation_id against the title cache (populated during prefetch).
    Falls back to 'category-{id}' for categories that have no answers anywhere
    in their subtree — those never appear in any answer's assets.
    """
    for tid in cat.get("translation_ids", []):
        if tid in _cat_titles:
            return _cat_titles[tid]
    return f"category-{cat['id']}"


def category_path(cat):
    """
    Walk the parent_id chain from this category up to the root and return
    a list of folder slugs in root-first order, e.g. ['fleet-ops', 'gunnery'].
    Used to build the output directory path for both _index.md and answer files.
    """
    parts, current = [], cat
    while True:
        parts.insert(0, slug(category_title(current)))
        parent_id = current.get("parent_id")
        if not parent_id:
            break
        current = fetch_category(parent_id)
    return parts


# ---- Image download and src rewriting ------------------------------------------------------------------------------------
_img_cache: dict[int, str] = {}  # attachment_id -> filename on disk

# Map Content-Type subtypes to canonical file extensions.
# Content-Disposition is not used — Zammad sends RFC 6266 encoded filenames
# (filename*=UTF-8''image.png) which are not safe to use directly.
_EXT = {
    "jpeg": "jpg", "jpg": "jpg", "png": "png", "gif": "gif",
    "webp": "webp", "svg+xml": "svg", "bmp": "bmp", "tiff": "tiff",
}


def download_image(attachment_id, answer_slug, n):
    """
    Download an attachment to output/images/{answer_slug}-{n}.{ext}.

    Filename is derived from the answer slug + a 1-based counter, not from
    the attachment's original name, to guarantee clean SEO-friendly filenames.
    Returns the filename on success, None on any failure. Failures are logged
    and the original src is left untouched in the Markdown output so no content
    is silently lost. Results are cached so the same attachment referenced
    across multiple answers is only downloaded once.
    """
    if attachment_id in _img_cache:
        return _img_cache[attachment_id]

    try:
        resp = session.get(f"{BASE_URL}/api/v1/attachments/{attachment_id}", stream=True)
        time.sleep(RATE_LIMIT)
        if not resp.ok:
            log.warning("Attachment %d returned HTTP %s — leaving src unchanged", attachment_id, resp.status_code)
            return None

        # Derive extension from Content-Type, not Content-Disposition (see module docstring)
        subtype = resp.headers.get("Content-Type", "").split(";")[0].split("/")[-1].strip().lower()
        ext = _EXT.get(subtype, subtype if re.match(r"^[a-z0-9]+$", subtype) else "bin")
        filename = f"{answer_slug}-{n}.{ext}"

        dest = OUTPUT_DIR / "images" / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)
            log.info("  image images/%s", filename)

        _img_cache[attachment_id] = filename
        return filename

    except Exception as e:
        log.warning("Could not download attachment %d: %s", attachment_id, e)
        return None


def rewrite_images(html, answer_slug, depth):
    """
    Find every <img src="...​/api/v1/attachments/{id}..."> in the HTML body,
    download the image, and rewrite src to a relative path.

    depth is the number of category folders between the answer file and the
    output root — used to build the correct ../ prefix to reach images/:
      depth 0 (root answer)       ->  images/slug-1.png
      depth 1 (one category deep) ->  ../images/slug-1.png
      depth 2 (subcategory)       ->  ../../images/slug-1.png

    Handles both relative (/api/v1/attachments/46) and absolute
    (https://zammad.example.com/api/v1/attachments/46) src values.
    If a download fails, the original src is left intact rather than
    producing a broken relative link.
    """
    pattern = re.compile(
        r'(<img\b[^>]*?\bsrc=")([^"]*?/api/v1/attachments/(\d+)[^"]*)(")',
        re.IGNORECASE,
    )
    up = "../" * depth
    counter = [0]  # list so the closure can mutate it

    def replace(m):
        counter[0] += 1
        filename = download_image(int(m.group(3)), answer_slug, counter[0])
        if not filename:
            return m.group(0)  # leave the original src if download failed
        return f"{m.group(1)}{up}images/{filename}{m.group(4)}"

    return pattern.sub(replace, html)


# ---- Answer export ------------------------------------------------------------------------------------------------------------
# Whether the token has permission to read tags. Set to False on first 403 so
# we stop trying and warn once rather than logging 403 for every single answer.
_tags_permission_ok = True


def answer_tags(answer_id):
    """
    Fetch tags for an answer via GET /api/v1/tags?object=KnowledgeBaseAnswer&o_id={id}

    The tags[] field embedded on every KnowledgeBaseAnswer object is permanently
    empty in the Zammad API — tags live in a separate polymorphic table and must
    be fetched here.

    IMPORTANT: this endpoint requires the 'admin.tag' permission (or an Agent role)
    in addition to knowledge_base.reader. A KB-only token will get a 200 back but
    the tags list will be empty, or a 403 on some Zammad versions.

    We bypass api_get() here intentionally. api_get() calls sys.exit() on 403,
    which raises SystemExit (a BaseException, not Exception), so it would crash
    the whole export rather than gracefully skipping tags. We need to inspect the
    status code ourselves and handle 403 with a clear permission warning.
    """
    global _tags_permission_ok
    if not _tags_permission_ok:
        return []  # already warned — don't log 403 for every answer

    try:
        resp = session.get(
            f"{BASE_URL}/api/v1/tags",
            params={"object": "KnowledgeBaseAnswer", "o_id": answer_id},
        )
        time.sleep(RATE_LIMIT)

        if resp.status_code == 403:
            _tags_permission_ok = False
            log.warning(
                "Tags will be skipped — the token needs 'admin.tag' permission "
                "(or an Agent role) in addition to knowledge_base.reader. "
                "Add it at Admin → Accounts → Token Access."
            )
            return []

        resp.raise_for_status()
        return resp.json().get("tags", [])

    except Exception as e:
        log.warning("Could not fetch tags for answer %d: %s", answer_id, e)
        return []


def answer_status(meta):
    """
    Derive a human-readable status string from Zammad's three state timestamps.

    Zammad tracks article state via nullable timestamps, not an enum field:
      archived_at set  -> archived  (checked first; an archived article may
                          also have published_at from before it was archived)
      published_at set -> published (visible to the public)
      internal_at set  -> internal  (visible to agents only)
      all null         -> draft     (not yet visible anywhere)
    """
    if meta.get("archived_at"):  return "archived"
    if meta.get("published_at"): return "published"
    if meta.get("internal_at"):  return "internal"
    return "draft"


def prefetch(cat_id):
    """
    Recursively walk every category and answer in the subtree, fetching the
    step-1 answer response for each answer not already in cache.

    This must run before export_category so that _cat_titles is fully populated
    when we start resolving folder names. Without this pass, _index.md files
    would be written with fallback names like 'category-7' because no answer
    has yet been fetched to teach us the real title.

    Step-1 answer response = GET /knowledge_bases/{id}/answers/{id}
    It does NOT include the body HTML — that needs a second call with
    ?include_contents. We only need titles and metadata here.
    """
    try:
        cat = fetch_category(cat_id)
    except Exception as e:
        log.warning("Could not fetch category %d during prefetch: %s", cat_id, e)
        return

    for answer_id in cat.get("answer_ids", []):
        if answer_id in _answer_cache:
            continue
        try:
            resp = api_get(f"/knowledge_bases/{KB_ID}/answers/{answer_id}")
            _answer_cache[answer_id] = resp
            cache_cat_titles(resp.get("assets", {}))
        except Exception as e:
            log.warning("Prefetch failed for answer %d: %s", answer_id, e)

    for child_id in cat.get("child_ids", []):
        prefetch(child_id)


def export_answer(answer_id, cat_parts):
    """
    Write one answer as a Markdown file with YAML frontmatter.

    Two-step fetch:
      Step 1 (metadata) — already in _answer_cache from prefetch. Falls back
        to a live fetch if missing (e.g. answers added between prefetch and export).
      Step 2 (body HTML) — GET .../answers/{id}?include_contents={translation_id}
        This is a separate Zammad API call; body is not included in step 1.

    cat_parts is the list of folder slug segments for this answer's category,
    used both to build the output path and to calculate image depth.
    Returns True if the file was written, False if skipped due to any error.
    """
    # Use the cached step-1 response if available; otherwise fetch it now
    resp = _answer_cache.get(answer_id)
    if resp is None:
        try:
            resp = api_get(f"/knowledge_bases/{KB_ID}/answers/{answer_id}")
            cache_cat_titles(resp.get("assets", {}))
        except Exception as e:
            log.warning("Skipping answer %d — could not fetch metadata: %s", answer_id, e)
            return False

    assets = resp.get("assets", {})

    # Answer metadata lives at assets.KnowledgeBaseAnswer[str(answer_id)]
    # (Zammad uses string keys even though the IDs are integers)
    meta = assets.get("KnowledgeBaseAnswer", {}).get(str(answer_id))
    if not meta:
        log.warning("Skipping answer %d — KnowledgeBaseAnswer missing from assets", answer_id)
        return False

    translation_ids = meta.get("translation_ids", [])
    if not translation_ids:
        log.warning("Skipping answer %d — no translation_ids (answer may be corrupt)", answer_id)
        return False

    # Walk translation_ids and pick the first one that has a non-empty title.
    # Multi-locale support is not implemented: kb_locale_id cannot be mapped to
    # a locale string without an undocumented endpoint, so we take the first hit.
    translations = assets.get("KnowledgeBaseAnswerTranslation", {})
    title, chosen_tid = f"Answer {answer_id}", translation_ids[0]
    for tid in translation_ids:
        t = translations.get(str(tid), {})
        if t.get("title"):
            title, chosen_tid = t["title"], tid
            break

    answer_slug = slug(title)

    # Step 2: fetch the body — Zammad withholds HTML bodies unless you pass
    # ?include_contents={translation_id}. This is a deliberate API design choice
    # to avoid sending large HTML payloads in list/metadata responses.
    try:
        body_resp = api_get(
            f"/knowledge_bases/{KB_ID}/answers/{answer_id}",
            {"include_contents": chosen_tid},
        )
    except Exception as e:
        log.warning("Skipping answer %d — could not fetch body: %s", answer_id, e)
        return False

    body_html = (
        body_resp.get("assets", {})
        .get("KnowledgeBaseAnswerTranslationContent", {})
        .get(str(chosen_tid), {})
        .get("body", "")
    )

    if body_html:
        # Rewrite before converting to Markdown — markdownify will turn the
        # rewritten relative src into a proper Markdown image reference
        body_html = rewrite_images(body_html, answer_slug, len(cat_parts))

    out_path = (
        OUTPUT_DIR / Path(*cat_parts, f"{answer_slug}.md")
        if cat_parts else OUTPUT_DIR / f"{answer_slug}.md"
    )

    # Fetch tags last — we want to write the file even if tags fail
    tags = answer_tags(answer_id)

    parts = []
    if FRONTMATTER:
        parts.append(frontmatter({
            "title":        title,
            "slug":         answer_slug,
            "zammad_id":    answer_id,
            "status":       answer_status(meta),
            # `or None` collapses empty string/list to None so the field is
            # omitted from frontmatter rather than written as [] or ""
            "category":     "/".join(cat_parts) or None,
            "tags":         tags or None,
            # promoted is always written as true/false — templates need it to
            # filter reliably and False must not be omitted (it is not None)
            "promoted":     bool(meta.get("promoted")),
            "published_at": meta.get("published_at"),
            "internal_at":  meta.get("internal_at"),
            "archived_at":  meta.get("archived_at"),
            "updated_at":   meta.get("updated_at"),
        }))

    parts.append(f"# {title}\n")
    if body_html:
        parts.append(to_md(body_html))

    write_md(out_path, "\n\n".join(parts) + "\n")
    return True


def export_category(cat_id, depth=0):
    """
    Write _index.md for this category, export all its answers, then recurse
    into child categories. Depth is used only for log indentation.
    """
    try:
        cat = fetch_category(cat_id)
    except Exception as e:
        log.warning("Skipping category %d — could not fetch it: %s", cat_id, e)
        return

    parts = category_path(cat)
    title = category_title(cat)

    log.info("%s[cat %d] %s", "  " * depth, cat_id, " / ".join(parts))

    # Write the category landing page
    index = OUTPUT_DIR / Path(*parts) / "_index.md"
    fm = frontmatter({"title": title, "zammad_id": cat_id, "layout": "category"}) if FRONTMATTER else ""
    write_md(index, f"{fm}\n\n# {title}\n" if fm else f"# {title}\n")

    # Export every answer in this category
    answer_ids = cat.get("answer_ids", [])
    ok = sum(export_answer(aid, parts) for aid in answer_ids)
    if answer_ids:
        log.info("%s  %d/%d answers written", "  " * depth, ok, len(answer_ids))

    # Recurse into subcategories
    for child_id in cat.get("child_ids", []):
        export_category(child_id, depth + 1)


# ---- Organisational data export ------------------------------------------------------------------------------------------------
#
# Users, organizations, roles, and groups are written to _data/ as YAML files.
# Every major SSG reads these natively: Jekyll via _data/, Hugo via data/,
# Astro via src/data/ or content collections, MkDocs via plugins.
#
# ?expand=true resolves integer ID references to human-readable names inline,
# so you get both organization_id (int, for joins) and organization (string, for display).
#
# Zammad returns group membership as {"group_id_as_string": "access_level"}.
# We normalise this into [{group: "name", access: "level"}] for readability
# and because the group_id keys are useless without a separate lookup.
#
# .get(key) or default_value is used instead of .get(key, default) throughout
# because .get(key, default) only fires when the key is absent. If Zammad
# returns the key with value null, .get returns None regardless of the default.
# `or default` handles both absent and null correctly.

def export_users():
    log.info("Exporting users...")
    raw = fetch_all_pages("/users", "users")
    users = []
    for u in raw:
        if not u.get("id"):
            continue
        if u.get("login") == "-":
            # Zammad's internal system actor — not a real account, skip it
            continue
        users.append({
            "id":              u["id"],
            "login":           u.get("login"),
            "email":           u.get("email"),
            "firstname":       u.get("firstname"),
            "lastname":        u.get("lastname"),
            "active":          u.get("active"),
            "organization_id": u.get("organization_id"),
            "organization":    u.get("organization"),   # resolved name via ?expand=true
            "role_ids":        u.get("role_ids") or [], # role_ids can be null, not just absent
            "roles":           u.get("roles") or [],    # resolved names via ?expand=true
            # group_ids comes as {"str(id)": "access_level"} — we use the expanded
            # groups dict {"group_name": "access_level"} for human-readable output
            "group_access":    [{"group": g, "access": a}
                                for g, a in (u.get("groups") or {}).items()],
            "last_login":      u.get("last_login"),
            "created_at":      u.get("created_at"),
            "updated_at":      u.get("updated_at"),
        })
    write_yaml(OUTPUT_DIR / "_data" / "users.yml", users)
    log.info("  %d users", len(users))


def export_organizations():
    log.info("Exporting organizations...")
    raw = fetch_all_pages("/organizations", "organizations")
    orgs = [
        {
            "id":           o["id"],
            "name":         o.get("name"),
            "note":         o.get("note") or None,
            "domain":       o.get("domain") or None,
            "active":       o.get("active"),
            # member_ids is a list of user IDs — we store count only;
            # full membership is available by joining on organization_id in users.yml
            "member_count": len(o.get("member_ids") or []),
            "created_at":   o.get("created_at"),
            "updated_at":   o.get("updated_at"),
        }
        for o in raw if o.get("id")
    ]
    write_yaml(OUTPUT_DIR / "_data" / "organizations.yml", orgs)
    log.info("  %d organizations", len(orgs))


def export_roles():
    log.info("Exporting roles...")
    raw = fetch_all_pages("/roles", "roles")
    roles = [
        {
            "id":                r["id"],
            "name":              r.get("name"),
            "note":              r.get("note") or None,
            "active":            r.get("active"),
            # default_at_signup can be null (not just absent) in older Zammad versions
            "default_at_signup": r.get("default_at_signup") or False,
            "created_at":        r.get("created_at"),
            "updated_at":        r.get("updated_at"),
        }
        for r in raw if r.get("id")
    ]
    write_yaml(OUTPUT_DIR / "_data" / "roles.yml", roles)
    log.info("  %d roles", len(roles))


def export_groups():
    log.info("Exporting groups...")
    raw = fetch_all_pages("/groups", "groups")
    groups = [
        {
            "id":                   g["id"],
            "name":                 g.get("name"),
            "note":                 g.get("note") or None,
            "active":               g.get("active"),
            "email":                g.get("email") or None,
            "follow_up_possible":   g.get("follow_up_possible"),
            "follow_up_assignment": g.get("follow_up_assignment"),
            "shared_drafts":        g.get("shared_drafts"),
            "created_at":           g.get("created_at"),
            "updated_at":           g.get("updated_at"),
        }
        for g in raw if g.get("id")
    ]
    write_yaml(OUTPUT_DIR / "_data" / "groups.yml", groups)
    log.info("  %d groups", len(groups))


# ---- Entry point ----------------------------------------------------------------------------------------------------------------
def main():
    log.info("Zammad: %s  |  KB: %d  |  Output: %s", BASE_URL, KB_ID, OUTPUT_DIR)

    # Org data first — independent of the KB, so a bad KB_ID won't affect them
    log.info("--- Organisational data")
    export_users()
    export_organizations()
    export_roles()
    export_groups()

    log.info("--- Knowledge Base")
    try:
        kb = api_get(f"/knowledge_bases/{KB_ID}")
    except Exception as e:
        sys.exit(f"Could not fetch Knowledge Base {KB_ID} — check ZAMMAD_KB_ID: {e}")

    all_cats = kb.get("category_ids", [])

    # Identify root categories (parent_id is None) — children are reached via recursion.
    # fetch_category is safe here because responses are cached from prefetch below.
    root_cats = []
    for cat_id in all_cats:
        try:
            if fetch_category(cat_id).get("parent_id") is None:
                root_cats.append(cat_id)
        except Exception as e:
            log.warning("Skipping category %d — could not determine parent: %s", cat_id, e)

    log.info("%d total categories, %d at root level", len(all_cats), len(root_cats))

    # Pass 1 — prefetch all answer metadata to populate _cat_titles.
    # We must know every category title before writing any folder or _index.md.
    log.info("Prefetching answers to resolve category titles...")
    for cat_id in root_cats:
        prefetch(cat_id)

    # Pass 2 — write _index.md for every category and .md for every answer
    log.info("Writing files...")
    for cat_id in root_cats:
        export_category(cat_id)

    log.info("Done.")


if __name__ == "__main__":
    main()
