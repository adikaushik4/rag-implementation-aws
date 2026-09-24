# rag-implementation-aws
Created a RAG pipeline from Scratch using AWS. PDF ingestion, chunking, embeddings, pgvector storage, semantic retrieval, public API → hosted front end.
day 1 -- day 2
DAY3:
## Day 2-3 — Vector Store Setup & Ingestion Pipeline

### What was built
- Provisioned RDS PostgreSQL (free-tier eligible instance) in private subnets, no public access
- Locked down RDS security group to accept inbound 5432 only from trusted VPC security groups
- Launched a temporary bastion host, accessed via AWS Systems Manager (SSM) Session Manager — no open SSH ports, no bastion key pair
- Enabled the `pgvector` extension and created the `document_chunks` schema:
```sql
  CREATE TABLE document_chunks (
      id            SERIAL PRIMARY KEY,
      document_key  TEXT NOT NULL,
      chunk_index   INT NOT NULL,
      chunk_text    TEXT NOT NULL,
      embedding     vector(1024),
      created_at    TIMESTAMPTZ DEFAULT now()
  );
  CREATE INDEX ON document_chunks USING hnsw (embedding vector_cosine_ops);
```
- Designed and implemented the ingestion Lambda: S3 event trigger → extract PDF text (`pypdf`) → chunk text → generate embeddings (Bedrock Titan Text Embeddings V2) → write chunk + vector to RDS

### Key tradeoffs
| Decision | Choice | Why |
|---|---|---|
| DB access method | Bastion host + SSM (terminated after use) | Avoided a permanent NAT Gateway/public endpoint just for one-time schema setup; SSM avoids managing SSH keys or open inbound ports |
| Vector index type | HNSW over IVFFlat | Faster queries, no need to pre-populate the table before indexing — better fit for incremental ingestion |
| Embedding model | Amazon Titan Text Embeddings V2 (1024-dim) | Cheapest Bedrock embedding option (~$0.02/M input tokens); no meaningful quality tradeoff at this scale |
| Chunking strategy | Word-count based, 500 words with 50-word overlap | Simple and sufficient for portfolio scale; token-based chunking (tiktoken) would be more precise but adds complexity without real benefit here |
| DB credentials in Lambda | Environment variables | Faster to implement than Secrets Manager, which would require an additional VPC interface endpoint (added cost + complexity) for a project this size |
| Bastion lifecycle | Terminated after schema setup | Not needed for runtime traffic (Lambda ↔ RDS is direct via security groups); avoids paying for idle EC2 |

### Known limitations

Day 4:
## Day 4 — Bedrock VPC Connectivity & Ingestion Pipeline Complete

### What was built
- Diagnosed and fixed a Lambda-in-VPC timeout calling Bedrock: the `bedrock-runtime` interface endpoint's security group wasn't allowing inbound 443 from the Lambda's security group
- Isolated the fix in stages: raw socket connectivity test → confirmed Bedrock API call → confirmed inside real ingestion flow
- Built and deployed the full ingestion Lambda handler:
  - S3 event → download PDF → extract text (`pypdf`) → chunk (500 words, 50-word overlap) → embed each chunk (Titan Text Embeddings V2) → write chunk + vector to `document_chunks` in RDS
- Added `psycopg2-binary` + `pypdf` as a Lambda layer
- Updated the Lambda execution role to include both S3 and Bedrock permissions (was previously scoped to S3 only)
- Ran an end-to-end test: ingested 22 chunks from a real PDF (`disaster-recovery-workloads-on-aws.pdf`), confirmed via query through the bastion host

### Key tradeoffs / decisions
| Decision | Choice | Why |
|---|---|---|
| Bedrock connectivity from VPC | Interface VPC endpoint (`bedrock-runtime`) | Cheaper than a NAT Gateway; scoped to only the service actually needed |
| Debugging approach | Isolated each layer (DNS → raw socket → API call → full pipeline) before combining | Made a single confusing timeout traceable to one specific security group misconfiguration, instead of guessing across multiple possible causes at once |
| Model access setup | None required — skipped the old "request model access" console step | AWS retired manual model access approval as of Sept 29, 2025; Bedrock models are now auto-enabled per account, access controlled via IAM only |
| Build vs. managed (Knowledge Bases) | Hand-rolled ingestion Lambda | More failure points, but every layer (chunking, embedding calls, schema, retrieval) is something I designed and can explain — stronger portfolio signal than a managed black box |

### Known limitations (carried over + new)
- Chunking is still naive (word-count based, not sentence/paragraph aware)
- DB credentials still via Lambda environment variables, not Secrets Manager
- Only tested against one PDF so far — text extraction quality on more complex layouts (multi-column, tables) not yet verified
- Bedrock has no free tier — costs are pay-per-token from the first call, kept low by model choice, not eliminated
- Chunking is naive (word count, not sentence/paragraph aware) — acceptable for now, a candidate for later refinement
- Credentials via env vars are fine for a portfolio project but wouldn't be production-appropriate as-is

day 5
## Day 5 — Generation, Model Access Troubleshooting & API Gateway

