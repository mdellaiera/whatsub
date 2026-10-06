#!/usr/bin/env python3
"""
check_deadlines.py — compare the deadlines in venues.json with the official conference pages.

For every conference it opens the pages listed in "watch_urls" (falling back to
"deadline_source"), finds dates written next to words like "deadline", "submission"
or "paper", and reports:

  * ANNOUNCED   an estimated / TBA deadline now has real dates on the official page
  * MISMATCH    a confirmed deadline no longer appears on the page (changed or extended?)
  * PASSED      the deadline in venues.json is over — time to move to the next edition
  * SOON        a deadline is less than 21 days away (reminder)
  * UNREACHABLE the page could not be loaded (often: next edition's site not online yet)

Nothing in venues.json is changed: it writes a Markdown report for a human to act on.

Usage:
  python check_deadlines.py                      # report to stdout
  python check_deadlines.py --out report.md      # also write the report to a file
Exit code: 0 always; the report's first line says whether anything needs attention.
Standard library only (Python 3.9+).
"""
import argparse, datetime as dt, html, json, re, sys, urllib.request, urllib.error

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
YEAR = r"(?:'|’)?(\d{4}|\d{2})"
PATTERNS = [
    # November 16, 2026 | Nov 16 '26 | Nov. 16 2026
    (re.compile(MON + r"\s+" + DAY + r"(?:\s*[-–]\s*\d{1,2})?,?\s+" + YEAR + r"\b", re.I), ("m", "d", "y")),
    # 16 November 2026 | 16 Nov. 2026
    (re.compile(r"\b" + DAY + r"\s+" + MON + r",?\s+" + YEAR + r"\b", re.I), ("d", "m", "y")),
    # 2026-11-16
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("Y", "M", "D")),
]
KEYWORDS = re.compile(r"deadline|submission|submit|paper|abstract|manuscript|due", re.I)
UA = "Mozilla/5.0 (deadline-checker; +https://github.com)"


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(3_000_000)
        charset = r.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def page_text(markup):
    markup = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?s)<[^>]+>", " ", markup)
    return re.sub(r"\s+", " ", html.unescape(markup))


def _to_date(parts, order):
    v = dict(zip(order, parts))
    try:
        if "Y" in v:
            return dt.date(int(v["Y"]), int(v["M"]), int(v["D"]))
        y = int(v["y"]); y = y + 2000 if y < 100 else y
        return dt.date(y, MONTHS[v["m"].lower()[:3]], int(v["d"]))
    except (ValueError, KeyError):
        return None


def deadline_dates(text, window=160):
    """Dates that appear within `window` characters of a deadline-ish keyword."""
    hits = set()
    for rx, order in PATTERNS:
        for m in rx.finditer(text):
            d = _to_date(m.groups(), order)
            if not d or not (2020 <= d.year <= 2035):
                continue
            ctx = text[max(0, m.start() - window): m.end() + 40]
            if KEYWORDS.search(ctx):
                hits.add(d)
    return hits


def check(venue, today, soon_days=21):
    urls = venue.get("watch_urls") or ([venue["deadline_source"]] if venue.get("deadline_source") else [])
    have = venue.get("deadline_date")
    status = venue.get("deadline_status", "tba")
    have_d = dt.date.fromisoformat(have) if have else None
    findings, found, reached = [], set(), []

    for u in urls:
        try:
            found |= deadline_dates(page_text(fetch(u)))
            reached.append(u)
        except urllib.error.HTTPError as e:
            findings.append(("UNREACHABLE", f"{u} → HTTP {e.code}" + (" (page not published yet?)" if e.code == 404 else "")))
        except Exception as e:  # DNS, timeout, TLS…
            findings.append(("UNREACHABLE", f"{u} → {type(e).__name__}: {str(e)[:80]}"))

    upcoming = sorted(d for d in found if d >= today - dt.timedelta(days=7))
    shown = ", ".join(d.isoformat() for d in upcoming[:8]) or "none"

    if have_d and have_d < today:
        findings.append(("PASSED", f"deadline {have} is over — set the next edition's name/date in venues.json "
                                   f"(the page already projects it automatically). Upcoming dates on the page: {shown}"))
    elif status == "confirmed" and reached:
        if have_d in found:
            findings.append(("OK", f"{have} still listed"))
        else:
            findings.append(("MISMATCH", f"{have} not found on the page any more — check for a change or extension. Dates on the page: {shown}"))
    elif status in ("estimated", "tba") and reached and upcoming:
        # For an estimate, only dates within ~4 months of it count; event dates elsewhere on a
        # homepage would otherwise trigger false alarms every week.
        near = [d for d in upcoming if not have_d or abs((d - have_d).days) <= 120]
        if near:
            findings.append(("ANNOUNCED", "official page now shows " + ", ".join(d.isoformat() for d in near[:6]) +
                             f" (venues.json has {have or 'TBA'}, {status}) — confirm and update"))
        else:
            findings.append(("OK", f"no deadline near the estimate yet (other dates on page: {shown})"))
    elif reached:
        findings.append(("OK", "no dates published yet"))

    if have_d and today <= have_d <= today + dt.timedelta(days=soon_days):
        findings.append(("SOON", f"deadline in {(have_d - today).days} days ({have})"))
    # Drop "unreachable" noise when another watched page for the same venue loaded fine
    if reached:
        findings = [f for f in findings if f[0] != "UNREACHABLE"]
    return findings


ORDER = ["ANNOUNCED", "MISMATCH", "PASSED", "SOON", "UNREACHABLE", "OK"]
ICON = {"ANNOUNCED": "🆕", "MISMATCH": "⚠️", "PASSED": "⏭️", "SOON": "⏰", "UNREACHABLE": "🔌", "OK": "✅"}
ACTION = {"ANNOUNCED", "MISMATCH", "PASSED", "SOON"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="venues.json")
    ap.add_argument("--out", help="write the Markdown report here as well")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
    a = ap.parse_args()

    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    data = json.load(open(a.data, encoding="utf-8"))
    rows = []
    for v in data["conferences"]:
        for kind, msg in check(v, today):
            rows.append((ORDER.index(kind), kind, v["name"], msg))
    rows.sort()

    need = sorted({r[2] for r in rows if r[1] in ACTION})
    lines = [("ACTION NEEDED: " + ", ".join(need)) if need else "No action needed.",
             "", f"# Conference deadline check — {today.isoformat()}", "",
             f"Checked {len(data['conferences'])} conferences against their official pages.", ""]
    for kind in ORDER:
        group = [r for r in rows if r[1] == kind]
        if not group:
            continue
        lines += [f"## {ICON[kind]} {kind.title()} ({len(group)})", ""]
        lines += [f"- **{r[2]}** — {r[3]}" for r in group] + [""]
    lines += ["---", "Dates are found automatically next to words like *deadline* or *submission*; "
              "always confirm on the official page before editing `venues.json`."]
    report = "\n".join(lines)
    print(report)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(report + "\n")


if __name__ == "__main__":
    main()
