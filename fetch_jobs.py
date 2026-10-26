#!/usr/bin/env python3
"""Daily job collector: boards (LinkedIn/Indeed/...) + company ATS pages -> jobs.json."""
import argparse, hashlib, json, math, os, re, sys, time
from datetime import date, datetime, timedelta, timezone

import requests
import yaml

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
TIMEOUT = 30
MAX_PAGES = 50
IST = timezone(timedelta(hours=5, minutes=30))
KEEP_DAYS = 21          # drop a job if not seen for this long
SOURCE_RANK = {"company": 0, "linkedin": 1, "naukri": 2, "indeed": 3, "glassdoor": 4, "google": 5}

WD_RE = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([A-Za-z0-9_-]+)")
GH_RE = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")
LV_RE = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")
AB_RE = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def today():
    return datetime.now(IST).date()


# ---------- helpers ----------
def http(method, url, **kw):
    last = None
    headers = {**UA, **kw.pop("headers", {})}
    for i in range(3):
        try:
            r = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def clean(v):
    """Turn None/NaN/NaT into '' and everything else into a stripped string."""
    if v is None:
        return ""
    try:
        if isinstance(v, float) and math.isnan(v):
            return ""
    except Exception:
        pass
    s = str(v).strip()
    return "" if s.lower() in ("nan", "nat", "none", "null") else s


def to_date(v):
    """Best-effort conversion to an ISO date string ('' if impossible)."""
    s = clean(v)
    if not s:
        return ""
    if re.fullmatch(r"\d{10,13}", s):                       # epoch seconds / ms
        n = int(s)
        n = n / 1000 if len(s) >= 13 else n
        return datetime.fromtimestamp(n, timezone.utc).date().isoformat()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else ""


def workday_posted(text):
    """'Posted 3 Days Ago' -> ISO date."""
    t = clean(text).lower()
    if "today" in t:
        return today().isoformat()
    if "yesterday" in t:
        return (today() - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*days?", t)
    if m:
        return (today() - timedelta(days=int(m.group(1)))).isoformat()
    return ""


def parse_board(text):
    m = WD_RE.search(text)
    if m:
        return "workday", m.groups()
    m = GH_RE.search(text)
    if m and m.group(1) != "embed":
        return "greenhouse", (m.group(1),)
    m = LV_RE.search(text)
    if m:
        return "lever", (m.group(1),)
    m = AB_RE.search(text)
    if m:
        return "ashby", (m.group(1),)
    return None, None


# ---------- company ATS fetchers ----------
def fetch_workday(tenant, wd, site):
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    jobs, offset, total = [], 0, None
    for _ in range(MAX_PAGES):
        d = http("POST", api, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}).json()
        if total is None:
            total = d.get("total", 0)
        page = d.get("jobPostings", [])
        if not page:
            break
        for p in page:
            path = p.get("externalPath", "")
            jobs.append({"title": p.get("title", ""), "location": p.get("locationsText", ""),
                         "url": f"{base}/{site}{path}", "posted": workday_posted(p.get("postedOn", ""))})
        offset += len(page)
        if offset >= total:
            break
        time.sleep(0.4)
    return jobs


def fetch_greenhouse(slug):
    d = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
    return [{"title": j.get("title", ""), "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url", ""),
             "posted": to_date(j.get("first_published") or j.get("updated_at"))} for j in d.get("jobs", [])]


def fetch_lever(slug):
    d = http("GET", f"https://api.lever.co/v0/postings/{slug}?mode=json").json()
    return [{"title": j.get("text", ""), "location": (j.get("categories") or {}).get("location", "") or "",
             "url": j.get("hostedUrl", ""), "posted": to_date(j.get("createdAt"))} for j in d]


