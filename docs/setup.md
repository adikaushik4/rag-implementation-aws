# VPC, endpoints, RDS, Lambda layer, API Gateway

Setup Guide

Step-by-step infrastructure setup to reproduce this project. Assumes an AWS free-tier account and basic familiarity with the console.

1. Networking
Create a new VPC with at least two private subnets (across two AZs, for the RDS subnet group).
Create an S3 Gateway endpoint and associate it with the route tables used by your private subnets. Gateway endpoints are free and required since Lambda in a private subnet has no internet route to S3 otherwise.
Create an Interface VPC endpoint for com.amazonaws.<region>.bedrock-runtime, in the same subnets/AZs as your Lambdas.
Enable Private DNS on the endpoint — without it, Bedrock calls resolve to a public IP that a private subnet with no NAT can't reach.
Give the endpoint its own security group allowing inbound 443 from your Lambda's security group. This is the single most common point of failure in this setup.
2. RDS (PostgreSQL + pgvector)
Create an RDS PostgreSQL instance (db.t3.micro or db.t4g.micro, free-tier eligible), Public access: No, placed in a DB subnet group spanning your private subnets.
Security group: inbound 5432 allowed only from your Lambda's security group (reference the SG, not a CIDR range).
Since RDS has no public access, connect via a temporary bastion host over AWS Systems Manager (SSM) — no SSH keys, no open inbound ports:
bash
   aws ssm start-session --target <bastion-instance-id>
   sudo dnf install -y postgresql15   # Amazon Linux 2023
   psql -h <rds-endpoint> -p 5432 -U <master-username> -d <db-name>
Enable pgvector and create the schema:
sql
   CREATE EXTENSION IF NOT EXISTS vector;

   CREATE TABLE document_chunks (
       id            SERIAL PRIMARY KEY,
       document_key  TEXT NOT NULL,
       chunk_index   INT NOT NULL,
       chunk_text    TEXT NOT NULL,
       embedding     vector(1024),
       created_at    TIMESTAMPTZ DEFAULT now(),
       UNIQUE (document_key, chunk_index)
   );

   CREATE INDEX ON document_chunks
   USING hnsw (embedding vector_cosine_ops);
Terminate the bastion once setup is confirmed with \dt and \dx — it isn't needed for runtime traffic, since Lambda reaches RDS directly over the VPC.
3. S3
Create a bucket for source PDFs (e.g. aurora-embeddings), private, no public access.
Upload PDFs under a prefix (e.g. initial/).
4. Lambda layer (shared dependencies)

psycopg2-binary ships compiled code, so build on the target platform explicitly:

bash
pip install -r requirements.txt -t python/ \
  --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12
zip -r layer.zip python

Match --python-version to the function's runtime, and use manylinux2014_aarch64 if running on Arm. Upload layer.zip as a Lambda layer and attach it to both Lambdas below.

5. Ingestion Lambda
Create the function, attach the shared layer, set it inside the VPC (private subnets, Lambda security group).
Environment variables: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.
Execution role permissions needed:
s3:GetObject on the source bucket's objects
s3:ListBucket on the bucket itself (separate resource ARN, no trailing /*) — only needed if using the batch/backfill mode
bedrock:InvokeModel on the Titan embedding model ARN
Add an S3 trigger: ObjectCreated events on the source bucket/prefix.
Timeout: at least 2–3 minutes for a single PDF; batch mode processing a whole folder can hit Lambda's 15-minute hard cap — see troubleshooting.md.
Memory: at least 512MB.
6. Retrieval/generation Lambda
Create a second function, same layer, same VPC placement.
Same DB environment variables as above.
Execution role permissions:
bedrock:InvokeModel on the Titan embedding model ARN
bedrock:InvokeModel on the generation model ARN (and, if using a cross-region inference profile like Nova Lite's apac. profile, a wildcarded region on the foundation-model ARN plus the inference-profile ARN — see troubleshooting.md)
Timeout: ~30 seconds is enough (one embedding call, one DB query, one generation call — no PDF parsing).
7. API Gateway
Create an HTTP API (cheaper and simpler than REST API for this use case).
Add route POST /ask, integration target: the retrieval Lambda.
Leave Authorization set to NONE for a public demo endpoint (or add a Lambda authorizer if you want to lock it down — see note below).
Enable CORS on the route: Access-Control-Allow-Origin: *, headers and methods wildcarded, so a browser-based front end can call it.
Confirm the stage is deployed ($default auto-deploys on HTTP APIs).

Cost/safety note: a public, unauthenticated endpoint that calls Bedrock on every request can run up a bill if discovered and hit repeatedly. Set a throttle limit on the stage (a few requests/second is plenty for a demo) and an AWS Budget alert.

8. Front end
Build a static HTML/JS page that fetch()s the API Gateway URL.
Host it on an S3 bucket configured for static website hosting, with a public bucket policy scoped to s3:GetObject:
json
   {
       "Version": "2012-10-17",
       "Statement": [{
           "Sid": "PublicReadGetObject",
           "Effect": "Allow",
           "Principal": "*",
           "Action": "s3:GetObject",
           "Resource": "arn:aws:s3:::<bucket-name>/*"
       }]
   }
Use the bucket's website endpoint URL (from Properties → Static website hosting), not the regular object URL.
Testing order

Don't wire everything together and test once. Test in this order, confirming each step before moving on:

Bastion/psql connection → schema exists
Isolated embedding call (hardcoded test string, no S3/RDS) → confirms Bedrock connectivity from inside the VPC
Full ingestion Lambda against one real PDF (synthetic S3 test event) → confirms rows land in document_chunks
Isolated retrieval/generation call (hardcoded question) → confirms Bedrock generation + pgvector search
API Gateway route, tested with a tool other than curl if curl behaves unexpectedly (see troubleshooting.md)
Front end against the live API