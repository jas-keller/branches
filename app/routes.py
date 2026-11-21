from functools import lru_cache

from pathlib import Path
from threading import RLock
from xml.sax.saxutils import escape

from flask import Blueprint, Response, abort, current_app, g, jsonify, render_template, request, send_from_directory, url_for

from .genealogy import GedcomRepository
from .editorial_store import EditorialStore
from .site_profile import profile
from .stories import STORIES

bp = Blueprint("main", __name__)
EDITORIAL_STORE_LOCK = RLock()


def repository():
    if 'genealogy_repository' not in g:
        # Each name snapshot gets its own repository, keyed by the forms, so a
        # concurrent import produces a new object instead of mutating this one.
        # Requests share it read-only: copying it per request cost seconds on a
        # large tree and threw away the derived caches (validation, branches).
        path = current_app.config.get('EDITORIAL_DB_PATH')
        forms = ()
        if path and Path(path).is_file():
            forms = tuple((person_id, tuple((row['type'], row['text']) for row in rows))
                          for person_id, rows in sorted(_load_editorial_store(path).all_name_forms().items()))
        g.genealogy_repository = _load_named_repository(
            current_app.config['GEDCOM_PATH'], current_app.config['SHOW_LIVING'], forms)
    return g.genealogy_repository


@lru_cache(maxsize=4)
def _load_named_repository(path, show_living, forms):
    from copy import deepcopy
    repo = deepcopy(_load_repository(path, show_living))
    repo.apply_name_forms({person_id: [{'type': kind, 'text': text} for kind, text in rows]
                           for person_id, rows in forms})
    repo._search_names()
    # Validate once here so no visitor request pays for the whole-tree audit.
    repo.research_review("")
    return repo.freeze()


@lru_cache(maxsize=4)
def _load_repository(path, show_living):
    return GedcomRepository(path, show_living=show_living)


@lru_cache(maxsize=4)
def _load_editorial_store(path):
    # Flask can receive several first requests together; serialize the one-time
    # SQLite migration so two threads cannot create the same table concurrently.
    with EDITORIAL_STORE_LOCK:
        return EditorialStore(path)


def editorial_summary(person_id):
    path = current_app.config.get("EDITORIAL_DB_PATH")
    if not path or not Path(path).is_file():
        return None
    return _load_editorial_store(path).person_summary_for_identifier(
        "gedcom_xref", person_id
    )


def story_catalog():
    with EDITORIAL_STORE_LOCK:
        store = _load_editorial_store(current_app.config["EDITORIAL_DB_PATH"])
        store.seed_stories(STORIES)
        return store.published_stories()


def related_stories(*, person_id=None, place_slug=None, surname_slug=None):
    return [story for story in story_catalog() if story.get("featured", False) and (
        (person_id and person_id in story.get("people", ()))
        or (place_slug and place_slug in story.get("places", ()))
        or (surname_slug and surname_slug in story.get("surnames", ()))
    )]


@bp.get("/")
def index():
    repo = repository()
    return render_template("index.html", stats=repo.stats(), root=repo.default_root(),
                           timeline=repo.home_timeline(), convergence=repo.convergence(),
                           stories=[story for story in story_catalog() if story.get("featured", False)],
                           outliers=repo.outlier_feed(limit=4), branch_cards=repo.branch_character_cards()[:8],
                           branches=repo.starting_branches(),
                           surname_count=len(repo.people_index()["groups"]))


@bp.get("/explore")
def explore():
    repo = repository()
    return render_template("explore.html", stats=repo.stats(), root=repo.default_root(),
                           branches=repo.starting_branches())


@bp.get("/favicon.ico")
def favicon():
    return send_from_directory(
        current_app.static_folder, "favicon.ico", mimetype="image/vnd.microsoft.icon"
    )


