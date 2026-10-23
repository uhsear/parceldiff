# parceldiff

Diff two vintages of a parcel layer on a key, and refuse a key that cannot carry the diff.
Refuses the key when it is on more than one row of either vintage, flags every key that now names
a different parcel than it did, and refuses to call a run clean when it could not check.
Optionally writes the classification of every key to a CSV.

A nightly job compares yesterday's parcel layer with today's. It loads each layer into a lookup
table keyed by the parcel number, subtracts one set of keys from the other, and reports what was
added, what was removed and what changed.

Three things go wrong, and none of them raises an error.

The first is a parcel number on two rows. A bad import leaves one number on two parcels. The lookup
table keeps one row and drops the other without a word. The job logs the size of the table, not
the number of rows it read, so the log looks healthy. The changes it reports pair with the wrong
parcel, and nobody can tell which.

The second is a number that is unique but was handed to a different parcel. A split or a merge
retires an old number and issues it again. The two rows pair on the key. The owner differs, the
area differs, and the report calls it an ordinary edit. The only evidence is that the parcel
moved a long way or changed size, and no diff measures that.

The third is a diff that ran and paired nothing, or paired and could not look. One vintage writes
`0012` and the other writes `12`, because the file passed through a tool that stored the key as a
number. Every parcel is "removed" and every parcel is "added". Or an export writes `1,580,200`,
which is not a number, so the centroid test reads nothing and the run still ends clean. A
scheduled job reads the clean exit as "safe to publish". This tool exits 4 and says so.

The fixtures in this README are synthetic. No real layer is quoted.

```
$ python parceldiff.py --self-test
parceldiff self-test: temporary files only, no network, no credentials
--------------------------------------------------------------------
PASS  a plain number and a padded number both read
...
PASS  two 19-digit ids that differ in the last digit are two values  <-- pinned defect
...
PASS  a key on two rows refuses the run and classifies nothing  <-- pinned defect
...
PASS  a key that jumped 9000 units is REUSED, not an owner edit, and the run exits 1  <-- pinned defect
...
PASS  keys spelled 0012 and 12 pair nothing, so the run exits 4, not 0  <-- pinned defect
...
PASS  a diff with no coordinate and no area column exits 4, not a clean 0  <-- pinned defect
PASS  one readable pair in fifty exits 4, not a clean 0  <-- pinned defect
...
PASS  --ap is refused, not read as --apply, and writes nothing  <-- pinned defect
...
PASS  a write that fails exits 2, not the 1 that means a reissued key  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
218 assertions, 0 failed
```

The full run prints all 218 assertions. Each `...` line above is where this block is cut.

## What already exists

The diff itself is a solved problem. These tools did most of the work before this one.

- **pandas.** `DataFrame.merge(indicator=True)` adds a `_merge` column that says whether each row
  came from the left side, the right side or both. `validate="one_to_one"` checks that "merge
  keys are unique in both left and right datasets"
  ([pandas.DataFrame.merge](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.merge.html)).
  The check works. On pandas 3.0.5 a merge with a key on two rows raised `MergeError: Merge keys
  are not unique in right dataset; not a one-to-one merge`. So the refusal in this tool is not
  new. What pandas does not do is look at a key that is on both sides and unique. On the demo
  vintages below, the same merge reported `P-3` and `P-4` as `both` and raised no warning, though
  the two rows are different parcels.
- **ArcGIS Pro, Feature Compare.** It "compares two feature classes or layers and returns the
  comparison results", with compare types for geometry, attributes, schema and spatial reference,
  at Basic, Standard and Advanced licence
  ([Feature Compare](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/feature-compare.html)).
  It lines the rows up by a sort field: "Sorting by a common field in both inputs ensures that you
  are comparing the same row from each input dataset." The page does not say that the sort field
  must be unique, and it does not say what happens with a repeated value. It also describes no
  threshold between a re-survey and a different parcel. Use it first when you want a full schema
  and geometry comparison. This tool does not compare schemas or vertices.
