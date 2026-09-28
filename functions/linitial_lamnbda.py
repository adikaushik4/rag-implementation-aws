import io
import json
import os
import boto3
import psycopg2
from psycopg2.extras import execute_values
from pypdf import PdfReader

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime", region_name="ap-south-1")

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]


def get_embedding(text: str) -> list[float]:
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({
            "inputText": text,
            "dimensions": 1024,
            "normalize": True
        })
    )
    result = json.loads(response["body"].read())
    return result["embedding"]


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        if chunk.strip():
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


def extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)
    return "\n".join(pages)


def insert_chunks_batch(conn, records: list[tuple]):
    """Inserts a batch of tuples: (document_key, chunk_index, chunk_text, embedding)"""
    query = """
        INSERT INTO document_chunks (document_key, chunk_index, chunk_text, embedding)
        VALUES %s
        ON CONFLICT (document_key, chunk_index)
        DO UPDATE SET 
            chunk_text = EXCLUDED.chunk_text,
            embedding = EXCLUDED.embedding;
    """
    with conn.cursor() as cur:
        # execute_values dynamically formats the (%s, %s, %s, %s) template
        execute_values(cur, query, records, template="(%s, %s, %s, %s)")
    conn.commit()


def lambda_handler(event, context):
    record = event["Records"][0]["s3"]
    bucket = record["bucket"]["name"]
    key = record["object"]["key"]

    print(f"Processing s3://{bucket}/{key}")

    # 1. Pull PDF from S3
    obj = s3.get_object(Bucket=bucket, Key=key)
    pdf_bytes = obj["Body"].read()

    # 2. Extract text
    full_text = extract_pdf_text(pdf_bytes)
    if not full_text.strip():
        return {"statusCode": 200, "body": f"No extractable text in {key}"}

    # 3. Chunk text
    chunks = chunk_text(full_text)
    print(f"Split into {len(chunks)} chunks")

    # 4. Generate embeddings and build records list
    records = []
    for i, chunk in enumerate(chunks):
        embedding = get_embedding(chunk)
        # embedding is formatted as a string for pgvector/TEXT columns
        records.append((key, i, chunk, str(embedding)))
        print(f"Generated embedding for chunk {i + 1}/{len(chunks)}")

    # 5. Connect and batch insert all records in a single round-trip
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
        sslmode="require"
    )

    try:
        insert_chunks_batch(conn, records)
        print(f"Successfully inserted {len(records)} chunks.")
    finally:
        conn.close()

    return {
        "statusCode": 200,
        "body": f"Ingested {len(chunks)} chunks from {key}"
    }