### What was built
- Built and tested the retrieval + generation Lambda: embeds the question (Titan), runs pgvector similarity search against `document_chunks`, assembles a prompt from the top-k chunks, calls Bedrock's Converse API for a generated answer, returns both the answer and its sources
- Fixed a chain of IAM and model-access issues to get generation working:
  - Added `bedrock:InvokeModel` permission for the generation model (separate from the existing Titan permission)
  - Fixed a real code bug: a hardcoded question string (`{"What is Pilot Eye?"}` as a Python set literal) that had been silently overriding the actual `question` parameter in every prompt
  - Diagnosed a `Sandbox.Timedout` batch-ingestion failure as a Lambda-hard-cap issue (15 min max), not a memory issue — resolved by processing PDFs individually rather than looping a whole folder in one invocation
  - Hit and worked through AWS Marketplace model-access requirements for Anthropic's Claude 3 Haiku (IAM Marketplace permissions, then account-level activation, then an `INVALID_PAYMENT_INSTRUMENT` billing block)
  - Switched generation model to **Amazon Nova Lite** to avoid the Marketplace/billing dependency entirely
  - Fixed a follow-on IAM gap when Nova Lite's cross-region inference profile routed the call to `ap-southeast-2` instead of `ap-south-1`, requiring a wildcarded region in the IAM resource ARN
- Created an HTTP API in API Gateway to front the retrieval/generation Lambda
- Currently debugging: invoke URL returns `{"message":"Not Found"}` — Lambda itself is confirmed fully working via direct test, so this is isolated to the API Gateway route/path/stage configuration, not the function logic

### Key tradeoffs / decisions
| Decision | Choice | Why |
|---|---|---|
| Generation model | Amazon Nova Lite over Anthropic Claude 3 Haiku | Claude 3 Haiku is a third-party AWS Marketplace model requiring a separate billing/subscription path; hit a payment verification block specific to Marketplace billing. Nova Lite is first-party, no Marketplace dependency, same Converse API — avoided unrelated billing complexity without changing the architecture |
| Model invocation method | Converse API over InvokeModel for generation | Provider-agnostic request/response shape — swapping generation models (as happened today) required no change to the request format, only the model ID and IAM resource |
| IAM scoping for inference profiles | Wildcarded region (`bedrock:*::foundation-model/...`) rather than pinned to `ap-south-1` | Cross-region inference profiles route calls to whichever destination region is available/optimal; a region-pinned IAM policy breaks unpredictably depending on where a given call lands |
| Batch ingestion pattern | Per-file invocation instead of one Lambda looping a whole folder | Hit Lambda's 15-minute hard timeout mid-folder; confirmed real-world RAG pipelines typically use SQS fan-out or Step Functions at scale — noted as a known limitation of the current simple design |

### Known limitations
- API Gateway integration not yet working end to end — Lambda works standalone, HTTP path still broken
- Generation model choice was driven partly by billing/access friction, not pure model-quality comparison — worth being explicit about this in the final write-up

day 6
## Day 6 (Final) — API Gateway, Front End, and Project Completion

### What was built
- Fixed the API Gateway "Not Found" chain, which turned out to be three separate, unrelated issues stacked together:
  1. A stray leftover **Lambda authorizer** on the `POST /ask` route requiring credentials no client was sending — removed
  2. `curl.exe` specifically being silently blocked/intercepted on the local machine (confirmed via a working `Invoke-WebRequest` call to the identical endpoint) — unrelated to AWS, purely a local tooling issue
  3. A transient local network connectivity issue, separate from both of the above, that resolved on its own
- Confirmed full pipeline working end to end over HTTP: `POST /ask` → embed question → pgvector retrieval → Bedrock generation → answer + sources, returned as real JSON over a public API
- Configured CORS on the route (`Access-Control-Allow-Origin: *`, headers/methods wildcarded) to support a browser-based front end
- Built and deployed a front end: a single-page HTML/CSS/JS interface with a configurable API endpoint field, loading state, and source citations displayed per answer
- Hosted the front end on **S3 static website hosting** (`my-rag-frontend-bckt`) with a public bucket read policy — keeping the entire stack, front end included, inside the same AWS project
- Verified end-to-end behavior against real questions, including confirming that off-corpus questions (e.g. a nonexistent "Pilot Eye" strategy) correctly return "I don't know" rather than a hallucinated answer

### Key tradeoffs / decisions
| Decision | Choice | Why |
|---|---|---|
| Debugging approach for the API Gateway failure | Isolated network, tooling, and auth as three separate variables rather than assuming one root cause | The symptoms (timeout, then 404, then 401) looked like one bug but were three independent issues; testing with a different tool (PowerShell vs curl) and a different auth config in isolation was what actually separated them |
| Refusal on unanswerable questions | Kept the "say you don't know" instruction in the generation prompt rather than loosening it | Retrieval already returns the nearest chunks regardless of relevance (no similarity threshold); a system that confidently answers off-corpus questions would be hallucinating, not helping — this is treated as correct behavior, not a bug |
| Front end hosting | S3 static website over an external host (Netlify/GitHub Pages) | Keeps the entire project — ingestion, compute, AI, storage, and the UI — inside one AWS account/project, consistent with the portfolio goal of demonstrating end-to-end AWS ownership |

### Known limitations (final list)
- No similarity threshold on retrieval — the system always returns its k-nearest chunks even when nothing in the corpus is actually relevant; generation is the only layer preventing a bad answer, not retrieval
- Chunking remains word-count based, not sentence/paragraph aware
- DB credentials passed via Lambda environment variables, not Secrets Manager
- Ingestion is single-file-per-invocation; a real production version would use SQS fan-out or Step Functions to handle larger batches within Lambda's 15-minute hard limit
- Generation model (Nova Lite) was chosen partly due to AWS Marketplace billing friction with the originally intended Claude 3 Haiku, not purely on model-quality comparison — noted explicitly rather than presented as the ideal choice

### Outcome
End-to-end RAG pipeline live and working: PDF ingestion → chunking → embeddings → pgvector storage → semantic retrieval → grounded generation → public API → hosted front end. Project considered complete.
