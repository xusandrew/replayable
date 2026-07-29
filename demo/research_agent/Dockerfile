FROM replayable/agent-base:local

RUN pip install --no-cache-dir \
    anthropic==0.75.0 \
    httpx==0.28.1

WORKDIR /app
COPY agent.py /app/agent.py

CMD ["python", "/app/agent.py", "deterministic replay for LLM agents"]
