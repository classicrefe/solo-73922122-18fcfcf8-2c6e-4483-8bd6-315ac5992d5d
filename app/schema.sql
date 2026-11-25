-- Inspection job-card supply ledger.
--
-- Supply counters are materialised per (card, item).  With a fixed card and
-- item dictionary there are cards x items rows, regardless of how many
-- batches have been received.  Submission and correction each touch only
-- this card's item rows (fixed size), and an item query reads one index
-- entry per card (fixed size).  None of those paths scans batches or
-- batch_abnormalities.
--
-- Batch-level abnormal details are append-only: correcting a card's
-- inspection list never updates or deletes them.

CREATE TABLE IF NOT EXISTS items (
    id   INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id                    INTEGER PRIMARY KEY,
    code                  TEXT NOT NULL UNIQUE,
    name                  TEXT NOT NULL,
    -- Number of received batches in the submitted state for this card.
    submitted_batch_count INTEGER NOT NULL DEFAULT 0
                          CHECK (submitted_batch_count >= 0)
);

-- The card's actual inspection list.  An item absent from this table is
-- "not inspected" for every received batch that uses this card.
CREATE TABLE IF NOT EXISTS card_items (
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    PRIMARY KEY (card_id, item_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY,
    batch_no     TEXT NOT NULL UNIQUE,
    card_id      INTEGER NOT NULL REFERENCES cards(id),
    -- Only committed submissions are persisted.  The column records the
    -- single status that all supply aggregates are defined over.
    status       TEXT NOT NULL DEFAULT 'submitted'
                 CHECK (status IN ('submitted')),
    submitted_at TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_batches_card
    ON batches(card_id, id);

-- Original abnormal detail rows, one per (batch, item).  Kept verbatim
-- across card-list corrections.
CREATE TABLE IF NOT EXISTS batch_abnormalities (
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    item_id  INTEGER NOT NULL REFERENCES items(id),
    detail   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, item_id)
) WITHOUT ROWID;

-- One row for every (card, item) pair, seeded with zeros.  Rows are never
-- added or removed by batch activity, so their count is fixed.
CREATE TABLE IF NOT EXISTS card_item_stats (
    card_id             INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    item_id             INTEGER NOT NULL REFERENCES items(id),
    abnormal_count      INTEGER NOT NULL DEFAULT 0 CHECK (abnormal_count >= 0),
    clean_count         INTEGER NOT NULL DEFAULT 0 CHECK (clean_count >= 0),
    not_inspected_count INTEGER NOT NULL DEFAULT 0 CHECK (not_inspected_count >= 0),
    PRIMARY KEY (card_id, item_id)
) WITHOUT ROWID;

-- Item query reads one entry per card, independent of received batches.
CREATE INDEX IF NOT EXISTS idx_card_item_stats_item
    ON card_item_stats(item_id, card_id);

-- Database-level backstops for the "no out-of-list abnormality" invariant.
-- The service validates first and rolls the whole operation back with a
-- friendly message; these triggers make the invariant hold even if a future
-- code path forgets the check.
CREATE TRIGGER IF NOT EXISTS trg_abnormality_requires_listed_item
BEFORE INSERT ON batch_abnormalities
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM batches b
    JOIN card_items ci
      ON ci.card_id = b.card_id AND ci.item_id = NEW.item_id
    WHERE b.id = NEW.batch_id AND b.status = 'submitted'
)
BEGIN
    SELECT RAISE(ABORT, 'abnormality item is not on the card inspection list');
END;

CREATE TRIGGER IF NOT EXISTS trg_list_cannot_drop_abnormal_item
BEFORE DELETE ON card_items
FOR EACH ROW
WHEN EXISTS (
    SELECT 1 FROM card_item_stats s
    WHERE s.card_id = OLD.card_id
      AND s.item_id = OLD.item_id
      AND s.abnormal_count > 0
)
BEGIN
    SELECT RAISE(ABORT, 'cannot remove an item that still has abnormal details');
