#!/usr/bin/env python
"""Diff two vintages of a parcel layer on a key, and refuse a key that cannot carry the diff.

Refuses the key when it is on more than one row of either vintage, flags every
key that now names a different parcel than it did, and refuses to call a run
clean when it compared nothing.

A diff of two vintages pairs rows by a key. If the key is on two rows, a lookup
table keeps one of them and drops the other without a word, so the changes pair
with the wrong parcel and the counts stop adding up. If the key is unique but
was reissued (a split or a merge handed an old number to a new parcel), the
rows pair, the attributes differ, and the report calls it an ordinary edit. The
only evidence is that the parcel moved or changed size, which no diff computes.

The obvious tools already do most of this. A pandas merge with indicator=True
names the side each row came from, and validate="one_to_one" refuses a key that
is not unique. ArcGIS Pro Feature Compare and QGIS Detect dataset changes
compare two layers and name what differs. Their documentation describes no
threshold that tells a re-survey from a different parcel under the same key.
That is the gap this tool fills.

    python parceldiff.py --self-test
    python parceldiff.py old.csv new.csv --key-field PARCEL --skip-reuse-check
    python parceldiff.py old.csv new.csv --key-field PARCEL --x-field X --y-field Y
    python parceldiff.py old.csv new.csv --area-field ACRES --area-ratio 3
    python parceldiff.py old.csv new.csv --key-field BOOK,PAGE --compare-fields OWNER,USE
    python parceldiff.py old.csv new.csv --out changes.csv --apply

Nothing is written without --apply.

Exit codes: 0 a diff was produced, the reuse check ran on enough pairs, and no
key was reissued, 1 at least one key now names a different parcel, 2 a file
could not be read or written, 3 the key was refused (not unique, or blank on
some row), 4 the run could not be trusted (no key on both sides, too few keys
on both sides, a reuse check that measured too few pairs, or no reuse check at
all), 64 usage error, 70 an unexpected crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import contextlib
import csv
import decimal
import importlib.util
import io
import math
import os
import re
import runpy
import shutil
import sys
import tempfile
import traceback

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Centroid shift above which a key is judged to name a different parcel, in the
# linear units of the input CRS. A re-survey moves a centroid by feet. A parcel
# number handed to a different parcel moves it by the distance between two
# parcels. 500 is a starting point and not a measurement: measure the shifts
# between two of your own vintages, then set --reuse-distance.
DEFAULT_REUSE_DISTANCE = 500.0

# Factor by which the area may grow or shrink before a key is judged reissued.
# 2.0 means doubled or halved. A split that keeps the parent's number halves the
# area on purpose; that is flagged on purpose.
DEFAULT_AREA_RATIO = 2.0

# Centroid shift above which a paired parcel counts as a geometry change. Below
# it the move is capture noise.
DEFAULT_MOVE_TOLERANCE = 1.0

# Fraction of the smaller vintage's keys that must be on both sides. Two
# vintages of one layer share nearly every key. Below this the key is probably
# spelled differently on the two sides, and the diff has compared nothing real.
DEFAULT_MIN_OVERLAP = 0.5

# Share of the shared keys that each requested reuse test must have measured.
# A pair with a blank or unreadable coordinate or area was not tested, and a
# reissued key among such pairs is invisible. Below this share the run cannot
# say the keys are sound, so it exits 4. A test that measured no pair always
# fails the run, whatever this is set to.
DEFAULT_MIN_MEASURED = 0.9

DEFAULT_KEY_FIELD = "PARCEL"
DEFAULT_UNITS = "ft"
DEFAULT_SAMPLE = 10

UNCHANGED = "UNCHANGED"
CHANGED = "CHANGED"
REUSED = "REUSED"
ADDED = "ADDED"
REMOVED = "REMOVED"
UNKEYED = "UNKEYED"
CLASSES = (UNCHANGED, CHANGED, REUSED, ADDED, REMOVED, UNKEYED)

OUT_COLUMNS = ("class", "key", "old_line", "new_line", "detail")

GEOMETRY_FIELD = "(geometry)"


# ---------------------------------------------------------------- the core

class Row(object):
    """One CSV row, reduced to what the diff reasons about.

    key is a tuple of trimmed strings, or None when any part of it is blank.
    line is the line of the file the record starts on.
    """

    def __init__(self, line, key, values, x=None, y=None, area=None):
        self.line = line
        self.key = key
        self.values = values
        self.x = x
        self.y = y
        self.area = area


class Entry(object):
    """The class one key landed in, with the rows behind it."""

    def __init__(self, cls, key, old, new, detail=""):
        self.cls = cls
        self.key = key
        self.old = old
        self.new = new
        self.detail = detail


class Report(object):
    """Everything compare() decided. Built once, read by describe() and gate()."""

    def __init__(self):
        self.entries = []
        self.dupes = []          # (side, key label, [lines]) when refused
        self.blanks = []         # (side, line) of rows with no key
        self.pairs = 0
        self.measured = 0        # pairs that every requested test measured
        self.measured_xy = 0     # pairs whose centroid shift could be computed
        self.measured_area = 0   # pairs whose area ratio could be computed
        self.unmeasured = []     # (key label, names of the tests that missed)
        self.old_keys = 0
        self.new_keys = 0
        self.min_overlap = DEFAULT_MIN_OVERLAP
        self.min_measured = DEFAULT_MIN_MEASURED
        self.skip_reuse = False
        self.reuse_distance = DEFAULT_REUSE_DISTANCE
        self.area_ratio = DEFAULT_AREA_RATIO
        self.check_xy = False
        self.check_area = False
        self.units = DEFAULT_UNITS
        self.notes = []

    @property
    def refused(self):
        """True when the key could not carry a diff, so no entry exists."""
        return bool(self.dupes or self.blanks)

    @property
    def checked(self):
        return self.check_xy or self.check_area

    def of(self, cls):
        return [e for e in self.entries if e.cls == cls]

    def counts(self):
        return dict((c, len(self.of(c))) for c in CLASSES)

    def overlap(self):
        """Share of the smaller vintage's keys that are on both sides."""
        smaller = min(self.old_keys, self.new_keys)
        return self.pairs / float(smaller) if smaller else 0.0


def _finite(value):
    return value is not None and not math.isnan(value) and \
        not math.isinf(value)


def to_number(text):
    """A finite float, or None. A thousands separator is not a number."""
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return value if _finite(value) else None


def key_label(key):
    """The key as shown to a person. A composite key joins with a bar."""
    return "|".join(key)


def _exact_number(text):
    """A finite Decimal, or None. Decimal keeps every digit; a float keeps 15."""
    try:
        value = decimal.Decimal(str(text))
    except (TypeError, ValueError, ArithmeticError):
        return None
    return value if value.is_finite() else None


def same_value(a, b):
    """True when two cells hold the same value after trimming.

    Numbers compare as exact decimals, so 1 and 1.0 agree: Excel rewrites a
    column on the way through, and that must not read as an edit. A float
    would call two 19-digit ids that differ in the last digit equal, so it is
    not used here. Text compares exactly. Keys never come through here.
    """
    if a == b:
        return True
    na, nb = _exact_number(a), _exact_number(b)
    return na is not None and nb is not None and na == nb


def separation(a, b):
    """Straight-line distance between two rows, or None if either lacks a point."""
    if any(v is None for v in (a.x, a.y, b.x, b.y)):
        return None
    return math.hypot(a.x - b.x, a.y - b.y)


def area_ratio_of(a, b):
    """How many times larger the bigger area is, or None if either is unusable."""
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return max(a, b) / min(a, b)


def index_rows(rows):
    """Group rows by key. Returns (rows by key, rows with no key, repeated keys)."""
    by_key = {}
    blank = []
    for row in rows:
        if row.key is None:
            blank.append(row)
        else:
            by_key.setdefault(row.key, []).append(row)
    repeated = dict((k, v) for k, v in by_key.items() if len(v) > 1)
    return by_key, blank, repeated


def judge_pair(old, new, fields, reuse_distance, area_ratio, move_tolerance,
               units=DEFAULT_UNITS):
    """Decide one key that is on both sides.

    Returns (class, detail, measured). measured is (xy, area): whether a
    centroid shift and an area ratio could be computed for this pair, so the
    caller knows which reuse test saw it.
    """
    reasons = []
    moved = False
    shift = separation(old, new)
    if shift is not None:
        if shift > reuse_distance:
            reasons.append("centroid moved %.1f %s" % (shift, units))
        moved = shift > move_tolerance
    ratio = area_ratio_of(old.area, new.area)
    measured = (shift is not None, ratio is not None)
    if ratio is not None:
        if ratio > area_ratio:
            reasons.append("area changed by a factor of %.2f" % ratio)
    if reasons:
        return REUSED, "; ".join(reasons), measured
    changes = []
    for field in fields:
        before = old.values.get(field, "")
        after = new.values.get(field, "")
        if not same_value(before, after):
            changes.append("%s: '%s' -> '%s'" % (field, before, after))
    if moved:
        changes.append("%s: moved %.1f %s" % (GEOMETRY_FIELD, shift, units))
    if changes:
        return CHANGED, "; ".join(changes), measured
    return UNCHANGED, "", measured


