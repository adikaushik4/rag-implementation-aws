# Troubleshooting Log

Real issues hit while building this project, how each was diagnosed, and the fix. Kept in full rather than summarized away, since the debugging process is as much a part of this project as the final architecture.

## Lambda-in-VPC can't reach Bedrock (timeout)

**Symptom:** Lambda calls to Bedrock hung and hit `Task timed out`, even after raising the timeout to 60s. Removing the Lambda from the VPC made the same call succeed immediately.

**Diagnosis path:**
1. Added a raw `socket.connect()` test (bypassing boto3 entirely) to isolate whether this was a networking issue or an SDK/API issue. It also timed out — confirmed network-layer.
2. Checked DNS resolution — resolved correctly to the interface endpoint's private IP, ruling out a DNS/private-DNS misconfiguration.
3. Checked the Bedrock interface endpoint's own security group — it was not allowing inbound 443 from the Lambda's security group.

**Fix:** added an inbound rule on the endpoint's security group: HTTPS (443) from the Lambda's security group. Re-ran the raw socket test, got `Port 443 reachable!`, then confirmed the real Bedrock call worked.

**Lesson:** an interface VPC endpoint has its own security group, separate from both the Lambda's and the target resource's. It's easy to create the endpoint and never think about its SG at all.

## Batch ingestion Lambda times out mid-folder

**Symptom:** A Lambda looping over 5 PDFs in one invocation hit `Status: timeout` with `Duration: 270000.00 ms` — an exact match to the configured timeout, not a crash.

**Diagnosis:** `Max Memory Used` was well under the configured limit, ruling out an out-of-memory kill. The exact-match duration meant it was cut off mid-run, not failing. CloudWatch logs showed it had completed PDF #1 and was partway through parsing PDF #2 (a much larger document) when the time ran out.

**Fix (short-term):** raised the timeout to Lambda's hard maximum (900s / 15 minutes) and re-uploaded remaining files individually to trigger independent invocations via the existing S3 event trigger, rather than looping a whole folder in one call.

**Lesson:** Lambda's 15-minute timeout is a hard ceiling, not a tunable limit. A single invocation processing an unbounded batch doesn't scale — a real production pipeline would fan out via SQS or Step Functions so each file gets its own invocation and timeout budget.

## Duplicate rows after re-running ingestion

**Symptom:** Querying `document_chunks` showed every `(document_key, chunk_index)` pair twice.

**Diagnosis:** The Lambda had been invoked twice for the same object (once via a manual synthetic S3 test event, once via the real trigger), and the original `INSERT` had no uniqueness constraint or conflict handling, so every re-run blindly duplicated.

**Fix:** added `UNIQUE (document_key, chunk_index)` to the table, and switched the insert to an upsert:
```sql
INSERT INTO document_chunks (...) VALUES (...)
ON CONFLICT (document_key, chunk_index)
DO UPDATE SET chunk_text = EXCLUDED.chunk_text, embedding = EXCLUDED.embedding;
```

**Lesson:** any ingestion pipeline that might legitimately be re-run (retries, re-uploads, manual testing) needs idempotent writes from the start, not as an afterthought.

## RDS connection: SSL and password errors

**Symptom:** `psycopg2.connect()` from Lambda failed with both a password authentication error and a "no pg_hba.conf entry... no encryption" error in the same traceback.

**Diagnosis:** Two separate issues bundled in one error. The SSL message revealed that RDS requires an encrypted connection (`rds.force_ssl`), which `psql` negotiates automatically but `psycopg2` does not unless told to.

**Fix:** added `sslmode="require"` explicitly to the `psycopg2.connect()` call, and re-verified the DB password matched what was used in the working bastion `psql` session.

## Bedrock model access: the Marketplace maze

**Symptom:** Three different errors in sequence when trying to use Anthropic's Claude 3 Haiku for generation:
1. `AccessDeniedException` — no IAM policy allows `bedrock:InvokeModel` on the model (expected, fixed by adding the permission)
2. `AccessDeniedException` — missing `aws-marketplace:Subscribe`/`ViewSubscriptions` permissions, since Anthropic's models route through AWS Marketplace as a third-party product (unlike Amazon's own Titan/Nova, which don't)
3. `AccessDeniedException: INVALID_PAYMENT_INSTRUMENT` — a Marketplace-specific billing/payment verification failure, separate from the account's normal AWS billing

**Resolution:** rather than chase down the Marketplace payment issue, switched the generation model to **Amazon Nova Lite** — a first-party model with no Marketplace dependency, using the same Converse API call shape, so the fix was a one-line model ID change plus an IAM update.

**Lesson:** first-party AWS models (Titan, Nova) and third-party models (Anthropic, AI21, Cohere, etc.) on Bedrock have genuinely different access paths. The "no more manual model access" change from late 2025 only applies to first-party models.

## Nova Lite: "on-demand throughput isn't supported"

**Symptom:** Switching to `amazon.nova-lite-v1:0` as a plain model ID failed with `ValidationException: Invocation of model ID ... with on-demand throughput isn't supported. Retry your request with the ID or ARN of an inference profile.`

**Fix:** used the cross-region inference profile ID instead (`apac.amazon.nova-lite-v1:0`), and added IAM permissions for both the foundation-model ARN and the separate inference-profile ARN.

**Follow-on issue:** the first attempt with the profile still failed with `AccessDeniedException`, but the error now named `ap-southeast-2` — a different region than the `ap-south-1`-pinned IAM policy allowed. The `apac.` profile routes calls across multiple APAC regions depending on availability, so a region-pinned policy intermittently breaks.

**Fix:** wildcarded the region on the foundation-model resource ARN (`arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0`), while keeping the inference-profile ARN itself pinned to the home region.

## API Gateway: the "Not Found" chain

**Symptom:** The `/ask` invoke URL returned `{"message":"Not Found"}`, across multiple troubleshooting attempts, despite the Lambda itself working perfectly in isolated console tests.

This turned out to be **three unrelated issues**, diagnosed one at a time:

1. **Testing with the wrong HTTP method.** A browser GET to a `POST`-only route always returns `Not Found`, since API Gateway treats method + path as a single routable unit. Pasting a URL into a browser tab or running bare `curl <url>` both default to GET.
2. **A local network connectivity issue.** `curl` hung for ~130 seconds and failed to connect at all, even across two different ISPs — but a browser request to the identical URL connected instantly. This isolated it to `curl.exe` specifically (common with some antivirus/endpoint security tools that treat command-line HTTP clients as higher-risk than browsers). Confirmed by testing with PowerShell's `Invoke-WebRequest` instead, which worked immediately.
3. **A leftover Lambda authorizer on the route**, requiring credentials no request was providing — once the method issue and the tooling issue were ruled out, this produced a clear `{"message":"Unauthorized"}` instead of a vague "Not Found," which pointed straight at the fix.

**Fix:** removed the Lambda authorizer (set route authorization to `NONE`), confirmed `POST /ask` worked via PowerShell, and only afterward resolved the `curl` issue became irrelevant to the actual debugging (switched to PowerShell/Postman for testing going forward).

**Lesson:** when a symptom resists an obvious fix, check whether it's actually more than one problem wearing the same error message. `nslookup`, testing with a second tool, and testing with a second request method each eliminated one variable at a time rather than guessing at the whole stack.

## A debugging habit that helped throughout

Reading the exact numbers in CloudWatch's `REPORT` line (not just the error message) caught two separate root causes directly: an exact timeout-duration match that ruled out a memory crash, and a `Max Memory Used` figure well under the limit that ruled out another. The raw data in these logs was consistently more reliable than the first guess at a cause.