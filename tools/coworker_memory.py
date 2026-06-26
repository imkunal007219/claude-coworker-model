"""Coworker Memory — Layer 1: a content-addressed cache for worker calls.

THE CORE IDEA
-------------
A normal cache keys on the *question text*. That's fragile: the same question
about a file that has since changed would wrongly return a stale answer.

We key on the *content* instead. The cache key is derived from:
    sha256( file contents ) + normalized question + tool + model

So a cached answer is only ever returned when the files are byte-for-byte the
same AND you ask the same thing. The instant a file changes, its hash changes,
the key changes, and you get a clean miss — no stale answers, no manual
invalidation. The freshness check is *built into the key*.

Because the key depends only on content (not on time or session), session #47
three months from now hits the exact same key as session #1. That is what makes
this memory survive across Claude Code sessions.

DESIGN PRINCIPLE: memory is an accelerator, never a dependency. Every function
here FAILS OPEN — on any error it behaves as a cache miss and lets the real
worker call proceed. A broken cache must never break the user's task.
"""
import array
import hashlib
import json
import math
import os
import pathlib
import sqlite3
import time
import urllib.request

# Where the memory lives. One SQLite file in the user's home, overridable for
# tests via COWORKER_MEMORY_DB. Survives reboots, sessions, months.
DB_PATH = pathlib.Path(
    os.environ.get("COWORKER_MEMORY_DB",
                   pathlib.Path.home() / ".coworker-memory" / "memory.db")
)

# --- Layer 2: embedder config ---------------------------------------------
# We embed text with a LOCAL Ollama model by default: free, offline, private,
# and it keeps coworker_memory.py dependency-free (we call Ollama over plain
# urllib, no extra pip packages). If Ollama isn't running, every embed returns
# None and Layer 2 silently disables itself — Layer 1 keeps working. Fail-open.
EMBED_URL = os.environ.get("COWORKER_EMBED_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("COWORKER_EMBED_MODEL", "nomic-embed-text")
# nomic-embed-text was TRAINED with task prefixes and loses discrimination
# without them (rephrasings collapse toward the unrelated band). We prepend the
# same prefix to every text — both at store and query — so comparisons stay
# valid. Set COWORKER_EMBED_PREFIX="" for models that don't expect a prefix.
EMBED_PREFIX = os.environ.get("COWORKER_EMBED_PREFIX", "search_query: ")

# Thresholds. Code clusters TIGHTLY in embedding space (constrained vocab:
# function names, APIs), so the semantic-cache tier uses a HIGH bar — returning
# a wrong cached answer is the catastrophic failure. Recall only *adds* context,
# so its floor is lower. Both overridable via env.
CACHE_THRESHOLD = float(os.environ.get("COWORKER_CACHE_THRESHOLD", "0.92"))
RECALL_FLOOR = float(os.environ.get("COWORKER_RECALL_FLOOR", "0.55"))
RECALL_HALF_LIFE_DAYS = float(os.environ.get("COWORKER_RECALL_HALFLIFE", "60"))


def _connect():
    """Open the DB, creating the schema on first use.

    `timeout=5` means: if another process holds the write lock, wait up to 5s
    instead of erroring instantly. SQLite is a single file with a single writer,
    and two Claude sessions might write at once, so we give them room.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            content_key TEXT PRIMARY KEY,  -- the cache key (see compute_key)
            project     TEXT,              -- scope tag (cwd basename)
            tool        TEXT,              -- 'ask' or 'write'
            question    TEXT,              -- original question, for recall later
            file_paths  TEXT,              -- JSON list of paths
            file_hashes TEXT,              -- JSON {path: sha256}
            output      TEXT,              -- the worker's answer (RAW, not summarized)
            model       TEXT,
            created_at  REAL,
            last_used   REAL,              -- updated on every hit (reinforcement)
            use_count   INTEGER DEFAULT 1, -- how often this memory proved useful
            embedding   BLOB               -- Layer 2: question vector (float32)
        )
        """
    )
    # Migration: an existing Layer-1 DB won't have the `embedding` column yet.
    # ALTER TABLE ADD COLUMN is cheap and idempotent-by-try.
    try:
        conn.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    return conn


