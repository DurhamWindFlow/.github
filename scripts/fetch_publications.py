#!/usr/bin/env python3
"""Find journal articles by group members that are not yet in publications.yml.

Reads every ORCID listed in people.yml, asks ORCID what each person has
published, drops anything already recorded, then fills in the gaps from
Crossref (which has fuller author lists and volume/article numbers than ORCID
summaries do).

Scope is a judgement call this script cannot make: the page is for wind-energy
and atmospheric-flow work, and members publish outside that. So the sweep sorts
what it finds by a keyword screen and writes to two files:

  publications.yml           new papers that look in scope, for review
  publications-excluded.yml  the rest, recorded so they are never re-proposed

Both files count as "seen". That matters: without the exclusion list, every
paper you declined would come back in next week's pull request forever. Moving
a DOI between the two files is how you overrule the screen in either direction.

Usage:
    python scripts/fetch_publications.py            # write changes
    python scripts/fetch_publications.py --dry-run  # report only
    python scripts/fetch_publications.py --summary-file summary.md
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "Profile" / "data"
PEOPLE_FILE = DATA / "people.yml"
PUBS_FILE = DATA / "publications.yml"
EXCLUDED_FILE = DATA / "publications-excluded.yml"

REVIEW_MARKER = "# --- FETCHED, PENDING REVIEW ---"

EXCLUDED_HEADER = """# ---------------------------------------------------------------------------
# Publications by group members deliberately NOT shown on the website.
#
# The sweep in scripts/fetch_publications.py writes here anything it judges out
# of scope for a wind-energy page. Its purpose is memory: a DOI listed here is
# never proposed again. Delete an entry and the next sweep will re-propose it.
#
# To publish something listed here, move it into publications.yml instead.
# ---------------------------------------------------------------------------
"""

# A polite identifier gets us Crossref's faster pool and is required by ORCID.
UA = "DurhamWindFlow-publication-sync (+https://durhamwindflow.github.io)"

# Words that make a paper plausibly in scope for this group's page. Used only
# to sort the review list into "likely relevant" and "check scope" — nothing is
# discarded on the strength of a keyword.
RELEVANT = (
    r"wind|turbine|wake|rotor|atmospheric|boundary.layer|offshore|yaw|"
    r"actuator.disk|aerodynamic|turbulen|geostrophic|stratified|renewable|"
    r"\baep\b|\babl\b"
)

# Rough theme guesses, checked in order; first match wins.
#
# `farm-aero` is last on purpose. It is the group's broad core theme, so its
# keywords ("wake", "wind farm", "turbine") match almost everything in scope;
# putting it above the narrower themes would swallow them. Anything that is
# not specifically about control, floating platforms or energy systems lands
# there, which is the right default.
THEME_HINTS = [
    ("floating",  ("floating", "semi-submersible", "six degrees", "platform motion")),
    ("control",   ("yaw", "wake steering", "flow control", "flap", "actuation", "control")),
    ("systems",   ("scheduling", "risk", "economic", "market", "reserve", "project",
                   "cost", "layout optimi", "aep", "resilience", "construction")),
    ("farm-aero", ("wake", "blockage", "actuator disk", "analytical model", "wind farm",
                   "turbine", "atmospheric boundary layer", "geostrophic", "stratified",
                   "inertia-gravity", "lagrangian mean", "wind tunnel", "experimental",
                   "miniature", "porous disc", "piv")),
]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def get_json(url: str, *, tries: int = 3) -> dict | None:
    """GET a JSON document, tolerating the occasional flaky response."""
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": UA}
    )
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if attempt == tries:
                print(f"  ! HTTP {exc.code} for {url}", file=sys.stderr)
                return None
        except Exception as exc:  # network hiccup, malformed JSON
            if attempt == tries:
                print(f"  ! {type(exc).__name__} for {url}", file=sys.stderr)
                return None
        time.sleep(2 * attempt)
    return None


# --------------------------------------------------------------------------
# Normalising and matching
# --------------------------------------------------------------------------

def norm_doi(doi: str | None) -> str:
    """Reduce a DOI to a comparable form, however it was written down."""
    if not doi:
        return ""
    doi = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi.strip("/")


def guess_theme(title: str, venue: str) -> str:
    blob = f"{title} {venue}".lower()
    for theme, words in THEME_HINTS:
        if any(w in blob for w in words):
            return theme
    return ""


def looks_relevant(title: str, venue: str) -> bool:
    """Keyword screen, on whole words.

    Substring matching was a trap here: a bare "abl" matches "Sustainable", and
    put a paper on office safety in the relevant pile.
    """
    return re.search(RELEVANT, f"{title} {venue}".lower()) is not None


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def orcid_dois(orcid_id: str) -> set[str]:
    """Every DOI ORCID knows about for one person, journal articles only."""
    payload = get_json(f"https://pub.orcid.org/v3.0/{orcid_id}/works")
    if not payload:
        return set()

    found: set[str] = set()
    for group in payload.get("group", []):
        summaries = group.get("work-summary") or []
        if not summaries:
            continue
        if summaries[0].get("type") != "journal-article":
            continue
        # A group is one work; its DOI may sit on any of the merged summaries.
        for summary in summaries:
            ids = ((summary.get("external-ids") or {}).get("external-id")) or []
            for ext in ids:
                if ext.get("external-id-type") == "doi":
                    doi = norm_doi(ext.get("external-id-value"))
                    if doi:
                        found.add(doi)
                        break
    return found


def crossref_entry(doi: str) -> dict | None:
    """Build a publications.yml entry from Crossref's record for a DOI."""
    payload = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}")
    if not payload:
        return None
    msg = payload.get("message") or {}

    if msg.get("type") != "journal-article":
        return None

    titles = msg.get("title") or []
    if not titles:
        return None
    title = " ".join(titles[0].split())

    # "Bastankhah, M., Hydon, P., & Meneveau, C." — matching the file's style.
    names = []
    for author in msg.get("author") or []:
        family = (author.get("family") or "").strip()
        if not family:
            continue
        initials = " ".join(
            f"{part[0]}." for part in (author.get("given") or "").split() if part
        )
        names.append(f"{family}, {initials}".strip().rstrip(","))
    if len(names) > 1:
        authors = ", ".join(names[:-1]) + f", & {names[-1]}"
    else:
        authors = names[0] if names else ""

    container = (msg.get("container-title") or [""])[0]
    venue_bits = [container] if container else []
    if msg.get("volume"):
        venue_bits.append(str(msg["volume"]))
    if msg.get("article-number"):
        venue_bits.append(str(msg["article-number"]))
    elif msg.get("page"):
        venue_bits.append(str(msg["page"]))
    venue = ", ".join(venue_bits)

    issued = ((msg.get("issued") or {}).get("date-parts") or [[None]])[0]
    year = issued[0] if issued and issued[0] else None
    if not year:
        return None

    return {
        "year": int(year),
        "title": title,
        "authors": authors,
        "venue": venue,
        "doi": f"https://doi.org/{norm_doi(doi)}",
        "theme": guess_theme(title, venue),
        "_relevant": looks_relevant(title, venue),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def yaml_str(value: str) -> str:
    """Double-quoted scalar, safe for colons and quotes in titles."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_entry(entry: dict, people: str) -> str:
    lines = [
        f"- year: {entry['year']}",
        f"  title: {yaml_str(entry['title'])}",
        f"  authors: {yaml_str(entry['authors'])}",
        f"  venue: {yaml_str(entry['venue'])}",
        f"  doi: {entry['doi']}",
    ]
    if entry["theme"]:
        lines.append(f"  theme: {entry['theme']}    # guessed — confirm")
    else:
        lines.append("  # theme: <key from site.yml themes>  # no confident guess")
    if not entry["_relevant"]:
        lines.append(
            "  # CHECK SCOPE: no wind/atmospheric-flow keywords in the title or"
        )
        lines.append(
            "  # venue. Delete this entry if it belongs on a personal page instead."
        )
    lines.append(f"  # via ORCID: {people}")
    return "\n".join(lines)


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary-file", type=pathlib.Path)
    args = parser.parse_args()

    people = yaml.safe_load(PEOPLE_FILE.read_text(encoding="utf-8"))
    pubs_text = PUBS_FILE.read_text(encoding="utf-8")
    existing = yaml.safe_load(pubs_text) or []

    excluded_text = (
        EXCLUDED_FILE.read_text(encoding="utf-8") if EXCLUDED_FILE.exists() else ""
    )
    excluded = yaml.safe_load(excluded_text) or [] if excluded_text else []

    # Anything already decided on, either way, is not news.
    known = {norm_doi(p.get("doi")) for p in existing}
    known |= {norm_doi(p.get("doi")) for p in excluded}
    known.discard("")

    # Who to sweep, and who owns each DOI we turn up.
    owners: dict[str, list[str]] = {}
    swept = 0
    for person in people:
        orcid_url = (person.get("links") or {}).get("orcid")
        if not orcid_url:
            continue
        orcid_id = orcid_url.rstrip("/").split("/")[-1]
        swept += 1
        dois = orcid_dois(orcid_id)
        print(f"  {person['name']}: {len(dois)} journal articles on ORCID")
        for doi in dois:
            owners.setdefault(doi, []).append(person["name"])

    if not swept:
        print("No ORCIDs in people.yml — nothing to sweep.", file=sys.stderr)
        return 1

    new_dois = sorted(set(owners) - known)
    print(f"\n{len(owners)} distinct DOIs found, {len(known)} already recorded, "
          f"{len(new_dois)} new.")

    entries = []
    for doi in new_dois:
        entry = crossref_entry(doi)
        if entry is None:
            print(f"  - skipped {doi} (not a journal article, or no Crossref record)")
            continue
        entry["_people"] = ", ".join(owners[doi])
        entries.append(entry)
        time.sleep(0.3)  # stay friendly to Crossref

    if not entries:
        print("\nNothing new to add.")
        if args.summary_file:
            args.summary_file.write_text(
                "No new publications found on ORCID.\n", encoding="utf-8"
            )
        return 0

    entries.sort(key=lambda e: (-e["year"], e["title"]))
    relevant = [e for e in entries if e["_relevant"]]
    check = [e for e in entries if not e["_relevant"]]

    if not args.dry_run:
        if relevant:
            block = [
                "",
                REVIEW_MARKER,
                "# Added automatically from ORCID. Confirm the guessed themes, then",
                "# remove this marker. Anything that does not belong on a wind-energy",
                "# page should be moved to publications-excluded.yml, not just deleted,",
                "# or the next sweep will propose it again.",
                "",
            ]
            for entry in relevant:
                block.append(render_entry(entry, entry["_people"]))
                block.append("")
            PUBS_FILE.write_text(
                pubs_text.rstrip("\n") + "\n" + "\n".join(block), encoding="utf-8"
            )
            reparsed = yaml.safe_load(PUBS_FILE.read_text(encoding="utf-8"))
            assert len(reparsed) == len(existing) + len(relevant), "entry count mismatch"
            print(f"\nAppended {len(relevant)} entries to {PUBS_FILE.name}.")

        if check:
            header = excluded_text.rstrip("\n") if excluded_text else EXCLUDED_HEADER
            block = [""]
            for entry in check:
                block.append(f"- doi: {entry['doi']}")
                block.append(f"  title: {yaml_str(entry['title'])}")
                block.append(f"  year: {entry['year']}")
                block.append(f"  author: {yaml_str(entry['_people'])}")
                block.append("  reason: no wind or atmospheric-flow keywords")
                block.append("")
            EXCLUDED_FILE.write_text(
                header + "\n" + "\n".join(block), encoding="utf-8"
            )
            reparsed = yaml.safe_load(EXCLUDED_FILE.read_text(encoding="utf-8")) or []
            assert len(reparsed) == len(excluded) + len(check), "exclusion count mismatch"
            print(f"Recorded {len(check)} entries in {EXCLUDED_FILE.name}.")

    lines = [f"Found **{len(entries)}** publication(s) not yet on the site.", ""]
    if relevant:
        lines.append(f"### Likely in scope ({len(relevant)})")
        for e in relevant:
            theme = e["theme"] or "no theme guess"
            lines.append(f"- **{e['year']}** {e['title']}  ")
            lines.append(f"  {e['venue']} · `{theme}` · {e['_people']}")
        lines.append("")
    if check:
        lines.append(f"### Recorded as out of scope ({len(check)})")
        lines.append("No wind or atmospheric-flow keywords, so these went to "
                     "`publications-excluded.yml` and will not be proposed again. "
                     "Move any that do belong into `publications.yml`.")
        for e in check:
            lines.append(f"- **{e['year']}** {e['title']}  ")
            lines.append(f"  {e['venue']} · {e['_people']}")
        lines.append("")
    lines.append(f"Confirm the guessed `theme:` values and remove the "
                 f"`{REVIEW_MARKER}` line before merging. To reject one of the "
                 "entries above, move it to `publications-excluded.yml` rather "
                 "than deleting it, so the next sweep leaves it alone.")
    summary = "\n".join(lines)

    print()
    print(summary)
    if args.summary_file:
        args.summary_file.write_text(summary + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