def compare(old, new, fields=(), reuse_distance=DEFAULT_REUSE_DISTANCE,
            area_ratio=DEFAULT_AREA_RATIO,
            move_tolerance=DEFAULT_MOVE_TOLERANCE,
            min_overlap=DEFAULT_MIN_OVERLAP, check_xy=False, check_area=False,
            allow_blank=False, units=DEFAULT_UNITS,
            min_measured=DEFAULT_MIN_MEASURED, skip_reuse=False):
    """Diff two vintages. The key is audited before anything is compared.

    A key on two rows, or a row with no key, stops the run and leaves
    report.entries empty: every class would have been computed under a key
    that cannot say which row the other vintage meant.
    """
    if not (_finite(move_tolerance) and move_tolerance >= 0):
        raise ValueError("the move tolerance must be a number of 0 or more")
    if not (_finite(reuse_distance) and reuse_distance >= move_tolerance):
        raise ValueError("the reuse distance must be a number at or above "
                         "the move tolerance")
    if not (_finite(area_ratio) and area_ratio >= 1.0):
        raise ValueError("the area ratio must be a number of 1 or more")
    if not (_finite(min_overlap) and 0.0 <= min_overlap <= 1.0):
        raise ValueError("the minimum overlap must be between 0 and 1")
    if not (_finite(min_measured) and 0.0 <= min_measured <= 1.0):
        raise ValueError("the minimum measured share must be between 0 and 1")

    report = Report()
    report.min_overlap = min_overlap
    report.min_measured = min_measured
    report.skip_reuse = skip_reuse
    report.reuse_distance = reuse_distance
    report.area_ratio = area_ratio
    report.check_xy = check_xy
    report.check_area = check_area
    report.units = units

    sides = (("old", old), ("new", new))
    indexed = dict((name, index_rows(rows)) for name, rows in sides)
    for name, _ in sides:
        _, blank, repeated = indexed[name]
        for key in sorted(repeated):
            report.dupes.append((name, key_label(key),
                                 [r.line for r in repeated[key]]))
        if blank and not allow_blank:
            report.blanks.extend((name, r.line) for r in blank)
    if report.refused:
        return report

    old_by, old_blank, _ = indexed["old"]
    new_by, new_blank, _ = indexed["new"]
    report.old_keys = len(old_by)
    report.new_keys = len(new_by)
    for name, blank in (("old", old_blank), ("new", new_blank)):
        for row in blank:
            report.entries.append(Entry(
                UNKEYED, "", row if name == "old" else None,
                row if name == "new" else None, "no key on this row"))

    for key in sorted(set(old_by) & set(new_by)):
        cls, detail, (xy_ok, area_ok) = judge_pair(
            old_by[key][0], new_by[key][0], fields, reuse_distance,
            area_ratio, move_tolerance, units)
        report.pairs += 1
        report.measured_xy += 1 if xy_ok else 0
        report.measured_area += 1 if area_ok else 0
        missed = [name for name, asked, ok in (("centroid", check_xy, xy_ok),
                                               ("area", check_area, area_ok))
                  if asked and not ok]
        if missed:
            report.unmeasured.append((key_label(key), " and ".join(missed)))
        elif check_xy or check_area:
            report.measured += 1
        report.entries.append(Entry(cls, key_label(key), old_by[key][0],
                                    new_by[key][0], detail))
    for key in sorted(set(new_by) - set(old_by)):
        report.entries.append(Entry(ADDED, key_label(key), None,
                                    new_by[key][0]))
    for key in sorted(set(old_by) - set(new_by)):
        report.entries.append(Entry(REMOVED, key_label(key), old_by[key][0],
                                    None))
    return report


def unverified_reasons(report):
    """Why a run that was not refused still compared nothing, in words."""
    reasons = []
    if report.pairs == 0:
        reasons.append("no key is on both sides, so nothing was compared. "
                       "A key spelled differently on the two sides looks "
                       "exactly like this")
    elif report.overlap() < report.min_overlap:
        reasons.append("only %.0f percent of the keys are on both sides, "
                       "under the %.0f percent floor. The key may be spelled "
                       "differently on the two sides"
                       % (100.0 * report.overlap(),
                          100.0 * report.min_overlap))
    if not report.checked and not report.skip_reuse:
        reasons.append("key reuse was not checked, because no coordinate or "
                       "area column was named. Name --x-field and --y-field, "
                       "or --area-field, or pass --skip-reuse-check to accept "
                       "a plain diff")
    for name, asked, got, noun in (
            ("centroid", report.check_xy, report.measured_xy, "coordinate"),
            ("area", report.check_area, report.measured_area, "area")):
        if not (asked and report.pairs):
            continue
        if got == 0:
            reasons.append("the %s test measured no pair, because every %s "
                           "was blank, zero or unreadable" % (name, noun))
        elif got / float(report.pairs) < report.min_measured:
            reasons.append("the %s test measured only %d of %d pair(s), under "
                           "the %.0f percent floor, so a reissued key among "
                           "the rest would go unseen"
                           % (name, got, report.pairs,
                              100.0 * report.min_measured))
    return reasons


def verdict(report):
    """(exit code, verdict text). The one place an exit code is decided."""
    if report.refused:
        return 3, "VERDICT: REFUSED"
    reused = len(report.of(REUSED))
    if reused:
        return 1, ("VERDICT: %d key(s) now name a different parcel than they "
                   "did. Do not pair these rows." % reused)
    reasons = unverified_reasons(report)
    if reasons:
        return 4, "VERDICT: UNVERIFIED, " + "; and ".join(reasons) + "."
    counts = report.counts()
    text = ("VERDICT: clean. %d changed, %d added, %d removed"
            % (counts[CHANGED], counts[ADDED], counts[REMOVED]))
    if counts[UNKEYED]:
        text += (", %d row(s) left out because they have no key"
                 % counts[UNKEYED])
    text += "."
    if report.unmeasured:
        text += (" %d pair(s) could not be measured for key reuse."
                 % len(report.unmeasured))
    if not report.checked:
        text += " Key reuse was not checked."
    return 0, text


def gate(report):
    return verdict(report)[0]


def _section(out, heading, lines, sample):
    """Append a heading and up to sample lines, then how many were cut."""
    if not lines or sample == 0:
        return
    out.append("")
    out.append(heading)
    for line in lines[:sample]:
        out.append("  " + line)
    if len(lines) > sample:
        out.append("  ... and %d more" % (len(lines) - sample))


def _where(entry):
    parts = []
    if entry.old is not None:
        parts.append("old line %s" % entry.old.line)
    if entry.new is not None:
        parts.append("new line %s" % entry.new.line)
    return ", ".join(parts)


def describe(report, sample=DEFAULT_SAMPLE):
    """The report as lines of text. Ends with the verdict."""
    out = []
    if report.dupes:
        out.append("REFUSED   the key is not unique, so nothing was compared")
        for side, label, lines in report.dupes[:sample]:
            out.append("      %s key '%s' is on %d rows: line %s"
                       % (side, label, len(lines),
                          ", ".join(str(n) for n in lines)))
        if len(report.dupes) > sample:
            out.append("      ... and %d more key(s)"
                       % (len(report.dupes) - sample))
        out.append("      Fix the key or name more columns in --key-field. A "
                   "key two rows claim cannot say which row the other "
                   "vintage meant.")
    if report.blanks:
        out.append("REFUSED   rows with no key cannot be paired, so nothing "
                   "was compared")
        for side, line in report.blanks[:sample]:
            out.append("      %s line %s has no key" % (side, line))
        if len(report.blanks) > sample:
            out.append("      ... and %d more row(s)"
                       % (len(report.blanks) - sample))
        out.append("      Fix the rows, or pass --allow-blank-keys to leave "
                   "them out and count them.")
    if report.refused:
        out.append("")
        out.append(verdict(report)[1])
        return out

    counts = report.counts()
    total = sum(counts[c] for c in (UNCHANGED, CHANGED, REUSED, ADDED, REMOVED))
    out.append("keys classified: %d of %d"
               % (total, report.old_keys + report.new_keys - report.pairs))
    for cls in CLASSES:
        if counts[cls]:
            out.append("  %-10s %d" % (cls, counts[cls]))
    out.append("pairs: %d on a shared key, %.0f percent of the smaller "
               "vintage" % (report.pairs, 100.0 * report.overlap()))
    tests = []
    if report.check_xy:
        tests.append("centroid moved above %.1f %s"
                     % (report.reuse_distance, report.units))
    if report.check_area:
        tests.append("area ratio above %.2f" % report.area_ratio)
    if tests:
        out.append("key reuse: checked on %d of %d pair(s), by %s"
                   % (report.measured, report.pairs, " or ".join(tests)))
    else:
        out.append("key reuse: NOT CHECKED, no coordinate or area column was "
                   "named%s" % (" (--skip-reuse-check)" if report.skip_reuse
                                else ""))
    out.extend(report.notes)

    _section(out, "keys that now name a different parcel:",
             ["%s  %s (%s)" % (e.key, e.detail, _where(e))
              for e in report.of(REUSED)], sample)
    _section(out, "pairs the key-reuse check could not measure:",
             ["%s (%s test)" % pair for pair in report.unmeasured], sample)
    _section(out, "keys whose attributes changed:",
             ["%s  %s (%s)" % (e.key, e.detail, _where(e))
              for e in report.of(CHANGED)], sample)
    _section(out, "keys only in the new vintage:",
             ["%s (%s)" % (e.key, _where(e)) for e in report.of(ADDED)],
             sample)
    _section(out, "keys only in the old vintage:",
             ["%s (%s)" % (e.key, _where(e)) for e in report.of(REMOVED)],
             sample)
    _section(out, "rows left out because they have no key:",
             ["%s" % _where(e) for e in report.of(UNKEYED)], sample)
    out.append("")
    out.append(verdict(report)[1])
    return out


def classified_rows(report):
    """One dict per key, in the shape OUT_COLUMNS names."""
    rows = []
    for e in report.entries:
        rows.append({
            "class": e.cls, "key": e.key,
            "old_line": "" if e.old is None else e.old.line,
            "new_line": "" if e.new is None else e.new.line,
            "detail": e.detail,
        })
    return rows


def plan_fields(old_header, new_header, key_fields, x_field, y_field,
                area_field, requested):
    """The columns to compare, and the columns only one vintage has.

    Raises ValueError when a column the run needs is missing from either
    header. With no --compare-fields every column both vintages share is
    compared, apart from the key and the coordinates.
    """
    named = list(key_fields) + [f for f in (x_field, y_field) if f]
    needed = named + ([area_field] if area_field else []) + list(requested)
    for side, header in (("old", old_header), ("new", new_header)):
        absent = [n for n in needed if n not in header]
        if absent:
            raise ValueError(
                "the %s CSV lacks column(s): %s. It has: %s"
                % (side, ", ".join(absent),
                   ", ".join(header) if header else "(no columns at all)"))
    if requested:
        return list(requested), []
    fields = [c for c in old_header if c in new_header and c not in named]
    only = [c for c in old_header if c not in new_header] + \
        [c for c in new_header if c not in old_header]
    return fields, only


def looks_like_degrees(rows):
    """True when every usable coordinate fits longitude and latitude.

    Degrees in X and Y fail silently: every shift is a fraction of a degree,
    so no key is ever judged reissued against a threshold of feet. A
    projected layer leaves the +-180 and +-90 box at once.
    """
    seen = False
    for row in rows:
        if row.x is None or row.y is None:
            continue
        seen = True
        if abs(row.x) > 180.0 or abs(row.y) > 90.0:
            return False
    return seen


# ----------------------------------------------------------------- the file

_BREAK = re.compile(r"\r\n|\r|\n")


class _Record(dict):
    """One CSV row as a dict, plus the line of the file it starts on."""
    line = None


def _breaks(value):
    """How many line breaks one parsed cell carries (a quoted newline)."""
    cells = value if isinstance(value, list) else [value]
    return sum(len(_BREAK.findall(c)) for c in cells if c)


