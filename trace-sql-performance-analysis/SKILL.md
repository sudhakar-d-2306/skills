---
name: trace-sql-performance-analysis
description: Analyze OpenTelemetry/Grafana trace JSON exports to find, time, and fix slow SQL. Use when asked to list or rank SQL execution timing from a trace, break SQL down by database or by a sub-operation nested inside one request, substitute real bind values into a templated SQL statement captured from a trace, or compare SQL timing before and after an index or query rewrite.
---

# Trace SQL performance analysis

## When this applies
Trigger this skill when the user:
- Gives an OpenTelemetry/Grafana trace export (JSON) and asks to find slow SQL, rank queries by time, or "list the SQL with timing"
- Asks for a before/after comparison of SQL after adding an index or rewriting a query
- Asks to break down one API's SQL by backend database, or by a sub-operation/sub-API nested inside the same trace
- Asks to substitute real bind values into a templated SQL statement captured from a trace
- Asks for an index recommendation based on slow statements found in a trace

## Trace shape this skill expects
An OTel/Grafana JSON export: a tree of `resourceSpans` / `instrumentationLibrarySpans` / `spans`, each span having `spanId`, `parentSpanId`, `startTimeUnixNano`, `endTimeUnixNano`, `traceId`, and an `attributes` array of `{key, value: {stringValue|intValue|...}}` pairs. A SQL call is a span carrying `db.statement` (usually alongside `db.name`, `db.system`). A logical sub-operation (a nested "mini API" call inside one outer request) is often marked by a header-echoing attribute such as `http.request.header.eventname` — grep the file for a handful of candidate attribute keys before assuming the name; it varies by system.

## Core workflow

1. **Locate and skim the file(s) first.** Trace exports can be multi-megabyte. Use `grep -o` for attribute keys and approximate span counts before loading the whole file, so you know what's actually in it before committing to a read.
2. **Decide which question is being asked** and use the matching procedure below (A–E). Most requests combine two or three of these.
3. **Never fabricate a value.** If a bind value shows as `?` or `${Name}` in the trace and no companion capture (request/response JSON, general query log, a dump you can query) supplies the real value, say so explicitly next to the SQL rather than inventing a plausible-looking one.
4. **Preserve SQL exactly.** When filling in bind values or `${}` placeholders, replace only the placeholder tokens. Do not reformat, reorder, or add/remove predicates, and never change the SELECT list — a query pasted from production must stay recognizably the same query, with values filled in.
5. **State your method, not just your numbers.** Every timing report should say which file(s) it came from, what "total time" means (sum of span durations — note this can exceed wall-clock time when work runs on parallel threads), and the time zone used for any timestamps.

### A. Rank SQL by total time in a trace
Run `scripts/sql_timing_report.py <trace.json> [--top N] [--min-ms X]`. It groups every `db.statement` span by `db.name`, then within each database ranks distinct statements (bind values and literals normalized so repeated calls of "the same query" group together) by total time, with calls/total/max. Report the top N and anything above a stated threshold — ask the user for the threshold if unspecified; default to flagging anything over 100 ms total as worth a look.

### B. Break down SQL by sub-operation inside one trace
When several logical "mini-APIs" execute inside one outer request (a workflow engine firing nested rule/event calls, for example), a flat per-database total hides which inner step owns the cost. Run `scripts/attribute_sql_by_event.py <trace.json> [--event-attr ATTR] [--events NAME ...]`. It walks each SQL span's `parentSpanId` chain up to the nearest ancestor span carrying the event-marking attribute, and reports SQL time per sub-operation plus each sub-operation's "SQL share of its own span" (SQL total ÷ span duration). High share = a direct candidate to index or rewrite. Low share = the wall time is something other than SQL (rendering, a downstream HTTP call, engine setup), and an index won't help it.

