import os, pathlib, tempfile, unittest, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))
import coworker_memory as mem

class StatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "memory.db")
        os.environ["COWORKER_MEMORY_DB"] = self.db
        mem.DB_PATH = pathlib.Path(self.db)

    def test_empty_store_reports_zero(self):
        s = mem.stats()
        self.assertIsInstance(s, dict)
        self.assertEqual(s["total_memories"], 0)

    def test_counts_inserted_rows(self):
        conn = mem._connect()
        conn.execute("INSERT INTO memories (content_key, created_at, last_used) VALUES (?,?,?)", ("k1", 0, 0))
        conn.execute("INSERT INTO memories (content_key, created_at, last_used) VALUES (?,?,?)", ("k2", 0, 0))
        conn.commit(); conn.close()
        self.assertEqual(mem.stats()["total_memories"], 2)

    def test_fails_open_on_bad_db(self):
        mem.DB_PATH = pathlib.Path("/nonexistent-dir/\x00bad/memory.db")
        s = mem.stats()
        self.assertEqual(s["total_memories"], 0)

if __name__ == "__main__":
    unittest.main()
