# Database contract

## Authority

The database is authoritative. Legacy files such as `saves/存档*.json`,
`live_party.json`, `world_state.json`, and `combat_state.json` are not part of
this workflow.

## Snapshot coverage

A complete snapshot contains campaign metadata and configuration plus:

- world state;
- party and characters;
- combats;
- plot summaries and campaign events;
- mutable module progress such as scene state;
- campaign-scoped player/character channel records if present.
- a player-facing recap generated from the delta against the previous save.

Module source documents, chapter metadata, and scene indexes are immutable imported content.
They remain in the database across restores and are referenced by scene-state IDs rather than
copied into every snapshot.

Snapshot rows, tool audit history, state revisions, dice audit history, and global
rule/compendium data are intentionally not nested into a snapshot. Restoring a
snapshot retains those historical records.

## Campaign memory

`campaign_memories` stores campaign-scoped narrative facts derived from recap
`memory_candidates` and `future_impact`. It is outside snapshot boundaries:
restoring an older save does not delete or roll back long-term memories.

Each memory uses `(campaign_id, entity_type, entity_id, fact_type)` as its stable
identity so a later recap updates the same fact instead of creating a near-duplicate.
High-priority facts become `permanent`, medium-priority facts become `candidate`,
and low-priority facts remain only in the snapshot recap.

## Vector storage

Dense embedding vectors use the configured BGE profile: BGE-M3 (1024-dim),
BGE Small Chinese (512-dim), or BGE Small English (384-dim). They are stored
**outside snapshot boundaries**:

- **ChromaDB** (when `CHROMA_DB_URL` or `CHROMA_DB_PATH` is set): vectors live in
  model-isolated `dnd_rules__<profile>` and `dnd_modules__<profile>` collections
  with a validated model/dimension manifest and HNSW indexing.
  Snapshots never read or write ChromaDB — vectors are regenerated from SQL chunk
  content on re-ingest or via `vector reindex`.
- **Fallback** (no ChromaDB configured): vectors are stored in the
  `rule_chunks.embedding_json` and `module_chunks.embedding_json` JSON columns.
  These columns are part of the static chunk rows and are never captured or
  restored by snapshots.

In both paths, restoring a snapshot does not delete, replace, or invalidate
vector data. After changing `DND_EMBEDDING_PROFILES`, run `vector reindex` so
SQL model metadata and ChromaDB collections remain compatible.

## Isolation and identity

All snapshot lookup uses `(campaign_id, slot)`. Restore additionally validates
that `snapshot_json.campaign_id` equals the requested campaign. Importing or
cloning into a new campaign is a separate future operation; ordinary load must
never rewrite campaign identity.

## USER.md projection

`Campaign.config.user_md_player_roles` is the database copy of the managed
campaign block in `USER.md`. Only an explicit workspace synchronization path
may project this player-role mapping:

- save: USER.md block -> campaign config -> snapshot;
- load: snapshot -> campaign config -> USER.md block;
- undo: restored campaign config -> USER.md block.

Native save, restore, recap, and campaign-memory operations never write
`USER.md`. Narrative facts, NPC relationships, plot state, and quest state
belong in `campaign_memories`, not `USER.md`. The rest of `USER.md` is outside
campaign state and must remain unchanged.

## Errors

CLI output is JSON. A nonzero exit with an `error` object means no successful
operation should be reported. Snapshot restore is transactional; database state
rolls back when restoration fails.
