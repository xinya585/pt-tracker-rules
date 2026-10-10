#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regenerate the PT rule lists from PT监护室 (savept.icu) + sites_map.csv.

sites_map.csv is the authoritative domain classification table:
  domain,group,status,name
    group  : domestic | foreign
    status : online | down | offline | unknown   (auto-updated from savept.icu)
    name   : site display name (filled when monitored)

Behavior:
  1. Fetch https://savept.icu/ and parse all site cards (current domain/status/type).
  2. Merge into sites_map.csv:
       - known domains: keep group, refresh status/name from live data
       - new monitored domains: add with heuristic group (内站->domestic, 外站->foreign)
       - unmonitored domains already in the map: kept as-is (coverage supplement)
  3. Regenerate pt_domestic.list / pt_foreign.list from the merged map.
  4. Refresh the snapshot date + rule counts in README.md.
  5. Only write a file when its content actually changed (avoids noisy commits).

Run with a working internet connection. Stdlib only.
"""
import csv
import io
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_CSV = os.path.join(REPO, "sites_map.csv")
DOM_LIST = os.path.join(REPO, "pt_domestic.list")
FOR_LIST = os.path.join(REPO, "pt_foreign.list")
README = os.path.join(REPO, "README.md")
SRC_URL = "https://savept.icu/"
UA = "Mozilla/5.0 (compatible; pt-site-rulers updater)"

STATUS_LABEL = {"online": "", "down": " # [病危]", "offline": " # [已关站]", "unknown": " # [状态未知]"}

HEADER = """# PT 站点域名规则集 - {title}
# 数据来源: PT监护室(savept.icu) {date} 快照 + 社区整理
# 状态说明: [病危]=监控异常暂不可达  [已关站]=已关站/死亡  [状态未知]=未监测到
# 如需直连国内站点/避免国外站点IP变动触发风控, 可整组指向直连
"""


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_sites(html):
    """domain -> {'status','type','name'}"""
    sites = {}
    pat = re.compile(
        r'<a href="https://([^"]+)"[^>]*class="card-link"[^>]*data-status="([^"]+)"'
        r'[^>]*data-type="([^"]+)"'
    )
    for m in pat.finditer(html):
        raw = m.group(1)
        try:
            host = urllib.parse.urlsplit("https://" + raw).hostname
        except ValueError:
            host = raw
        domain = (host or raw).rstrip("/").rstrip(".")
        if not domain:
            continue
        seg = html[m.end():m.end() + 2500]
        nm = re.search(r'card-title">\s*(.*?)\s*</div>', seg)
        sites[domain] = {
            "status": m.group(2),
            "type": m.group(3),
            "name": nm.group(1).strip() if nm else "",
        }
    return sites


def load_map(path):
    rows = {}
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows[r["domain"]] = {
                "group": (r.get("group") or "").strip(),
                "status": (r.get("status") or "").strip(),
                "name": (r.get("name") or "").strip(),
            }
    return rows


def norm(d):
    return d[4:] if d.startswith("www.") else d


def heuristic_group(site):
    return "domestic" if site.get("type") != "external" else "foreign"


def merge(mapped, live):
    live_norm = {}
    for d, v in live.items():
        live_norm.setdefault(norm(d), []).append((d, v))

    refreshed = 0
    for d, v in mapped.items():
        hits = live_norm.get(norm(d))
        if hits:
            cur = hits[0][1]
            if v["status"] != cur["status"] or v["name"] != cur["name"]:
                v["status"], v["name"] = cur["status"], cur["name"]
                refreshed += 1
    added = 0
    for d, v in live.items():
        key = next((k for k in mapped if norm(k) == norm(d)), None)
        if key is None:
            mapped[d] = {"group": heuristic_group(v), "status": v["status"], "name": v["name"]}
            added += 1
    return refreshed, added


def write_map(path, mapped):
    ordered = sorted(mapped.items(), key=lambda kv: (0 if kv[1]["group"] == "domestic" else 1, kv[0]))
    buf = io.StringIO()
    buf.write("domain,group,status,name\n")
    for d, v in ordered:
        buf.write("%s,%s,%s,%s\n" % (d, v["group"], v["status"], v["name"].replace(",", " ")))
    _write_if_changed(path, buf.getvalue())


def build_list(title, date_str, group, mapped):
    out = io.StringIO()
    out.write(HEADER.format(title=title, date=date_str))
    for d, v in sorted((k, v) for k, v in mapped.items() if v["group"] == group):
        out.write("DOMAIN-SUFFIX,%s%s\n" % (d, STATUS_LABEL.get(v["status"], "")))
    return out.getvalue()


def _write_if_changed(path, text):
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old != text:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print("  updated:", os.path.basename(path))
        return True
    print("  unchanged:", os.path.basename(path))
    return False


def update_readme(date_str, n_dom, n_for):
    if not os.path.exists(README):
        return
    with open(README, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"快照：`[0-9]{4}-[0-9]{2}-[0-9]{2}`", "快照：`%s`" % date_str, text)
    text = re.sub(r"\| `pt_domestic\.list` \| 国内 PT 站点 \| \d+ \|", "| `pt_domestic.list` | 国内 PT 站点 | %d |" % n_dom, text)
    text = re.sub(r"\| `pt_foreign\.list` \| 国外 / 国际 PT 站点 \| \d+ \|", "| `pt_foreign.list` | 国外 / 国际 PT 站点 | %d |" % n_for, text)
    total = n_dom + n_for
    text = re.sub(r"\| \*\*合计\*\* \| \| \*\*\d+\*\* \|", "| **合计** | | **%d** |" % total, text)
    _write_if_changed(README, text)


def main():
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print("Fetching %s ..." % SRC_URL)
    try:
        html = fetch(SRC_URL)
    except Exception as e:  # network / parse failure -> keep last data
        print("FETCH FAILED, keeping existing lists. (%s)" % e)
        return 1

    live = parse_sites(html)
    mapped = load_map(MAP_CSV)
    print("live sites from savept.icu:", len(live), "| mapped rows:", len(mapped))
    if not mapped:
        print("sites_map.csv missing/empty -> generating from live data")
        for d, v in live.items():
            mapped[d] = {"group": heuristic_group(v), "status": v["status"], "name": v["name"]}
    else:
        refreshed, added = merge(mapped, live)
        print("refreshed %d rows; added %d new sites" % (refreshed, added))

    write_map(MAP_CSV, mapped)

    n_dom = len([v for v in mapped.values() if v["group"] == "domestic"])
    n_for = len([v for v in mapped.values() if v["group"] == "foreign"])
    dom = build_list("国内 PT 站点", date_str, "domestic", mapped)
    for_ = build_list("国外/国际 PT 站点", date_str, "foreign", mapped)
    _write_if_changed(DOM_LIST, dom)
    _write_if_changed(FOR_LIST, for_)
    update_readme(date_str, n_dom, n_for)
    print("domestic=%d foreign=%d total=%d" % (n_dom, n_for, n_dom + n_for))
    return 0


if __name__ == "__main__":
    sys.exit(main())