def read_csv(path):
    """Raw rows and the header of a CSV, as dicts with a .line attribute.

    utf-8-sig, because Excel and Esri's Table To Table write a UTF-8 BOM that
    would otherwise glue itself to the first column name. DictReader keeps the
    last of two columns with one name and drops the other without a word, so a
    repeated column name is refused here. The line is the one the record
    starts on: a quoted newline and a blank line do not move it.
    """
    with open(path, "r", newline="", encoding="utf-8-sig",
              errors="replace") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        repeated = sorted(set(n for n in header if header.count(n) > 1))
        if repeated:
            raise ValueError("%s: column name(s) repeated: %s. A diff cannot "
                             "tell which column is meant."
                             % (path, ", ".join(n or "(blank name)"
                                                for n in repeated)))
        rows = []
        for raw in reader:
            record = _Record(raw)
            record.line = reader.line_num - sum(
                _breaks(v) for v in raw.values())
            rows.append(record)
        return rows, header


def extract_rows(raw_rows, key_fields, x_field, y_field, area_field):
    """Reduce each CSV row to a Row. A key with a blank part is None."""
    out = []
    for raw in raw_rows:
        values = dict((k, ("" if v is None else str(v)).strip())
                      for k, v in raw.items() if k is not None)
        parts = tuple(values.get(f, "") for f in key_fields)
        key = None if any(p == "" for p in parts) else parts
        out.append(Row(
            getattr(raw, "line", None), key, values,
            to_number(values.get(x_field)) if x_field else None,
            to_number(values.get(y_field)) if y_field else None,
            to_number(values.get(area_field)) if area_field else None))
    return out


