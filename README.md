# Merchant Analytics Assistant

This project builds two separated merchant analytics workflows with the OpenAI SDK and a Gradio chat UI.

## Internal Workflows

- File preparation and metrics workflow: cleans an uploaded transaction file, archives the input, calculates MTD TPV, previous-month TPV, and MoM growth, then saves outputs in `output/`.
- Analytics response workflow: uses saved metrics, saved markdown references, optional public web research, and a concise synthesized rule set derived from the instruction files. The runtime prompt does not paste the full PDF, DOCX, or workbook documentation into the model.

## Input File

The app accepts two kinds of uploads:

- **Tabular data** — CSV, TSV, XLSX, XLS, JSON (transaction data, analyzed with pandas).
- **Documents** — PDF, Word (.docx), PowerPoint (.pptx), TXT, Markdown (reports/decks; text + tables
  are extracted and analyzed). The agent picks the right reader automatically based on file type.

For tabular transaction data, the standard columns are:

- `Date`
- `Merchant`
- `SOF_Type`
- `Acq_Type`
- `TPV`

The app also accepts common aliases such as `transaction_date`, `merchant_name`, `source_of_fund`, `acquisition_type`, and `amount`.

For Excel workbooks, the app looks for a transaction sheet in this order:

- `Input_Template`
- `Input`
- `Transactions`
- `Raw_Data`
- `Data`

If a workbook includes `MoM_Analysis`, the app uses its `Prev_Month_TPV` column as the historical baseline. If the uploaded workbook includes `Detail_Analysis` or `Voucher_Breakdown`, those sheets are passed as compact data context for the analytics workflow.

## Configure API key (`.env`)

Copy `.env.example` to `.env` and fill in your key:

```bash
cp .env.example .env
```

```bash
GREENNODE_API_KEY=your_key_here
GREENNODE_BASE_URL=https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1
```

Notes:
- Alternative variable names are also accepted: `AI_PLATFORM_API_KEY`, `OPENAI_API_KEY`, `GREEENODE_API_KEY` (3 E's), `OPENAI_BASE_URL`. `OPENAI_MODEL` is optional.
- The app **requires** an API key — it will not start the workflow without one.

## Run locally (Python)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:7860> in your browser.

If your local Gradio port is busy, choose one explicitly:

```bash
GRADIO_SERVER_PORT=8123 python app.py
```

## Run with Docker (recommended)

The repo ships a `Dockerfile` + `docker-compose.yml`. The image bundles all
dependencies, the Vietnamese-capable PDF font, and pre-downloads the embedding
model, so the first run is fast.

```bash
# 1. Make sure .env exists in the project root (see "Configure API key" above)
# 2. Build + start
docker compose up --build

# Run in background:
docker compose up -d --build

# View logs / stop:
docker compose logs -f
docker compose down
```

Open <http://localhost:7860>.

How config and data are handled:
- **API key** — read at runtime from `.env` via `env_file` (NOT baked into the image, so it stays private).
- **Persistent data** — `output/`, `uploads/`, and `chroma_db/` (the RAG vector store) are mounted as host volumes, so they survive container rebuilds.

## Run from a pre-built image (pull & run)

If you only have the **image** (pulled from a registry) — not the source — you just need
Docker, a `.env`, and one command. Code lives in the image; **only the API key must be
supplied** (data folders are auto-created, the RAG store starts empty and fills up with use).

### A. Owner — push the image once

```bash
docker tag zalopay-merchant-analytics:latest <registry-user>/zalopay-merchant-analytics:latest
docker login
docker push <registry-user>/zalopay-merchant-analytics:latest
```

### B. Whoever pulls it — 3 steps

```bash
# 1. Create .env with YOUR own key (only file you must create)
cp .env.example .env        # then edit GREENNODE_API_KEY

# 2. Pull
docker pull <registry-user>/zalopay-merchant-analytics:latest

# 3a. Run with docker run (volume mounts persist data; Docker auto-creates the folders)
docker run -d -p 7860:7860 \
  --env-file .env \
  -v "$(pwd)/chroma_db:/app/chroma_db" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/uploads:/app/uploads" \
  <registry-user>/zalopay-merchant-analytics:latest
```

Open <http://localhost:7860>.

### One-command alternative — `docker-compose.deploy.yml`

The repo ships `docker-compose.deploy.yml` (uses `image:`, no build). Hand the puller just
this file + `.env`:

```bash
# Point it at the pushed image (or edit the default in the file)
export MERCHANT_ANALYTICS_IMAGE=<registry-user>/zalopay-merchant-analytics:latest
docker compose -f docker-compose.deploy.yml up -d
```

What the puller does / doesn't create:

| Item | Create manually? | Why |
| --- | --- | --- |
| `.env` (API key) | **Yes** | The app won't start without a key; it's personal. |
| `chroma_db/`, `output/`, `uploads/` | **No** | Auto-created by the volume mounts (or `mkdir` baked in the image). RAG starts empty and grows. |

## RAG knowledge base

The analytics agent stores every research/computation result as a markdown log
and indexes it into a local ChromaDB vector store (`chroma_db/`) using
`sentence-transformers` (`all-MiniLM-L6-v2`, runs locally, no extra API key).

- Agent 1 writes a log after each task → auto-indexed into ChromaDB.
- Agent 2 answers history questions via semantic search over that store
  (with a keyword-search fallback if the RAG libraries are unavailable).

The store starts empty and grows as you analyze more merchants.

## Public Web Research

When merchant performance needs external context, the analytics workflow can search public web pages and indexed social pages for all main scenarios: Organic decline, Paid decline, QR decline, and Growth-up diagnosis.

Research queries cover merchant payment context, competitor offers, voucher/campaign changes, QR acceptance issues, owned payment offers, public incidents, and user discussion on public/indexed social pages.

This uses public search results only. It does not log in to Facebook, Threads, or other private/social accounts.

To disable this behavior:

```bash
WEB_RESEARCH_ENABLED=false python app.py
```

Optional tuning:

```bash
WEB_RESEARCH_MAX_TARGETS=2
WEB_RESEARCH_MAX_QUERIES_PER_TARGET=10
WEB_RESEARCH_MAX_RESULTS=4
```

## Merchant Website Crawl

When a merchant segment drops (especially BNPL/Buy Now Pay Later), the agent automatically crawls the merchant's official website to detect:

- **Competing installment/BNPL services** — e.g., the merchant running their own 0% installment or partnering with Home Credit, Kredivo, etc.
- **Competing payment methods** — MoMo, VNPay, ShopeePay banners on the merchant's checkout page
- **Scandal/complaint signals** — if the merchant's own site has complaint-related language

This gives high-confidence direct evidence. For example: if Thế Giới Di Động's BNPL drops and the crawler finds "trả góp 0%" or "Home Credit" on their site, Agent 2 will flag this as the likely root cause.

To disable:

```bash
MERCHANT_WEBSITE_CRAWL_ENABLED=false python app.py
```

## Outputs

Each upload creates:

- A timestamped archived copy of the uploaded input file in `uploads/`
- A timestamped input summary markdown file in `uploads/`
- `*_arranged_*.csv`
- `*_metrics_*.csv`
- `*_summary_*.json`
- `*_metrics_report.md`
- `*_public_web_research_report.md` when web research runs

Metric output files are saved in `output/`. The chatbot reads recent markdown reports from `output/` and input summaries from `uploads/` when answering follow-up questions without a new upload.