def hash_file(path):
    """SHA-256 of the file's BYTES — its true identity.

    Why hash content and not the modification time (mtime)?
      - `git checkout`, `cp`, `touch` all change mtime without changing content.
      - Editing then undoing leaves content identical but mtime newer.
    mtime answers "was this file touched?"; we need "is this the same file?".
    Content hashing answers that exactly. We stream in 64 KB chunks so a huge
    file never has to sit fully in memory.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_question(q):
    """Collapse trivial differences so 'Find  the BUGS' == 'find the bugs'.

    We deliberately stay conservative here — only case and whitespace. Matching
    on *meaning* ('find bugs' == 'locate defects') is Layer 2's job (semantic
    recall). The exact tier must never guess.
    """
    return " ".join(q.lower().split())


def compute_key(paths, question, tool, model):
    """Build the content-addressed cache key.

    Returns (key, file_hashes). Sorting the hashes means the *order* of --paths
    doesn't matter: asking about [a.py, b.py] hits the same key as [b.py, a.py].
    """
    file_hashes = {p: hash_file(p) for p in paths}
    blob = json.dumps(
        {
            "files": sorted(file_hashes.values()),
            "q": _normalize_question(question),
            "tool": tool,
            "model": model,
        },
        sort_keys=True,
    )
    key = hashlib.sha256(blob.encode()).hexdigest()
    return key, file_hashes


def current_project():
    """Identify the current project for memory scoping.

    Prefer the git repository's top-level folder name (stable, survives `cd`
    into subdirs), falling back to the cwd basename when not in a git repo.
    Note: still not globally unique (two repos named 'api' collide) — good
    enough for scoping, and Layer 2's cross-project search tolerates it.
    """
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=1)
        if r.returncode == 0 and r.stdout.strip():
            return os.path.basename(r.stdout.strip())
    except Exception:
        pass
    return os.path.basename(os.getcwd())


def _normalize(vec):
    """Scale a vector to unit length. Once both vectors are unit-length,
    cosine similarity is just their dot product — one multiply-add, no
    division at query time. We normalize once at store/query and store the
    result, so search is as cheap as possible."""
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def embed(text):
    """Text -> unit vector via local Ollama. Returns None on any failure.

    None is the 'embedder unavailable' signal that makes Layer 2 degrade
    gracefully to Layer 1. We never raise — a missing/slow embedder must not
    break a worker call.
    """
    try:
        data = json.dumps({"model": EMBED_MODEL,
                           "prompt": EMBED_PREFIX + text}).encode()
        req = urllib.request.Request(
            EMBED_URL.rstrip("/") + "/api/embeddings",
            data=data, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            vec = json.loads(r.read()).get("embedding")
        return _normalize(vec) if vec else None
    except Exception:
        return None


def _to_blob(vec):
    """Pack a float list into compact float32 bytes for the SQLite BLOB."""
    return array.array("f", vec).tobytes()


def _from_blob(blob):
    """Unpack float32 bytes back into an indexable array."""
    a = array.array("f")
    a.frombytes(blob)
    return a


def _cosine(a, b):
    """Dot product of two unit vectors == cosine similarity. We brute-force
    over all rows in Python. For a single user's local store (hundreds to a
    few thousand memories) this is a few milliseconds — cheaper than standing
    up a vector index. A dedicated vector DB only earns its keep at far larger
    scale."""
    return sum(x * y for x, y in zip(a, b))


def lookup(paths, question, tool, model):
    """Return the cached output, or None on a miss. Never raises.

    A hit requires BOTH: the key matches, AND every file still hashes the same
    (defense-in-depth — the hashes are already baked into the key, but verifying
    again costs nothing and guards against edge cases like hash collisions or a
    file swapped between key-compute and lookup).

    On a hit we *reinforce* the memory: bump use_count and last_used. Layer 2's
    consolidation will use these to keep frequently-useful memories alive and
    let one-offs decay away.
    """
    conn = None
    try:
        key, file_hashes = compute_key(paths, question, tool, model)
        conn = _connect()
        row = conn.execute(
            "SELECT output, file_hashes FROM memories WHERE content_key=?",
            (key,),
        ).fetchone()
        if not row:
            return None
        stored_output, stored_hashes = row[0], json.loads(row[1])
        if stored_hashes != file_hashes:
            return None
        conn.execute(
            "UPDATE memories SET last_used=?, use_count=use_count+1 "
            "WHERE content_key=?",
            (time.time(), key),
        )
        conn.commit()
        return stored_output
    except (OSError, sqlite3.Error, json.JSONDecodeError):
        return None  # fail open: any trouble -> treat as a miss
    finally:
        if conn is not None:
            conn.close()


def store(paths, question, tool, model, output, project=None):
    """Persist a worker result. Best-effort: failures are swallowed.

    We store the RAW output (not an LLM re-summary) so future recall stays
    faithful to what the worker actually said. use_count is preserved across
    re-stores of the same key via COALESCE.
    """
    conn = None
    try:
        key, file_hashes = compute_key(paths, question, tool, model)
        project = project or current_project()
        now = time.time()
        # Embed the QUESTION (what was asked). Layer 2 matches a new question
        # against these. If the embedder is down, emb is None and this memory
        # simply won't participate in semantic search until re-stored.
        emb = embed(question)
        blob = _to_blob(emb) if emb else None
        conn = _connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO memories
              (content_key, project, tool, question, file_paths, file_hashes,
               output, model, created_at, last_used, use_count, embedding)
            VALUES (?,?,?,?,?,?,?,?,?,?,
              COALESCE((SELECT use_count FROM memories WHERE content_key=?), 1), ?)
            """,
            (key, project, tool, question, json.dumps(paths),
             json.dumps(file_hashes), output, model, now, now, key, blob),
        )
        conn.commit()
    except (OSError, sqlite3.Error):
        pass  # storing is best-effort; never break the caller
    finally:
        if conn is not None:
            conn.close()


