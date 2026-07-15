-- Required before enabling Hermes employee billing on PostgreSQL.
-- The guard reconciles one employee's current-month ledger before each
-- reservation. INCLUDE(spend) permits an index-only SUM when visibility map
-- coverage allows it, avoiding a full monthly SpendLogs scan.
CREATE INDEX CONCURRENTLY IF NOT EXISTS
    "LiteLLM_SpendLogs_user_startTime_spend_idx"
ON "LiteLLM_SpendLogs" ("user", "startTime")
INCLUDE ("spend");
