#!/usr/bin/env python3
"""Personal job tracker: polls company ATS boards, pushes NEW matches to ntfy."""
import argparse, json, os, re, sys, time
import requests, yaml

UA = {"User-Agent": "Mozilla/5.0 (personal job tracker)"}
TIMEOUT = 30
STATE_FILE = "seen.json"
MAX_PAGES = 100
FAIL_ALERT_AFTER = 3

WD_RE = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([A-Za-z0-9_-]+)")
GH_RE = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")
LV_RE = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")
AB_RE = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")


def http(method, url, **kw):
    last = None
    for i in range(3):
        try:
            r = requests.request(method, url, headers=UA, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # retry on any network/HTTP error
            last = e
            time.sleep(2 * (i + 1))
    raise last


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


def fetch_workday(tenant, wd, site):
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    jobs, offset, total = [], 0, None
    for _ in range(MAX_PAGES):
        d = http("POST", api, json={"appliedFacets": {}, "limit": 20,
                                    "offset": offset, "searchText": ""}).json()
        if total is None:
            total = d.get("total", 0)  # Workday only reliably returns total on page 1
        page = d.get("jobPostings", [])
        if not page:
            break
        for p in page:
            path = p.get("externalPath", "")
            jobs.append({"id": path, "title": p.get("title", ""),
                         "location": p.get("locationsText", ""),
                         "url": f"{base}/{site}{path}"})
        offset += len(page)
        if offset >= total:
            break
        time.sleep(0.5)
    return jobs


def fetch_greenhouse(slug):
    d = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
    return [{"id": str(j["id"]), "title": j.get("title", ""),
             "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url", "")} for j in d.get("jobs", [])]


def fetch_lever(slug):
    d = http("GET", f"https://api.lever.co/v0/postings/{slug}?mode=json").json()
    return [{"id": j["id"], "title": j.get("text", ""),
             "location": (j.get("categories") or {}).get("location", ""),
             "url": j.get("hostedUrl", "")} for j in d]


def fetch_ashby(slug):
    d = http("GET", f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
    return [{"id": j["id"], "title": j.get("title", ""),
             "location": j.get("location", "") or "",
             "url": j.get("jobUrl", "")} for j in d.get("jobs", [])]


FETCHERS = {"workday": fetch_workday, "greenhouse": fetch_greenhouse,
            "lever": fetch_lever, "ashby": fetch_ashby}


def word_re(w):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(w.strip()) + r"(?![A-Za-z0-9])", re.I)


def make_matcher(cfg):
    roles = [word_re(k) for k in cfg["role_keywords"]]
    excl = [word_re(k) for k in cfg.get("exclude_keywords", [])]
    locs = [l.strip().lower() for l in cfg["locations"]]

    def match(job):
        t = job["title"]
        if not any(r.search(t) for r in roles) or any(e.search(t) for e in excl):
            return None
        ll = (job["location"] or "").strip().lower()
        if not ll or re.fullmatch(r"\d+\s+locations?", ll):
            return "location unclear"
        return "ok" if any(l in ll for l in locs) else None
    return match


def ntfy(topic, title, body, click=None, dry=False):
    if dry or not topic:
        print(f"[DRY/NO-TOPIC] {title}\n{body}\n")
        return
    chunks, cur = [], ""
    for line in body.split("\n"):
        if len((cur + line).encode()) > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for i, c in enumerate(chunks):
        h = {"Title": (title + (f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else "")).encode("ascii", "ignore").decode(),
             "Priority": "4", "Tags": "briefcase"}
        if click:
            h["Click"] = click
        requests.post(f"https://ntfy.sh/{topic}", data=c.encode("utf-8"), headers=h, timeout=TIMEOUT)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)


def resolve(co):
    for key in ("board_url", "careers_url"):
        ats, ident = parse_board(co.get(key) or "")
        if ats:
            return ats, ident
    return None, None


def run(cfg, topic, dry):
    state, match = load_state(), make_matcher(cfg)
    for co in cfg["companies"]:
        name = co["name"]
        st = state.setdefault(name, {"seen": None, "fails": 0, "last_total": 0})
        ats, ident = resolve(co)
        try:
            if not ats:
                raise RuntimeError("no ATS board_url configured (run detect mode)")
            jobs = FETCHERS[ats](*ident)
            if not jobs and st["last_total"] > 0:
                raise RuntimeError("board returned 0 jobs (was >0), likely blocked/changed")
        except Exception as e:
            st["fails"] += 1
            print(f"[{name}] FAILED ({st['fails']}): {e}")
            if st["fails"] == FAIL_ALERT_AFTER:
                ntfy(topic, f"Tracker broken: {name}",
                     f"{FAIL_ALERT_AFTER} failed checks in a row: {e}", dry=dry)
            continue
        st["fails"], st["last_total"] = 0, len(jobs)
        hits = [(j, t) for j in jobs if (t := match(j))]
        first = st["seen"] is None
        seen = set(st["seen"] or [])
        new = [(j, t) for j, t in hits if j["id"] not in seen]
        print(f"[{name}] {ats}: {len(jobs)} jobs, {len(hits)} match, {len(new)} new")
        if new:
            lines = [f"- {j['title']} | {j['location'] or 'n/a'}"
                     + (" [location unclear]" if t != "ok" else "") + f"\n  {j['url']}"
                     for j, t in new]
            head = (f"{name}: tracking started, {len(new)} current match(es)" if first
                    else f"{name}: {len(new)} new match(es)")
            ntfy(topic, head, "\n".join(lines),
                 click=new[0][0]["url"] if len(new) == 1 else None, dry=dry)
        st["seen"] = sorted(seen | {j["id"] for j, _ in hits})
    save_state(state)


def detect(cfg):
    for co in cfg["companies"]:
        name = co["name"]
        ats, ident = resolve(co)
        if ats:
            print(f"[{name}] OK -> {ats} {ident}")
            continue
        url = co.get("careers_url")
        try:
            r = http("GET", url)
            ats, ident = parse_board(r.url + "\n" + r.text)
        except Exception as e:
            print(f"[{name}] could not open {url}: {e}")
            continue
        if ats:
            print(f"[{name}] FOUND {ats} {ident} -> put the board URL in config.yaml board_url")
        else:
            print(f"[{name}] no known ATS in static HTML (page likely JS-rendered or custom). "
                  f"Open the job list in a browser, DevTools > Network, look for myworkdayjobs/greenhouse/lever/ashby.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["run", "detect", "notify-test"], default="run")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    topic = os.environ.get("NTFY_TOPIC", "")
    if a.mode == "detect":
        detect(cfg)
    elif a.mode == "notify-test":
        if not topic:
            sys.exit("NTFY_TOPIC secret not set")
        ntfy(topic, "Job tracker test", "If you see this, alerts work.")
    else:
        run(cfg, topic, a.dry_run)
