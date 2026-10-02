# Evaluation

Manual test questions run against the live system, checking both retrieval relevance and generation quality — including intentional "this should fail gracefully" cases.

> This file is a template with two confirmed results filled in. Add the rest of your own test runs here before publishing the repo — a real evaluation table with 8–10 rows is stronger portfolio content than this partial one.

## How to use this

For each row: ask the question through the live front end (or directly via `curl`/Postman against `/ask`), record the actual answer and which source chunks came back, and judge whether it's correct, partially correct, or a reasonable refusal. Pull a few questions straight from the ingested PDFs' content, and include at least one question with no real answer in the corpus (like the Pilot Eye example) to demonstrate the system doesn't hallucinate.

| # | Question | Expected | Actual result | Verdict | Notes |
|---|---|---|---|---|---|
| 1 | What is Pilot Light? | A disaster recovery strategy involving minimal always-on infrastructure that's scaled up during failover | Correctly described the Pilot Light approach, citing the disaster recovery PDF | ✅ Correct, grounded | Confirmed during development testing |
| 2 | What is Pilot Eye and its uses? | No answer exists — "Pilot Eye" is not a real AWS term | Model responded that it doesn't know / the context doesn't cover this | ✅ Correct refusal | Retrieval still returns the nearest chunks (Pilot Light content) by design — pgvector has no relevance threshold. Generation correctly declines rather than guessing from a near-miss. See `troubleshooting.md` / `decisions-and-tradeoffs.md` for the full reasoning |
| 3 | Who is Sudha Murty? | Sudha Murty is a prolific writer in English and Kannada, who has written novels, technical books, travelogues, collections of short stories and non-fictional pieces, and four books for children.

## Known limitations surfaced by testing

- Retrieval has no similarity threshold — it always returns its *k*-nearest chunks, even when nothing in the corpus is actually relevant to the question. Generation is the only layer preventing a bad answer in that case, not retrieval itself.
- Not yet tested: behavior on very long or multi-part questions, or on questions that combine information from several widely separated chunks.