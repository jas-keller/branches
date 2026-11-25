# Family Branches

An interactive, privacy-aware family history site built from a GEDCOM export.
It turns a family tree into a living atlas: a migration map, a tree explorer,
person and place pages, curated stories, and evidence labels that keep
record-supported facts apart from submitted-tree assertions.

The repository contains the framework and a **fictional example family** (the
Halvorsens of Minnesota). Every person, story and record in the example is
invented; the places are real so the maps have somewhere to land.

## What it includes

- A narrative homepage built on the tree's actual geography: where branches
  began, where they first appear in the new country, and where they took root
- Searchable descendant and ancestor views, starting from familiar grandparents
- Detection of two migration streams and the marriages where they first join
- Individual life-event and family-connection pages with expandable citations
- Place pages grouped into three chapters: overseas origins, footholds, and the
  home region's communities
- Public-by-default privacy: people without a death record who would be under
  100 today, or whose birth year is unknown, are never exposed
- A validation-first SQLite import and editorial layer for reviewed claims,
  curated name forms, and a private research archive

## Quick start

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\flask --app wsgi run --debug
```

Open `http://127.0.0.1:5000`. With no GEDCOM of your own, the app serves
`samples/example-family.ged` with the example profile.

Run the tests with `python -m unittest discover -s tests`.

## Making it your family's site

Two things are specific to a family, and both stay out of Git:

1. **The tree.** Export a GEDCOM and save it as `data/family-tree.ged` (or point
   `BRANCHES_GEDCOM_PATH` at it). `data/` is ignored apart from its README.
2. **The site profile.** Copy `app/profiles/example.py` to
   `app/profiles/<name>.py` and start the app with `BRANCHES_PROFILE=<name>`.
   Only `example.py` is tracked; other profiles are ignored, and CI refuses them.

The profile holds everything the engine would otherwise have to hard-code:

| Setting | What it controls |
| --- | --- |
| `SITE`, `HOME` | Site name, brand, page titles, homepage and About copy |
| `BRANCH_ANCHORS`, `PRIMARY_SURNAME` | The people each public branch is named after (usually four grandparents) and which branch comes first |
| `STREAMS` | The two ancestral streams, identified by birth country |
| `HOME_COUNTRY`, `HOME_REGION` | Where the family settled, for the footholds and home-region chapters |
| `GAZETTEER`, `COUNTRIES` | An offline place lookup: map coordinates and country detection |
| `PLACE_TIERS`, `PLACE_REGIONS`, `PLACE_RULES` | How the places page groups communities into regions |
| `MIGRATION_REGIONS`, `CORRIDOR_*`, `BRANCH_MAP_JUNCTIONS` | The symbolic family streams on the home map |
| `TIMELINE`, `MAP_CHAPTERS` | Narrative copy, with placeholders filled from the tree |
| `NAME_CORRECTIONS`, `PLACE_CORRECTIONS` | Display-only fixes for recurring spellings in the export |
| `STORIES`, `SHOWCASE_PERSON_IDS` | Curated stories and editorially chosen people |

`tools/analyze_geography.py` prints the immigration events, first home-country
records, home-region places, and every place the gazetteer cannot locate yet,
which is most of what you need to write `GAZETTEER` and `PLACE_RULES`.
`tools/extract_region_geojson.py` makes an optional home-region outline for the
map from Natural Earth data.

Environment variables:

| Variable | Default |
| --- | --- |
| `BRANCHES_PROFILE` | `example` |
| `BRANCHES_GEDCOM_PATH` | `data/family-tree.ged`, else the sample |
| `BRANCHES_EDITORIAL_DB_PATH` | `data/editorial.sqlite3` |
| `BRANCHES_SITE_URL` | the profile's `SITE["url"]` |
| `BRANCHES_SHOW_LIVING` | `0`. Set `1` only on a protected, private deployment |

## Map data

The homepage base map uses the public-domain Natural Earth 1:110m Admin 0
country boundaries, stored locally in `app/static/data`. Family place markers
and routes come from the profile gazetteer; route curves communicate broad
convergence patterns rather than exact documented travel itineraries.

## Data updates

Edit `data/curated/names.csv` (columns `gedcom_id,name_type,name,note`) and run
`.venv\Scripts\python tools/import_names.py` to refresh public nicknames and name
variants; living and unknown IDs are refused.

Before replacing a public dataset, audit and stage it:

```powershell
.venv\Scripts\python.exe tools\audit_genealogy.py data\family-tree.ged --format json --fail-on-error
.venv\Scripts\python.exe tools\stage_import.py data\family-tree.ged
```

The staging database is private and ignored by Git. A GEDCOM checksum is stored
once; newer parser or validation versions create separate immutable validation
runs against that same source import.

Review items are append-only and require a reviewer plus rationale. Errors cannot
be waived; they must be corrected at the source and re-imported:

```powershell
.venv\Scripts\python.exe tools\review_import.py list <import-id>
.venv\Scripts\python.exe tools\review_import.py decide <import-id> <fingerprint> accepted `
  --reviewer "Name" --rationale "Evidence reviewed..."
```

## Private research archive

Create the ignored local archive folders before accessioning documents:

```powershell
.venv\Scripts\python.exe tools\init_research_archive.py
```

The archive is deliberately excluded from Git. Keep a separate encrypted backup,
and use `docs/research-archive/artifact-metadata-template.yaml` when accessioning
an item. See `docs/research-archive/README.md` for the workflow and public-display
rules.

Verify preserved files against the private accession register, then import its
metadata into the private editorial database:

```powershell
.venv\Scripts\python.exe tools\verify_research_archive.py
.venv\Scripts\python.exe tools\import_research_archive.py
```

The application reads the editorial database for canonical claim overlays when
that file exists. No route serves the private artifact directory; person pages
receive only claim summaries and source titles.

Normalize a staged GEDCOM and inspect its publication gate:

```powershell
.venv\Scripts\python.exe tools\normalize_import.py
.venv\Scripts\python.exe tools\report_normalization.py
.venv\Scripts\python.exe tools\manage_publication.py status
```

Only a completed `ready` run can be published. Releases and rollbacks require a
named reviewer and rationale; a blocked run cannot be forced through this CLI.
Before publishing, compare the new run to its predecessor and append an explicit
approval:

```powershell
.venv\Scripts\python.exe tools\diff_normalizations.py <base-run-id> <target-run-id>
.venv\Scripts\python.exe tools\manage_publication.py review <target-run-id> approved `
  --reviewer "Name" --rationale "Semantic diff and validation reviewed"
.venv\Scripts\python.exe tools\manage_publication.py publish <target-run-id> `
  --reviewer "Name" --rationale "Approved for public activation"
```

## Deployment

See [how I host Flask apps on DreamHost](docs/dreamhost/flask-hosting.md) for the
deployment overview.

`wsgi.py` exposes the app for any WSGI server; `passenger_wsgi.py` suits
Passenger hosts, and `gunicorn.conf.py` binds to `GUNICORN_BIND` (default
`0.0.0.0:8005`) behind a reverse proxy. Keep `BRANCHES_SHOW_LIVING` unset on a
public site: a GEDCOM export contains information about living relatives.
