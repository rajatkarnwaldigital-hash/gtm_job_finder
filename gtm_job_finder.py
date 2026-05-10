"""
GTM Job Finder
--------------
Scans company career pages for GTM Engineer roles.
Uses ATS API detection (Greenhouse/Lever/Ashby) + Claude for JD analysis.

Usage:
    python gtm_job_finder.py --input companies_input.csv --output results.csv
    python gtm_job_finder.py --input companies_input.csv --output results.csv --workers 15
"""

import csv
import json
import time
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = "YOUR_ANTHROPIC_API_KEY_HERE"
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"
MAX_WORKERS       = 10
REQUEST_TIMEOUT   = 12  # seconds per HTTP request
RATE_LIMIT_SLEEP  = 0.3 # seconds between Claude calls

CANDIDATE_PROFILE = """
GTM Engineer with 5+ years building outbound systems and revenue automation for B2B companies.
Core stack: Python (ThreadPoolExecutor, API integrations), Clay, n8n, Claude API (Haiku/Sonnet),
HubSpot, Apollo, Ahrefs, SEMrush, Instantly, Smartlead. Proven results: $259K+ pipeline generated,
signal-led outbound campaigns (12k → 1,500 qualified contacts), AI scoring workflows, n8n reply
classifiers, MCP agent design. Strong on the full GTM stack — sourcing, enrichment, sequencing,
CRM ops, and AI agents. Based in India (UTC+5:30), open to remote roles.
"""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