@bp.get("/site.webmanifest")
def manifest():
    return jsonify({
        "name": profile.SITE["name"],
        "short_name": profile.SITE["name"],
        "description": "A living, privacy-aware family atlas.",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#dceaf0",
        "theme_color": "#0f2b49",
        "icons": [
            {"src": url_for("static", filename="images/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": url_for("static", filename="images/icon-512.png"), "sizes": "512x512", "type": "image/png"},
        ],
    })


@bp.get("/robots.txt")
def robots():
    site_url = current_app.config["SITE_URL"].rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        f"Sitemap: {site_url}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@bp.get("/sitemap.xml")
def sitemap():
    repo = repository()
    paths = [
        url_for("main.index"),
        url_for("main.explore"),
        url_for("main.people"),
        url_for("main.places"),
        url_for("main.stories"),
        url_for("main.surnames"),
        url_for("main.timeline"),
        url_for("main.records"),
        url_for("main.search"),
        url_for("main.contribute"),
        url_for("main.about"),
    ]
    paths.extend(url_for("main.story", slug=item["slug"]) for item in story_catalog())
    paths.extend(
        url_for("main.person", person_id=person.id)
        for person in repo.people.values()
        if repo.visible(person)
    )
    paths.extend(url_for("main.place", place_slug=item["slug"]) for item in repo.places())
    site_url = current_app.config["SITE_URL"].rstrip("/")
    urls = "".join(f"  <url><loc>{escape(site_url + path)}</loc></url>\n" for path in paths)
    body = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}</urlset>\n'
    return Response(body, mimetype="application/xml")


@bp.get("/people/<person_id>")
def person(person_id):
    detail = repository().person_detail(person_id)
    if detail is None:
        abort(404)
    return render_template(
        "person.html", person=detail, editorial=editorial_summary(person_id),
        stories=related_stories(person_id=person_id)
    )


@bp.get("/people")
def people():
    return render_template("people.html", index=repository().people_index())


@bp.get("/places")
def places():
    hierarchy = repository().place_hierarchy()
    return render_template("places.html", hierarchy=hierarchy)


@bp.get("/places/<place_slug>")
def place(place_slug):
    detail = repository().place_detail(place_slug)
    if detail is None:
        abort(404)
    return render_template("place.html", place=detail, stories=related_stories(place_slug=place_slug))


@bp.get("/stories")
def stories():
    return render_template("stories.html", stories=[story for story in story_catalog()
                                                    if story.get("featured", False)])


@bp.get("/story/<slug>")
def story(slug):
    item = next((story for story in story_catalog() if story["slug"] == slug), None)
    if item is None:
        abort(404)
    repo = repository()
    people = [repo.person_summary(repo.people[pid]) for pid in item["people"]
              if pid in repo.people and repo.visible(repo.people[pid])]
    return render_template("story.html", story=item, people=people)


@bp.get("/surnames")
def surnames():
    return render_template("surnames.html", index=repository().people_index())


@bp.get("/surnames/<slug>")
def surname(slug):
    group = next((group for group in repository().people_index()["groups"]
                  if group["slug"] == slug), None)
    if group is None:
        abort(404)
    return render_template("surname.html", group=group,
                           stories=related_stories(surname_slug=slug))


@bp.get("/timeline")
def timeline():
    return render_template("timeline.html", timeline=repository().home_timeline())


@bp.get("/records")
def records():
    index = repository().people_index()
    return render_template("records.html", index=index, stats=repository().stats())


@bp.get("/search")
def search():
    query = request.args.get("q", "").strip()
    return render_template("search.html", query=query,
                           results=repository().search(query, limit=40) if query else [])


@bp.get("/contribute")
def contribute():
    return render_template("contribute.html")


@bp.get("/about")
def about():
    return render_template("about.html", convergence=repository().convergence())


@bp.get("/api/tree")
def api_tree():
    return jsonify(repository().tree(
        request.args.get("root"),
        request.args.get("direction", "descendants"),
        request.args.get("generations", 6, type=int),
    ))


@bp.get("/api/places")
def api_places():
    return jsonify(repository().places())


@bp.get("/api/geography")
def api_geography():
    return jsonify(repository().geography_stages())


@bp.get("/api/search")
def api_search():
    return jsonify(repository().search(
        request.args.get("q", ""),
        include_surnames=request.args.get("surnames", "1") != "0",
    ))


@bp.app_errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404


@bp.get("/health")
def health():
    try:
        count = len(repository().people)
        return jsonify(ok=True, people=count)
    except FileNotFoundError as exc:
        return jsonify(ok=False, error=str(exc)), 503
