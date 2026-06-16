# ── ZaloPay Merchant Analytics — Docker image ─────────────────────────────
FROM python:3.11-slim

# Tránh sinh .pyc, log unbuffered, Gradio bind ra ngoài container
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=8080 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# System deps: libgomp cho onnxruntime/torch, curl cho healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Cài torch CPU-only TRƯỚC để tránh kéo bản CUDA ~2GB (sentence-transformers cần torch)
RUN pip install --no-cache-dir torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu

# Cài dependencies còn lại (gồm: Gradio, OpenAI Agents SDK, ChromaDB + sentence-transformers cho RAG,
# fpdf2 xuất PDF, và pdfplumber/pypdf/python-docx/python-pptx để đọc tài liệu PDF/Word/PowerPoint).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download embedding model để lần chạy đầu không phải tải (~90MB)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy source code (3 agent) + static (logo, ảnh nền, bundled fonts cho PDF tiếng Việt)
COPY app.py merchant_agent_workflow.py rag_indexer.py ./
COPY static/ ./static/

# Bake BASELINE RAG vào image: môi trường không có volume (AgentBase) vẫn có sẵn kiến thức.
# Local docker-compose mount ./chroma_db sẽ che (shadow) cái này → dùng data host.
# Dùng wildcard [b] để OPTIONAL: nếu thư mục chroma_db không có (vd clone mới) thì bỏ qua, không fail.
COPY chroma_d[b]/ ./chroma_db/

# Tạo thư mục runtime còn lại (mkdir -p idempotent, giữ chroma_db đã copy)
RUN mkdir -p output uploads chroma_db

EXPOSE 8080

# Healthcheck — AgentBase yêu cầu GET /health → 200 trên port 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["python", "app.py"]
