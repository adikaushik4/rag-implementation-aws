import json
import os
import boto3
import psycopg2

bedrock = boto3.client("bedrock-runtime", region_name="ap-south-1")

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
GEN_MODEL_ID = "apac.amazon.nova-lite-v1:0"
  

def get_embedding(text: str) -> list[float]:
    response = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps({
            "inputText": text,
            "dimensions": 1024,
            "normalize": True
        })
    )
    result = json.loads(response["body"].read())
    return result["embedding"]


def retrieve_chunks(conn, query_embedding: list[float], k: int = 5) -> list[dict]:
    """Returns the top-k most similar chunks to the query embedding."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT document_key, chunk_index, chunk_text,
                   embedding <-> %s::vector AS distance
            FROM document_chunks
            ORDER BY embedding <-> %s::vector
            LIMIT %s
            """,
            (str(query_embedding), str(query_embedding), k)
        )
        rows = cur.fetchall()

    return [
        {"document_key": r[0], "chunk_index": r[1], "chunk_text": r[2], "distance": r[3]}
        for r in rows
    ]


def build_prompt(question: str, chunks: list[dict]) -> str:
    context = "\n\n".join(
        f"[Source: {c['document_key']}, chunk {c['chunk_index']}]\n{c['chunk_text']}"
        for c in chunks
    )
    return f"""Answer the question using only the context below. If the context doesn't contain the answer, say you don't know rather than guessing.

Context:
{context}

Question: {question}

Answer:"""


def generate_answer(prompt: str) -> str:
    response = bedrock.converse(
        modelId=GEN_MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [{"text": prompt}]
            }
        ],
        inferenceConfig={
            "maxTokens": 512,
            "temperature": 0.2
        }
    )
    return response["output"]["message"]["content"][0]["text"]

def lambda_handler(event, context):
    print(f"Raw event: {json.dumps(event)}")
    body = json.loads(event["body"]) if "body" in event else event
    question = body.get("question")

    if not question:
        return {"statusCode": 400, "body": json.dumps({"error": "Missing 'question' in request"})}

    print(f"Question: {question}")

    # 1. Embed the question
    query_embedding = get_embedding(question)

    # 2. Retrieve relevant chunks
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
        sslmode="require"
    )
    try:
        chunks = retrieve_chunks(conn, query_embedding, k=5)
    finally:
        conn.close()

    print(f"Retrieved {len(chunks)} chunks")
    if not chunks:
        return {"statusCode": 200, "body": json.dumps({"answer": "No relevant context found."})}

    # 3. Build prompt and generate answer
    prompt = build_prompt(question, chunks)
    answer = generate_answer(prompt)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "answer": answer,
            "sources": [{"document": c["document_key"], "chunk": c["chunk_index"]} for c in chunks]
        })
    }