def write_classified(path, report):
    """Write one row per key, with its class and what changed."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUT_COLUMNS))
        writer.writeheader()
        for row in classified_rows(report):
            writer.writerow(row)


# ----------------------------------------------------------------- self-test

# Synthetic vintages. The README demo runs on these exact files.
OLD_CSV = (
    "PARCEL,X,Y,ACRES,OWNER,USE\n"
    "P-1,620100,1580200,1.00,Ames,RES\n"
    "P-2,620500,1580600,2.50,Baker,RES\n"
    "P-3,621000,1581000,4.00,Cole,COM\n"
    "P-4,621400,1581400,0.50,Dunn,RES\n"
    "P-5,621800,1581800,10.0,Eaves,AGR\n"
    "P-6,622600,1582600,3.00,Frye,RES\n")
NEW_CSV = (
    "PARCEL,X,Y,ACRES,OWNER,USE\n"
    "P-1,620100,1580200,1.00,Ames,RES\n"
    "P-2,620500.4,1580600,2.50,Bakker,RES\n"
    "P-3,621000,1581000,1.90,Gray,COM\n"
    "P-4,628000,1589000,0.50,Hale,RES\n"
    "P-6,622600,1582600,3.00,Frye,RES\n"
    "P-7,623000,1583000,6.00,Ivy,AGR\n")


def summary(passed_count, failures):
    """Print the footer and return the exit status of the self-test."""
    print("-" * 68)
    total = passed_count + len(failures)
    if failures:
        print("%d assertions, %d failed" % (total, len(failures)))
        for f in failures:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


def self_test():
    """Run the assertions in a temp directory that is removed even when one
    of them raises, so a crash never leaves parceldiff-selftest-* behind."""
    tmp = tempfile.mkdtemp(prefix="parceldiff-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    """Assertions over the decision core and the CLI. No network, no
    credentials. The only files are the ones written under tmp."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def mk(key, x=None, y=None, area=None, **vals):
        return Row(None, None if key is None else (key,),
                   dict((n, str(v)) for n, v in vals.items()), x, y, area)

    def vintage(*rows):
        for index, row in enumerate(rows):
            row.line = index + 2
        return list(rows)

    def slurp(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def put(name, text):
        path = os.path.join(tmp, name)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def run_cli(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def pair(old_row, new_row, **kw):
        settings = dict(fields=("OWNER",), reuse_distance=500.0,
                        area_ratio=2.0, move_tolerance=1.0)
        settings.update(kw)
        return judge_pair(old_row, new_row, settings["fields"],
                          settings["reuse_distance"], settings["area_ratio"],
                          settings["move_tolerance"])

    print("parceldiff self-test: temporary files only, no network, no "
          "credentials")
    print("-" * 68)

    # ---- numbers and values
    check(to_number("12.5") == 12.5 and to_number(" 7 ") == 7.0,
          "a plain number and a padded number both read")
    check(to_number("1,000") is None,
          "a thousands separator is not a number")
    check(to_number("") is None and to_number(None) is None,
          "a blank cell and a missing cell are no number")
    check(to_number("nan") is None and to_number("inf") is None,
          "nan and inf are no number, so they cannot pass a threshold")
    check(same_value("Ames", "Ames") and same_value("", ""),
          "equal text agrees, and so do two blanks")
    check(same_value("1", "1.0") and same_value("2.50", "2.5"),
          "1 and 1.0 agree, so a spreadsheet round trip is not an edit")
    check(not same_value("1", "2") and not same_value("Ames", "Amos"),
          "different numbers and different text disagree")
    check(not same_value("", "0") and not same_value("1,000", "1000"),
          "a blank is not zero, and a separator is not a number")
    check(key_label(("BK", "12")) == "BK|12",
          "a composite key shows with a bar between its parts")
    check(not same_value("1234567890123456789", "1234567890123456788"),
          "two 19-digit ids that differ in the last digit are two values"
          "  <-- pinned defect")
    check(same_value("01234", "1234") and same_value("1e3", "1000")
          and same_value("1_000", "1000"),
          "leading zeros, an exponent and an underscore read as the same "
          "number")
    check(not same_value("nan", "NaN") and not same_value("inf", "Infinity")
          and not same_value("1e99999999999999999999", "1"),
          "nan, inf and an exponent too large to hold are never equal "
          "numbers, and none of them raises")

    # ---- geometry helpers
    check(separation(mk("a", 0.0, 0.0), mk("a", 3.0, 4.0)) == 5.0,
          "a 3-4-5 shift measures 5")
    check(separation(mk("a", 7.0, 7.0), mk("a", 7.0, 7.0)) == 0.0,
          "the same point is 0 apart")
    check(separation(mk("a", 1.0, None), mk("a", 3.0, 4.0)) is None,
          "a row with no Y has no separation")
    check(separation(mk("a"), mk("a")) is None,
          "two rows with no point have no separation")
    check(area_ratio_of(1.0, 2.0) == 2.0 and area_ratio_of(2.0, 1.0) == 2.0,
          "the area ratio is the same for growth and for shrinkage")
    check(area_ratio_of(3.0, 3.0) == 1.0, "equal areas give a ratio of 1")
    check(area_ratio_of(None, 1.0) is None and area_ratio_of(1.0, None) is None,
          "a missing area gives no ratio")
    check(area_ratio_of(0.0, 1.0) is None and area_ratio_of(1.0, -2.0) is None,
          "a zero or negative area gives no ratio, never a divide by zero")
    check(area_ratio_of(5.0, 0.0) is None and area_ratio_of(0.0, 0.0) is None,
          "a new area of zero gives no ratio either, never a divide by zero")
    check(to_number("1_000") == 1000.0,
          "an underscore is read as a digit separator by float()")

    # ---- one pair
    same = mk("k", 0.0, 0.0, 1.0, OWNER="Ames")
    cls, detail, measured = pair(same, mk("k", 0.0, 0.0, 1.0, OWNER="Ames"))
    check(cls == UNCHANGED and detail == "" and measured == (True, True),
          "an identical pair is UNCHANGED and was measured by both tests")
    cls, detail, _ = pair(same, mk("k", 0.0, 0.0, 1.0, OWNER="Bell"))
    check(cls == CHANGED and detail == "OWNER: 'Ames' -> 'Bell'",
          "an owner edit is CHANGED and names both values")
    check(pair(mk("k", 0.0, 0.0, OWNER="A"),
               mk("k", 500.0, 0.0, OWNER="A"))[0] == CHANGED,
          "a shift of exactly the reuse distance is not REUSED, the "
          "threshold is exclusive")
    check(pair(mk("k", 0.0, 0.0, OWNER="A"),
               mk("k", 500.001, 0.0, OWNER="A"))[0] == REUSED,
          "a thousandth past the reuse distance is REUSED")
    check(pair(mk("k", 0.0, 0.0, OWNER="A"),
               mk("k", 1.0, 0.0, OWNER="A"))[0] == UNCHANGED,
          "a shift of exactly the move tolerance is capture noise")
    cls, detail, _ = pair(mk("k", 0.0, 0.0, OWNER="A"),
                          mk("k", 1.001, 0.0, OWNER="A"))
    check(cls == CHANGED and detail.startswith("(geometry): moved 1.0"),
          "a shift past the move tolerance is a CHANGED geometry")
    check(pair(mk("k", area=1.0, OWNER="A"),
               mk("k", area=2.0, OWNER="A"))[0] == UNCHANGED,
          "an area ratio of exactly the limit does not reissue the key")
    check(pair(mk("k", area=1.0, OWNER="A"),
               mk("k", area=2.001, OWNER="A"))[0] == REUSED,
          "an area that more than doubled is REUSED")
    check(pair(mk("k", area=2.001, OWNER="A"),
               mk("k", area=1.0, OWNER="A"))[0] == REUSED,
          "an area that more than halved is REUSED, a split that kept the "
          "number")
    cls, detail, _ = pair(mk("k", 0.0, 0.0, 1.0, OWNER="A"),
                          mk("k", 900.0, 0.0, 5.0, OWNER="B"))
    check(cls == REUSED and "centroid moved 900.0 ft" in detail
          and "area changed by a factor of 5.00" in detail
          and "OWNER" not in detail,
          "a reissued key names every reason and does not report the "
          "owner edit, because the two rows are different parcels")
    cls, detail, _ = pair(mk("k", 0.0, 0.0, 1.0, ACRES="1.00"),
                          mk("k", 0.0, 0.0, 1.04, ACRES="1.04"),
                          fields=("ACRES",))
    check(cls == CHANGED and detail == "ACRES: '1.00' -> '1.04'",
          "a 4 percent area adjustment is CHANGED, not REUSED")
    cls, detail, _ = pair(mk("k", area=5.0, OWNER="A"),
                          mk("k", area=0.0, OWNER="B"))
    check(cls == CHANGED and pair(mk("k", area=0.0, OWNER="A"),
                                  mk("k", area=5.0, OWNER="B"))[0] == CHANGED,
          "a parcel that falls to an area of 0, or rises from it, is "
          "CHANGED and not REUSED")
    cls, detail, _ = pair(mk("k", pin="1234567890123456789"),
                          mk("k", pin="1234567890123456788"), fields=("pin",))
    check(cls == CHANGED and "pin:" in detail,
          "a changed 19-digit id is CHANGED, not UNCHANGED"
          "  <-- pinned defect")
    cls, detail, measured = pair(mk("k", OWNER="A"), mk("k", OWNER="B"))
    check(cls == CHANGED and measured == (False, False),
          "a pair with no point and no area still diffs, and is not "
          "measured")
    cls, _, measured = pair(mk("k", area=1.0, OWNER="A"),
                            mk("k", area=1.0, OWNER="A"))
    check(cls == UNCHANGED and measured == (False, True),
          "an area alone measures the area test and not the centroid test")
    check(pair(mk("k", 0.0, 0.0, OWNER="A"), mk("k", 0.0, 0.0, OWNER="A"),
               reuse_distance=10.0, move_tolerance=5.0)[0] == UNCHANGED,
          "tighter thresholds still leave an unmoved parcel alone")
    check(pair(mk("k", OWNER="1"), mk("k", OWNER="1.0"))[0] == UNCHANGED,
          "a field rewritten from 1 to 1.0 is not an edit")
    check(pair(mk("k", OWNER="A"), mk("k"), fields=("OWNER",))[0] == CHANGED,
          "a field missing on one row reads as blank and differs")

    # ---- the key audit
    by_key, blank, repeated = index_rows(vintage(
        mk("a"), mk("b"), mk("a"), mk(None)))
    check(sorted(by_key) == [("a",), ("b",)] and len(blank) == 1
          and list(repeated) == [("a",)],
          "the audit finds one repeated key and one blank key")
    check(index_rows([])[2] == {} and index_rows([])[1] == [],
          "no rows have no repeats")
    rows = vintage(mk("a", OWNER="1"), mk("b"), mk("a", OWNER="2"))
    table = dict((r.key, r) for r in rows)
    check(len(rows) == 3 and len(table) == 2
          and table[("a",)].values["OWNER"] == "2",
          "a lookup table built from rows with a repeated key keeps the "
          "last row and drops the other without a word")

    # ---- the refusal of a key that is not unique
    old = vintage(mk("A"), mk("B"), mk("B"))
    new = vintage(mk("A"), mk("B"))
    report = compare(old, new)
    check(report.refused and report.entries == [] and gate(report) == 3,
          "a key on two rows refuses the run and classifies nothing"
          "  <-- pinned defect")
    check(report.dupes == [("old", "B", [3, 4])],
          "the refusal names the side, the key and both lines")
    check(compare(new, old).dupes == [("new", "B", [3, 4])],
          "a repeat in the new vintage is refused the same way")
    both = compare(old, old)
    check(len(both.dupes) == 2 and gate(both) == 3,
          "a repeat on both sides names both")
    lines = describe(report)
    check(lines[0].startswith("REFUSED   the key is not unique")
          and lines[-1] == "VERDICT: REFUSED"
          and "old key 'B' is on 2 rows: line 3, 4" in lines[1],
          "the refusal text says what, where and what to do")
    check(not any("classified" in line for line in lines),
          "a refused run prints no counts, because none are sound")
    many = vintage(*[mk(str(n)) for n in range(4) for _ in (0, 1)])
    many_lines = describe(compare(many, many), sample=2)
    check(any("and 6 more key(s)" in line for line in many_lines),
          "a long list of repeated keys is cut at the sample and says so")
    check(not any(line.strip().startswith("... and") for line in
                  describe(compare(many, many), sample=8)),
          "a list exactly as long as the sample is not cut")
    check(not any("rows with no key" in line for line in lines),
          "a refusal for a repeated key does not claim a row had no key")

    trimmed = extract_rows(
        [{"K": " P-1", "V": "x"}, {"K": "P-1 ", "V": "y"}], ["K"], None, None,
        None)
    check(trimmed[0].key == ("P-1",) and trimmed[1].key == ("P-1",)
          and compare(trimmed, trimmed).refused,
          "a key with a stray space is the same key, so it is refused"
          "  <-- pinned defect")
    comp = extract_rows(
        [{"B": "7", "P": "1"}, {"B": "7", "P": "2"}], ["B", "P"], None, None,
        None)
    check(comp[0].key == ("7", "1") and not compare(comp, comp).refused,
          "two columns together can make a key that one column cannot")
    check(compare(extract_rows([{"B": "7", "P": "1"}, {"B": "7", "P": "9"}],
                               ["B"], None, None, None),
                  []).refused,
          "and the first column alone is refused")
    half = extract_rows([{"B": "7", "P": ""}], ["B", "P"], None, None, None)
    check(half[0].key is None,
          "a composite key with one blank part is a blank key")

    # ---- the refusal of a row with no key
    blank_old = vintage(mk("A"), mk(None))
    report = compare(blank_old, vintage(mk("A")))
    check(report.refused and report.blanks == [("old", 3)]
          and gate(report) == 3,
          "a row with no key refuses the run")
    lines = describe(report)
    check(any("old line 3 has no key" in line for line in lines)
          and any("--allow-blank-keys" in line for line in lines),
          "the refusal names the line and the flag that overrides it")
    check(not any("key is not unique" in line for line in lines),
          "a refusal for a blank key does not claim the key repeats")
    check(not any(line.strip().startswith("... and") for line in
                  describe(compare(vintage(*[mk(None) for _ in range(3)]),
                                   vintage(mk("A"))), sample=3)),
          "a list of blank rows exactly as long as the sample is not cut")
    many_blank = vintage(*[mk(None) for _ in range(3)])
    check(any("and 1 more row(s)" in line for line in
              describe(compare(many_blank, vintage(mk("A"))), sample=2)),
          "a long list of blank rows is cut at the sample and says so")
    report = compare(blank_old, vintage(mk("A")), allow_blank=True,
                     skip_reuse=True)
    check(not report.refused and report.counts()[UNKEYED] == 1
          and report.counts()[UNCHANGED] == 1 and gate(report) == 0,
          "with --allow-blank-keys the blank row is counted, not dropped")
    check("1 row(s) left out because they have no key"
          in describe(report)[-1],
          "and the verdict counts the rows it left out, so a clean exit "
          "does not hide them  <-- pinned defect")
    check(any("rows left out because they have no key" in line
              for line in describe(report)),
          "and it is listed, so leaving it out is not silent")
    with_new_blank = compare(vintage(mk("A")), vintage(mk("A"), mk(None)),
                             allow_blank=True)
    check(with_new_blank.of(UNKEYED)[0].new is not None
          and with_new_blank.of(UNKEYED)[0].old is None,
          "a blank row in the new vintage is attributed to the new side")

    # ---- the diff itself
    old = vintage(mk("A", OWNER="1"), mk("B", OWNER="1"), mk("C", OWNER="1"),
                  mk("D", OWNER="1"))
    new = vintage(mk("A", OWNER="1"), mk("B", OWNER="2"), mk("E", OWNER="1"),
                  mk("D", OWNER="1"), mk("F", OWNER="1"))
    report = compare(old, new, ("OWNER",), skip_reuse=True)
    counts = report.counts()
    check(counts == {UNCHANGED: 2, CHANGED: 1, REUSED: 0, ADDED: 2,
                     REMOVED: 1, UNKEYED: 0},
          "two unchanged, one changed, two added and one removed")
    check([e.key for e in report.of(ADDED)] == ["E", "F"]
          and [e.key for e in report.of(REMOVED)] == ["C"],
          "the added and removed keys are the right ones, in order")
    check(sum(counts[c] for c in (UNCHANGED, CHANGED, REUSED, ADDED, REMOVED))
          == report.old_keys + report.new_keys - report.pairs,
          "every key lands in exactly one class")
    check(gate(report) == 0 and describe(report)[-1].startswith(
        "VERDICT: clean. 1 changed, 2 added, 1 removed."),
          "a clean diff exits 0 and says so")
    check(describe(report)[-1].endswith("Key reuse was not checked."),
          "a diff with no geometry column says that reuse was not checked")
    check(any("key reuse: NOT CHECKED" in line for line in describe(report)),
          "and the summary says so too")
    check(report.overlap() == 0.75 and report.pairs == 3,
          "three of the four keys of the smaller vintage are on both sides")
    two_added = compare(vintage(mk("A")), vintage(mk("A"), mk("B"), mk("C")),
                        skip_reuse=True)
    check(not any(line.strip().startswith("... and") for line in
                  describe(two_added, sample=2)),
          "a section exactly as long as the sample is not cut")
    check("... and 1 more" in "\n".join(describe(two_added, sample=1)),
          "and one longer than the sample is")

    # ---- a reissued key
    old = vintage(mk("A", 0.0, 0.0, 1.0, OWNER="X"),
                  mk("B", 0.0, 0.0, 1.0, OWNER="X"))
    new = vintage(mk("A", 0.0, 0.0, 1.0, OWNER="X"),
                  mk("B", 9000.0, 0.0, 1.0, OWNER="Y"))
    report = compare(old, new, ("OWNER",), check_xy=True, check_area=True)
    check(report.counts()[REUSED] == 1 and report.counts()[CHANGED] == 0
          and gate(report) == 1,
          "a key that jumped 9000 units is REUSED, not an owner edit, and "
          "the run exits 1  <-- pinned defect")
    lines = describe(report)
    check(any(line.startswith("  B  centroid moved 9000.0 ft") for line in
              lines) and lines[-1].startswith("VERDICT: 1 key(s) now name"),
          "the report names the key, the distance and both lines")
    check(any("key reuse: checked on 2 of 2 pair(s), by centroid moved above "
              "500.0 ft or area ratio above 2.00" in line for line in lines),
          "the summary says which tests ran and on how many pairs")
    lines_800 = describe(compare(old, new, ("OWNER",), check_xy=True,
                                 check_area=True, reuse_distance=800.0,
                                 area_ratio=3.0, units="m"))
    check(any("by centroid moved above 800.0 m or area ratio above 3.00"
              in line for line in lines_800),
          "the summary prints the thresholds and the unit that were used")
    xy_only = describe(compare(old, new, ("OWNER",), check_xy=True))
    check(any("by centroid moved above 500.0 ft" in line and
              "area ratio" not in line for line in xy_only),
          "with coordinates only, only the shift test is named")
    area_only = describe(compare(old, new, ("OWNER",), check_area=True))
    check(any("by area ratio above 2.00" in line for line in area_only),
          "with an area only, only the ratio test is named")
    check(compare(old, new, ("OWNER",), check_xy=True,
                  reuse_distance=9000.0).counts()[REUSED] == 0,
          "a looser reuse distance lets the same jump through as CHANGED")
    check(compare(old, new, ("OWNER",), check_xy=True, units="m")
          .of(REUSED)[0].detail == "centroid moved 9000.0 m",
          "--units only relabels the distance")

    # ---- a run that compared nothing
    old = vintage(mk("0012", OWNER="1"), mk("0013", OWNER="1"))
    new = vintage(mk("12", OWNER="1"), mk("13", OWNER="1"))
    report = compare(old, new, ("OWNER",))
    check(gate(report) == 4 and report.pairs == 0,
          "keys spelled 0012 and 12 pair nothing, so the run exits 4, not 0"
          "  <-- pinned defect")
    check(describe(report)[-1].startswith("VERDICT: UNVERIFIED, no key is on "
                                          "both sides"),
          "and the verdict says why")
    check(gate(compare([], [])) == 4,
          "two empty vintages compared nothing")
    check(gate(compare(vintage(mk("a")), [])) == 4,
          "an empty new vintage compared nothing")
    old = vintage(mk("1"), mk("2"), mk("3"), mk("4"))
    new = vintage(mk("1"), mk("2"), mk("7"), mk("8"))
    check(gate(compare(old, new, skip_reuse=True)) == 0,
          "exactly the overlap floor passes, the floor is inclusive")
    report = compare(old, vintage(mk("1"), mk("7"), mk("8"), mk("9")))
    check(gate(report) == 4 and "25 percent of the keys are on both sides"
          in describe(report)[-1],
          "a quarter of the keys on both sides is under the floor and "
          "exits 4  <-- pinned defect")
    check(gate(compare(old, vintage(mk("1"), mk("7"), mk("8"), mk("9")),
                       min_overlap=0.0, skip_reuse=True)) == 0,
          "a floor of 0 turns the overlap check off")

    # ---- a run with no reuse check at all
    plain_old = vintage(mk("a", OWNER="1"), mk("b", OWNER="1"))
    plain_new = vintage(mk("a", OWNER="1"), mk("b", OWNER="2"))
    report = compare(plain_old, plain_new)
    check(gate(report) == 4 and "key reuse was not checked" in
          describe(report)[-1],
          "a diff with no coordinate and no area column exits 4, not a "
          "clean 0  <-- pinned defect")
    report = compare(plain_old, plain_new, skip_reuse=True)
    check(gate(report) == 0 and describe(report)[-1].endswith(
        "Key reuse was not checked."),
          "--skip-reuse-check accepts a plain diff and says so")
    flat = vintage(mk("a", 0.0, 0.0, 1.0), mk("b", 5.0, 5.0, 1.0))
    report = compare(flat, flat, check_xy=True, check_area=True)
    check(gate(report) == 0 and describe(report)[-1] ==
          "VERDICT: clean. 0 changed, 0 added, 0 removed."
          and not any("NOT CHECKED" in line for line in describe(report)),
          "a checked run that found nothing is plainly clean and does not "
          "claim the check was skipped")

    # ---- a reuse check that measured nothing
    old = vintage(mk("a", None, None, None), mk("b"))
    new = vintage(mk("a"), mk("b"))
    report = compare(old, new, check_xy=True)
    check(gate(report) == 4 and report.measured == 0
          and "measured no pair" in describe(report)[-1],
          "coordinates asked for and none readable exits 4, not a clean 0"
          "  <-- pinned defect")
    check(gate(compare(old, new, skip_reuse=True)) == 0,
          "the same rows with --skip-reuse-check are not UNVERIFIED")
    both_reasons = compare(
        vintage(mk("a"), mk("b"), mk("c"), mk("d")),
        vintage(mk("a"), mk("x"), mk("y"), mk("z")), check_xy=True)
    check(describe(both_reasons)[-1].count("; and ") == 1,
          "two reasons are both given")
    check(len(unverified_reasons(compare(
        vintage(mk("a")), vintage(mk("b")), check_xy=True, check_area=True))
        ) == 1,
          "a run that paired nothing names that reason and no other")
    reused_and_thin = compare(
        vintage(mk("a", 0.0, 0.0), mk("b"), mk("c"), mk("d")),
        vintage(mk("a", 9999.0, 0.0), mk("x"), mk("y"), mk("z")),
        check_xy=True)
    check(gate(reused_and_thin) == 1,
          "a reissued key outranks an unverified run, because it is a "
          "finding")

    # ---- a reuse check that measured only some pairs
    def spread(total, readable, area=None):
        """total keys; only the first readable have a point, all have area."""
        rows = [mk("K%d" % n, 1000.0 * n if n < readable else None,
                   0.0 if n < readable else None, area)
                for n in range(total)]
        return vintage(*rows)

    report = compare(spread(2, 2, 1.0), spread(2, 1, 1.0),
                     check_xy=True, check_area=True)
    check(gate(report) == 4 and report.measured_area == 2
          and "the centroid test measured no pair" not in describe(report)[-1]
          and "measured only 1 of 2 pair(s)" in describe(report)[-1],
          "a readable area does not stand in for an unreadable coordinate"
          "  <-- pinned defect")
    report = compare(spread(2, 0, 1.0), spread(2, 0, 1.0), check_xy=True,
                     check_area=True)
    check(gate(report) == 4 and report.measured_area == 2
          and "the centroid test measured no pair" in describe(report)[-1],
          "coordinates unreadable on every row exit 4 even when the area "
          "reads  <-- pinned defect")
    report = compare(spread(50, 1), spread(50, 1), check_xy=True)
    check(gate(report) == 4 and report.measured_xy == 1
          and "measured only 1 of 50 pair(s), under the 90 percent floor"
          in describe(report)[-1],
          "one readable pair in fifty exits 4, not a clean 0"
          "  <-- pinned defect")
    report = compare(spread(10, 7), spread(10, 7), check_xy=True)
    check(gate(report) == 4,
          "seven readable pairs in ten is under the floor and exits 4"
          "  <-- pinned defect")
    report = compare(spread(10, 9), spread(10, 9), check_xy=True)
    lines = describe(report)
    check(gate(report) == 0 and report.measured == 9
          and report.unmeasured == [("K9", "centroid")]
          and "pairs the key-reuse check could not measure:" in lines
          and "1 pair(s) could not be measured for key reuse." in lines[-1]
          and "K9 (centroid test)" in "\n".join(lines)
          and "checked on 9 of 10 pair(s)" in "\n".join(lines),
          "nine pairs in ten is exactly the floor: the run passes, and "
          "the verdict and the report name the pair it could not measure")
    check(gate(compare(spread(10, 9), spread(10, 9), check_xy=True,
                       min_measured=1.0)) == 4,
          "a floor of 1 refuses a run that left one pair unmeasured")
    check(gate(compare(spread(50, 1), spread(50, 1), check_xy=True,
                       min_measured=0.0)) == 0,
          "a floor of 0 accepts any run that measured one pair")
    check(gate(compare(spread(50, 0), spread(50, 0), check_xy=True,
                       min_measured=0.0)) == 4,
          "but a test that measured no pair fails whatever the floor is")
    both = compare(vintage(mk("k")), vintage(mk("k")), check_xy=True,
                   check_area=True)
    check(both.unmeasured == [("k", "centroid and area")],
          "a pair that neither test could measure names both tests")
    check(compare(plain_old, plain_new, skip_reuse=True).measured == 0,
          "a run with no reuse test counts no pair as measured")
    report = compare(spread(10, 10), spread(10, 10), check_xy=True)
    check(report.unmeasured == [] and "could not be measured" not in
          describe(report)[-1]
          and "could not measure" not in "\n".join(describe(report)),
          "a run that measured every pair prints no unmeasured section")

    # ---- thresholds
    raises(lambda: compare([], [], min_measured=1.5),
           "a measured floor over 1 is refused")
    raises(lambda: compare([], [], min_measured=float("nan")),
           "a nan measured floor is refused")
    check(len(compare([], [], min_measured=0.0).entries) == 0
          and len(compare([], [], min_measured=1.0).entries) == 0,
          "a measured floor of 0 and of 1 are both legal")
    check(len(compare([], [], move_tolerance=0.0, reuse_distance=0.0)
              .entries) == 0,
          "a move tolerance of 0 is legal")
    check(len(compare([], [], area_ratio=1.0).entries) == 0,
          "an area ratio of 1 is legal")
    raises(lambda: compare([], [], move_tolerance=-1.0),
           "a negative move tolerance is refused")
    raises(lambda: compare([], [], move_tolerance=float("nan")),
           "a nan move tolerance is refused")
    raises(lambda: compare([], [], reuse_distance=0.5),
           "a reuse distance under the move tolerance is refused")
    raises(lambda: compare([], [], reuse_distance=float("inf")),
           "an infinite reuse distance is refused, it would never fire")
    raises(lambda: compare([], [], area_ratio=0.9),
           "an area ratio under 1 is refused")
    raises(lambda: compare([], [], min_overlap=1.5),
           "an overlap floor over 1 is refused")
    raises(lambda: compare([], [], min_overlap=float("nan")),
           "a nan overlap floor is refused")
    check(len(compare([], [], min_overlap=1.0).entries) == 0
          and len(compare([], [], min_overlap=0.0).entries) == 0,
          "an overlap floor of 0 and of 1 are both legal")
    check(len(compare([], [], reuse_distance=1.0, move_tolerance=1.0)
              .entries) == 0,
          "a reuse distance equal to the move tolerance is legal")

    # ---- the column plan
    fields, only = plan_fields(["K", "X", "Y", "A", "O"], ["K", "X", "Y", "A",
                                                            "O", "N"],
                               ["K"], "X", "Y", "A", [])
    check(fields == ["A", "O"] and only == ["N"],
          "the key and the coordinates are not compared, and a column only "
          "one vintage has is named")
    fields, only = plan_fields(["K", "O", "P"], ["K", "O", "P"], ["K"], None,
                               None, None, ["P"])
    check(fields == ["P"] and only == [],
          "--compare-fields narrows the comparison")
    raises(lambda: plan_fields(["K"], ["K", "O"], ["K"], None, None, None,
                               ["O"]),
           "a requested column missing from the old CSV is refused")
    raises(lambda: plan_fields(["K", "O"], ["K"], ["K"], None, None, None,
                               ["O"]),
           "and from the new CSV")
    raises(lambda: plan_fields([], ["K"], ["K"], None, None, None, []),
           "an empty header is refused")
    raises(lambda: plan_fields(["K"], ["K"], ["K"], "X", "Y", None, []),
           "a coordinate column that does not exist is refused")
    raises(lambda: plan_fields(["K", "X", "Y"], ["K", "X", "Y"], ["K"], "X",
                               "Y", "A", []),
           "an area column that does not exist is refused")

    # ---- degrees
    check(looks_like_degrees(vintage(mk("a", -82.1, 29.2))),
          "longitude and latitude read as degrees")
    check(not looks_like_degrees(vintage(mk("a", 620100.0, 1580200.0))),
          "state-plane feet do not")
    check(not looks_like_degrees(vintage(mk("a", 10.0, 10.0),
                                         mk("b", 620100.0, 1580200.0))),
          "one projected row among small ones is enough to say projected")
    check(not looks_like_degrees(vintage(mk("a", 10.0, 1580200.0)))
          and not looks_like_degrees(vintage(mk("a", 620100.0, 10.0))),
          "one coordinate outside the box is enough to say projected")
    check(not looks_like_degrees(vintage(mk("a"))),
          "no coordinates is not degrees")
    check(looks_like_degrees(vintage(mk("a", 180.0, 90.0)))
          and looks_like_degrees(vintage(mk("a", -180.0, -90.0))),
          "the corners of the longitude and latitude box are degrees")
    check(not looks_like_degrees(vintage(mk("a", 180.5, 0.0)))
          and not looks_like_degrees(vintage(mk("a", 0.0, 90.5))),
          "just outside the box is not degrees")

    # ---- reading real files
    path = put("bom.csv", "\ufeffK,V\r\n1,a\r\n\r\n2,\"line\r\nbreak\"\r\n"
                          "3,c\r\n")
    raw, header = read_csv(path)
    check(header == ["K", "V"],
          "a UTF-8 BOM does not glue itself to the first column name"
          "  <-- pinned defect")
    check([r.line for r in raw] == [2, 4, 6],
          "a blank line and a quoted newline do not move the line a record "
          "starts on  <-- pinned defect")
    path = put("short.csv", "K,V\n1\n2,b,extra,more\n")
    raw, header = read_csv(path)
    rows = extract_rows(raw, ["K"], None, None, None)
    check(rows[0].values == {"K": "1", "V": ""}
          and rows[1].values == {"K": "2", "V": "b"},
          "a short row reads as blank and a long row loses its extras")
    path = put("ragged.csv", "PARCEL,ADDR,X,Y\nA,1 Main St, Apt 2,620100,"
                             "1580200\n")
    raw, header = read_csv(path)
    rows = extract_rows(raw, ["PARCEL"], "X", "Y", None)
    check(rows[0].x is None and rows[0].y == 620100.0,
          "an unquoted comma in an address shifts the cells, so Y reads "
          "the X value")
    path = put("extra.csv", "K,V\n2,b,\"x\ny\"\n3,c\n")
    check([r.line for r in read_csv(path)[0]] == [2, 4],
          "a quoted newline in an extra cell does not move the line either")
    raises(lambda: read_csv(put("dup.csv", "K,V,V\n1,a,b\n")),
           "a repeated column name is refused, the CSV reader would keep "
           "the last and drop the first  <-- pinned defect")
    raises(lambda: read_csv(put("blanknames.csv", "K,,\n1,2,3\n")),
           "two empty column names are refused as a repeated name")
    check(read_csv(put("empty.csv", ""))[1] == [],
          "an empty file has no header")
    rows = extract_rows([{"K": "1", "X": "1,000", "Y": "5", "A": ""}],
                        ["K"], "X", "Y", "A")
    check(rows[0].x is None and rows[0].y == 5.0 and rows[0].area is None,
          "a coordinate with a thousands separator and a blank area read "
          "as missing")

    # ---- the CLI on the demo vintages
    old_path = put("old.csv", OLD_CSV)
    new_path = put("new.csv", NEW_CSV)
    base = [old_path, new_path, "--x-field", "X", "--y-field", "Y",
            "--area-field", "ACRES"]
    code, out, err = run_cli(base)
    check(code == 1 and "keys classified: 7 of 7" in out
          and "  REUSED     2" in out and "  CHANGED    1" in out
          and "  ADDED      1" in out and "  REMOVED    1" in out
          and "  UNCHANGED  2" in out,
          "the demo run classifies seven keys and exits 1")
    check("P-3  area changed by a factor of 2.11" in out
          and "P-4  centroid moved 10065.8 ft" in out
          and "P-2  OWNER: 'Baker' -> 'Bakker'" in out
          and "P-7 (new line 7)" in out and "P-5 (old line 6)" in out,
          "the demo run names each key and its reason")
    check("USE" not in out.split("keys whose attributes")[1].split("keys only")
          [0], "an unchanged column is not listed")
    check(err == "", "and nothing is on stderr")
    code, out, err = run_cli([old_path, new_path, "--skip-reuse-check"])
    check(code == 0 and "key reuse: NOT CHECKED" in out
          and "named (--skip-reuse-check)" in out
          and out.rstrip().endswith("Key reuse was not checked.")
          and "  CHANGED    3" in out,
          "the same files with no geometry column diff and say reuse was "
          "not checked")
    check("columns only one vintage has" not in out,
          "two vintages with the same columns print no column note")
    code, out, err = run_cli([old_path, new_path])
    check(code == 4 and "VERDICT: UNVERIFIED, key reuse was not checked" in out
          and "--skip-reuse-check" in out,
          "the same files with no geometry column and no flag exit 4, and "
          "the verdict names the flag  <-- pinned defect")
    code, out, _ = run_cli(base + ["--reuse-distance", "800", "--area-ratio",
                                   "3", "--units", "m"])
    check("by centroid moved above 800.0 m or area ratio above 3.00" in out
          and "P-4  centroid moved 10065.8 m" in out,
          "the run prints the thresholds and the unit it used")
    code, out, _ = run_cli(base + ["--compare-fields", "USE"])
    check(code == 1 and "  CHANGED    0" not in out
          and "OWNER:" not in out,
          "--compare-fields USE ignores the owner edit")
    code, out, _ = run_cli(base + ["--sample", "0"])
    check(code == 1 and "keys that now name" not in out
          and "  REUSED     2" in out,
          "--sample 0 lists nothing and still counts")
    code, out, _ = run_cli(base + ["--sample", "1"])
    check("... and 1 more" in out,
          "--sample 1 cuts the list of two reissued keys and says so")
    code, out, _ = run_cli(base + ["--reuse-distance", "20000",
                                   "--area-ratio", "3"])
    check(code == 0 and "  REUSED" not in out and "  CHANGED    3" in out,
          "looser thresholds turn both reissued keys into plain edits")
    code, out, _ = run_cli([old_path, put("new2.csv", NEW_CSV.replace(
        "PARCEL,X,Y,ACRES,OWNER,USE", "PARCEL,X,Y,ACRES,OWNER,ZONE")),
        "--key-field", "PARCEL", "--skip-reuse-check"])
    check(code == 0 and "columns only one vintage has, not compared: USE, "
          "ZONE" in out,
          "a column only one vintage has is named, not skipped silently")

    # ---- the refusals through the CLI
    dupe_new = put("dupe.csv", NEW_CSV + "P-1,999999,999999,1.00,Zed,RES\n")
    code, out, err = run_cli([old_path, dupe_new])
    check(code == 3 and "new key 'P-1' is on 2 rows: line 2, 8" in out
          and "classified" not in out and "VERDICT: REFUSED" in out,
          "a repeated key exits 3 and prints no counts")
    out_path = os.path.join(tmp, "refused.csv")
    code, out, _ = run_cli([old_path, dupe_new, "--out", out_path,
                            "--apply"])
    check(code == 3 and not os.path.exists(out_path)
          and "Refused. %s was not written" % out_path in out
          and "wrote " not in out,
          "a refused run writes nothing even with --apply")
    gap = put("gap.csv", "PARCEL,OWNER\n,Ames\nP-1,Baker\n")
    code, out, _ = run_cli([gap, gap])
    check(code == 3 and "old line 2 has no key" in out,
          "a blank key exits 3")
    code, out, _ = run_cli([gap, gap, "--allow-blank-keys",
                            "--skip-reuse-check"])
    check(code == 0 and "  UNKEYED    2" in out
          and "VERDICT: clean. 0 changed, 0 added, 0 removed, 2 row(s) left "
          "out because they have no key." in out,
          "--allow-blank-keys counts the blank rows and diffs the rest")
    zeros_old = put("z_old.csv", "PARCEL,OWNER\n0012,A\n0013,B\n")
    zeros_new = put("z_new.csv", "PARCEL,OWNER\n12,A\n13,B\n")
    code, out, _ = run_cli([zeros_old, zeros_new])
    check(code == 4 and "VERDICT: UNVERIFIED" in out,
          "the leading-zero case exits 4 through the CLI "
          " <-- pinned defect")
    zero_out = os.path.join(tmp, "zero_out.csv")
    code, out, _ = run_cli([zeros_old, zeros_new, "--out", zero_out,
                            "--apply"])
    check(code == 4 and not os.path.exists(zero_out)
          and "Unverified. %s was not written" % zero_out in out
          and "wrote " not in out,
          "an UNVERIFIED run writes no file even with --apply, so a job "
          "never reads a plausible diff from it  <-- pinned defect")
    sep_old = put("s_old.csv", "PARCEL,X,Y\nA,\"620,100\",1580200\n")
    code, out, _ = run_cli([sep_old, sep_old, "--x-field", "X", "--y-field",
                            "Y"])
    check(code == 4 and "measured no pair" in out,
          "thousands separators in every coordinate exit 4, not 0"
          "  <-- pinned defect")
    comp_path = put("comp.csv", "BOOK,PAGE,OWNER\n7,1,A\n7,2,B\n")
    code, out, _ = run_cli([comp_path, comp_path, "--key-field", "BOOK"])
    check(code == 3, "a composite key's first column alone is refused")
    code, out, _ = run_cli([comp_path, comp_path, "--key-field",
                            "BOOK, PAGE", "--skip-reuse-check"])
    check(code == 0 and "key BOOK|PAGE" in out,
          "naming both columns makes the key unique, spaces and all")

    # ---- usage errors
    code, _, err = run_cli([])
    check(code == 64 and "two CSV vintages are required" in err,
          "a run with no vintage is a usage error")
    check(run_cli([old_path])[0] == 64, "one vintage is a usage error too")
    code, out, err = run_cli(base + ["--out", out_path, "--ap"])
    check(code == 64 and "unrecognized arguments: --ap" in err
          and not os.path.exists(out_path),
          "--ap is refused, not read as --apply, and writes nothing"
          "  <-- pinned defect")
    code, _, err = run_cli(base + ["--out", out_path, "--appl"])
    check(code == 64 and not os.path.exists(out_path),
          "--appl is refused too  <-- pinned defect")
    code, _, err = run_cli(base + ["--key"])
    check(code == 64, "a prefix of --key-field is refused")
    check(run_cli(["--help"])[0] == 0, "--help still exits 0")
    code, _, err = run_cli(base + ["--apply"])
    check(code == 64 and "--apply needs --out" in err,
          "--apply without --out is a usage error")
    code, _, err = run_cli([old_path, new_path, "--x-field", "X"])
    check(code == 64 and "go together" in err,
          "--x-field without --y-field is a usage error")
    code, _, err = run_cli([old_path, new_path, "--key-field", " , "])
    check(code == 64 and "--key-field names no column" in err,
          "a key field list with no name in it is a usage error")
    code, _, err = run_cli(base + ["--sample", "-1"])
    check(code == 64 and "--sample cannot be negative" in err,
          "a negative sample is a usage error")
    code, _, err = run_cli(base + ["--compare-fields", ","])
    check(code == 64 and "--compare-fields names no column" in err,
          "--compare-fields with no name in it is a usage error, not 'every "
          "shared column'  <-- pinned defect")
    code, _, err = run_cli([old_path, new_path, "--skip-reuse-check",
                            "--area-field", "ACRES"])
    check(code == 64 and "contradicts" in err,
          "--skip-reuse-check beside a geometry column is a usage error")
    code, _, err = run_cli(base + ["--min-measured", "1.5"])
    check(code == 64 and "minimum measured share" in err,
          "a measured floor over 1 is a usage error")
    code, _, err = run_cli([put("blank_names.csv", "PARCEL,,\nA,1,2\n"),
                            old_path])
    check(code == 64 and "repeated: (blank name)" in err,
          "two empty column names are a usage error that says what they are")
    utf16 = os.path.join(tmp, "utf16.csv")
    with open(utf16, "wb") as handle:
        handle.write(u"PARCEL,OWNER\nA,B\n".encode("utf-16"))
    code, _, err = run_cli([utf16, utf16, "--skip-reuse-check"])
    check((code == 64 and "lacks column(s): PARCEL" in err)
          or (code == 2 and "NUL" in err),
          "a UTF-16 export is refused: as a missing column on Python 3.11 "
          "and later, and as a NUL byte on 3.9 and 3.10")
    code, _, err = run_cli(base + ["--reuse-distance", "0.5"])
    check(code == 64 and "reuse distance" in err,
          "a reuse distance under the move tolerance is a usage error")
    code, _, err = run_cli(base + ["--out", old_path])
    check(code == 64 and "also an input vintage" in err
          and slurp(old_path) == OLD_CSV,
          "--out naming an input is refused on a dry run, and the input is "
          "untouched  <-- pinned defect")
    check(run_cli(base + ["--out", new_path, "--apply"])[0] == 64
          and slurp(new_path) == NEW_CSV,
          "and with --apply the new vintage survives")
    code, _, err = run_cli([old_path, new_path, "--key-field", "NOPE"])
    check(code == 64 and "lacks column(s): NOPE" in err,
          "a key column that is not in the CSV is a usage error")
    code, _, err = run_cli([put("rep.csv", "K,K\n1,2\n"), old_path,
                            "--key-field", "K"])
    check(code == 64 and "repeated: K" in err,
          "a repeated column name is a usage error through the CLI")
    code, _, err = run_cli([os.path.join(tmp, "absent.csv"), new_path])
    check(code == 2, "a file that does not exist exits 2, not 64")
    code, _, err = run_cli([tmp, new_path])
    check(code == 2, "a directory in place of a file exits 2")
    huge = put("huge.csv", "PARCEL,OWNER\nA," + "x" * 140000 + "\n")
    code, _, err = run_cli([huge, huge])
    check(code == 2 and "field larger than field limit" in err,
          "a cell over the csv size limit exits 2, not a crash")
    degrees = put("deg.csv", "PARCEL,X,Y\nA,-82.1,29.2\n")
    code, _, err = run_cli([degrees, degrees, "--x-field", "X", "--y-field",
                            "Y"])
    check(code == 64 and "look like degrees" in err,
          "coordinates that fit longitude and latitude are refused"
          "  <-- pinned defect")
    code, out, _ = run_cli([degrees, degrees, "--x-field", "X", "--y-field",
                            "Y", "--projected"])
    check(code == 0, "--projected skips the degrees check")

    # ---- what a clean exit does not cover (each is a README limit)
    def raw_put(name, data):
        path = os.path.join(tmp, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    keys = ["K%d" % n for n in range(1000)]
    full = put("full.csv", "PARCEL,OWNER\n" + "".join(
        "%s,A\n" % k for k in keys))
    cut = put("cut.csv", "PARCEL,OWNER\n" + "".join(
        "%s,A\n" % k for k in keys[:10]))
    code, out, _ = run_cli([full, cut, "--skip-reuse-check"])
    check(code == 0 and "VERDICT: clean. 0 changed, 0 added, 990 removed."
          in out,
          "a new vintage that holds 10 of 1000 keys exits 0 and lists the "
          "rest as REMOVED")
    old_fix = raw_put("cp_old.csv", b"PARCEL,OWNER\nA,Mu\xf1oz\n"
                                    b"\xe91,a\n\xe82,b\n")
    new_fix = raw_put("cp_new.csv", b"PARCEL,OWNER\nA,Mu\xe4oz\n"
                                    b"\xe81,a\n\xe92,b\n")
    code, out, _ = run_cli([old_fix, new_fix, "--skip-reuse-check"])
    check(code == 0 and "UNCHANGED  3" in out,
          "cp1252 owners that differ in one accented letter read as equal, "
          "and two keys that swap accented letters read as UNCHANGED")
    collide = raw_put("collide.csv", b"PARCEL,V\n\xe91,a\n\xe81,b\n")
    code, out, _ = run_cli([collide, collide, "--skip-reuse-check"])
    check(code == 3 and "key '\ufffd1' is on 2 rows" in out,
          "two keys that differ only in non-UTF-8 bytes collapse into one "
          "and are refused as a repeated key")

    def shift(n):
        return "%.1f" % (n * 0.3048)

    feet = [("P-1", 620100, 1580200), ("P-2", 621000, 1581000),
            ("P-3", 622600, 1582600)]
    old_ft = put("crs_old.csv", "PARCEL,X,Y\n" + "".join(
        "%s,%d,%d\n" % row for row in feet))
    new_m = put("crs_new.csv", "PARCEL,X,Y\n" + "".join(
        "%s,%s,%s\n" % (k, shift(x), shift(y)) for k, x, y in feet))
    code, out, err = run_cli([old_ft, new_m, "--x-field", "X", "--y-field",
                              "Y"])
    check(code == 1 and "  REUSED     3" in out and "CRS" not in out + err,
          "feet against metres shows as every key REUSED and never names "
          "the CRS")
    wide = [Row(1, ("a|b", "c"), {}), Row(2, ("a", "b|c"), {})]
    report = compare(wide, wide, skip_reuse=True)
    check(report.pairs == 2 and [e.key for e in report.entries]
          == ["a|b|c", "a|b|c"],
          "two composite keys can print as one label although the tool "
          "keeps them apart")
    unlike = compare(vintage(mk(u"\u00e9"), mk("A"), mk(u"B\u200b")),
                     vintage(mk(u"e\u0301"), mk("a"), mk("B")),
                     skip_reuse=True)
    check(unlike.pairs == 0 and gate(unlike) == 4
          and unlike.counts()[ADDED] == 3 and unlike.counts()[REMOVED] == 3,
          "NFC against NFD, a case difference and a zero-width space do not "
          "pair, so the run reports ADDED and REMOVED and exits 4")
    nl_old = put("nl_old.csv", "PARCEL,OWNER\nP-1,a\n")
    nl_new = put("nl_new.csv", "PARCEL,OWNER\nP-1,\"b\nc\x1b[31m\"\n")
    code, out, _ = run_cli([nl_old, nl_new, "--skip-reuse-check"])
    check("c\x1b[31m' (old line 2, new line 2)" in out.splitlines(),
          "a line break and a terminal escape in a cell print as they are")
    eq_path = put("eq.csv", "PARCEL,OWNER\n=1+1,A\n")
    eq_out = os.path.join(tmp, "eq_out.csv")
    run_cli([eq_path, eq_path, "--skip-reuse-check", "--out", eq_out,
             "--apply"])
    check("UNCHANGED,=1+1,2,2," in slurp(eq_out),
          "a key that starts with = is written to --out unescaped")
    hl_old = put("hl_old.csv", OLD_CSV)
    hl_new = put("hl_new.csv", NEW_CSV)
    hl_alias = os.path.join(tmp, "hl_alias.csv")
    os.link(hl_old, hl_alias)
    code, _, _ = run_cli([hl_old, hl_new, "--skip-reuse-check", "--out",
                          hl_alias, "--apply"])
    check(code == 0 and slurp(hl_old).startswith("class,key,"),
          "a hard link to an input passes the --out check and --apply "
          "overwrites the input through it")

    class Closed(io.StringIO):
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

    shut, shut_err = Closed(), io.StringIO()
    with contextlib.redirect_stdout(shut), contextlib.redirect_stderr(
            shut_err):
        code = main(base)
    check(code == 70 and "BrokenPipeError" in shut_err.getvalue(),
          "a pipe closed by the reader ends the run with exit 70")

    # ---- writing
    out_path = os.path.join(tmp, "changes.csv")
    code, out, _ = run_cli(base + ["--out", out_path])
    check(code == 1 and not os.path.exists(out_path)
          and "Check only. %s was not written. Re-run with --apply." % out_path
          in out and "wrote " not in out,
          "without --apply the output is not written, and the run says so")
    code, out, _ = run_cli(base + ["--out", out_path, "--apply"])
    check(code == 1 and os.path.exists(out_path)
          and ("wrote %s" % out_path) in out,
          "--apply writes the output")
    with open(out_path, newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    check(len(written) == 7 and sorted(written[0]) == sorted(OUT_COLUMNS),
          "the output has one row per key and the documented columns")
    by = dict((r["key"], r) for r in written)
    check(by["P-4"]["class"] == REUSED and by["P-7"]["old_line"] == ""
          and by["P-7"]["new_line"] == "7" and by["P-5"]["new_line"] == "",
          "the output carries the class and the line on each side")
    code, out, err = run_cli(base + ["--out", os.path.join(
        tmp, "no", "such", "dir.csv"), "--apply"])
    check(code == 2 and "could not write" in err,
          "a write that fails exits 2, not the 1 that means a reissued key"
          "  <-- pinned defect")
    real_rows = globals()["classified_rows"]
    part_path = os.path.join(tmp, "part.csv")
    try:
        def half_way(report):
            yield real_rows(report)[0]
            raise OSError("probe: disk full")
        globals()["classified_rows"] = half_way
        code, _, err = run_cli(base + ["--out", part_path, "--apply"])
    finally:
        globals()["classified_rows"] = real_rows
    check(code == 2 and "disk full" in err and os.path.exists(part_path)
          and slurp(part_path).count("\n") == 2,
          "a write that fails half way exits 2 and leaves the partial file "
          "it had written")
    unkeyed_path = os.path.join(tmp, "unkeyed.csv")
    run_cli([gap, gap, "--allow-blank-keys", "--skip-reuse-check", "--out",
             unkeyed_path, "--apply"])
    with open(unkeyed_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    check([r["class"] for r in rows].count(UNKEYED) == 2
          and rows[0]["key"] == "",
          "a blank-key row is written with a blank key and its class")

    # ---- exit codes and the entry point
    real_run = globals()["_run"]
    try:
        def boom(argv):
            raise RuntimeError("probe")
        globals()["_run"] = boom
        code, _, err = run_cli(["x"])
    finally:
        globals()["_run"] = real_run
    check(code == 70 and "unexpected RuntimeError" in err
          and "not a finding" in err and "Traceback" in err,
          "an unexpected exception exits 70, not the 1 that means a key "
          "was reissued  <-- pinned defect")
    real_self_test = globals()["self_test"]
    try:
        globals()["self_test"] = lambda: 7
        handed = run_cli(["--self-test", "ignored.csv"])[0]
    finally:
        globals()["self_test"] = real_self_test
    check(handed == 7,
          "main returns the exit status of --self-test, so a red self-test "
          "fails CI  <-- pinned defect")

    # A strict cp1252 stdout, which is what a pipe on Windows is.
    odd = put("odd.csv", "")
    odd_new = put("odd_new.csv", "PARCEL,OWNER\nZ,A\nB,C\n")
    with open(odd, "wb") as handle:
        handle.write(b"PARCEL,OWNER\n\xff,A\nZ,A\n")
    strict = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                              errors="strict")
    real_stdout = sys.stdout
    sys.stdout = strict
    try:
        code = main([odd, odd_new, "--skip-reuse-check"])
        strict.flush()
        shown = strict.buffer.getvalue()
    finally:
        sys.stdout = real_stdout
    check(code == 0 and b"VERDICT: clean" in shown
          and b"? (old line 2)" in shown,
          "a key the console cannot encode prints as ? and the verdict "
          "still arrives  <-- pinned defect")
    _tolerate_odd_characters()
    check(True, "the stream setup runs on the real console too")

    # The module as a script, so the last two lines are exercised for real.
    saved_argv = sys.argv
    sys.argv = ["parceldiff.py", "--ap"]
    quiet_err = io.StringIO()
    script_code = None
    try:
        with contextlib.redirect_stderr(quiet_err):
            runpy.run_path(os.path.abspath(__file__), run_name="__main__")
    except SystemExit as exc:
        script_code = exc.code
    finally:
        sys.argv = saved_argv
    check(script_code == 64,
          "run as a script, --ap exits 64")

    # A self-test that raises still removes its temp directory.
    seen = []
    real_body = globals()["_self_test"]
    try:
        def crash(path):
            seen.append(path)
            raise RuntimeError("probe")
        globals()["_self_test"] = crash
        try:
            self_test()
        except RuntimeError:
            seen.append("raised")
    finally:
        globals()["_self_test"] = real_body
    check(len(seen) == 2 and seen[1] == "raised"
          and not os.path.exists(seen[0]),
          "a self-test that raises still removes its temp directory  "
          "<-- pinned defect")

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1"
          "  <-- pinned defect")

    # ---- importing the module runs nothing.
    spec = importlib.util.spec_from_file_location(
        "parceldiff_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(buf.getvalue() == "" and imported.gate(imported.compare([], [])) == 4,
          "importing the module prints nothing and exposes the core")

    return summary(passed[0], failed)


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="parceldiff.py",
        description="Diff two vintages of a parcel layer on a key, and "
                    "refuse a key that cannot carry the diff.",
        epilog="Both vintages must be in one projected CRS. Nothing is "
               "written without --apply.",
        allow_abbrev=False,
    )
    ap.add_argument("old", nargs="?", metavar="OLD",
                    help="the older vintage as a CSV")
    ap.add_argument("new", nargs="?", metavar="NEW",
                    help="the newer vintage as a CSV")
    ap.add_argument("--key-field", dest="key_field", default=DEFAULT_KEY_FIELD,
                    help="the key column, or several joined by commas for a "
                         "composite key (default %s)" % DEFAULT_KEY_FIELD)
    ap.add_argument("--x-field", dest="x_field",
                    help="the centroid X column. Needs --y-field.")
    ap.add_argument("--y-field", dest="y_field",
                    help="the centroid Y column. Needs --x-field.")
    ap.add_argument("--area-field", dest="area_field",
                    help="the area column, used to judge a reissued key")
    ap.add_argument("--compare-fields", dest="compare_fields",
                    help="the columns to compare, joined by commas. Default: "
                         "every column both vintages share.")
    ap.add_argument("--reuse-distance", dest="reuse_distance", type=float,
                    default=DEFAULT_REUSE_DISTANCE,
                    help="centroid shift above which a key is judged "
                         "reissued, in the CRS's own units (default %.0f)"
                         % DEFAULT_REUSE_DISTANCE)
    ap.add_argument("--area-ratio", dest="area_ratio", type=float,
                    default=DEFAULT_AREA_RATIO,
                    help="area factor above which a key is judged reissued "
                         "(default %.1f)" % DEFAULT_AREA_RATIO)
    ap.add_argument("--move-tolerance", dest="move_tolerance", type=float,
                    default=DEFAULT_MOVE_TOLERANCE,
                    help="centroid shift above which a parcel counts as "
                         "changed (default %.1f)" % DEFAULT_MOVE_TOLERANCE)
    ap.add_argument("--min-overlap", dest="min_overlap", type=float,
                    default=DEFAULT_MIN_OVERLAP,
                    help="share of the smaller vintage's keys that must be on "
                         "both sides, 0 to 1 (default %.1f)"
                         % DEFAULT_MIN_OVERLAP)
    ap.add_argument("--min-measured", dest="min_measured", type=float,
                    default=DEFAULT_MIN_MEASURED,
                    help="share of the shared keys that each reuse test must "
                         "have measured, 0 to 1 (default %.1f)"
                         % DEFAULT_MIN_MEASURED)
    ap.add_argument("--skip-reuse-check", dest="skip_reuse",
                    action="store_true",
                    help="accept a plain diff when no coordinate or area "
                         "column is named. Without this such a run exits 4.")
    ap.add_argument("--allow-blank-keys", dest="allow_blank",
                    action="store_true",
                    help="leave rows with no key out and count them, instead "
                         "of refusing the run")
    ap.add_argument("--units", default=DEFAULT_UNITS,
                    help="label printed after every distance. Nothing is "
                         "converted. (default %s)" % DEFAULT_UNITS)
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="how many records to list under each heading "
                         "(default %d)" % DEFAULT_SAMPLE)
    ap.add_argument("--projected", action="store_true",
                    help="say that the coordinates are projected, to skip the "
                         "check that refuses values that all fit longitude "
                         "and latitude")
    ap.add_argument("--out", help="path for the classification CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _names(text):
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode.

    A piped stdout on Windows is cp1252, and a key read from a file that is
    not UTF-8 arrives as U+FFFD, which cp1252 cannot write. The
    UnicodeEncodeError would exit 1 and the VERDICT line would never print.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


def _same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(
        os.path.realpath(b))


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a key was reissued'."""
    _tolerate_odd_characters()
    try:
        return _run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the data." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        return 70


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file
        # could not be read. A mistyped --ap is a usage error, so it gets 64.
        return 64 if exc.code == 2 else exc.code

    if args.self_test:
        return self_test()

    if not args.old or not args.new:
        print("error: two CSV vintages are required. Use --self-test to "
              "verify the tool without them.", file=sys.stderr)
        return 64
    key_fields = _names(args.key_field)
    if not key_fields:
        print("error: --key-field names no column.", file=sys.stderr)
        return 64
    if bool(args.x_field) != bool(args.y_field):
        print("error: --x-field and --y-field go together.", file=sys.stderr)
        return 64
    if args.sample < 0:
        print("error: --sample cannot be negative.", file=sys.stderr)
        return 64
    if args.compare_fields is not None and not _names(args.compare_fields):
        print("error: --compare-fields names no column.", file=sys.stderr)
        return 64
    if args.skip_reuse and (args.x_field or args.area_field):
        print("error: --skip-reuse-check contradicts --x-field, --y-field "
              "and --area-field, which turn the check on.", file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return 64
    if args.out:
        # Checked on a dry run too: the run that would clobber a vintage
        # should say so before --apply is added, not after.
        for vintage in (args.old, args.new):
            if _same_file(args.out, vintage):
                print("error: --out %s is also an input vintage. Name a new "
                      "file." % args.out, file=sys.stderr)
                return 64

    try:
        old_raw, old_header = read_csv(args.old)
        new_raw, new_header = read_csv(args.new)
        fields, only = plan_fields(
            old_header, new_header, key_fields, args.x_field, args.y_field,
            args.area_field, _names(args.compare_fields))
    except ValueError as exc:
        # A column name the operator got wrong is a usage error, and a file
        # that could not be read is not. Only the exit code tells them apart.
        print("error: %s" % exc, file=sys.stderr)
        return 64
    except (IOError, OSError, csv.Error) as exc:
        # csv.Error too. A field over csv's size limit, or a NUL in the
        # file, is an unreadable input, not a diff that failed.
        print("error: %s" % exc, file=sys.stderr)
        return 2

    old = extract_rows(old_raw, key_fields, args.x_field, args.y_field,
                       args.area_field)
    new = extract_rows(new_raw, key_fields, args.x_field, args.y_field,
                       args.area_field)
    if args.x_field and not args.projected and looks_like_degrees(old + new):
        print("error: every coordinate fits longitude -180..180 and latitude "
              "-90..90, so these look like degrees. Distances in degrees mean "
              "nothing against a threshold in feet. Project both vintages, or "
              "pass --projected if this is a local grid.", file=sys.stderr)
        return 64

    try:
        report = compare(
            old, new, fields, args.reuse_distance, args.area_ratio,
            args.move_tolerance, args.min_overlap, bool(args.x_field),
            bool(args.area_field), args.allow_blank, args.units,
            args.min_measured, args.skip_reuse)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    if only:
        report.notes.append("columns only one vintage has, not compared: %s"
                            % ", ".join(only))

    # After compare, so a usage error leaves stdout empty rather than looking
    # like the start of a run that went well.
    print("parceldiff: %s %d row(s), %s %d row(s), key %s"
          % (args.old, len(old), args.new, len(new), "|".join(key_fields)))
    for line in describe(report, args.sample):
        print(line)

    if args.out:
        if not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
        elif report.refused:
            print("\nRefused. %s was not written, because nothing was "
                  "classified." % args.out)
        elif unverified_reasons(report):
            print("\nUnverified. %s was not written, because a file of "
                  "classes would look like a diff that can be trusted."
                  % args.out)
        else:
            # A traceback here exits 1, and 1 is the code a scheduled job
            # reads as "a key was reissued". A write that failed is a
            # different fact and gets its own exit code.
            try:
                write_classified(args.out, report)
            except (IOError, OSError, csv.Error) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.out)

    return gate(report)


if __name__ == "__main__":
    sys.exit(main())