def fetch_ashby(slug):
    d = http("GET", f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
    return [{"title": j.get("title", ""), "location": j.get("location", "") or "",
             "url": j.get("jobUrl", ""), "posted": to_date(j.get("publishedAt"))} for j in d.get("jobs", [])]


ATS = {"workday": fetch_workday, "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def collect_ats(cfg):
    out, status = [], []
    for co in cfg.get("companies") or []:
        name, url = co.get("name", "?"), co.get("url", "")
        kind, args = parse_board(url)
        if not kind:
            status.append({"source": name, "ok": False, "count": 0, "error": "unrecognized careers URL"})
            continue
        try:
            rows = ATS[kind](*args)
            for r in rows:
                r.update(company=name, source="company", remote=False)
            out += rows
            status.append({"source": name, "ok": True, "count": len(rows), "error": ""})
        except Exception as e:
            status.append({"source": name, "ok": False, "count": 0, "error": str(e)[:150]})
            log(f"[{name}] failed: {e}")
    return out, status


# ---------- job boards via python-jobspy ----------
def df_to_rows(df, site):
    rows = []
    if df is None or len(df) == 0:
        return rows
    cols = set(df.columns)
    ccol = "company" if "company" in cols else ("company_name" if "company_name" in cols else None)
    for r in df.to_dict("records"):
        url = clean(r.get("job_url")) or clean(r.get("job_url_direct"))
        title = clean(r.get("title"))
        if not title or not url:
            continue
        loc = clean(r.get("location"))
        rem = r.get("is_remote")
        rows.append({"title": title, "company": clean(r.get(ccol)) if ccol else "",
                     "location": loc, "url": url, "posted": to_date(r.get("date_posted")),
                     "source": clean(r.get("site")) or site,
                     "remote": rem is True or clean(rem).lower() == "true"})
    return rows


def collect_boards(cfg, scrape=None):
    if scrape is None:
        from jobspy import scrape_jobs as scrape
    out, status = [], []
    loc = cfg.get("search_location", "India")
    n = int(cfg.get("results_per_search", 25))
    hours = int(cfg.get("max_age_days", 14)) * 24
    for site in cfg.get("boards") or []:
        got, err = 0, ""
        for term in cfg.get("searches") or []:
            kw = dict(site_name=[site], search_term=term, location=loc, results_wanted=n,
                      hours_old=hours, country_indeed="India", verbose=0)
            if site == "google":
                kw["google_search_term"] = f"{term} jobs near {loc} since last month"
            try:
                rows = df_to_rows(scrape(**kw), site)
                got += len(rows)
                out += rows
            except Exception as e:
                err = str(e)[:150]
                log(f"[{site}/{term}] failed: {e}")
            time.sleep(1.5)
        status.append({"source": site, "ok": got > 0, "count": got, "error": "" if got else (err or "no results")})
    return out, status


# ---------- filtering & scoring ----------
def rx(patterns):
    return [re.compile(p, re.I) for p in (patterns or [])]


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def job_id(company, title, location):
    city = norm(location.split(",")[0]) if location else ""
    return hashlib.sha1(f"{norm(company)}|{norm(title)}|{city}".encode()).hexdigest()[:12]


def score_job(j, cfg, strong, good, senior, prio, locs, first_seen):
    t, loc, co = j["title"], j["location"], j["company"]
    s = 0
    hits = sum(1 for p in strong if p.search(t))
    if hits:
        s += 40 + min(hits - 1, 2) * 10
    elif any(p.search(t) for p in good):
        s += 20
    if any(p.search(t) for p in senior):
        s += 8
    low = (loc + " " + t).lower()
    if "bengaluru" in low or "bangalore" in low:
        s += 20
    elif j.get("remote") or "remote" in low:
        s += 12
    elif "india" in low:
        s += 5
    ref = j.get("posted") or first_seen
    try:
        age = (today() - date.fromisoformat(ref)).days
    except Exception:
        age = 7
    s += 15 if age <= 1 else 10 if age <= 3 else 5 if age <= 7 else 0
    if any(p.search(co) for p in prio):
        s += 12
    return max(0, min(100, s))


def process(raw, cfg, prev_jobs):
    strong, good = rx(cfg.get("strong_keywords")), rx(cfg.get("good_keywords"))
    excl, senior = rx(cfg.get("exclude_title")), rx(cfg.get("seniority_good"))
    locs = rx(cfg.get("locations"))
    prio = [re.compile(r"\b" + re.escape(c) + r"\b", re.I) for c in (cfg.get("priority_companies") or [])]
    max_age = int(cfg.get("max_age_days", 14))
    t = today()
    prev = {j["id"]: j for j in prev_jobs}
    found = {}
    for j in raw:
        j["title"], j["company"], j["location"] = clean(j["title"]), clean(j.get("company")), clean(j.get("location"))
        if not j["title"] or not j["url"]:
            continue
        if not (any(p.search(j["title"]) for p in strong) or any(p.search(j["title"]) for p in good)):
            continue
        if any(p.search(j["title"]) for p in excl):
            continue
        if locs and j["location"] and not any(p.search(j["location"]) for p in locs) and not j.get("remote"):
            continue
        if j.get("posted"):
            try:
                if (t - date.fromisoformat(j["posted"])).days > max_age:
                    continue
            except ValueError:
                j["posted"] = ""
        jid = job_id(j["company"], j["title"], j["location"])
        if jid in found:
            f = found[jid]
            if j["source"] not in f["sources"]:
                f["sources"].append(j["source"])
            if SOURCE_RANK.get(j["source"], 9) < SOURCE_RANK.get(f["source"], 9):
                f.update(url=j["url"], source=j["source"])
            if not f["posted"] and j.get("posted"):
                f["posted"] = j["posted"]
            continue
        found[jid] = {"id": jid, "title": j["title"], "company": j["company"], "location": j["location"],
                      "url": j["url"], "source": j["source"], "sources": [j["source"]],
                      "posted": j.get("posted", ""), "remote": bool(j.get("remote")) or "remote" in j["location"].lower()}
    now = t.isoformat()
    for jid, j in found.items():
        old = prev.get(jid)
        j["first_seen"] = old["first_seen"] if old else now
        j["last_seen"] = now
    # keep recently seen jobs that weren't fetched today (e.g. a source was blocked today)
    for jid, old in prev.items():
        if jid in found:
            continue
        try:
            if (t - date.fromisoformat(old["last_seen"])).days <= KEEP_DAYS:
                found[jid] = old
        except Exception:
            pass
    for j in found.values():
        j["score"] = score_job(j, cfg, strong, good, senior, prio, locs, j["first_seen"])
        j["new"] = j["first_seen"] == now
    jobs = sorted(found.values(), key=lambda j: (-(j["score"] + (5 if j["new"] else 0)), j.get("posted") or "", j["title"]))
    return jobs


def load_prev(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("jobs", []) if isinstance(d, dict) else []
    except Exception:
        return []


def main(argv=None, scrape=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-o", "--out", default="jobs.json")
    ap.add_argument("--no-boards", action="store_true")
    ap.add_argument("--no-ats", action="store_true")
    a = ap.parse_args(argv)
    with open(a.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    raw, status = [], []
    if not a.no_ats:
        r, s = collect_ats(cfg)
        raw += r
        status += s
    if not a.no_boards:
        try:
            r, s = collect_boards(cfg, scrape)
            raw += r
            status += s
        except Exception as e:  # e.g. jobspy not installed
            status.append({"source": "boards", "ok": False, "count": 0, "error": str(e)[:150]})
            log(f"boards failed: {e}")
    jobs = process(raw, cfg, load_prev(a.out))
    out = {"updated": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"), "today": today().isoformat(),
           "total": len(jobs), "status": status, "jobs": jobs[:600]}
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, a.out)
    log(f"fetched {len(raw)} raw -> {len(jobs)} kept; wrote {a.out}")
    return out


if __name__ == "__main__":
    main()
