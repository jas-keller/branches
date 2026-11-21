"""Branch lookups and directory views stay correct after the large-tree speedups."""

from collections import deque
import unittest
from pathlib import Path

from app.genealogy import GedcomRepository


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_GEDCOM = PROJECT_ROOT / "samples" / "example-family.ged"


def reference_branch_path(repo, person_id):
    """The original per-person search: shortest path from the nearest anchor, earlier anchor on ties."""
    best = None
    for anchor in repo._grandparent_anchors():
        queue = deque([(anchor.id, [anchor.id])])
        seen = set()
        while queue:
            node_id, path = queue.popleft()
            if node_id in seen or len(path) > 24:
                continue
            seen.add(node_id)
            if node_id == person_id:
                if best is None or len(path) < len(best):
                    best = path
                break
            queue.extend((n, path + [n]) for n in repo._family_neighbors(node_id) if n not in seen)
    return best


def reference_generations(repo, anchor_id, person_id):
    queue = deque([(anchor_id, 0)])
    seen = set()
    while queue:
        node_id, depth = queue.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)
        if node_id == person_id:
            return depth
        queue.extend((parent_id, depth + 1) for parent_id in repo._parent_ids(node_id))
    return None


class LargeTreePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.repo = GedcomRepository(EXAMPLE_GEDCOM)

    def test_cached_anchor_search_matches_per_person_search(self):
        anchors = {anchor.id for anchor in self.repo._grandparent_anchors()}
        self.assertTrue(anchors)
        for person_id in self.repo.people:
            expected = reference_branch_path(self.repo, person_id)
            context = self.repo.branch_context(person_id)
            if expected is None:
                self.assertIsNone(context)
                continue
            self.assertEqual(context["anchorId"], expected[0])
            self.assertEqual(context["connections"], len(expected) - 1)
            self.assertEqual(self.repo._anchor_path(expected[0], person_id), expected)
            for anchor_id in anchors:
                self.assertEqual(self.repo._anchor_ancestor_depths(anchor_id).get(person_id),
                                 reference_generations(self.repo, anchor_id, person_id))
        # One search per anchor, however many people were looked up.
        self.assertLessEqual(len(self.repo._anchor_tree_cache), len(anchors))

    def test_frozen_repository_builds_people_index_once(self):
        frozen = GedcomRepository(EXAMPLE_GEDCOM).freeze()
        self.assertIs(frozen.people_index(), frozen.people_index())

    def test_unfrozen_repository_sees_edits(self):
        first = self.repo.people_index()
        self.assertIsNot(first, self.repo.people_index())
        person = next(p for p in self.repo.people.values() if self.repo.visible(p) and p.event("BIRT"))
        person.event("BIRT").place = "Dunedin, New Zealand"
        entries = [entry for group in self.repo.people_index()["groups"] for entry in group["people"]]
        self.assertEqual(next(e for e in entries if e["id"] == person.id)["birthPlace"], "Dunedin, New Zealand")


if __name__ == "__main__":
    unittest.main()