### C. Substitute real bind values into a captured SQL template
1. Find the real values from the best available source, in this order: literal values already inlined elsewhere in the same trace (some retrievers log the resolved SQL, not just the bound template); a companion request/response capture sharing the same request id; a database you can query for a plausible real row.
2. Replace only the `?` or `${Name}` tokens, one for one, in the order they appear. Count placeholders in the SELECT list, WHERE clause, and any subqueries/derived tables separately — nested `IF()`/`CASE` templates often repeat the same token shape for unrelated values.
3. Where a column alias comes from a bound value (`... AS ?`), infer it from the query's documented output or an unobfuscated sibling version of the same script, and say explicitly that you inferred it.
4. Run the substituted SQL against a safe/non-production copy of the schema and sanity-check the row count before trusting it as a timing baseline.

### D. Before/after comparison (adding an index or rewriting a query)
Run as one session so the timings are comparable:
1. `SHOW INDEX FROM <table>` for every table in the query; skip creating an index if an equivalent one (same leading columns, same order) already exists.
2. Warm the cache with one throwaway run, then time N repetitions (5–20) of the **original** query with a wall-clock pair around the call (e.g. `SYSDATE(6)` before/after in MySQL), and save the result set into a scratch table.
3. Create the candidate index, or apply the SQL rewrite.
4. Repeat the timed runs for the **new** version, saving its result set into a second scratch table.
5. Compare the two result sets for exact equality in both directions (e.g. `(A EXCEPT ALL B)` and `(B EXCEPT ALL A)` both empty) — a faster query that returns different rows is a regression, not a fix.
6. Report before/after average and worst-case time, row counts, and the EXPLAIN plan change (type/key/rows/Extra) alongside the raw timing.
7. Clean up: drop scratch tables and, unless told to keep it, drop any index created only for the test.

Flag explicitly if the test database's engine version differs from production (a newer MySQL may pick a hash join where production's older optimizer uses a nested loop, for example) — a result from the test box is not guaranteed to transfer.

### E. Producing the report
Default to a short document (or a table in the reply) with: a one-line summary of before vs. after, a table of the slow statements (description, calls, total ms, max ms), the SQL before and after values were substituted, and an explicit list of assumptions/gaps (missing tables, values the trace hid, environment differences). Keep code in fenced blocks; keep numbers in a table, not inline in prose.

## Safeguards
- Treat trace/request/response capture files as read-only evidence — never edit a source capture.
- Never run `DROP`, `TRUNCATE`, or an index change against anything other than an explicitly designated test/local database. Confirm before running anything against a shared or production-named connection.
- Redact or never echo connection strings, passwords, tokens, API keys, or session cookies found inside captures — note their presence ("an auth token is present") instead of printing the value.
- If a table a query needs doesn't exist in the environment you're testing against, say so and either ask for a real dump or build clearly-labeled synthetic data (tag every synthetic row, e.g. a sentinel value in a `CREATED_BY`/`source` column). Never present a result produced on synthetic data as production-representative timing without repeating that label every time the number is used.
- Don't assume a column alias, table relationship, or business rule you can't see in the trace or schema — ask or flag it rather than guessing silently.

## Project-specific notes (this workspace)
Delete or replace this section if reusing the skill in a different project.
- SQL is served through a retriever framework: a master/child event name resolves to a stored script (a `CNF_OBJECT_SCRIPTS`-style table) whose text has `${Placeholder}` tokens the engine substitutes before execution, or `?` placeholders bound as a prepared statement — check which form a given capture uses before counting placeholders.
- Multiple backend databases are typically involved per request (a product-config store, a system-config store holding the retriever definitions themselves, and a transaction store) — always report SQL time split by `db.name`, not a single total.
- `SET SESSION sql_mode = REPLACE(@@sql_mode,'ONLY_FULL_GROUP_BY','')` is frequently needed to run a captured query as-is outside the application, since some configured queries predate strict mode.
- A session-store call (e.g. a Mongo `find`/`update` on a sessions collection) often appears in the same trace outside any SQL event tree — count it in the total, but don't call it a MySQL cost.