def semantic_lookup(paths, question, tool, model, qvec=None, threshold=None):
    """TIER 1 — semantic cache. Return a cached answer when a *near-identical*
    question was asked about the *same unchanged files*.

    This catches rephrasings the exact tier misses ("find the bugs" vs "where
    are the defects"). It is deliberately strict: same tool, same model, same
    file hashes, AND question similarity >= a HIGH threshold. Returning a wrong
    answer here is worse than a miss, so we err toward missing.
    """
    threshold = CACHE_THRESHOLD if threshold is None else threshold
    if qvec is None:
        qvec = embed(question)
    if qvec is None:
        return None  # embedder unavailable -> Layer 2 off
    conn = None
    try:
        _, file_hashes = compute_key(paths, question, tool, model)
        target = sorted(file_hashes.values())
        conn = _connect()
        rows = conn.execute(
            "SELECT content_key, output, file_hashes, embedding FROM memories "
            "WHERE tool=? AND model=? AND embedding IS NOT NULL",
            (tool, model),
        ).fetchall()
        best_key, best_out, best_score = None, None, threshold
        for ckey, output, fh, emb in rows:
            if sorted(json.loads(fh).values()) != target:
                continue  # different files -> not a cache-equivalent
            score = _cosine(qvec, _from_blob(emb))
            if score >= best_score:
                best_key, best_out, best_score = ckey, output, score
        if best_key:
            conn.execute(
                "UPDATE memories SET last_used=?, use_count=use_count+1 "
                "WHERE content_key=?", (time.time(), best_key))
            conn.commit()
            return best_out
        return None
    except (OSError, sqlite3.Error, json.JSONDecodeError):
        return None
    finally:
        if conn is not None:
            conn.close()


def recall(question, project=None, qvec=None, k=3, floor=None,
           scope="project+global"):
    """TIER 2 — recall related past conclusions to INJECT as context.

    Unlike the cache tiers this never short-circuits the worker; it just hands
    the worker relevant prior findings. So the bar is lower and the inputs need
    not match — a conclusion about the same module from another question (or
    another repo) is fair game.

    Ranking blends three signals:
      cosine            — semantic relevance (the main driver)
      recency           — older memories decay (half-life configurable), but
                          we floor the decay so a very relevant old memory still
                          surfaces. Reinforced memories (high use_count) resist
                          decay via a small boost.
    Scope honours your 'scoped + opt-in global' choice: search THIS project
    first; only widen into other projects if in-project hits are thin.
    """
    floor = RECALL_FLOOR if floor is None else floor
    if qvec is None:
        qvec = embed(question)
    if qvec is None:
        return []
    project = project or current_project()
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT question, output, project, embedding, created_at, use_count "
            "FROM memories WHERE embedding IS NOT NULL").fetchall()
        now = time.time()

        def rank(subset, cross):
            out = []
            for q_text, output, proj, emb, created, uses in subset:
                cos = _cosine(qvec, _from_blob(emb))
                if cos < floor:
                    continue
                age_days = max(0.0, (now - created) / 86400.0)
                recency = max(0.6, 0.5 ** (age_days / RECALL_HALF_LIFE_DAYS))
                boost = 1.0 + min(0.2, 0.05 * math.log1p(uses))  # reinforcement
                penalty = 0.85 if cross else 1.0  # gently prefer in-project
                eff = cos * recency * boost * penalty
                out.append({"score": eff, "cosine": cos, "project": proj,
                            "question": q_text, "output": output,
                            "age_days": age_days})
            out.sort(key=lambda m: m["score"], reverse=True)
            return out

        in_proj = [r for r in rows if r[2] == project]
        results = rank(in_proj, cross=False)[:k]
        if len(results) < k and scope == "project+global":
            others = [r for r in rows if r[2] != project]
            results += rank(others, cross=True)[: k - len(results)]
        return results
    except (sqlite3.Error, json.JSONDecodeError):
        return []
    finally:
        if conn is not None:
            conn.close()


