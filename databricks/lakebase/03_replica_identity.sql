-- Lakehouse Sync (Lakebase to Unity Catalog) needs full row images for every synced table.
ALTER TABLE pdoom.doom_sessions REPLICA IDENTITY FULL;
ALTER TABLE pdoom.doom_events REPLICA IDENTITY FULL;
ALTER TABLE pdoom.doom_decisions REPLICA IDENTITY FULL;