OUTPUT_FIELDS = [
    "company_name",
    "career_url",
    "ats_detected",
    "job_title",
    "job_url",
    "location_raw",
    "timezone_viable",
    "fit_score",
    "fit_reason",
    "gtm_match",
    "status",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── ATS Detection ─────────────────────────────────────────────────────────────

def detect_ats(base_url: str, html: str) -> str:
    """Detect which ATS the company uses from page content / URL patterns."""
    lower = html.lower()
    if "greenhouse.io" in lower or "boards.greenhouse.io" in lower:
        return "Greenhouse"
    if "lever.co" in lower or "jobs.lever.co" in lower:
        return "Lever"
    if "ashbyhq.com" in lower or "jobs.ashbyhq.com" in lower:
        return "Ashby"
    if "workday.com" in lower:
        return "Workday"
    if "myworkdayjobs.com" in lower:
        return "Workday"
    if "smartrecruiters.com" in lower:
        return "SmartRecruiters"
    if "recruitee.com" in lower:
        return "Recruitee"
    if "bamboohr.com" in lower:
        return "BambooHR"
    if "rippling.com" in lower:
        return "Rippling"
    return "Unknown"


def extract_ats_id(url: str, ats: str) -> str | None:
    """Pull the company slug/ID from career page URLs for ATS API calls."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]

    if ats == "Greenhouse" and "greenhouse.io" in parsed.netloc and parts:
        slug = parts[0]
        log.debug(f"Greenhouse slug from URL: {slug}")
        return slug
    if ats == "Lever" and "lever.co" in parsed.netloc and parts:
        slug = parts[0]
        log.debug(f"Lever slug from URL: {slug}")
        return slug
    if ats == "Ashby" and "ashbyhq.com" in parsed.netloc and parts:
        slug = parts[0]
        log.debug(f"Ashby slug from URL: {slug}")
        return slug
    return None


def resolve_ats_slug_from_html(html: str, ats: str) -> str | None:
    """
    When a company links out to an ATS, extract the slug from embedded hrefs.
    Searches both <a href> and raw text (some pages embed ATS URLs in JS).
    """
    domain_map = {
        "Greenhouse": ["greenhouse.io/", "boards.greenhouse.io/"],
        "Lever":      ["jobs.lever.co/", "lever.co/"],
        "Ashby":      ["jobs.ashbyhq.com/", "ashbyhq.com/posting-api/job-board/"],
    }
    patterns = domain_map.get(ats, [])
    if not patterns:
        return None

    # Check anchor hrefs first (most reliable)
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for pattern in patterns:
            if pattern in href:
                parsed = urlparse(href)
                parts = [p for p in parsed.path.strip("/").split("/") if p]
                if parts:
                    slug = parts[0]
                    log.debug(f"{ats} slug from href: {slug}")
                    return slug

    # Also scan raw HTML text for ATS URLs embedded in JS/data attributes
    for pattern in patterns:
        idx = html.find(pattern)
        if idx != -1:
            after = html[idx + len(pattern):]
            slug = after.split('"')[0].split("'")[0].split("/")[0].split("?")[0].strip()
            if slug and len(slug) < 60 and slug.replace("-", "").replace("_", "").isalnum():
                log.debug(f"{ats} slug from raw HTML: {slug}")
                return slug

    log.debug(f"Could not extract {ats} slug from HTML")
    return None


# ── ATS API Fetchers ──────────────────────────────────────────────────────────

def fetch_greenhouse_jobs(slug: str) -> list[dict]:
    """Hit Greenhouse public JSON API."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return []
        data = r.json()
        jobs = []
        for job in data.get("jobs", []):
            jobs.append({
                "title": job.get("title", ""),
                "url":   job.get("absolute_url", ""),
                "location": job.get("location", {}).get("name", ""),
                "description": BeautifulSoup(
                    job.get("content", ""), "html.parser"
                ).get_text(" ", strip=True)[:3000],
            })
        return jobs
    except Exception:
        return []


def fetch_lever_jobs(slug: str) -> list[dict]:
    """Hit Lever public JSON API."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return []
        data = r.json()
        jobs = []
        for job in data:
            desc_parts = []
            for section in job.get("descriptionBody", {}).get("descriptionPlain", []):
                desc_parts.append(section)
            description = " ".join(desc_parts)[:3000]

            jobs.append({
                "title":       job.get("text", ""),
                "url":         job.get("hostedUrl", ""),
                "location":    job.get("categories", {}).get("location", ""),
                "description": description,
            })
        return jobs
    except Exception:
        return []


def fetch_ashby_jobs(slug: str) -> list[dict]:
    """Hit Ashby public JSON API."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return []
        data = r.json()
        jobs = []
        for job in data.get("jobs", []):
            jobs.append({
                "title":       job.get("title", ""),
                "url":         job.get("jobUrl", ""),
                "location":    job.get("location", ""),
                "description": BeautifulSoup(
                    job.get("descriptionHtml", ""), "html.parser"
                ).get_text(" ", strip=True)[:3000],
            })
        return jobs
    except Exception:
        return []


# ── HTML Scraper Fallback ─────────────────────────────────────────────────────

GTM_KEYWORDS = [
    "gtm engineer", "gtm engineering", "revenue engineer", "revenue engineering",
    "growth engineer", "revops engineer", "revenue operations engineer",
    "sales engineer", "go-to-market engineer", "marketing engineer",
    "automation engineer", "outbound engineer", "demand generation engineer",
    "sales automation", "marketing automation engineer",
]

CAREER_PATH_SUFFIXES = [
    "/careers", "/jobs", "/about/careers", "/company/careers",
    "/work-with-us", "/join-us", "/open-positions", "/en/careers",
    "/careers/open-positions", "/en/jobs",
]


def _candidate_urls(base_url: str) -> list[str]:
    """
    Build the full list of URLs to try for a company's career page.
    Includes path suffixes on the base domain AND careers/jobs subdomains.
    """
    parsed = urlparse(base_url)
    # Strip to bare domain: strip www, keep root
    netloc = parsed.netloc or parsed.path  # handle URLs without scheme
    bare = netloc.replace("www.", "")

    subdomain_bases = [
        f"https://careers.{bare}",
        f"https://jobs.{bare}",
    ]

    path_variants = [base_url.rstrip("/")] + [
        base_url.rstrip("/") + s for s in CAREER_PATH_SUFFIXES
    ]

    # Subdomains first — they usually point straight at the ATS embed
    return subdomain_bases + path_variants


def _is_real_career_page(html: str) -> bool:
    """
    Quick check: does this HTML actually look like a career/jobs page
    and not just a homepage or error page?
    """
    lower = html.lower()
    signals = [
        "greenhouse.io", "lever.co", "ashbyhq.com", "workday",
        "open positions", "open roles", "job opening", "we're hiring",
        "we are hiring", "join our team", "current openings",
        "apply now", "view all jobs", "see all roles",
    ]
    return any(s in lower for s in signals)


def _fetch_with_requests(url: str) -> tuple[str, str]:
    """Single requests.get attempt. Returns (resolved_url, html) or ('', '')."""
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 500:
            return r.url, r.text
    except Exception:
        pass
    return "", ""


def _fetch_with_playwright(url: str) -> tuple[str, str]:
    """
    Playwright fallback for JS-rendered pages (Greenhouse embeds, React SPAs, etc).
    Returns (resolved_url, html) or ('', '').
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
                java_script_enabled=True,
            )
            page.goto(url, timeout=20000, wait_until="domcontentloaded")
            # Give JS a moment to render job listings
            page.wait_for_timeout(2500)
            html = page.content()
            resolved = page.url
            browser.close()
            if len(html) > 500:
                return resolved, html
    except Exception as e:
        log.debug(f"Playwright failed for {url}: {e}")
    return "", ""


def find_career_page(base_url: str) -> tuple[str, str]:
    """
    Try to find a working career page for a company.
    Strategy:
      1. Try all candidate URLs with requests (fast, parallel-friendly)
      2. For the first URL that returns HTML, check if it looks like a real career page
      3. If no candidate gives a real career page, retry the best candidate with Playwright
    Returns (resolved_url, html_text) or (base_url, "").
    """
    candidates = _candidate_urls(base_url)
    best_url, best_html = "", ""

    for url in candidates:
        resolved, html = _fetch_with_requests(url)
        if not html:
            continue
        if _is_real_career_page(html):
            log.debug(f"Career page found via requests: {resolved}")
            return resolved, html
        # Keep first successful response as fallback
        if not best_html:
            best_url, best_html = resolved, html

    # Requests didn't find a real career page — try Playwright on the most likely URL
    # Priority: careers subdomain > /careers path > base URL
    playwright_targets = [
        f"https://careers.{urlparse(base_url).netloc.replace('www.','')}",
        base_url.rstrip("/") + "/careers",
        base_url.rstrip("/") + "/jobs",
        base_url,
    ]
    for url in playwright_targets:
        log.debug(f"Trying Playwright: {url}")
        resolved, html = _fetch_with_playwright(url)
        if html and _is_real_career_page(html):
            log.debug(f"Career page found via Playwright: {resolved}")
            return resolved, html

    # Last resort: return whatever requests got
    return (best_url or base_url), best_html


def scrape_jobs_from_html(html: str, career_url: str) -> list[dict]:
    """
    Best-effort scrape for job links/titles on a plain HTML career page.
    Looks for anchor text matching GTM keywords.
    """
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True).lower()
        if any(kw in text for kw in GTM_KEYWORDS):
            href = a["href"]
            if not href.startswith("http"):
                href = urljoin(career_url, href)
            found.append({
                "title":       a.get_text(" ", strip=True),
                "url":         href,
                "location":    "",
                "description": "",
            })
    return found


# ── Claude Analysis ───────────────────────────────────────────────────────────

def call_claude(prompt: str) -> str:
    """Single Claude API call. Returns text response."""
    if ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY_HERE":
        # Placeholder mode — return mock response
        return json.dumps({
            "gtm_match": "Maybe",
            "timezone_viable": "Maybe",
            "fit_score": 5,
            "fit_reason": "API key not set — placeholder result",
        })

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        log.warning(f"Claude API error: {e}")
        return json.dumps({
            "gtm_match": "Error",
            "timezone_viable": "Error",
            "fit_score": 0,
            "fit_reason": str(e),
        })


def analyze_job(company: str, job: dict) -> dict:
    """
    Send JD to Claude for:
    - GTM match (is this functionally a GTM Eng role even if titled differently?)
    - Timezone viability for UTC+5:30
    - Fit score 1-10 against candidate profile
    - One-line fit reason
    """
    prompt = f"""You are evaluating a job posting for a GTM Engineer candidate.

CANDIDATE PROFILE:
{CANDIDATE_PROFILE}

COMPANY: {company}
JOB TITLE: {job['title']}
LOCATION: {job['location']}
JOB DESCRIPTION (excerpt):
{job['description'][:2000]}

Answer ONLY with a JSON object. No preamble, no markdown, no explanation outside the JSON.

{{
  "gtm_match": "Yes" | "No" | "Maybe",
  "timezone_viable": "Yes" | "No" | "Maybe",
  "fit_score": <integer 1-10>,
  "fit_reason": "<one concise sentence explaining the score>"
}}

Rules:
- gtm_match: "Yes" if the role is functionally GTM engineering (automation, Clay, outbound systems, RevOps tooling, revenue systems) even if the title is different. "No" if clearly unrelated (pure frontend, pure data science, etc). "Maybe" if unclear.
- timezone_viable: "Yes" if the role explicitly allows UTC+5:30 (India). "No" if clearly US/EU timezone required with no flexibility. "Maybe" if "Worldwide" or ambiguous.
- fit_score: 1-10 based on how well the candidate's skills match this specific role.
- fit_reason: Be specific — mention one concrete match or gap."""

    time.sleep(RATE_LIMIT_SLEEP)
    raw = call_claude(prompt)

    try:
        # Strip markdown fences if present
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(clean)
    except Exception:
        return {
            "gtm_match":      "Error",
            "timezone_viable": "Error",
            "fit_score":      0,
            "fit_reason":     f"Parse error: {raw[:100]}",
        }


# ── Per-Company Pipeline ──────────────────────────────────────────────────────

def process_company(row: dict) -> list[dict]:
    """
    Full pipeline for one company.
    Returns list of result rows (one per relevant job, or one error row).
    """
    company_name = row["company_name"]
    career_url   = row["career_url"].strip()

    log.info(f"Processing: {company_name}")

    # 1. Fetch career page
    resolved_url, html = find_career_page(career_url)

    if not html:
        return [{
            "company_name": company_name,
            "career_url":   career_url,
            "ats_detected": "Unknown",
            "job_title":    "",
            "job_url":      "",
            "location_raw": "",
            "timezone_viable": "",
            "fit_score":    "",
            "fit_reason":   "",
            "gtm_match":    "",
            "status":       "Blocked / Unreachable",
        }]

    # 2. Detect ATS
    ats = detect_ats(resolved_url, html)
    log.debug(f"{company_name}: ATS={ats}, career_url={resolved_url}")

    # 3. Fetch job listings via ATS API or HTML scrape
    jobs = []

    if ats in ("Greenhouse", "Lever", "Ashby"):
        slug = extract_ats_id(resolved_url, ats)
        if not slug:
            slug = resolve_ats_slug_from_html(html, ats)

        log.debug(f"{company_name}: {ats} slug={slug!r}")

        if slug:
            if ats == "Greenhouse":
                jobs = fetch_greenhouse_jobs(slug)
            elif ats == "Lever":
                jobs = fetch_lever_jobs(slug)
            elif ats == "Ashby":
                jobs = fetch_ashby_jobs(slug)
            log.debug(f"{company_name}: {ats} API returned {len(jobs)} jobs")

    if not jobs:
        # Fallback: scrape HTML for keyword-matching job links
        jobs = scrape_jobs_from_html(html, resolved_url)

    if not jobs:
        return [{
            "company_name":  company_name,
            "career_url":    resolved_url,
            "ats_detected":  ats,
            "job_title":     "",
            "job_url":       "",
            "location_raw":  "",
            "timezone_viable": "",
            "fit_score":     "",
            "fit_reason":    "",
            "gtm_match":     "",
            "status":        "No jobs found",
        }]

    # 4. Filter to GTM-adjacent jobs before sending to Claude
    # (saves API calls — only analyze plausibly relevant roles)
    gtm_adjacent_titles = [
        "gtm", "revenue", "growth", "sales", "marketing", "outbound",
        "automation", "revops", "operations", "demand", "pipeline",
        "crm", "enablement", "go-to-market",
    ]

    relevant_jobs = [
        j for j in jobs
        if any(kw in j["title"].lower() for kw in gtm_adjacent_titles)
    ]

    # If no title-filtered jobs, don't send everything to Claude
    if not relevant_jobs:
        return [{
            "company_name":  company_name,
            "career_url":    resolved_url,
            "ats_detected":  ats,
            "job_title":     "",
            "job_url":       "",
            "location_raw":  "",
            "timezone_viable": "",
            "fit_score":     "",
            "fit_reason":    "",
            "gtm_match":     "No",
            "status":        f"No GTM-adjacent roles (total jobs: {len(jobs)})",
        }]

    # 5. Analyze each relevant job with Claude
    results = []
    for job in relevant_jobs:
        analysis = analyze_job(company_name, job)
        results.append({
            "company_name":  company_name,
            "career_url":    resolved_url,
            "ats_detected":  ats,
            "job_title":     job["title"],
            "job_url":       job["url"],
            "location_raw":  job["location"],
            "timezone_viable": analysis.get("timezone_viable", ""),
            "fit_score":     analysis.get("fit_score", ""),
            "fit_reason":    analysis.get("fit_reason", ""),
            "gtm_match":     analysis.get("gtm_match", ""),
            "status":        "Scraped",
        })

    return results


# ── Checkpoint Helpers ────────────────────────────────────────────────────────

CHECKPOINT_EVERY = 10  # save progress every N completed companies


def checkpoint_path(output: str) -> str:
    """Derive checkpoint file path from output path."""
    base, ext = output.rsplit(".", 1) if "." in output else (output, "csv")
    return f"{base}.checkpoint.json"


def load_checkpoint(output: str) -> tuple[list[dict], set[str]]:
    """
    Load existing checkpoint if present.
    Returns (saved_rows, set_of_already_processed_company_names).
    """
    cp = checkpoint_path(output)
    if not __import__("os").path.exists(cp):
        return [], set()
    try:
        with open(cp, encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("rows", [])
        done = set(data.get("done_companies", []))
        log.info(f"Checkpoint found: {len(done)} companies already done, {len(rows)} rows loaded.")
        return rows, done
    except Exception as e:
        log.warning(f"Could not read checkpoint ({e}), starting fresh.")
        return [], set()


def save_checkpoint(output: str, rows: list[dict], done_companies: set[str]) -> None:
    """Persist current progress to checkpoint file."""
    cp = checkpoint_path(output)
    try:
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"rows": rows, "done_companies": list(done_companies)}, f)
        log.info(f"Checkpoint saved ({len(done_companies)} companies done).")
    except Exception as e:
        log.warning(f"Checkpoint save failed: {e}")


def clear_checkpoint(output: str) -> None:
    """Remove checkpoint file after a successful full run."""
    cp = checkpoint_path(output)
    try:
        __import__("os").remove(cp)
        log.info("Checkpoint cleared.")
    except FileNotFoundError:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GTM Job Finder")
    parser.add_argument("--input",   default="companies_input.csv",
                        help="Input CSV with company_name, career_url columns")
    parser.add_argument("--output",  default="gtm_results.csv",
                        help="Output CSV path")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help="Number of parallel workers (default 10)")
    parser.add_argument("--limit",   type=int, default=None,
                        help="Process only first N companies (for testing)")
    parser.add_argument("--fresh",   action="store_true",
                        help="Ignore existing checkpoint and start from scratch")
    args = parser.parse_args()

    # Load input
    with open(args.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        companies = list(reader)

    if args.limit:
        companies = companies[: args.limit]

    # Load checkpoint (skip if --fresh)
    if args.fresh:
        all_results, done_companies = [], set()
        log.info("--fresh flag set, ignoring any existing checkpoint.")
    else:
        all_results, done_companies = load_checkpoint(args.output)

    # Filter out already-processed companies
    remaining = [c for c in companies if c["company_name"] not in done_companies]

    log.info(
        f"Loaded {len(companies)} companies. "
        f"{len(done_companies)} already done, {len(remaining)} to process. "
        f"Workers: {args.workers}"
    )

    completed_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_company, row): row for row in remaining}
        for i, future in enumerate(as_completed(futures), 1):
            company = futures[future]["company_name"]
            try:
                rows = future.result()
                all_results.extend(rows)
                done_companies.add(company)
                log.info(f"[{i}/{len(remaining)}] {company} → {len(rows)} row(s)")
            except Exception as e:
                log.error(f"[{i}/{len(remaining)}] {company} failed: {e}")
                all_results.append({
                    "company_name": company,
                    "career_url":   futures[future]["career_url"],
                    "ats_detected": "",
                    "job_title":    "",
                    "job_url":      "",
                    "location_raw": "",
                    "timezone_viable": "",
                    "fit_score":    "",
                    "fit_reason":   str(e),
                    "gtm_match":    "",
                    "status":       "Error",
                })
                done_companies.add(company)

            completed_count += 1
            if completed_count % CHECKPOINT_EVERY == 0:
                save_checkpoint(args.output, all_results, done_companies)

    # Sort by fit_score descending (blanks last)
    def sort_key(r):
        try:
            return -int(r["fit_score"])
        except (ValueError, TypeError):
            return 1

    all_results.sort(key=sort_key)

    # Write final output
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(all_results)

    # Clean up checkpoint now that output is written
    clear_checkpoint(args.output)

    # Summary
    gtm_yes   = sum(1 for r in all_results if r.get("gtm_match") == "Yes")
    gtm_maybe = sum(1 for r in all_results if r.get("gtm_match") == "Maybe")
    scraped   = sum(1 for r in all_results if r.get("status") == "Scraped")
    blocked   = sum(1 for r in all_results if "Blocked" in str(r.get("status", "")))

    log.info("─" * 50)
    log.info(f"Done. Output: {args.output}")
    log.info(f"Total rows:        {len(all_results)}")
    log.info(f"GTM match (Yes):   {gtm_yes}")
    log.info(f"GTM match (Maybe): {gtm_maybe}")
    log.info(f"Jobs analyzed:     {scraped}")
    log.info(f"Blocked/unreachable: {blocked}")


if __name__ == "__main__":
    main()
