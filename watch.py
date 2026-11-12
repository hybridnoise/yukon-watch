#!/usr/bin/env python3
"""Yukon challenge watcher (runs free in GitHub Actions every 15 minutes).

Fingerprints only what matters, compares to state.json, and POSTs a short
change note to a Grok Bot webhook routine ONLY when something differs.
Scores / leaderboard numbers are deliberately ignored.

Env: YUKON_WEBHOOK_URL, YUKON_WEBHOOK_KEY (repo secrets), GITHUB_TOKEN (auto).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

STATE = Path(__file__).with_name("state.json")
GH = "https://api.github.com"
ET = ZoneInfo("America/Indianapolis")
QUIET_START, QUIET_END = 0, 8  # midnight-8am ET: hold changes until 8am
FIXTURES = {"mooselumph/toy-benchmark-review"}
CHALLENGE_OWNERS = {"layr-labs", "mooselumph", "holindauer", "eigenlabs",
                    "gumbiidigital", "davidtai", "gpsanant", "proximity-prize",
                    "brandon-eigenlabs"}


def http(url, headers=None, data=None, timeout=40):
    h = {"User-Agent": "yukon-watch/1.0"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def gh(path):
    h = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return json.loads(http(GH + path, h)[1])


def items_of(obj, *keys):
    if isinstance(obj, list):
        return obj
    for k in keys:
        if isinstance(obj.get(k), list):
            return obj[k]
    raise ValueError("unexpected API shape")


# ---- sources: each returns a JSON-able snapshot -------------------------
def src_homepage():
    html = http("https://www.yukon.org/")[1]
    m = re.search(r"Showing\s*(?:<!-- -->)?\s*(\d+)\s*(?:<!-- -->)?\s*of\s*"
                  r"(?:<!-- -->)?\s*(\d+)", html)
    hrefs = sorted(set(re.findall(r'href="(?:https://www\.yukon\.org)?(/[a-z0-9][^"#?]*)"', html)))
    hrefs = [h for h in hrefs if not h.startswith(("/_next", "/api", "/favicon"))
             and not re.search(r"\.(png|jpg|svg|ico|css|js|webp)$", h)]
    return {"showing": f"{m.group(1)} of {m.group(2)}" if m else None, "links": hrefs}


def src_api_challenges():
    c = json.loads(http("https://www.yukon.org/api/challenges")[1])
    return sorted({str(x.get("name") or x.get("slug") or x.get("id"))
                   for x in items_of(c, "challenges", "data")})


def src_api_tracks():
    b = json.loads(http("https://www.yukon.org/api/benchmarks")[1])
    return {str(x.get("name") or x.get("id")): str(x.get("status"))
            for x in items_of(b, "benchmarks", "data")}


def src_layr_repos():
    names, page = [], 1
    while page <= 10:
        batch = gh(f"/orgs/Layr-Labs/repos?type=public&per_page=100&page={page}")
        names += [r["name"] for r in batch]
        if len(batch) < 100:
            break
        page += 1
    if not names:
        raise ValueError("empty repo list")
    return sorted(set(names))


def src_validate_repos():
    repos, ok = {}, 0
    for q in ('is:pr in:title "Validate submission"',
              "is:pr author:app/yukon-eigen", "is:pr author:app/yukon-autoresearch"):
        try:
            d = gh(f"/search/issues?q={urllib.parse.quote(q)}&sort=created&order=desc&per_page=50")
            ok += 1
        except Exception:
            continue
        for x in d.get("items") or []:
            full = (x.get("repository_url") or "").split("/repos/")[-1]
            user = ((x.get("user") or {}).get("login") or "").lower()
            if "validate submission" not in (x.get("title") or "").lower():
                continue
            if not ("yukon" in user or full.split("/")[0].lower() in CHALLENGE_OWNERS):
                continue
            if full.lower() in FIXTURES:
                continue
            repos.setdefault(full.lower(), x.get("html_url"))
    if ok == 0:
        raise ValueError("all PR searches failed")
    return repos  # repo -> sample PR url


def src_org_events():
    out = {}
    for ev in gh("/orgs/Layr-Labs/events?per_page=50"):
        t = ev.get("type")
        if t == "PublicEvent" or (t == "CreateEvent" and (ev.get("payload") or {}).get("ref_type") == "repository"):
            out[str(ev.get("id"))] = {"type": t, "repo": (ev.get("repo") or {}).get("name"),
                                      "at": ev.get("created_at")}
    return out


SOURCES = {"homepage": src_homepage, "api_challenges": src_api_challenges,
           "api_tracks": src_api_tracks, "layr_repos": src_layr_repos,
           "validate_prs": src_validate_repos, "org_events": src_org_events}


# ---- diffing ------------------------------------------------------------
def diff(kind, old, new):
    if old is None:
        return None
    if kind in ("api_challenges", "layr_repos"):
        a, r = sorted(set(new) - set(old)), sorted(set(old) - set(new))
        return {"added": a, "removed": r} if a or r else None
    if kind == "api_tracks":
        added = {k: v for k, v in new.items() if k not in old}
        removed = sorted(k for k in old if k not in new)
        changed = {k: f"{old[k]} -> {v}" for k, v in new.items() if k in old and old[k] != v}
        return ({"added": added, "removed": removed, "status_changed": changed}
                if added or removed or changed else None)
    if kind == "homepage":
        a = sorted(set(new["links"]) - set(old["links"]))
        r = sorted(set(old["links"]) - set(new["links"]))
        if a or r or new["showing"] != old["showing"]:
            return {"links_added": a, "links_removed": r,
                    "showing": f'{old["showing"]} -> {new["showing"]}'}
        return None
    if kind == "validate_prs":  # only NEW repos matter; old set is cumulative
        a = {k: v for k, v in new.items() if k not in old}
        return {"new_repos": a} if a else None
    if kind == "org_events":
        a = {k: v for k, v in new.items() if k not in old}
        return {"new_events": list(a.values())} if a else None


def merge(kind, old, new):
    """What to store. Cumulative sets for PR repos and event ids."""
    if kind == "validate_prs":
        m = dict(old or {}); m.update(new); return m
    if kind == "org_events":
        m = dict(old or {}); m.update(new)
        return dict(sorted(m.items())[-200:])
    return new


def post(note):
    url, key = os.environ.get("YUKON_WEBHOOK_URL"), os.environ.get("YUKON_WEBHOOK_KEY")
    if not url or not key:
        print("webhook secrets missing; would post:", json.dumps(note)[:2000])
        return False
    st, _ = http(url, {"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                       "X-Automation-Key": key}, json.dumps(note).encode(), timeout=15)
    print("webhook status", st)
    return 200 <= st < 300


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    snaps, fails = state.get("snapshots", {}), state.get("fail_streak", {})
    degraded = set(state.get("degraded", []))
    now = dt.datetime.now(ET)
    quiet = QUIET_START <= now.hour < QUIET_END
    force = os.environ.get("YUKON_FORCE_TEST") == "1"

    changes, coverage, new_snaps = [], [], dict(snaps)
    for kind, fn in SOURCES.items():
        try:
            cur = fn()
        except Exception as e:
            fails[kind] = fails.get(kind, 0) + 1
            print(f"{kind} failed ({fails[kind]}x): {e}")
            if fails[kind] >= 2 and kind not in degraded:
                coverage.append({"kind": "coverage_degraded", "source": kind, "error": str(e)[:200]})
                degraded.add(kind)
            continue
        if kind in degraded:
            coverage.append({"kind": "coverage_recovered", "source": kind})
            degraded.discard(kind)
        fails[kind] = 0
        if kind == "validate_prs":  # Layr-Labs repos already in the name list are covered by layr_repos
            known_layr = {f"layr-labs/{n.lower()}" for n in (new_snaps.get("layr_repos") or [])}
            cur = {k: v for k, v in cur.items() if k not in known_layr}
        d = diff(kind, snaps.get(kind), cur)
        if d:
            changes.append({"kind": kind, **d})
        new_snaps[kind] = merge(kind, snaps.get(kind), cur)

    note = None
    if force:
        note = {"source": "yukon-watch", "kind": "test", "at": now.isoformat(), "changes": []}
    elif changes or coverage:
        note = {"source": "yukon-watch", "at": now.isoformat(), "changes": changes + coverage,
                "run": f'{os.environ.get("GITHUB_SERVER_URL","")}/{os.environ.get("GITHUB_REPOSITORY","")}/actions/runs/{os.environ.get("GITHUB_RUN_ID","")}'}

    save = True
    if note and not force:
        if quiet and changes:
            print("quiet hours: holding", json.dumps(changes)[:1500])
            save = False  # keep old snapshot so the diff fires at 8am
        else:
            try:
                if not post(note):
                    save = False
            except Exception as e:
                print("webhook POST failed:", e)
                save = False  # retry next run
    elif force:
        post(note)

    if save:
        state["snapshots"] = new_snaps
    state["fail_streak"], state["degraded"] = fails, sorted(degraded)
    state.pop("last_run_et", None)
    state["heartbeat_date"] = now.strftime("%Y-%m-%d")  # one commit/day keeps the schedule alive
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    print("changes:", len(changes), "coverage:", len(coverage), "saved:", save)


if __name__ == "__main__":
    sys.exit(main())