def consolidate(max_age_days=90, min_uses=1, merge_threshold=0.97,
                dry_run=False):
    """CONSOLIDATION — keep the store small, fast, and trustworthy.

    Borrowed from how brains turn a flood of experiences into durable memory:
    strengthen what's used, drop the trivia. Four levers:

      MERGE     collapse redundant memories. We ONLY merge memories that are
                already cache-equivalent under Tier 1 — same files + tool +
                model, and near-identical question (cosine >= merge_threshold).
                This guarantees we never fold two genuinely DIFFERENT answers
                into one. The survivor inherits the others' use_count.
      DECAY     a memory's 'age' is measured from last_used, NOT created_at —
                so every cache hit (reinforcement) resets its clock. Frequently
                useful memories never grow old.
      IMPORTANCE use_count is the importance proxy. A reused memory is precious.
      EVICTION  delete memories that are BOTH old (last_used > max_age_days) AND
                cold (use_count <= min_uses, i.e. never reinforced). Old-but-used
                and recent memories are kept.

    Returns a report; pass dry_run=True to preview without changing anything.
    """
    conn = None
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT content_key, project, tool, model, file_hashes, question, "
            "embedding, last_used, use_count FROM memories").fetchall()
        total = len(rows)

        # --- MERGE -------------------------------------------------------
        # Group by the Tier-1 equivalence key, then greedily cluster within
        # each group by question similarity.
        groups = {}
        for r in rows:
            ckey, project, tool, model, fh, q, emb, last, uses = r
            if emb is None:
                continue  # can't compare without a vector
            gkey = (project, tool, model, fh)
            groups.setdefault(gkey, []).append(r)

        to_delete = set()
        add_uses = {}  # survivor content_key -> inherited use_count
        for members in groups.values():
            if len(members) < 2:
                continue
            # canonical = most-used, then most-recent
            members = sorted(members, key=lambda r: (r[8], r[7]), reverse=True)
            survivors = []
            for m in members:
                m_emb = _from_blob(m[6])
                dup_of = next((s for s in survivors
                               if _cosine(m_emb, _from_blob(s[6])) >= merge_threshold),
                              None)
                if dup_of:
                    to_delete.add(m[0])
                    add_uses[dup_of[0]] = add_uses.get(dup_of[0], 0) + m[8]
                else:
                    survivors.append(m)
        merged = len(to_delete)

        # --- DECAY -> EVICTION ------------------------------------------
        now = time.time()
        evict = set()
        for r in rows:
            ckey, last, uses = r[0], r[7], r[8]
            if ckey in to_delete:
                continue
            age_days = (now - last) / 86400.0
            eff_uses = uses + add_uses.get(ckey, 0)
            if age_days > max_age_days and eff_uses <= min_uses:
                evict.add(ckey)
        evicted = len(evict)

        if not dry_run:
            for ckey, extra in add_uses.items():
                if ckey not in evict:
                    conn.execute("UPDATE memories SET use_count=use_count+? "
                                 "WHERE content_key=?", (extra, ckey))
            for ckey in (to_delete | evict):
                conn.execute("DELETE FROM memories WHERE content_key=?", (ckey,))
            conn.commit()

        return {"scanned": total, "merged": merged, "evicted": evicted,
                "remaining": total - merged - evicted, "dry_run": dry_run}
    except (sqlite3.Error, json.JSONDecodeError):
        return {"scanned": 0, "merged": 0, "evicted": 0, "remaining": 0,
                "dry_run": dry_run, "error": True}
    finally:
        if conn is not None:
            conn.close()


def stats():
    """Quick health snapshot for the admin command / tests."""
    conn = None
    try:
        conn = _connect()
        n, hits = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(use_count - 1), 0) FROM memories"
        ).fetchone()
        return {"memories": n, "cache_hits_served": hits, "db": str(DB_PATH)}
    except sqlite3.Error:
        return {"memories": 0, "cache_hits_served": 0, "db": str(DB_PATH)}
    finally:
        if conn is not None:
            conn.close()


def stats():
    """Read-only, fail-open snapshot of the memory store.

    Returns a dict (currently just total cached entries). On ANY error
    (missing/locked/corrupt DB) it returns safe zero defaults rather than
    raising — memory is an accelerator, never a dependency.
    """
    try:
        conn = _connect()
        try:
            (n,) = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        finally:
            conn.close()
        return {"total_memories": int(n)}
    except Exception:
        return {"total_memories": 0}
