# Coworker Memory — TODO / Open Problems

Deferred items to discuss and tackle deliberately. Not blocking; Layers 1 & 2
are live-validated and working. Captured 2026-06-05.

---

## 1. Embedding "usage contract" robustness (prefix + model versioning)

**Observed:** `nomic-embed-text` needs a task prefix (`search_query: `). Without
it, a genuine rephrase scored 0.884; with it, 0.931. Same texts, same model —
~0.05–0.10 swing purely from prompt format. Currently handled by a hardcoded
default `EMBED_PREFIX="search_query: "`.

**Why it's a real problem:**
- The correct prefix is **model-specific**. Switch `COWORKER_EMBED_MODEL` and the
  default prefix may be wrong (or harmful).
- **Silent corruption risk:** stored vectors are only comparable to query vectors
  if BOTH used the same model + prefix. If a user changes `EMBED_MODEL` or
  `EMBED_PREFIX`, every previously-stored embedding becomes incomparable — recall
  silently returns garbage similarities, no error.

**Direction to discuss:**
- Stamp each row with the embedding `model` + `prefix` (a "embed signature").
  On query, only compare against rows with a matching signature.
- On signature change, lazily re-embed (or flag stale) instead of mixing spaces.
- Consider asymmetric prefixes (`search_query:` for the question, `search_document:`
  for the stored side) — may improve recall quality.
- Document the contract loudly. This is a top real-world embedding bug.

---

## 2. Single global similarity threshold is fragile (→ adaptive thresholds)

**Observed:** "where are the vulnerabilities" vs "find security bugs" sits at
0.60 — genuinely the same *intent*, but below the 0.92 Tier-1 bar, so it
(correctly, safely) fell through to recall-only. A fixed global number can't
cleanly separate "safe to return cached answer" from "merely related" across all
query types, because different query categories have different density in
embedding space (code clusters tightly; prose spreads out).

**Why it's a real problem:** one threshold either over-fires on prose or
under-fires on code. This is the **vCache** finding from our research.

**Direction to discuss:**
- Per-entry / adaptive thresholds (vCache-style online learning) instead of one
  global constant.
- Or category-aware thresholds (detect code-heavy vs prose questions).
- First step is measurement: log the cosine distribution of known-equivalent vs
  known-different pairs before changing anything.

---

## 3. Recall is not free — token budget control

**Observed:** Run 4 used 470 input tokens vs Run 1's 210 — the injected recall
memory added ~260 tokens. Recall trades input cost for a better-grounded answer
(Run 4's answer was more thorough). Right now we inject up to `k=3` memories with
no size cap.

**Why it's a real problem:** uncapped injection can erase the token savings the
whole tool exists to deliver, or blow the worker's context on a big repo.

**Direction to discuss:**
- A token budget for injected context (cap count AND total chars).
- `--recall-k` / `--no-recall` per-call control (flag already exists for the
  latter; expose k).
- Only inject when confidence (cosine) is high enough to be worth the tokens —
  a recall-injection floor distinct from the recall-display floor.
- Measure NET token savings (cache hits saved − recall tokens spent) and surface
  it in `coworker-memory stats`.

---

## Also queued (from the build, not yet scheduled)
- **Consolidation / `prune`**: Importance · Merge (cos>0.97 dedup) · Decay · Eviction.
- **Embed the output too** (not just the question) for richer Tier-2 recall.
- **Layer 3: MCP exposure** — expose `recall` as an MCP resource, workers as MCP
  tools (after Anthropic Academy Intro-to-MCP course).
