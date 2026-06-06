"""Test suite for the Coworker Memory layer (Layer 1 cache + Layer 2 recall).

Run:  python3 -m unittest discover tests -v

We inject a DETERMINISTIC fake embedder so similarity is exactly predictable
and the tests never depend on a live Ollama model. Vector scheme (3-D), all
unit-normalized, cosine vs the 'bugs' axis [1,0,0]:

    bugs      -> [1, 0, 0]      cosine 1.00
    defects   -> [.96,.28,0]    cosine 0.96   (>= 0.92 Tier-1 threshold)
    slightly  -> [.8, .6, 0]    cosine 0.80   (>= 0.55 Tier-2 floor, < Tier-1)
    x         -> [0, 0, 1]      cosine 0.00   (miss everywhere)
    (other)   -> [0, 1, 0]      cosine 0.00
"""
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
import coworker_memory as mem


def fake_embed(text):
    t = text.lower()
    if "defect" in t:
        v = [0.96, 0.28, 0]
    elif "slightly" in t:
        v = [0.8, 0.6, 0]
    elif "bug" in t:
        v = [1, 0, 0]
    elif "x" in t:
        v = [0, 0, 1]
    else:
        v = [0, 1, 0]
    return mem._normalize(v)


class MemoryTestBase(unittest.TestCase):
    def setUp(self):
        # Fresh throwaway DB per test -> full isolation.
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "memory.db")
        os.environ["COWORKER_MEMORY_DB"] = self.db
        mem.DB_PATH = pathlib.Path(self.db)
        # Default: embedder ON (fake). Individual tests can disable.
        self._real_embed = mem.embed
        mem.embed = fake_embed

    def tearDown(self):
        mem.embed = self._real_embed

    def make_file(self, name="auth.py", content="def login():pass\n"):
        p = pathlib.Path(self.tmp, name)
        p.write_text(content)
        return str(p)

    def set_age(self, question, days):
        """Backdate a memory's created_at to test recency decay."""
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE memories SET created_at=? WHERE question=?",
                     (time.time() - days * 86400, question))
        conn.commit()
        conn.close()

    def set_last_used(self, question, days):
        """Backdate a memory's last_used to test eviction (decay clock)."""
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE memories SET last_used=? WHERE question=?",
                     (time.time() - days * 86400, question))
        conn.commit()
        conn.close()

    def count_rows(self):
        conn = sqlite3.connect(self.db)
        (n,) = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        conn.close()
        return n

    def use_count_of(self, question):
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT use_count FROM memories WHERE question=?",
                           (question,)).fetchone()
        conn.close()
        return row[0] if row else None


# --------------------------------------------------------------------------
class TestLayer1ExactCache(MemoryTestBase):
    def test_cold_lookup_misses(self):
        f = self.make_file()
        self.assertIsNone(mem.lookup([f], "what does this do", "ask", "k"))

    def test_store_then_hit(self):
        f = self.make_file()
        mem.store([f], "what does this do", "ask", "k", "- defines login")
        self.assertEqual(mem.lookup([f], "what does this do", "ask", "k"),
                         "- defines login")

    def test_question_normalization(self):
        f = self.make_file()
        mem.store([f], "Find The Bugs", "ask", "k", "ans")
        # case + whitespace differences still hit the exact tier
        self.assertEqual(mem.lookup([f], "find   the bugs", "ask", "k"), "ans")

    def test_file_change_invalidates(self):
        f = self.make_file()
        mem.store([f], "q", "ask", "k", "old answer")
        pathlib.Path(f).write_text("def login():return True\n")  # content changes
        self.assertIsNone(mem.lookup([f], "q", "ask", "k"))

    def test_path_order_independent(self):
        a = self.make_file("a.py", "A\n")
        b = self.make_file("b.py", "B\n")
        mem.store([a, b], "q", "ask", "k", "ans")
        self.assertEqual(mem.lookup([b, a], "q", "ask", "k"), "ans")  # reversed

    def test_model_is_part_of_key(self):
        f = self.make_file()
        mem.store([f], "q", "ask", "kimi", "ans")
        self.assertIsNone(mem.lookup([f], "q", "ask", "deepseek"))

    def test_tool_is_part_of_key(self):
        f = self.make_file()
        mem.store([f], "q", "ask", "k", "ans")
        self.assertIsNone(mem.lookup([f], "q", "write", "k"))

    def test_reinforcement_increments_use_count(self):
        f = self.make_file()
        mem.store([f], "q", "ask", "k", "ans")
        mem.lookup([f], "q", "ask", "k")
        mem.lookup([f], "q", "ask", "k")
        conn = sqlite3.connect(self.db)
        (uses,) = conn.execute("SELECT use_count FROM memories").fetchone()
        conn.close()
        self.assertEqual(uses, 3)  # 1 at store + 2 hits

    def test_lookup_missing_file_fails_open(self):
        # Nonexistent path must not raise — returns None like a miss.
        self.assertIsNone(mem.lookup(["/no/such/file"], "q", "ask", "k"))

    def test_store_missing_file_does_not_raise(self):
        try:
            mem.store(["/no/such/file"], "q", "ask", "k", "ans")
        except Exception as e:
            self.fail(f"store raised on missing file: {e}")