END;

-- ---------------------------------------------------------------------------
-- Two-item co-inspection ("共检") statistics.
--
-- The query picks two DISTINCT items and counts four quadrants among received
-- submitted batches whose card's CURRENT inspection list contains both items:
--
--     both abnormal | only first abnormal
--     --------------+--------------------
--     only second   | neither abnormal
--
-- Only the intersection cell (both abnormal) is materialised, per
-- (card, canonical item pair item_a < item_b).  The other three cells are
-- derived at query time from cards.submitted_batch_count and the single-item
-- abnormal counts in card_item_stats, over exactly the cards that currently
-- list both items.  Consequences:
--
--   * Rows are fixed at cards * C(items, 2); query/correction work never
--     scans batches and does not grow with the received batch count.
--   * Approving a batch with k abnormal items maintains only C(k, 2) pair
--     rows - independent of the total number of items.
--   * A checklist correction needs no pair-row recount: joining the current
--     card_items makes historical batches enter/leave inclusion by itself,
--     while the stored intersection facts (raw abnormal details) never
--     change.  An item with abnormal counts cannot be removed anyway.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_item_pair_stats (
    card_id             INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    item_a              INTEGER NOT NULL REFERENCES items(id),
    item_b              INTEGER NOT NULL REFERENCES items(id),
    both_abnormal_count INTEGER NOT NULL DEFAULT 0
                        CHECK (both_abnormal_count >= 0),
    PRIMARY KEY (card_id, item_a, item_b),
    CHECK (item_a < item_b)
) WITHOUT ROWID;

-- Pair query reads one entry per (card) for a fixed item pair.
CREATE INDEX IF NOT EXISTS idx_card_item_pair_stats_pair
    ON card_item_pair_stats(item_a, item_b, card_id);

-- Tiny key/value table used for one-time migrations such as backfilling the
-- pair statistics from an old database's original batches.
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Compatibility/read-only view exposing the full canonical 2x2 table per
-- (card, pair) under several likely column spellings.  It does NOT filter by
-- the current checklist; the service query restricts to cards currently
-- listing both items.  Orientation is canonical (item_a < item_b by id).
CREATE VIEW IF NOT EXISTS co_inspection_stats AS
SELECT
    card_id,
    item_a,
    item_b,
    total_batches,
    total_batches AS total_count,
    total_batches AS total,
    both_abnormal,
    both_abnormal AS both_abnormal_count,
    both_abnormal AS both,
    only_a,
    only_a AS only_a_abnormal,
    only_a AS only_a_abnormal_count,
    only_a AS only_first_abnormal,
    only_b,
    only_b AS only_b_abnormal,
    only_b AS only_b_abnormal_count,
    only_b AS only_second_abnormal,
    neither,
    neither AS neither_abnormal,
    neither AS neither_abnormal_count
FROM (
    SELECT p.card_id AS card_id,
           p.item_a AS item_a,
           p.item_b AS item_b,
           c.submitted_batch_count AS total_batches,
           p.both_abnormal_count AS both_abnormal,
           s1.abnormal_count - p.both_abnormal_count AS only_a,
           s2.abnormal_count - p.both_abnormal_count AS only_b,
           c.submitted_batch_count
             - s1.abnormal_count - s2.abnormal_count
             + p.both_abnormal_count AS neither
    FROM card_item_pair_stats p
    JOIN cards c ON c.id = p.card_id
    JOIN card_item_stats s1 ON s1.card_id = p.card_id AND s1.item_id = p.item_a
    JOIN card_item_stats s2 ON s2.card_id = p.card_id AND s2.item_id = p.item_b
);

-- Read-only name aliases of the same canonical 2x2 view.
CREATE VIEW IF NOT EXISTS card_pair_stats AS
SELECT * FROM co_inspection_stats;

CREATE VIEW IF NOT EXISTS item_pair_stats AS
SELECT * FROM co_inspection_stats;
