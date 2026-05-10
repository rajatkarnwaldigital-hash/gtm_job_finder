# GTM Job Finder

A Python script that scans 170+ company career pages for GTM Engineer roles and scores each one against a candidate profile using Claude AI.

Built with Python and Claude API. No manual searching — drop in a list of companies, get back a ranked CSV of relevant roles with fit scores, timezone viability, and apply links.

---

## What it does

- Detects which ATS each company uses (Greenhouse, Lever, Ashby, BambooHR, Recruitee)
- Hits their public APIs directly for structured job data
- Falls back to HTML scraping for companies not on standard ATS platforms
- Sends each relevant job description to Claude API for analysis
- Scores each role 1-10 against your profile, checks timezone viability, flags GTM match
- Outputs a ranked CSV — best matches first

---

## Output

One row per job posting:

| Column | What's in it |
|---|---|
| `company_name` | Company name |
| `career_url` | Career page URL that was scraped |
| `ats_detected` | Greenhouse / Lever / Ashby / Unknown |
| `job_title` | Exact title from the job posting |
| `job_url` | Direct link to the job posting |
| `location_raw` | Location as listed |
| `timezone_viable` | Yes / No / Maybe for your timezone |
| `fit_score` | 1-10 fit score from Claude |
| `fit_reason` | One-line explanation of the score |
| `gtm_match` | Yes / No / Maybe |
| `status` | Scraped / No jobs found / Error |

---

## Setup

**Requirements**
- Python 3.10+
- An Anthropic API key (get one at console.anthropic.com — free tier available)

**Install**

```bash
pip install requests beautifulsoup4 anthropic httpx
```

**Configure**

Open `gtm_job_finder.py` and update two things:

```python
# Line 23 — add your Anthropic API key
ANTHROPIC_API_KEY = "your-key-here"

# Line 30 — replace with your own background
CANDIDATE_PROFILE = """
Your role, years of experience, tools you use, what kind of company you want.
The more specific, the better the scoring.
"""
```

---

## Run

```bash
# Test with 10 companies first
python gtm_job_finder.py --input companies_input.csv --output results.csv --limit 10

# Full run
python gtm_job_finder.py --input companies_input.csv --output results.csv

# Adjust parallel workers (default 10)
python gtm_job_finder.py --input companies_input.csv --output results.csv --workers 15
```

**Input CSV format** — just two columns:

```
company_name,career_url
Stripe,https://stripe.com
Notion,https://notion.so
```

A starter list of 170+ remote-friendly companies is included as `companies_input.csv`.

---

## Checkpoint / resume

The script saves progress every 10 companies to a `.checkpoint.json` file. If it crashes or you stop it, re-running the same command picks up where it left off.

To start fresh:
```bash
python gtm_job_finder.py --input companies_input.csv --output results.csv --fresh
```

---

## Cost

Using Claude Haiku (default), a full run of 170 companies costs roughly $0.10-0.30 depending on how many GTM-adjacent roles are found. Only roles that pass the title filter get sent to Claude — pure engineering or design roles are skipped automatically.

---

## Limitations

- JS-rendered career pages (many modern SaaS companies) may return no results — the script can't run a browser. Direct job posting URLs work better for these.
- Some companies block scrapers. The script logs these as "Blocked / Unreachable".
- ATS slug detection works for most Greenhouse/Lever/Ashby setups but can fail on heavily customized embeds.

---

## Stack

Python, Claude API (Haiku), httpx, BeautifulSoup, Greenhouse/Lever/Ashby public APIs