- **ArcGIS Pro, Detect Feature Changes.** It matches features in space, within a search distance
  that must be greater than zero. Its match fields are optional. It needs the Advanced licence,
  and its page describes only line features
  ([Detect Feature Changes](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/detect-feature-changes.html)).
  It is a different method from a key diff, and its page does not cover parcel polygons. The page
  also says input features should be "singlepart and not duplicate", which is about the features
  and not about a key.
- **QGIS, Detect dataset changes.** It "compares two vector layers, and determines which features
  are unchanged, added or deleted between the two", and it is "designed for comparing two
  different versions of the same dataset"
  ([Vector general](https://docs.qgis.org/latest/en/docs/user_manual/processing_algs/qgis/vectorgeneral.html)).
  Its parameter "Attributes to consider for match" compares "all attributes" by default, and its
  parameter "Geometry comparison behavior" offers Exact Match or Tolerant Match. It lists no key
  field that you name, so its page has no concept of a number that was reissued.
- **property-change-monitor.** A small pandas project that compares two land-parcel CSV snapshots
  on a parcel id and writes an Excel report of new, deleted and updated parcels
  ([Gopal12ud/property-change-monitor](https://github.com/Gopal12ud/property-change-monitor)).
  On 2026-10-23 I listed its files through the GitHub API and searched its four source files for
  "duplic", "unique", "is_unique" and "validate". The loader and the differ have no hit. The other
  two files use `nunique()` to count ids for the report. I found no uniqueness check on the id and
  no test of whether a shared id still names the same parcel. I did not run it.

I read the three documentation pages on 2026-10-23. I did not run either ArcGIS or QGIS tool, so
what this README says about them comes from their documentation and nothing more.

Before this repository was public, `gh search repos parceldiff` on 2026-10-22 returned no
repository with that name. On 2026-10-23 the same search returns only this repository, and
`gh search repos "parcel vintage compare"` and `gh search repos "parcel diff duplicate key"` return
nothing. A GitHub search finds only what its index holds, so this shows that the search found no
equivalent, and no more.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database. Both vintages are CSVs, which is what every parcel layer can be exported as.

The self-test prints the same 218 assertions on Windows (Python 3.13.2) and on Ubuntu (Python
3.9.25, 3.10.21, 3.11.16 and 3.12.3). The five outputs have the same checksum, apart from the line
endings that Windows adds. One assertion is written to hold on every version: on Python 3.9 and
3.10 the `csv` module refuses a NUL byte, so a UTF-16 file exits 2 there and 64 on 3.11 and later.
`coverage run --branch parceldiff.py --self-test` reports 100 percent of statements and branches
(1014 statements, 192 branches) on Windows and on Ubuntu.

The key is compared as text and the geometry is read from columns. The tool measures a straight
line between two centroids and converts nothing. Both vintages must already be in one projected
CRS, in feet or in metres. When every coordinate fits longitude (-180 to 180) and latitude (-90 to
90), the tool refuses the run and says the values look like degrees. A local grid that really sits
inside that box needs `--projected`.

```
git clone https://github.com/uhsear/parceldiff.git
```

## Usage

Export both vintages to CSV. Put a centroid X and Y, and an area, in columns if you want the
key-reuse check. Save the CSV as UTF-8.

```
python parceldiff.py --self-test
python parceldiff.py old.csv new.csv --key-field PARCEL --skip-reuse-check
python parceldiff.py old.csv new.csv --x-field X --y-field Y --area-field ACRES
python parceldiff.py old.csv new.csv --key-field BOOK,PAGE --compare-fields OWNER,USE --skip-reuse-check
python parceldiff.py old.csv new.csv --x-field X --y-field Y --reuse-distance 800 --area-ratio 3
python parceldiff.py old.csv new.csv --x-field X --y-field Y --area-field ACRES --out changes.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `OLD` | none | The older vintage as a CSV. Required unless `--self-test`. |
| `NEW` | none | The newer vintage as a CSV. Required unless `--self-test`. |
| `--key-field` | `PARCEL` | The key column. Name several, joined by commas, for a composite key. |
| `--x-field` | off | The centroid X column. Turns on the shift test. Needs `--y-field`. |
| `--y-field` | off | The centroid Y column. Needs `--x-field`. |
| `--area-field` | off | The area column. Turns on the area-ratio test. |
| `--compare-fields` | all shared | The columns to compare, joined by commas. |
| `--reuse-distance` | `500` | A centroid shift above this means the key names a different parcel. |
| `--area-ratio` | `2.0` | An area factor above this means the key names a different parcel. |
| `--move-tolerance` | `1.0` | A shift above this, and not above the reuse distance, counts as a changed geometry. |
| `--min-overlap` | `0.5` | The share of the smaller vintage's keys that must be on both sides. |
| `--min-measured` | `0.9` | The share of the shared keys that each reuse test must have measured. |
| `--skip-reuse-check` | off | Accept a plain diff when no coordinate or area column is named. Without it such a run exits 4. |
| `--allow-blank-keys` | off | Leave rows with no key out and count them, instead of refusing the run. |
| `--units` | `ft` | Label printed after every distance. Nothing is converted. |
| `--sample` | `10` | How many records to list under each heading. `0` lists none. |
| `--projected` | off | Say the coordinates are projected, to skip the check that refuses degrees. |
| `--out` | off | Write the classification of every key to this CSV. Needs `--apply`. |
| `--apply` | off | Actually write. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. Other flags are ignored. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is refused
with exit 64 instead of being read as `--apply`, `--appl` is refused too, and `--key` is refused
instead of being read as `--key-field`. The self-test pins all three.

`--out` must name a new file. A path that is also an input vintage is refused with exit 64, on a
dry run too. Without that check `--apply` would replace a source vintage with its own
classification. The check compares real paths and nothing else. See Limits.

## What it checks

Every key is audited before anything is compared. The key must be unique on each side, and no row
may lack one. After that, every key lands in exactly one class:

| Class | What it means |
|---|---|
| `UNCHANGED` | The key is on both sides and no compared column differs. |
| `CHANGED` | The key is on both sides and a compared column differs, or the centroid moved. |
| `REUSED` | The key is on both sides, and the centroid or the area says it is a different parcel. |
| `ADDED` | The key is only in the new vintage. |
| `REMOVED` | The key is only in the old vintage. |
| `UNKEYED` | The row has no key. Counted only under `--allow-blank-keys`. |

The counts add up. The line `keys classified: 7 of 7` compares the classified keys with the keys
that the two vintages hold between them, which is a separate sum. A `REUSED` key does not also
count as `CHANGED`, and its column differences are not reported, because the two rows are
different parcels.

A number is compared as an exact decimal, so `1` and `1.0` agree and a spreadsheet round trip is
not an edit. Two ids that differ in any digit, including the nineteenth, are two values. Text
compares exactly after trimming. A key is always text, never a number.

The demo vintages are two small tables. `old.csv` has six parcels and `new.csv` has six.

```
$ cat old.csv
PARCEL,X,Y,ACRES,OWNER,USE
P-1,620100,1580200,1.00,Ames,RES
P-2,620500,1580600,2.50,Baker,RES
P-3,621000,1581000,4.00,Cole,COM
P-4,621400,1581400,0.50,Dunn,RES
P-5,621800,1581800,10.0,Eaves,AGR
P-6,622600,1582600,3.00,Frye,RES
$ cat new.csv
PARCEL,X,Y,ACRES,OWNER,USE
P-1,620100,1580200,1.00,Ames,RES
P-2,620500.4,1580600,2.50,Bakker,RES
P-3,621000,1581000,1.90,Gray,COM
P-4,628000,1589000,0.50,Hale,RES
P-6,622600,1582600,3.00,Frye,RES
P-7,623000,1583000,6.00,Ivy,AGR
```

Name the centroid and the area, and the tool checks for a reissued key:

```
$ python parceldiff.py old.csv new.csv --x-field X --y-field Y --area-field ACRES
parceldiff: old.csv 6 row(s), new.csv 6 row(s), key PARCEL
keys classified: 7 of 7
  UNCHANGED  2
  CHANGED    1
  REUSED     2
  ADDED      1
  REMOVED    1
pairs: 5 on a shared key, 83 percent of the smaller vintage
key reuse: checked on 5 of 5 pair(s), by centroid moved above 500.0 ft or area ratio above 2.00

keys that now name a different parcel:
  P-3  area changed by a factor of 2.11 (old line 4, new line 4)
  P-4  centroid moved 10065.8 ft (old line 5, new line 5)

keys whose attributes changed:
  P-2  OWNER: 'Baker' -> 'Bakker' (old line 3, new line 3)

keys only in the new vintage:
  P-7 (new line 7)

keys only in the old vintage:
  P-5 (old line 6)

VERDICT: 2 key(s) now name a different parcel than they did. Do not pair these rows.
```

`P-2` moved 0.4 feet, which is under the move tolerance, so only its owner edit is reported.
`P-3` kept its number while its area fell by more than half. `P-4` is 10,066 feet from where it
was. The `old line` and `new line` numbers are the lines the records start on in their own files.
A blank line or a quoted field that holds a line break does not shift them.

Name no geometry column and the tool cannot tell whether a key was reissued, so it does not call
the run clean. It exits 4 with this verdict:

```
$ python parceldiff.py old.csv new.csv
...
VERDICT: UNVERIFIED, key reuse was not checked, because no coordinate or area column was named. Name --x-field and --y-field, or --area-field, or pass --skip-reuse-check to accept a plain diff.
```

If you want a plain diff on purpose, say so with `--skip-reuse-check`. The tool then prints the
same lists, calls `P-3` and `P-4` edits, and ends with a verdict that says it did not check:

```
$ python parceldiff.py old.csv new.csv --skip-reuse-check
parceldiff: old.csv 6 row(s), new.csv 6 row(s), key PARCEL
keys classified: 7 of 7
  UNCHANGED  2
  CHANGED    3
  ADDED      1
  REMOVED    1
pairs: 5 on a shared key, 83 percent of the smaller vintage
key reuse: NOT CHECKED, no coordinate or area column was named (--skip-reuse-check)

keys whose attributes changed:
  P-2  X: '620500' -> '620500.4'; OWNER: 'Baker' -> 'Bakker' (old line 3, new line 3)
  P-3  ACRES: '4.00' -> '1.90'; OWNER: 'Cole' -> 'Gray' (old line 4, new line 4)
  P-4  X: '621400' -> '628000'; Y: '1581400' -> '1589000'; OWNER: 'Dunn' -> 'Hale' (old line 5, new line 5)

keys only in the new vintage:
  P-7 (new line 7)

keys only in the old vintage:
  P-5 (old line 6)

VERDICT: clean. 3 changed, 1 added, 1 removed. Key reuse was not checked.
```

The same runs on a Windows workstation and on the Ubuntu host printed the same text.

**Refusal 1: a key that is not unique.** Checked before anything else, and it stops the run.
`dupe.csv` is `new.csv` with one more row at the end, `P-1,999999,999999,1.00,Zed,RES`.

```
$ python parceldiff.py old.csv dupe.csv
parceldiff: old.csv 6 row(s), dupe.csv 7 row(s), key PARCEL
REFUSED   the key is not unique, so nothing was compared
      new key 'P-1' is on 2 rows: line 2, 8
      Fix the key or name more columns in --key-field. A key two rows claim cannot say which row the other vintage meant.

VERDICT: REFUSED
```

No counts are printed, and `--apply` writes no file, because every class would have been computed
under a key that cannot say which of its two rows the other vintage meant. With `--out` and
`--apply` the run prints one more line, `Refused. <path> was not written, because nothing was
classified.` A key with a stray space counts as the same key: `P-1` and `P-1 ` are one key on two
rows.

Two columns together can make a key that one column cannot. `--key-field BOOK,PAGE` reads the pair
as the key. A row with a blank part in the key is a row with no key.

**Refusal 2: a run that compared nothing.** A run is clean only when at least one key was on both
sides. The leading-zero case shows it. `a.csv` holds `0012` and `0013`, `b.csv` holds `12` and
`13`:

```
$ python parceldiff.py a.csv b.csv --skip-reuse-check
parceldiff: a.csv 2 row(s), b.csv 2 row(s), key PARCEL
keys classified: 4 of 4
  ADDED      2
  REMOVED    2
pairs: 0 on a shared key, 0 percent of the smaller vintage
key reuse: NOT CHECKED, no coordinate or area column was named (--skip-reuse-check)

keys only in the new vintage:
  12 (new line 2)
  13 (new line 3)

keys only in the old vintage:
  0012 (old line 2)
  0013 (old line 3)

VERDICT: UNVERIFIED, no key is on both sides, so nothing was compared. A key spelled differently on the two sides looks exactly like this.
```

The exit code is 4. A key that pairs fewer than half of the smaller vintage's keys is UNVERIFIED
too. The floor is `--min-overlap`, and a run at exactly the floor passes. `--min-overlap 0`
turns the check off.

**Refusal 3: a reuse check that could not look.** The tool counts a pair as measured by a test
only when both rows give a readable value to that test. The centroid test needs an X and a Y on
both sides. The area test needs an area above zero on both sides. If a requested test measured no
pair, the run exits 4. An export that formats numbers with a thousands separator writes
`1,580,200`, and that is not a number:

```
$ python parceldiff.py sep.csv sep.csv --x-field X --y-field Y
parceldiff: sep.csv 2 row(s), sep.csv 2 row(s), key PARCEL
keys classified: 2 of 2
  UNCHANGED  2
pairs: 2 on a shared key, 100 percent of the smaller vintage
key reuse: checked on 0 of 2 pair(s), by centroid moved above 500.0 ft

pairs the key-reuse check could not measure:
  A (centroid test)
  B (centroid test)

VERDICT: UNVERIFIED, the centroid test measured no pair, because every coordinate was blank, zero or unreadable.
```

A test that measured only a few pairs is not much better, because a reissued key among the rest
goes unseen. Each requested test must measure at least `--min-measured` of the shared keys, 0.9 by
default. Here three of ten pairs have a blank coordinate in the new vintage, so seven of ten were
measured:

```
$ python parceldiff.py q_old.csv q_new.csv --x-field X --y-field Y
parceldiff: q_old.csv 10 row(s), q_new.csv 10 row(s), key PARCEL
keys classified: 10 of 10
  UNCHANGED  10
pairs: 10 on a shared key, 100 percent of the smaller vintage
key reuse: checked on 7 of 10 pair(s), by centroid moved above 500.0 ft

pairs the key-reuse check could not measure:
  K7 (centroid test)
  K8 (centroid test)
  K9 (centroid test)

VERDICT: UNVERIFIED, the centroid test measured only 7 of 10 pair(s), under the 90 percent floor, so a reissued key among the rest would go unseen.
```

With one blank coordinate, nine of ten were measured. That is exactly the floor, so the run passes.
The verdict and a section of the report still name the pair that was not measured:

```
$ python parceldiff.py p_old.csv p_new.csv --x-field X --y-field Y
parceldiff: p_old.csv 10 row(s), p_new.csv 10 row(s), key PARCEL
keys classified: 10 of 10
  UNCHANGED  10
pairs: 10 on a shared key, 100 percent of the smaller vintage
key reuse: checked on 9 of 10 pair(s), by centroid moved above 500.0 ft

pairs the key-reuse check could not measure:
  K9 (centroid test)

VERDICT: clean. 0 changed, 0 added, 0 removed. 1 pair(s) could not be measured for key reuse.
```

A readable area does not stand in for an unreadable coordinate. Each test is counted by itself.
A floor of 0 accepts any run that measured one pair, but a test that measured no pair always
fails the run.

## The output file

With `--out changes.csv --apply`, the demo run above writes one row per key:

```
class,key,old_line,new_line,detail
UNCHANGED,P-1,2,2,
CHANGED,P-2,3,3,OWNER: 'Baker' -> 'Bakker'
REUSED,P-3,4,4,area changed by a factor of 2.11
REUSED,P-4,5,5,centroid moved 10065.8 ft
UNCHANGED,P-6,7,6,
ADDED,P-7,,7,
REMOVED,P-5,6,,
```

A run that exits 3 or 4 writes nothing, even with `--apply`, because a file of classes looks like
a diff that can be trusted. It prints `Refused. ... was not written` for exit 3 and `Unverified.
... was not written` for exit 4. A run that exits 1 writes the file, because a reissued key is a
finding and the file lists it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | A diff was produced, the reuse check ran on enough pairs, and no key was reissued. Changes, additions and removals do not fail the gate. With `--skip-reuse-check` the reuse check did not run, and the verdict says so. |
| 1 | At least one key now names a different parcel. Do not pair these rows. |
| 2 | A file could not be read, or `--out` could not be written. |
| 3 | Refused. A key is on more than one row, or a row has no key, so nothing was compared. |
| 4 | The run could not be trusted. No key is on both sides, too few are, a requested reuse test measured too few pairs, or no reuse test was asked for. |
| 64 | Usage error, including a column that is not in the CSV, a repeated column name, an unknown or abbreviated flag, an output path that is an input, and coordinates that look like degrees. |
| 70 | An unexpected crash. This is a fault in the tool and not a finding. The traceback is on stderr. |

Exit 3 is deliberately not exit 1. A job that files a finding for "a key was reissued" must not
file the same finding for "this key cannot be joined on at all". Exit 4 is not exit 0 for the same
reason: "I checked and found nothing" and "I could not check" are different. When a run has a
reissued key and is also thin on overlap, it exits 1, because the reissued key is a finding.

## Limits

- It reasons about two things only: the centroid shift and the area ratio. A key can name a
  different parcel and keep its centroid and its area, and then this tool cannot see it. There is
  no `--stable-field` for a column that must never change under one key.
- It reads centroids and areas from columns. It does not read a polygon, a shapefile, a
  geodatabase or an SDE connection, and it does not compute a centroid. Reading a geodatabase
  would need `arcpy`, which would stop the tool running anywhere else.
- `500`, `2.0`, `1.0`, `0.5` and `0.9` are defaults and not a standard. I did not measure them on a
  real layer. Measure the shifts between two of your own vintages, then set the flags. Do not
  inherit these numbers because they are printed here.
- The area test flags a split that kept the parent's number, because the parent's area falls by
  more than the ratio. That is on purpose: the old row and the new row describe different
  extents. A boundary adjustment of a few percent is reported as `CHANGED`, not `REUSED`. A large
  parcel whose centroid moves by more than the reuse distance in a re-survey is `REUSED`.
- A parcel whose area falls from 5 acres to 0, or rises from 0, is `CHANGED` and not `REUSED`,
  because a ratio with a zero in it is not computed. The area test counts that pair as not
  measured, and the report lists it.
- It measures a straight line between two points. No projection, no datum, no geodesic. The
  degrees check guards against the commonest mistake. It does not prove that the CRS is right.
  A CRS mismatch is not named: one vintage in feet and the other in metres shows as every key
  `REUSED` and exit 1, with no word about the CRS.
- A number is compared as an exact decimal, so `007` and `7` agree, `1e3` and `1000` agree, and
  `1_000` and `1000` agree. A ZIP or id column that lost its leading zeros, or was rewritten in
  exponent form, is not reported as an edit. A key is never compared as a number. Keys are trimmed
  with `str.strip()` and nothing else. Zero-width spaces, NFC against NFD forms of one accented
  letter, and case differences do not pair, so the run reports ADDED and REMOVED, or exits 4 when
  nothing pairs. If the two vintages spell the key differently, fix that first with
  [nalmatch](https://github.com/uhsear/nalmatch).
- If a vintage has two rows for one key, the run is refused. There is no `--keep-last` and no
  `--keep-first`, because the choice is an edit somebody signs for.
- A composite key joins its parts with `|` for display. The keys `a|b` plus `c`, and `a` plus
  `b|c`, are different keys to the tool but both print as `a|b|c` in the report and in `--out`.
- A placeholder coordinate such as `0,0` is read as a real point. Blank out missing coordinates
  before the run.
- A coordinate cell is read with Python `float()`, so underscores are accepted as digits. `1_000`
  reads as 1000. A thousands separator such as `1,000` is not a number and reads as no coordinate.
- The columns named in `--x-field` and `--y-field` are not compared as attributes. A coordinate
  that moves by less than the move tolerance is not listed as `CHANGED`. A coordinate that cannot
  be read is listed as not measured, and nothing more.
- Only UTF-8 CSV is supported. Other encodings are decoded with `errors="replace"` and no warning.
  Every byte that is not UTF-8 becomes U+FFFD, so two Windows-1252 owners that differ only in an
  accented letter read as equal. Two keys that swap accented letters, such as `e-acute 1` and
  `e-grave 2`, swapped, read as `UNCHANGED` with exit 0. Two different keys can also collapse
  into one. Such a pair is refused as a repeated key, which is the safe direction. Save the CSV as
  UTF-8.
- A UTF-16 export, such as Excel "Unicode Text", is refused, but the message does not name the
  encoding. On Python 3.11 and later it says the CSV lacks a column and exits 64. On 3.9 and 3.10
  the `csv` module stops on the NUL byte and the run exits 2.
- A character the console cannot show prints as `?` instead of stopping the run. The file the
  tool writes is always UTF-8.
- A short row has its missing cells read as blank, and a long row has its extra cells ignored.
  A repeated column name is refused, because a CSV reader keeps the last column of that name and
  drops the other without a word. Two or more empty column names count as one repeated name. An
  unquoted comma in an address shifts every later cell in that row, so X, Y and ACRES can be read
  from the wrong cells. The row then gives a wrong `REUSED` verdict or an unmeasured pair, and
  nothing flags the shift.
- Keys and detail text print as they are. A quoted line break in a cell splits the report line in
  two, and a terminal escape sequence in a cell goes to the console unchanged. Review an untrusted
  CSV as text first.
- A new vintage that holds a subset of the old keys exits 0 and lists the rest as `REMOVED`. The
  overlap floor uses the smaller vintage, and removals do not fail the gate. A vintage that kept
  10 of 1,000 keys gave `0 changed, 0 added, 990 removed` and exit 0. Read the `REMOVED` count
  yourself.
- `--allow-blank-keys` leaves the keyless rows out of the diff. The verdict counts them, but the
  exit code stays 0, because you asked for that.
- It reads both files into memory. A run of 200,000 rows a side, with the centroid and area tests
  on, took about 7 seconds on one Windows workstation. I did not time it on the Ubuntu host.
- Output files are written in place, not through a temporary file. `--out` onto an existing file
  replaces it. A failed write can leave a partial file, and the exit code is 2.
- The check that `--out` is not an input compares real paths and nothing else. A hard link to an
  input passes it, and `--apply` then overwrites the input through the link. The self-test shows
  that with a hard link. On Windows, an extended-length path such as `//?/C:/path/old.csv` passed
  the same check when I tried it by hand. The self-test does not cover that form, because it only
  applies to Windows.
- A key that starts with `=`, `+`, `-` or `@` is written as it came. Open the output as text or
  quote it first, because a spreadsheet may read it as a formula.
- When the reader closes the pipe early, as in `parceldiff.py ... | head -1`, the print fails and
  the run ends with exit 70 and a traceback.
- `--self-test` ignores every other valid flag. It writes only to a temporary directory under the
  system temp path, which it removes at the end, even when an assertion raises. It creates one
  hard link there.
- It opens no socket and imports no network module. There is no network path to test, and no
  credential anywhere in it to leak.
- Nothing is written without `--apply`, and a run that exits 3 or 4 writes nothing at all.
- Every statement and branch is reached by the self-test, so there is no uncovered line to name.
  Coverage was measured with `coverage run --branch` on Windows and on Ubuntu.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [idreuse](https://github.com/uhsear/idreuse) - the same question for two different layers that
  share an identifier. It asks whether a matched pair of points is far enough apart to be two
  objects, and refuses an identifier that is not unique. This tool diffs two vintages of one
  layer, and checks centroids and areas of polygons you export.
- [nalmatch](https://github.com/uhsear/nalmatch) - reconcile two tables on an identifier, and
  refuse an identifier rule that would merge two records into one key. Run it first when the two
  vintages spell the key differently. This tool does no normalisation.
- [fcload](https://github.com/uhsear/fcload) - load the result without corrupting it.