# --------------------------------------------------------------------------
class TestLayer2Tier1SemanticCache(MemoryTestBase):
    def test_rephrase_within_threshold_hits(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "- bug at line 3")
        # 'defects' cos 0.96 to 'bugs', same file -> HIT
        self.assertEqual(
            mem.semantic_lookup([f], "locate the defects", "ask", "k"),
            "- bug at line 3")

    def test_below_threshold_misses(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans")
        # 'slightly' cos 0.80 < 0.92 -> miss
        self.assertIsNone(mem.semantic_lookup([f], "slightly related", "ask", "k"))

    def test_same_question_different_files_misses(self):
        f1 = self.make_file("a.py", "A\n")
        f2 = self.make_file("b.py", "B\n")
        mem.store([f1], "find the bugs", "ask", "k", "ans")
        # identical question, but different file content -> not a cache-equivalent
        self.assertIsNone(mem.semantic_lookup([f2], "find the bugs", "ask", "k"))

    def test_embedder_offline_returns_none(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans")
        mem.embed = lambda t: None  # simulate Ollama down
        self.assertIsNone(mem.semantic_lookup([f], "find the bugs", "ask", "k"))

    def test_unrelated_question_misses(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans")
        self.assertIsNone(mem.semantic_lookup([f], "what is x", "ask", "k"))


# --------------------------------------------------------------------------
class TestLayer2Tier2Recall(MemoryTestBase):
    def test_relevant_recall_surfaces(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "- bug here", project="p")
        hits = mem.recall("locate the defects", project="p")
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0]["cosine"], 0.96, places=2)

    def test_below_floor_excluded(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans", project="p")
        self.assertEqual(mem.recall("what is x", project="p"), [])

    def test_empty_store_returns_empty(self):
        self.assertEqual(mem.recall("anything", project="p"), [])

    def test_embedder_offline_returns_empty(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans", project="p")
        mem.embed = lambda t: None
        self.assertEqual(mem.recall("find the bugs", project="p"), [])

    def test_k_limit_respected(self):
        for i in range(5):
            f = self.make_file(f"f{i}.py", f"content {i}\n")
            mem.store([f], f"bug number {i}", "ask", "k", f"ans{i}", project="p")
        self.assertEqual(len(mem.recall("find the bugs", project="p", k=3)), 3)

    def test_project_first_then_global_widen(self):
        f1 = self.make_file("a.py", "A\n")
        f2 = self.make_file("b.py", "B\n")
        mem.store([f1], "bugs in mine", "ask", "k", "mine", project="here")
        mem.store([f2], "bugs in theirs", "ask", "k", "theirs", project="there")
        hits = mem.recall("find the bugs", project="here", k=5)
        # equal cosine (both 1.0) but in-project ranks first via scope weighting
        self.assertEqual(hits[0]["project"], "here")
        self.assertEqual(hits[1]["project"], "there")
        self.assertGreater(hits[0]["score"], hits[1]["score"])

    def test_recency_decay_orders_newer_first(self):
        f1 = self.make_file("a.py", "A\n")
        f2 = self.make_file("b.py", "B\n")
        mem.store([f1], "ancient bugs", "ask", "k", "old", project="p")
        mem.store([f2], "fresh bugs", "ask", "k", "new", project="p")
        self.set_age("ancient bugs", days=300)  # backdate the first
        hits = mem.recall("find the bugs", project="p", k=5)
        # same cosine, but the fresh one should rank above the 300-day-old one
        self.assertEqual(hits[0]["question"], "fresh bugs")


# --------------------------------------------------------------------------
class TestConsolidation(MemoryTestBase):
    def test_merge_collapses_cache_equivalent_dups(self):
        # Same file/tool/model, two near-identical questions (both -> bugs vec).
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans1")
        mem.store([f], "locate the bugs", "ask", "k", "ans2")
        self.assertEqual(self.count_rows(), 2)
        r = mem.consolidate(merge_threshold=0.97)
        self.assertEqual(r["merged"], 1)
        self.assertEqual(self.count_rows(), 1)

    def test_merge_sums_use_count_into_survivor(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans1")   # use_count 1
        mem.store([f], "locate the bugs", "ask", "k", "ans2") # use_count 1
        mem.lookup([f], "find the bugs", "ask", "k")          # survivor -> 2
        mem.consolidate(merge_threshold=0.97)
        # survivor keeps its 2 and inherits the merged one's 1 -> 3
        conn = sqlite3.connect(self.db)
        (uses,) = conn.execute("SELECT use_count FROM memories").fetchone()
        conn.close()
        self.assertEqual(uses, 3)

    def test_merge_does_not_cross_different_files(self):
        # Same question vector but DIFFERENT files -> different answers, keep both.
        f1 = self.make_file("a.py", "A\n")
        f2 = self.make_file("b.py", "B\n")
        mem.store([f1], "find the bugs", "ask", "k", "ans_a")
        mem.store([f2], "find the bugs", "ask", "k", "ans_b")
        r = mem.consolidate(merge_threshold=0.97)
        self.assertEqual(r["merged"], 0)
        self.assertEqual(self.count_rows(), 2)

    def test_evicts_old_and_cold(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans")  # use_count 1
        self.set_last_used("find the bugs", days=120)       # older than 90d
        r = mem.consolidate(max_age_days=90, min_uses=1)
        self.assertEqual(r["evicted"], 1)
        self.assertEqual(self.count_rows(), 0)

    def test_protects_old_but_reused(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans")
        mem.lookup([f], "find the bugs", "ask", "k")  # reinforced -> use_count 2
        self.set_last_used("find the bugs", days=120)
        r = mem.consolidate(max_age_days=90, min_uses=1)
        self.assertEqual(r["evicted"], 0)  # used > min_uses -> kept
        self.assertEqual(self.count_rows(), 1)

    def test_protects_recent_unused(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans")  # fresh, use_count 1
        r = mem.consolidate(max_age_days=90, min_uses=1)
        self.assertEqual(r["evicted"], 0)  # not old enough
        self.assertEqual(self.count_rows(), 1)

    def test_dry_run_changes_nothing(self):
        f = self.make_file()
        mem.store([f], "find the bugs", "ask", "k", "ans")
        self.set_last_used("find the bugs", days=120)
        r = mem.consolidate(max_age_days=90, dry_run=True)
        self.assertEqual(r["evicted"], 1)        # reports what it would do
        self.assertEqual(self.count_rows(), 1)   # but nothing deleted


class TestHelpers(MemoryTestBase):
    def test_normalize_unit_length(self):
        v = mem._normalize([3, 4])  # 3-4-5 triangle
        self.assertAlmostEqual(v[0], 0.6)
        self.assertAlmostEqual(v[1], 0.8)

    def test_blob_roundtrip(self):
        v = mem._normalize([1, 2, 3, 4])
        back = list(mem._from_blob(mem._to_blob(v)))
        for a, b in zip(v, back):
            self.assertAlmostEqual(a, b, places=5)

    def test_cosine_identical_is_one(self):
        v = mem._normalize([1, 1, 1])
        self.assertAlmostEqual(mem._cosine(v, v), 1.0, places=5)

    def test_current_project_returns_something(self):
        self.assertTrue(mem.current_project())  # non-empty


if __name__ == "__main__":
    unittest.main(verbosity=2)
