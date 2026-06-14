from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd
import requests
from dotenv import load_dotenv
from openai import AsyncOpenAI
from agents import Agent, Runner, handoff, function_tool, ModelSettings
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
UPLOAD_DIR = BASE_DIR / "uploads"

# RAG indexer — lazy, không crash nếu chưa cài chromadb/sentence-transformers
try:
    import rag_indexer as rag
    _RAG_AVAILABLE = True
except Exception:
    rag = None  # type: ignore
    _RAG_AVAILABLE = False

REQUIRED_COLUMNS = ["Date", "Merchant", "SOF_Type", "Acq_Type", "TPV"]
GROUP_COLUMNS = ["Merchant", "SOF_Type", "Acq_Type"]
EXCEL_TRANSACTION_SHEETS = ["Input_Template", "Input", "Transactions", "Raw_Data", "Data"]
FALSEY_ENV_VALUES = {"0", "false", "no", "off", "disabled"}

DATA_PREPARATION_INSTRUCTIONS = """
Data preparation workflow.
- Accept CSV, JSON, or Excel workbook uploads.
- For Excel, find the transaction sheet by schema, preferring Input_Template.
- Required transaction fields are Date, Merchant, SOF_Type, Acquisition_Type or Acq_Type, and TPV.
- Clean dates and TPV, standardize Acquisition_Type internally to Acq_Type, and drop invalid rows.
- Aggregate by Merchant, SOF_Type, and Acq_Type.
- Calculate MTD_TPV from the first day of the as-of month through the as-of date.
- Use MoM_Analysis.Prev_Month_TPV as the historical baseline when the workbook provides it; otherwise calculate previous month TPV from uploaded transactions.
- Calculate MoM_Growth_% = (MTD_TPV - Prev_Month_TPV) / Prev_Month_TPV.
- Save arranged transactions, segment metrics, and JSON summary into the output folder.
- Archive every uploaded source file into the uploads folder with a timestamped filename.
- Save an input summary markdown file and an output metric markdown report with timestamped names.
- Do not create business recommendations; only produce trustworthy prepared data and metrics.
""".strip()

ANALYTICS_INSTRUCTIONS = """
Bạn là ZaloPay Merchant Analytics Assistant — trợ lý phân tích hiệu suất thanh toán cho các merchant của ZaloPay.

## Vai trò
Sau khi merchant ký kết với ZaloPay, mọi giao dịch của họ đi qua cổng thanh toán ZaloPay.
Nhiệm vụ của bạn là phân tích TPV (Total Payment Volume) theo từng segment (Merchant × SOF_Type × Acquisition channel),
tìm nguyên nhân tăng/giảm, và đề xuất hành động cụ thể để tối ưu PnL.

## Quy tắc giao tiếp
- Không tự giới thiệu trừ khi được hỏi. Nếu hỏi, trả lời: "Tôi là trợ lý phân tích merchant của ZaloPay."
- KHÔNG nhắc đến tên file, đường dẫn, CSV, JSON, output folder với user. Chỉ trình bày insights.
- KHÔNG emit tool-call syntax trong câu trả lời.
- Sau mỗi lần phân tích xong, luôn hỏi user: "Bạn có muốn xuất báo cáo PDF không?"

## Logic phân tích theo 4 Scenarios

### Scenario 1 — ORGANIC CHANNEL ↓
1. So YoY: nếu seasonal → monitor, không cần action ngay
2. Nếu bất thường → điều tra:
   - Competitor lấy thị phần? → counter-campaign
   - Internal campaign kết thúc? → relaunch
   - Feedback tiêu cực trên social (Threads, Facebook)? → fix UX, retention campaign

### Scenario 2 — PAID CHANNEL ↓
1. Breakdown theo voucher (xem Voucher_Breakdown nếu có)
2. Phân tích budget:
   - Budget bị cắt → BÌNH THƯỜNG, đánh giá ROI
   - Budget giữ nguyên → CHẤT LƯỢNG KÉM, test creative/targeting mới
   - Budget tăng mà vẫn giảm → MARKET SATURATION, thử voucher thay thế

### Scenario 3 — QR CHANNEL ↓
- KHÔNG tự chẩn đoán. Escalate lên BIZ team / Area Manager kèm: MTD vs Previous, YoY, vùng ảnh hưởng.

### Scenario 4 — GROWTH ↑
- Organic growth: amplify (PR, social push), replicate với minimal budget
- Paid growth: kiểm tra ROI trước → nếu dương mới scale budget, monitor ad fatigue

## Nguyên nhân thường gặp theo SOF_Type
- **BNPL drop**: Merchant tự ra dịch vụ trả góp riêng (vd: treo banner Home Credit, Kredivo trên website) → crawl website merchant
- **VietQR drop**: Lỗi kỹ thuật, campaign offline kết thúc → escalate BIZ
- **Organic drop**: Phốt trên Threads/Facebook → search social
- **Paid drop**: Budget thay đổi, voucher kém hiệu quả → xem Voucher_Breakdown

## Web Research
- Website merchant (HIGH confidence): tìm banner BNPL/trả góp cạnh tranh
- News/báo chí (MEDIUM confidence): campaign, sự kiện
- Social public posts (LOW-MEDIUM confidence): phốt, khiếu nại, viral
- Luôn ghi confidence level và cite URL khi trình bày web findings

## Format output
Trả lời dạng markdown:
1. **Executive Summary**: Tổng MTD TPV, MoM growth
2. **High Growth Segments**: Top tăng mạnh + driver là gì
3. **Underperforming Segments**: Top giảm + chẩn đoán nguyên nhân theo scenario
4. **New Segments**: Xuất hiện tháng này
5. **Recommended Actions**: Hành động cụ thể
6. **Web Research Findings**: Bằng chứng từ internet (nếu có)
""".strip()


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-") or "merchant_transactions"


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _canonical_column_map(columns: List[str]) -> Dict[str, str]:
    aliases = {
        "date": "Date",
        "transaction_date": "Date",
        "trans_date": "Date",
        "txn_date": "Date",
        "merchant": "Merchant",
        "merchant_name": "Merchant",
        "merchant_id": "Merchant",
        "sof_type": "SOF_Type",
        "sof": "SOF_Type",
        "source_of_fund": "SOF_Type",
        "source_of_funds": "SOF_Type",
        "payment_method": "SOF_Type",
        "acq_type": "Acq_Type",
        "acquisition_type": "Acq_Type",
        "acquisition": "Acq_Type",
        "channel": "Acq_Type",
        "tpv": "TPV",
        "amount": "TPV",
        "transaction_amount": "TPV",
        "payment_volume": "TPV",
        "total_payment_volume": "TPV",
    }
    mapping: Dict[str, str] = {}
    used: set = set()
    for column in columns:
        canonical = aliases.get(_normalize_header(column))
        if canonical and canonical not in used:
            mapping[column] = canonical
            used.add(canonical)
    return mapping


def _canonical_metric_column_map(columns: List[str]) -> Dict[str, str]:
    aliases = {
        "merchant": "Merchant",
        "merchant_name": "Merchant",
        "merchant_id": "Merchant",
        "sof_type": "SOF_Type",
        "sof": "SOF_Type",
        "source_of_fund": "SOF_Type",
        "source_of_funds": "SOF_Type",
        "payment_method": "SOF_Type",
        "acq_type": "Acq_Type",
        "acquisition_type": "Acq_Type",
        "acquisition": "Acq_Type",
        "channel": "Acq_Type",
        "mtd_tpv": "MTD_TPV",
        "month_to_date_tpv": "MTD_TPV",
        "prev_month_tpv": "Prev_Month_TPV",
        "previous_month_tpv": "Prev_Month_TPV",
        "mom_growth": "MoM_Growth_%",
        "mom_growth_pct": "MoM_Growth_%",
        "mom_growth_percent": "MoM_Growth_%",
    }
    mapping: Dict[str, str] = {}
    used: set = set()
    for column in columns:
        canonical = aliases.get(_normalize_header(column))
        if canonical and canonical not in used:
            mapping[column] = canonical
            used.add(canonical)
    return mapping


def _read_excel_transactions(path: Path) -> pd.DataFrame:
    excel = pd.ExcelFile(path)
    candidates = [sheet for sheet in EXCEL_TRANSACTION_SHEETS if sheet in excel.sheet_names]
    candidates.extend(sheet for sheet in excel.sheet_names if sheet not in candidates)

    for sheet in candidates:
        preview = pd.read_excel(excel, sheet_name=sheet, nrows=0)
        mapping = _canonical_column_map([str(column) for column in preview.columns])
        if all(column in mapping.values() for column in REQUIRED_COLUMNS):
            frame = pd.read_excel(excel, sheet_name=sheet)
            frame.attrs["source_sheet"] = sheet
            return frame

    raise ValueError(
        "No transaction sheet found. Expected a sheet like Input_Template with "
        "Date, Merchant, SOF_Type, Acquisition_Type, and TPV columns."
    )


def _read_reference_previous_month(path: Path) -> Tuple[Optional[pd.DataFrame], str]:
    if path.suffix.lower() not in {".xlsx", ".xlsm", ".xls"}:
        return None, "uploaded transactions"

    excel = pd.ExcelFile(path)
    if "MoM_Analysis" not in excel.sheet_names:
        return None, "uploaded transactions"

    reference = pd.read_excel(excel, sheet_name="MoM_Analysis")
    reference = reference.rename(
        columns=_canonical_metric_column_map([str(column) for column in reference.columns])
    )
    required = GROUP_COLUMNS + ["Prev_Month_TPV"]
    if any(column not in reference.columns for column in required):
        return None, "uploaded transactions"

    reference = reference[required].copy()
    reference["Prev_Month_TPV"] = pd.to_numeric(
        reference["Prev_Month_TPV"].astype(str).str.replace(r"[^\d.-]", "", regex=True),
        errors="coerce",
    )
    for column in GROUP_COLUMNS:
        reference[column] = reference[column].astype(str).str.strip()
    reference = reference.dropna(subset=required)
    reference = reference.groupby(GROUP_COLUMNS, dropna=False)["Prev_Month_TPV"].sum().reset_index()
    if reference.empty:
        return None, "uploaded transactions"
    return reference, "MoM_Analysis sheet"


def _read_uploaded_workbook_context(file_path: str) -> str:
    path = Path(file_path).expanduser().resolve()
    if path.suffix.lower() not in {".xlsx", ".xlsm", ".xls"}:
        return ""

    excel = pd.ExcelFile(path)
    context_blocks: List[str] = []
    for sheet_name in ["Detail_Analysis", "Voucher_Breakdown"]:
        if sheet_name not in excel.sheet_names:
            continue
        frame = pd.read_excel(excel, sheet_name=sheet_name).dropna(how="all")
        if frame.empty:
            continue
        records = frame.head(20).to_dict(orient="records")
        context_blocks.append(
            f"{sheet_name} data from uploaded workbook:\n"
            f"{json.dumps(records, ensure_ascii=False, default=str)}"
        )

    if not context_blocks:
        return ""
    return "\n\n".join(context_blocks)


def _archive_input_file(source_path: Path, timestamp: str) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    source_path = source_path.resolve()
    upload_root = UPLOAD_DIR.resolve()
    if upload_root in source_path.parents:
        return source_path

    suffix = source_path.suffix.lower()
    archived_path = UPLOAD_DIR / f"{timestamp}_{_slug(source_path.stem)}{suffix}"
    shutil.copy2(source_path, archived_path)
    return archived_path


def _workbook_sheet_names(path: Path) -> List[str]:
    if path.suffix.lower() not in {".xlsx", ".xlsm", ".xls"}:
        return []
    try:
        excel = pd.ExcelFile(path)
        return list(excel.sheet_names)
    except Exception:
        return []


def _markdown_table(records: List[Dict[str, Any]], columns: List[str]) -> str:
    if not records:
        return "_None._"

    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    rows = []
    for record in records:
        values = []
        for column in columns:
            value = record.get(column, "")
            if isinstance(value, float):
                value = round(value, 2)
            values.append(str(value).replace("\n", " "))
        rows.append("| " + " | ".join(values) + " |")
    return "\n".join([header, separator, *rows])


def _write_input_summary_markdown(
    *,
    original_path: Path,
    archived_path: Path,
    frame: pd.DataFrame,
    source_sheet: str,
    timestamp: str,
) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = UPLOAD_DIR / f"{timestamp}_{_slug(original_path.stem)}_input_summary.md"
    date_min = frame["Date"].min().strftime("%Y-%m-%d")
    date_max = frame["Date"].max().strftime("%Y-%m-%d")
    sheet_names = _workbook_sheet_names(archived_path)

    lines = [
        "# Input Archive Summary",
        "",
        f"- Created at: {timestamp}",
        f"- Original file: {original_path}",
        f"- Archived file: {archived_path}",
        f"- Source sheet: {source_sheet}",
        f"- Workbook sheets: {', '.join(sheet_names) if sheet_names else 'N/A'}",
        f"- Valid transaction rows: {len(frame)}",
        f"- Date range: {date_min} to {date_max}",
        f"- Merchant count: {frame['Merchant'].nunique()}",
        f"- SOF types: {', '.join(sorted(frame['SOF_Type'].dropna().unique()))}",
        f"- Acquisition types: {', '.join(sorted(frame['Acq_Type'].dropna().unique()))}",
        "",
        "## Purpose",
        "",
        "This archived file is retained so historical metrics can be recalculated later, "
        "including previous MTD or previous month baselines.",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def _write_metrics_markdown_report(
    *,
    summary: Dict[str, Any],
    timestamp: str,
    stem: str,
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"{timestamp}_{stem}_metrics_report.md"
    segment_columns = ["Merchant", "SOF_Type", "Acq_Type", "MTD_TPV", "Prev_Month_TPV", "MoM_Growth_%", "MoM_Status"]

    lines = [
        "# Merchant Metrics Report",
        "",
        f"- Created at: {timestamp}",
        f"- Source file: {summary['source_file']}",
        f"- Archived input file: {summary['archived_input_file']}",
        f"- Input summary file: {summary['input_summary_file']}",
        f"- Source sheet: {summary['source_sheet']}",
        f"- As-of date: {summary['as_of_date']}",
        f"- MTD period: {summary['mtd_period']}",
        f"- Previous month period: {summary['previous_month_period']}",
        f"- Previous month source: {summary['previous_month_source']}",
        f"- Rows ingested: {summary['rows_ingested']}",
        f"- Merchant count: {summary['merchant_count']}",
        f"- Segment count: {summary['segment_count']}",
        f"- Total MTD TPV: {round(summary['total_mtd_tpv'], 2)}",
        f"- Total previous month TPV: {round(summary['total_previous_month_tpv'], 2)}",
        "",
        "## Saved Data Files",
        "",
        f"- Arranged transactions: {summary['arranged_file']}",
        f"- Metrics CSV: {summary['metrics_file']}",
        f"- Metrics JSON: {summary['summary_file']}",
        "",
        "## Top High Growth Segments",
        "",
        _markdown_table(summary["top_high_growth_segments"], segment_columns),
        "",
        "## Top Underperforming Segments",
        "",
        _markdown_table(summary["top_underperforming_segments"], segment_columns),
        "",
        "## Top New Segments",
        "",
        _markdown_table(summary["top_new_segments"], segment_columns),
        "",
        "## Follow-up Usage Notes",
        "",
        "Use this report as the primary reference for follow-up questions. "
        "Use the archived input file only when recalculation is required.",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _load_saved_markdown_references(limit: int = 6, max_characters: int = 24000) -> str:
    files = []
    for folder in [OUTPUT_DIR, UPLOAD_DIR]:
        if folder.exists():
            files.extend(path for path in folder.glob("*.md") if path.is_file())
    files = sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[: max(1, limit)]

    blocks: List[str] = []
    remaining = max_characters
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if remaining <= 0:
            break
        snippet = text[:remaining]
        remaining -= len(snippet)
        blocks.append(f"Reference markdown file: {path}\n{snippet}")

    if not blocks:
        return "No saved markdown references found yet."
    return "\n\n---\n\n".join(blocks)


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in FALSEY_ENV_VALUES


def _strip_html(value: str) -> str:
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _normalize_search_url(href: str) -> str:
    href = unescape(href or "").strip()
    if href.startswith("//"):
        href = f"https:{href}"
    elif href.startswith("/"):
        href = f"https://duckduckgo.com{href}"

    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    redirected = query.get("uddg")
    if redirected:
        return unquote(redirected[0])
    return href


def _public_web_search(query: str, max_results: int = 4) -> List[Dict[str, str]]:
    query = query.strip()
    if not query:
        return []

    timeout = float(os.getenv("WEB_RESEARCH_TIMEOUT_SECONDS", "12"))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        )
    }
    endpoints = ["https://duckduckgo.com/html/", "https://html.duckduckgo.com/html/"]

    last_error: Optional[Exception] = None
    for endpoint in endpoints:
        try:
            response = requests.get(endpoint, params={"q": query}, headers=headers, timeout=timeout)
            response.raise_for_status()
            html = response.text
            break
        except requests.RequestException as exc:
            last_error = exc
    else:
        raise RuntimeError(f"Public web search failed for query '{query}': {last_error}")

    anchor_pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>'
        r"(?P<title>.*?)</a>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    anchors = list(anchor_pattern.finditer(html))
    results: List[Dict[str, str]] = []
    seen_urls: set[str] = set()

    for index, match in enumerate(anchors):
        title = _strip_html(match.group("title"))
        url = _normalize_search_url(match.group("href"))
        if not title or not url or url in seen_urls:
            continue
        seen_urls.add(url)

        next_start = anchors[index + 1].start() if index + 1 < len(anchors) else len(html)
        result_block = html[match.end() : next_start]
        snippet_match = re.search(
            r'<[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</',
            result_block,
            flags=re.DOTALL | re.IGNORECASE,
        )
        snippet = _strip_html(snippet_match.group("snippet")) if snippet_match else ""
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max(1, int(max_results)):
            break

    return results


def _should_run_web_research(user_message: str, summaries: Optional[List[Dict[str, Any]]] = None) -> bool:
    if not _env_flag("WEB_RESEARCH_ENABLED", True):
        return False

    text = (user_message or "").lower()

    # Always run web research when user asks about a specific merchant by name
    merchant_inquiry_patterns = [
        re.compile(r"merchant\s+\w", re.IGNORECASE),
        re.compile(r"(thông tin|thong tin|information|info|nghiên cứu|nghien cuu|tìm hiểu|tim hieu)\s+(về|ve|about|on)\s+\w", re.IGNORECASE),
        re.compile(r"(về|ve|about)\s+(merchant|thương hiệu|thuong hieu|chuỗi|chuoi|brand)\s+\w", re.IGNORECASE),
        re.compile(r"cho\s+tôi\s+biết\s+.*(merchant|thương hiệu|chuỗi)", re.IGNORECASE),
        re.compile(r"(tell|show|give)\s+me\s+.*(merchant|about)", re.IGNORECASE),
    ]
    if any(p.search(user_message or "") for p in merchant_inquiry_patterns):
        return True

    trigger_terms = [
        "why", "reason", "root cause", "research", "search", "web", "social",
        "facebook", "threads", "competitor", "growth", "increase", "paid",
        "organic", "qr", "vietqr", "voucher", "campaign", "drop", "down",
        "decrease", "tăng", "tang", "tại sao", "tai sao", "nguyên nhân",
        "nguyen nhan", "lý do", "ly do", "nghiên cứu", "nghien cuu",
        "tìm hiểu", "tim hieu", "tìm kiếm", "tim kiem", "giảm", "giam",
        "tụt", "tut", "phốt", "phot", "đối thủ", "doi thu",
        "chiến dịch", "chien dich", "khuyến mãi", "khuyen mai",
        "thông tin", "thong tin", "cho biết", "cho biet",
    ]
    if any(term in text for term in trigger_terms):
        return True

    for summary in summaries or []:
        if (
            summary.get("top_underperforming_segments")
            or summary.get("top_high_growth_segments")
            or summary.get("top_new_segments")
        ):
            return True
    return False


def _research_section_priority(user_message: str) -> List[Tuple[str, str]]:
    text = (user_message or "").lower()
    growth_terms = ["growth", "up", "increase", "tăng", "tang", "cao", "high growth"]
    new_terms = ["new", "mới", "moi", "new segment"]
    drop_terms = ["drop", "down", "decrease", "giảm", "giam", "tụt", "tut", "underperform"]

    if any(term in text for term in new_terms):
        return [
            ("top_new_segments", "New segment diagnosis"),
            ("top_high_growth_segments", "Growth-up diagnosis"),
            ("top_underperforming_segments", "Decline diagnosis"),
        ]
    if any(term in text for term in growth_terms):
        return [
            ("top_high_growth_segments", "Growth-up diagnosis"),
            ("top_new_segments", "New segment diagnosis"),
            ("top_underperforming_segments", "Decline diagnosis"),
        ]
    if any(term in text for term in drop_terms):
        return [
            ("top_underperforming_segments", "Decline diagnosis"),
            ("top_high_growth_segments", "Growth-up diagnosis"),
            ("top_new_segments", "New segment diagnosis"),
        ]
    return [
        ("top_underperforming_segments", "Decline diagnosis"),
        ("top_high_growth_segments", "Growth-up diagnosis"),
        ("top_new_segments", "New segment diagnosis"),
    ]


def _research_targets_from_summaries(
    summaries: List[Dict[str, Any]],
    user_message: str = "",
    max_targets: int = 2,
) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    section_priority = _research_section_priority(user_message)
    for summary in summaries:
        for section_name, scenario in section_priority:
            for segment in summary.get(section_name, []):
                merchant = str(segment.get("Merchant", "")).strip()
                sof_type = str(segment.get("SOF_Type", "")).strip()
                acq_type = str(segment.get("Acq_Type", "")).strip()
                key = (merchant.lower(), sof_type.lower(), acq_type.lower())
                if not merchant or key in seen:
                    continue
                seen.add(key)
                targets.append(
                    {
                        "merchant": merchant,
                        "sof_type": sof_type,
                        "acq_type": acq_type,
                        "scenario": scenario,
                        "mom_growth": segment.get("MoM_Growth_%"),
                        "mtd_tpv": segment.get("MTD_TPV"),
                        "prev_month_tpv": segment.get("Prev_Month_TPV"),
                    }
                )
                if len(targets) >= max(1, int(max_targets)):
                    return targets
    return targets


def _extract_merchant_target_from_message(user_message: str) -> Optional[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", user_message or "").strip()
    if not text:
        return None

    patterns = [
        r"(?:merchant|merchent)\s+(?P<merchant>[^\n,;]+)",
        r"(?:thông tin|thong tin|information|info)\s+(?:về|ve|about|on)\s+(?:merchant\s+)?(?P<merchant>[^\n,;]+)",
        r"(?:nghiên cứu|nghien cuu|tìm kiếm|tim kiem|tìm hiểu|tim hieu|research|search)\s+(?:merchant\s+)?(?P<merchant>[^\n,;]+)",
        r"(?:cho\s+(?:tôi|toi|me)\s+biết|tell\s+me|show\s+me)\s+.{0,30}?(?:merchant\s+|về\s+|ve\s+|about\s+)(?P<merchant>[^\n,;]+)",
        r"(?:về|ve|about)\s+(?P<merchant>[^\n,;]+)",
    ]
    stop_pattern = re.compile(
        r"\b(?:ở|o|kênh|kenh|channel|sof|drop|giảm|giam|tăng|tang|vì|vi|do|tại sao|tai sao)\b",
        flags=re.IGNORECASE,
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        merchant = match.group("merchant").strip(" .,:;!?\"'")
        stop_match = stop_pattern.search(merchant)
        if stop_match:
            merchant = merchant[: stop_match.start()].strip(" .,:;!?\"'")
        if len(merchant) >= 2:
            return {
                "merchant": merchant,
                "sof_type": "",
                "acq_type": "",
                "scenario": "Public merchant research",
                "mom_growth": None,
                "mtd_tpv": None,
                "prev_month_tpv": None,
            }
    return None


def _latest_metric_summaries(limit: int = 2) -> List[Dict[str, Any]]:
    if not OUTPUT_DIR.exists():
        return []

    summaries: List[Dict[str, Any]] = []
    paths = sorted(OUTPUT_DIR.glob("*_summary_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in paths[: max(1, int(limit))]:
        try:
            summaries.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return summaries


def _append_unique(values: List[str], value: str, limit: int) -> None:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if cleaned and cleaned not in values and len(values) < limit:
        values.append(cleaned)


def _build_public_research_queries(
    target: Optional[Dict[str, Any]],
    user_message: str,
    max_queries: int = 6,
) -> List[str]:
    queries: List[str] = []
    max_queries = max(1, int(max_queries))
    merchant = str((target or {}).get("merchant", "")).strip()
    sof_type = str((target or {}).get("sof_type", "")).strip()
    acq_type = str((target or {}).get("acq_type", "")).strip()
    scenario = str((target or {}).get("scenario", "")).strip().lower()
    message = re.sub(r"\s+", " ", user_message or "").strip()

    if merchant:
        quoted_merchant = f'"{merchant}"'
        acq_lower = acq_type.lower()
        sof_lower = sof_type.lower()

        if sof_type:
            _append_unique(queries, f"{quoted_merchant} {sof_type} thanh toán", max_queries)
        _append_unique(queries, f"{quoted_merchant} ZaloPay thanh toán", max_queries)
        _append_unique(queries, f'site:facebook.com "{merchant}" thanh toán', max_queries)
        _append_unique(queries, f'site:threads.net "{merchant}"', max_queries)
        _append_unique(queries, f'site:tiktok.com "{merchant}"', max_queries)

        if acq_lower == "paid":
            _append_unique(queries, f"{quoted_merchant} voucher khuyến mãi thanh toán", max_queries)
            _append_unique(queries, f"{quoted_merchant} chiến dịch khuyến mãi", max_queries)
            _append_unique(queries, f"{quoted_merchant} quảng cáo ưu đãi thanh toán", max_queries)
        elif acq_lower == "organic":
            _append_unique(queries, f"{quoted_merchant} đối thủ thanh toán", max_queries)
            _append_unique(queries, f"{quoted_merchant} cộng đồng đánh giá", max_queries)

        if any(term in sof_lower for term in ["bnpl", "buy now", "pay later", "paylater", "trả góp", "tra gop"]):
            _append_unique(queries, f"{quoted_merchant} trả góp", max_queries)
            _append_unique(queries, f"{quoted_merchant} Buy Now Pay Later", max_queries)
            _append_unique(queries, f"{quoted_merchant} BNPL", max_queries)
        if any(term in sof_lower for term in ["qr", "vietqr"]):
            _append_unique(queries, f"{quoted_merchant} VietQR", max_queries)
            _append_unique(queries, f"{quoted_merchant} QR thanh toán lỗi", max_queries)
            _append_unique(queries, f"{quoted_merchant} cửa hàng thanh toán QR", max_queries)

        if "growth" in scenario or "new segment" in scenario:
            _append_unique(queries, f"{quoted_merchant} tăng trưởng khuyến mãi", max_queries)
            _append_unique(queries, f"{quoted_merchant} campaign thanh toán", max_queries)
            _append_unique(queries, f"{quoted_merchant} ra mắt dịch vụ mới", max_queries)

        _append_unique(queries, f"{quoted_merchant} thanh toán", max_queries)
        _append_unique(queries, f"{quoted_merchant} khuyến mãi voucher", max_queries)
        _append_unique(queries, f"{quoted_merchant} phốt khiếu nại", max_queries)
        _append_unique(queries, f"{quoted_merchant} lỗi thanh toán", max_queries)

    if not queries and message:
        compact_message = message[:160]
        _append_unique(queries, compact_message, max_queries)
        _append_unique(queries, f"{compact_message} Facebook", max_queries)
        _append_unique(queries, f"{compact_message} Threads", max_queries)
        _append_unique(queries, f"{compact_message} tin tức", max_queries)

    return queries


def _format_research_target(target: Optional[Dict[str, Any]]) -> str:
    if not target:
        return "User question"
    scenario = str(target.get("scenario", "")).strip()
    parts = [
        str(target.get("merchant", "")).strip(),
        str(target.get("sof_type", "")).strip(),
        str(target.get("acq_type", "")).strip(),
    ]
    label = " / ".join(part for part in parts if part)
    if scenario and label:
        label = f"{label} [{scenario}]"
    growth = target.get("mom_growth")
    if growth is not None:
        label = f"{label} (MoM growth: {growth}%)"
    return label or "User question"


def _write_public_research_markdown(
    *,
    timestamp: str,
    targets: List[Optional[Dict[str, Any]]],
    query_results: List[Dict[str, Any]],
) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"{timestamp}_public_web_research_report.md"

    lines = [
        "# Public Web Research Report",
        "",
        f"- Created at: {timestamp}",
        "- Scope: public web pages and social posts discoverable by a search engine.",
        "- Note: this does not access private, logged-in, or non-indexed Facebook/Threads content.",
        "",
        "## Research Targets",
        "",
    ]
    if targets:
        for target in targets:
            lines.append(f"- {_format_research_target(target)}")
    else:
        lines.append("- User question")

    for item in query_results:
        target_label = _format_research_target(item.get("target"))
        lines.extend(["", f"## Query: {item['query']}", "", f"- Target: {target_label}"])
        if item.get("error"):
            lines.append(f"- Search status: {item['error']}")
            continue
        results = item.get("results", [])
        if not results:
            lines.append("- No public search results found.")
            continue
        for result in results:
            lines.append(f"- [{result['title']}]({result['url']})")
            if result.get("snippet"):
                lines.append(f"  - Snippet: {result['snippet']}")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _resolve_merchant_website(merchant: str, timeout: float = 10.0) -> Optional[str]:
    """Search DuckDuckGo to find the official website URL of a merchant."""
    query = f'"{merchant}" trang web chính thức site'
    try:
        results = _public_web_search(query, max_results=3)
    except Exception:
        return None

    blocked_domains = {
        "facebook.com", "threads.net", "tiktok.com", "youtube.com",
        "twitter.com", "instagram.com", "zalo.me", "zalopay.vn",
        "duckduckgo.com", "google.com", "wikipedia.org",
    }
    merchant_slug = re.sub(r"[^a-z0-9]", "", merchant.lower())
    for result in results:
        url = result.get("url", "")
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower().lstrip("www.")
            if any(blocked in domain for blocked in blocked_domains):
                continue
            domain_slug = re.sub(r"[^a-z0-9]", "", domain)
            if merchant_slug[:4] in domain_slug or domain_slug[:4] in merchant_slug:
                return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            continue
    return None


def _fetch_page_text(url: str, timeout: float = 10.0, max_chars: int = 8000) -> str:
    """Fetch a webpage and return cleaned plain text."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        return _strip_html(response.text)[:max_chars]
    except Exception:
        return ""


_BNPL_KEYWORDS = [
    "trả góp", "tra gop", "installment", "buy now pay later", "bnpl", "pay later",
    "paylater", "trả chậm", "tra cham", "góp 0%", "gop 0%", "lãi suất 0",
    "kredivo", "home credit", "mcredit", "fia credit", "aeon", "shinhan",
    "trả góp 0%", "mua trả góp",
]
_COMPETING_PAYMENT_KEYWORDS = [
    "momo", "mo mo", "shopee pay", "shopeepay", "vnpay", "vn pay",
    "grab pay", "grabpay", "moca", "airpay", "onepay", "payoo",
    "napas", "viettel pay", "viettelpay",
]
_SCANDAL_KEYWORDS = [
    "phốt", "phot", "lừa đảo", "lua dao", "khiếu nại", "khieu nai",
    "tố cáo", "to cao", "bóc phốt", "boc phot", "scam", "gian lận",
    "gian lan", "chặt chém", "chat chem", "thất vọng", "that vong",
]

_PAYMENT_SUBPATHS = [
    "/thanh-toan", "/phuong-thuc-thanh-toan", "/tra-gop",
    "/payment", "/checkout", "/installment",
]


def _detect_competing_services(text: str) -> Dict[str, List[str]]:
    """Scan page text for competing BNPL/payment keywords and return findings by category."""
    text_lower = text.lower()
    findings: Dict[str, List[str]] = {"bnpl_competitors": [], "payment_competitors": [], "scandal_signals": []}
    for kw in _BNPL_KEYWORDS:
        if kw in text_lower and kw not in findings["bnpl_competitors"]:
            findings["bnpl_competitors"].append(kw)
    for kw in _COMPETING_PAYMENT_KEYWORDS:
        if kw in text_lower and kw not in findings["payment_competitors"]:
            findings["payment_competitors"].append(kw)
    for kw in _SCANDAL_KEYWORDS:
        if kw in text_lower and kw not in findings["scandal_signals"]:
            findings["scandal_signals"].append(kw)
    return findings


def _crawl_merchant_website(merchant: str, sof_type: str = "") -> str:
    """Crawl the merchant's official website to detect competing payment/BNPL services.

    Returns a markdown-formatted summary of findings, or empty string if nothing found.
    """
    if not _env_flag("MERCHANT_WEBSITE_CRAWL_ENABLED", True):
        return ""

    timeout = float(os.getenv("WEB_RESEARCH_TIMEOUT_SECONDS", "12"))
    base_url = _resolve_merchant_website(merchant, timeout=timeout)
    if not base_url:
        return ""

    pages_to_check = [base_url]
    sof_lower = sof_type.lower()
    if any(t in sof_lower for t in ["bnpl", "buy now", "pay later", "paylater", "trả góp", "tra gop"]):
        for subpath in _PAYMENT_SUBPATHS:
            pages_to_check.append(base_url.rstrip("/") + subpath)
    else:
        pages_to_check.append(base_url.rstrip("/") + "/payment")
        pages_to_check.append(base_url.rstrip("/") + "/thanh-toan")

    all_findings: Dict[str, List[str]] = {"bnpl_competitors": [], "payment_competitors": [], "scandal_signals": []}
    crawled_urls: List[str] = []

    for url in pages_to_check[:4]:
        text = _fetch_page_text(url, timeout=timeout)
        if not text:
            continue
        crawled_urls.append(url)
        page_findings = _detect_competing_services(text)
        for category, keywords in page_findings.items():
            for kw in keywords:
                if kw not in all_findings[category]:
                    all_findings[category].append(kw)

    has_findings = any(all_findings[cat] for cat in all_findings)
    if not has_findings and not crawled_urls:
        return ""

    lines = [
        f"## Merchant Website Analysis: {merchant}",
        f"- Official website detected: {base_url}",
        f"- Pages crawled: {', '.join(crawled_urls) if crawled_urls else 'None reachable'}",
        "",
    ]
    if all_findings["bnpl_competitors"]:
        lines.append(
            "**BNPL / Installment services found on merchant site** "
            f"(potential reason for ZaloPay BNPL drop): {', '.join(all_findings['bnpl_competitors'])}"
        )
    if all_findings["payment_competitors"]:
        lines.append(
            "**Competing payment methods found on merchant site**: "
            + ", ".join(all_findings["payment_competitors"])
        )
    if all_findings["scandal_signals"]:
        lines.append(
            "**Scandal/complaint signals detected on merchant site**: "
            + ", ".join(all_findings["scandal_signals"])
        )
    if not has_findings:
        lines.append("No competing payment services or scandal signals detected on the merchant's website.")

    return "\n".join(lines) + "\n"


def _run_public_web_research(user_message: str, summaries: Optional[List[Dict[str, Any]]] = None) -> str:
    summaries = summaries or []
    if not _should_run_web_research(user_message, summaries):
        return ""

    max_targets = int(os.getenv("WEB_RESEARCH_MAX_TARGETS", "2"))
    max_queries = int(os.getenv("WEB_RESEARCH_MAX_QUERIES_PER_TARGET", "10"))
    max_results = int(os.getenv("WEB_RESEARCH_MAX_RESULTS", "4"))
    # If user explicitly names a merchant, always prioritize researching that merchant first
    explicit_merchant_target = _extract_merchant_target_from_message(user_message)
    if explicit_merchant_target:
        targets: List[Optional[Dict[str, Any]]] = [explicit_merchant_target]
    else:
        targets = _research_targets_from_summaries(
            summaries,
            user_message=user_message,
            max_targets=max_targets,
        )
        if not targets:
            targets = [None]

    query_results: List[Dict[str, Any]] = []
    website_analysis_blocks: List[str] = []

    for target in targets:
        merchant = str((target or {}).get("merchant", "")).strip()
        sof_type = str((target or {}).get("sof_type", "")).strip()

        if merchant:
            website_block = _crawl_merchant_website(merchant, sof_type=sof_type)
            if website_block:
                website_analysis_blocks.append(website_block)

        queries = _build_public_research_queries(target, user_message, max_queries=max_queries)
        for query in queries:
            item: Dict[str, Any] = {"target": target, "query": query, "results": []}
            try:
                item["results"] = _public_web_search(query, max_results=max_results)
            except Exception as exc:
                item["error"] = str(exc)
            query_results.append(item)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = _write_public_research_markdown(
        timestamp=timestamp,
        targets=targets,
        query_results=query_results,
    )
    report_text = report_path.read_text(encoding="utf-8", errors="ignore")

    output_parts = ["Public web research context. Use it as supporting evidence only, with citations and confidence levels:\n", report_text]
    if website_analysis_blocks:
        output_parts.append(
            "\n\nMerchant website crawl findings (direct evidence from merchant's own site — "
            "high confidence if keywords found):\n"
            + "\n\n".join(website_analysis_blocks)
        )
    return "\n".join(output_parts) + "\n\n"


def _read_transactions(file_path: str) -> pd.DataFrame:
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Uploaded file was not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path)
    elif suffix in {".xlsx", ".xlsm"}:
        frame = _read_excel_transactions(path)
    elif suffix == ".xls":
        frame = _read_excel_transactions(path)
    elif suffix == ".json":
        frame = pd.read_json(path)
    else:
        raise ValueError("Upload a CSV, XLSX, XLSM, XLS, or JSON transaction file.")

    mapping = _canonical_column_map([str(column) for column in frame.columns])
    frame = frame.rename(columns=mapping)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(
            "Missing required columns: "
            + ", ".join(missing)
            + ". Expected Date, Merchant, SOF_Type, Acq_Type, TPV."
        )

    frame = frame[REQUIRED_COLUMNS].copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame["TPV"] = pd.to_numeric(
        frame["TPV"].astype(str).str.replace(r"[^\d.-]", "", regex=True),
        errors="coerce",
    )
    for column in ["Merchant", "SOF_Type", "Acq_Type"]:
        frame[column] = frame[column].astype(str).str.strip()

    frame = frame.dropna(subset=["Date", "Merchant", "SOF_Type", "Acq_Type", "TPV"])
    frame = frame[frame["TPV"] >= 0]
    if frame.empty:
        raise ValueError("No valid transaction rows remained after cleaning the file.")

    if "source_sheet" not in frame.attrs:
        frame.attrs["source_sheet"] = "file"
    return frame.sort_values(["Date", "Merchant", "SOF_Type", "Acq_Type"]).reset_index(drop=True)


def _period_bounds(frame: pd.DataFrame, as_of_date: str) -> Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    if as_of_date:
        as_of = pd.to_datetime(as_of_date, errors="raise")
    else:
        as_of = frame["Date"].max()

    month_start = pd.Timestamp(year=as_of.year, month=as_of.month, day=1)
    previous_month_end = month_start - pd.Timedelta(days=1)
    previous_month_start = pd.Timestamp(
        year=previous_month_end.year,
        month=previous_month_end.month,
        day=1,
    )
    return as_of.normalize(), month_start, previous_month_start, previous_month_end


def _growth_status(mtd_tpv: float, previous_tpv: float, growth_pct: Optional[float]) -> str:
    if previous_tpv == 0 and mtd_tpv > 0:
        return "New segment"
    if previous_tpv > 0 and mtd_tpv == 0:
        return "Dropped"
    if growth_pct is None:
        return "No comparable volume"
    if growth_pct >= 20:
        return "High growth"
    if growth_pct >= 0:
        return "Stable growth"
    return "Underperforming"


def calculate_merchant_metrics(file_path: str, as_of_date: str = "") -> str:
    """Arrange uploaded merchant transactions, calculate metrics, and save output files.

    Args:
        file_path: Absolute path of the uploaded transaction file.
        as_of_date: Optional analysis date in YYYY-MM-DD format. If blank, uses the latest
            transaction date in the uploaded file.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    original_source_path = Path(file_path).expanduser().resolve()
    source_path = _archive_input_file(original_source_path, timestamp)
    frame = _read_transactions(str(source_path))
    source_sheet = frame.attrs.get("source_sheet", "file")
    input_summary_path = _write_input_summary_markdown(
        original_path=original_source_path,
        archived_path=source_path,
        frame=frame,
        source_sheet=source_sheet,
        timestamp=timestamp,
    )
    as_of, month_start, prev_start, prev_end = _period_bounds(frame, as_of_date)

    arranged = frame.copy()
    arranged["Date"] = arranged["Date"].dt.strftime("%Y-%m-%d")
    arranged = arranged.rename(columns={"Acq_Type": "Acquisition_Type"})

    mtd_frame = frame[(frame["Date"] >= month_start) & (frame["Date"] <= as_of)]
    prev_frame = frame[(frame["Date"] >= prev_start) & (frame["Date"] <= prev_end)]

    mtd = (
        mtd_frame.groupby(GROUP_COLUMNS, dropna=False)["TPV"]
        .sum()
        .reset_index()
        .rename(columns={"TPV": "MTD_TPV"})
    )
    previous = (
        prev_frame.groupby(GROUP_COLUMNS, dropna=False)["TPV"]
        .sum()
        .reset_index()
        .rename(columns={"TPV": "Prev_Month_TPV"})
    )
    reference_previous, previous_month_source = _read_reference_previous_month(source_path)
    if reference_previous is not None:
        if previous.empty:
            previous = reference_previous
        else:
            previous = previous.merge(
                reference_previous,
                on=GROUP_COLUMNS,
                how="outer",
                suffixes=("_from_transactions", "_from_reference"),
            )
            previous["Prev_Month_TPV"] = previous["Prev_Month_TPV_from_reference"].combine_first(
                previous["Prev_Month_TPV_from_transactions"]
            )
            previous = previous[GROUP_COLUMNS + ["Prev_Month_TPV"]]
    metrics = mtd.merge(previous, on=GROUP_COLUMNS, how="outer").fillna(0)

    if metrics.empty:
        metrics = pd.DataFrame(columns=GROUP_COLUMNS + ["MTD_TPV", "Prev_Month_TPV"])

    metrics["MTD_TPV"] = metrics["MTD_TPV"].astype(float)
    metrics["Prev_Month_TPV"] = metrics["Prev_Month_TPV"].astype(float)
    metrics["MoM_Growth_%"] = metrics.apply(
        lambda row: None
        if row["Prev_Month_TPV"] == 0
        else round(((row["MTD_TPV"] - row["Prev_Month_TPV"]) / row["Prev_Month_TPV"]) * 100, 2),
        axis=1,
    )
    metrics["MoM_Status"] = metrics.apply(
        lambda row: _growth_status(row["MTD_TPV"], row["Prev_Month_TPV"], row["MoM_Growth_%"]),
        axis=1,
    )
    metrics["As_Of_Date"] = as_of.strftime("%Y-%m-%d")
    metrics["MTD_Period"] = f"{month_start:%Y-%m-%d} to {as_of:%Y-%m-%d}"
    metrics["Prev_Month_Period"] = f"{prev_start:%Y-%m-%d} to {prev_end:%Y-%m-%d}"
    metrics = metrics.sort_values(["MTD_TPV", "Prev_Month_TPV"], ascending=False).reset_index(drop=True)

    stem = _slug(original_source_path.stem)
    arranged_path = OUTPUT_DIR / f"{stem}_arranged_{timestamp}.csv"
    metrics_path = OUTPUT_DIR / f"{stem}_metrics_{timestamp}.csv"
    summary_path = OUTPUT_DIR / f"{stem}_summary_{timestamp}.json"

    arranged.to_csv(arranged_path, index=False)
    metrics.to_csv(metrics_path, index=False)

    comparable = metrics[metrics["Prev_Month_TPV"] > 0].copy()
    high_growth = comparable[comparable["MoM_Growth_%"] > 0].sort_values(
        "MoM_Growth_%",
        ascending=False,
    ).head(5)
    underperforming = comparable[comparable["MoM_Growth_%"] < 0].sort_values(
        "MoM_Growth_%",
        ascending=True,
    ).head(5)
    new_segments = metrics[metrics["MoM_Status"] == "New segment"].sort_values(
        "MTD_TPV",
        ascending=False,
    ).head(5)

    summary: Dict[str, Any] = {
        "source_file": str(original_source_path),
        "archived_input_file": str(source_path),
        "input_summary_file": str(input_summary_path),
        "source_sheet": source_sheet,
        "arranged_file": str(arranged_path),
        "metrics_file": str(metrics_path),
        "summary_file": str(summary_path),
        "previous_month_source": previous_month_source,
        "as_of_date": as_of.strftime("%Y-%m-%d"),
        "mtd_period": f"{month_start:%Y-%m-%d} to {as_of:%Y-%m-%d}",
        "previous_month_period": f"{prev_start:%Y-%m-%d} to {prev_end:%Y-%m-%d}",
        "rows_ingested": int(len(frame)),
        "merchant_count": int(frame["Merchant"].nunique()),
        "segment_count": int(len(metrics)),
        "total_mtd_tpv": float(metrics["MTD_TPV"].sum()),
        "total_previous_month_tpv": float(metrics["Prev_Month_TPV"].sum()),
        "top_high_growth_segments": high_growth.to_dict(orient="records"),
        "top_underperforming_segments": underperforming.to_dict(orient="records"),
        "top_new_segments": new_segments.to_dict(orient="records"),
    }
    markdown_report_path = _write_metrics_markdown_report(
        summary=summary,
        timestamp=timestamp,
        stem=stem,
    )
    summary["markdown_report_file"] = str(markdown_report_path)

    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)

    return json.dumps(summary, indent=2, ensure_ascii=False)


def _safe_output_path(file_path: str) -> Path:
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = OUTPUT_DIR / candidate
    candidate = candidate.resolve()
    output_root = OUTPUT_DIR.resolve()
    if output_root not in candidate.parents and candidate != output_root:
        raise ValueError("Only files inside the output folder can be read.")
    if not candidate.exists():
        raise FileNotFoundError(f"Output file was not found: {candidate}")
    return candidate


def search_output_files(query: str = "", limit: int = 8) -> str:
    """Search generated output files by filename and small content snippets.

    Args:
        query: Optional text to search for in output filenames and file contents.
        limit: Maximum number of matching files to return.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    query_lower = query.lower().strip()
    matches: List[Dict[str, Any]] = []
    for path in sorted(OUTPUT_DIR.glob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_file():
            continue
        text = ""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError:
            text = ""

        haystack = f"{path.name}\n{text}".lower()
        if query_lower and query_lower not in haystack:
            continue
        matches.append(
            {
                "file": str(path),
                "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "snippet": text[:600],
            }
        )
        if len(matches) >= max(1, int(limit)):
            break

    if not matches:
        return "No matching output files found."
    return json.dumps(matches, indent=2, ensure_ascii=False)


def read_output_file(file_path: str, max_characters: int = 12000) -> str:
    """Read one generated output file from the output folder.

    Args:
        file_path: Output file path or filename.
        max_characters: Maximum number of characters to return.
    """
    path = _safe_output_path(file_path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    return text[: max(1000, int(max_characters))]


def write_agent1_log(
    content: str,
    merchants: Optional[List[str]] = None,
    task_type: str = "analysis",
) -> str:
    """Write Agent 1 findings to a timestamped markdown log in the output folder.

    The log accumulates over time and serves as a historical knowledge base for
    Agent 2 to answer past-period questions (e.g. TPV trends, previous diagnoses).

    Args:
        content: Markdown content of the findings/analysis.
        merchants: List of merchant names involved (used in filename for easy search).
        task_type: Short label for the task: 'metrics', 'web_research', 'analysis', 'crawl'.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    merchant_slug = "_".join(_slug(m) for m in (merchants or []))[:40] or "general"
    filename = f"agent1_log_{timestamp}_{task_type}_{merchant_slug}.md"
    log_path = OUTPUT_DIR / filename

    header = (
        f"# Agent 1 Log — {task_type.title()}\n\n"
        f"- **Timestamp**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- **Task type**: {task_type}\n"
        f"- **Merchants**: {', '.join(merchants) if merchants else 'N/A'}\n\n"
        "---\n\n"
    )
    log_path.write_text(header + content, encoding="utf-8")

    # Auto-index vào ChromaDB ngay sau khi ghi
    if _RAG_AVAILABLE:
        try:
            rag.index_file(log_path)
        except Exception:
            pass

    return str(log_path)


def export_analysis_pdf(analysis_text: str, title: str = "Merchant Analytics Report") -> str:
    """Generate a PDF report from the analysis text and return the file path.

    Args:
        analysis_text: The full markdown/text analysis to include in the PDF.
        title: Report title shown at the top of the PDF.
    """
    from fpdf import FPDF

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_path = OUTPUT_DIR / f"merchant_analytics_report_{timestamp}.pdf"

    pdf = FPDF()
    pdf.set_margins(left=15, top=15, right=15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Load a Unicode TTF font that supports Vietnamese.
    # Priority: bundled project font → fpdf2 bundled → macOS system fonts.
    font_name = "Helvetica"
    _bundled = BASE_DIR / "static" / "fonts"
    _font_candidates = [
        (_bundled / "Arial.ttf", _bundled / "Arial-Bold.ttf"),
        (Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
         Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")),
        (Path("/Library/Fonts/Arial Unicode.ttf"),
         Path("/Library/Fonts/Arial Unicode.ttf")),
    ]
    for regular, bold in _font_candidates:
        try:
            if regular.exists():
                pdf.add_font("Uni", "", str(regular))
                pdf.add_font("Uni", "B", str(bold if bold.exists() else regular))
                font_name = "Uni"
                break
        except Exception:
            continue

    unicode_font = font_name != "Helvetica"

    def _safe_text(text: str) -> str:
        if unicode_font:
            return text
        return text.encode("latin-1", errors="replace").decode("latin-1")

    # Title
    pdf.set_font(font_name, "B", 16)
    pdf.set_fill_color(0, 100, 200)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 12, _safe_text(title), ln=True, fill=True, align="C")
    pdf.ln(4)

    # Timestamp
    pdf.set_font(font_name, "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 6, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  ZaloPay Merchant Analytics", ln=True, align="C")
    pdf.ln(6)
    pdf.set_text_color(0, 0, 0)

    page_w = pdf.w - pdf.l_margin - pdf.r_margin

    def _strip_inline_md(text: str) -> str:
        # Bỏ dấu ** _ ` và # còn sót trong nội dung
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`(.+?)`", r"\1", text)
        text = text.replace("**", "").replace("`", "")
        return text

    def _write_line(text: str, font: str, style: str, size: int, h: int, fill: bool = False) -> None:
        pdf.set_font(font, style, size)
        pdf.multi_cell(page_w, h, _safe_text(_strip_inline_md(text)), fill=fill)

    # Body — parse markdown-ish headings and content
    for line in analysis_text.splitlines():
        stripped = line.strip()
        if not stripped:
            pdf.ln(3)
            continue

        # Bỏ dòng "# ..." (title trùng) và "---" (separator)
        if stripped.startswith("# ") or stripped == "---":
            continue

        if stripped.startswith("## "):
            pdf.set_fill_color(230, 240, 255)
            _write_line(stripped[3:], font_name, "B", 13, 8, fill=True)
            pdf.ln(2)
        elif stripped.startswith("### "):
            _write_line(stripped[4:], font_name, "B", 11, 7)
        elif stripped.startswith("**") and stripped.endswith("**"):
            _write_line(stripped.strip("*"), font_name, "B", 10, 6)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            _write_line("  • " + stripped[2:], font_name, "", 10, 6)
        elif stripped.startswith("|"):
            _write_line(stripped, font_name, "", 8, 5)
        else:
            _write_line(stripped, font_name, "", 10, 6)

    pdf.output(str(pdf_path))
    return str(pdf_path)


# ---------------------------------------------------------------------------
# GreenNode config
# ---------------------------------------------------------------------------

def _greennode_config() -> Tuple[str, str]:
    """Return (api_key, base_url) for GreenNode — shared by both agents."""
    load_dotenv(BASE_DIR / ".env")
    api_key = (
        os.getenv("AI_PLATFORM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("GREENNODE_API_KEY")
        or os.getenv("GREEENODE_API_KEY")
    )
    base_url = (
        os.getenv("OPENAI_BASE_URL")
        or os.getenv("GREENNODE_BASE_URL")
        or os.getenv("GREEENODE_BASE_URL")
        or "https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1"
    )
    if not api_key:
        raise RuntimeError("No API key found. Add GREEENODE_API_KEY to the .env file.")
    return api_key, base_url.rstrip("/")


def _make_client(model_name: str) -> OpenAIChatCompletionsModel:
    """Create an OpenAIChatCompletionsModel backed by the GreenNode endpoint."""
    api_key, base_url = _greennode_config()
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


# ---------------------------------------------------------------------------
# Simple-message detection (fast path — skip agents for greetings)
# ---------------------------------------------------------------------------

_SIMPLE_MESSAGE_PATTERN = re.compile(
    r"^(hi|hello|hey|xin chào|chào|alo|ok|okay|cảm ơn|cam on|thanks|thank you|"
    r"bắt đầu|bat dau|start|help|giúp|giup|hướng dẫn|huong dan|\?+|\.+)$",
    re.IGNORECASE,
)
_ANALYTICAL_TERMS = re.compile(
    r"merchant|tpv|mom|mtd|growth|drop|giảm|tăng|phân tích|phan tich|"
    r"kênh|channel|organic|paid|qr|bnpl|voucher|segment|báo cáo|bao cao|"
    r"thông tin|thong tin|tìm|tim|nghiên|nghien|tại sao|tai sao|nguyên nhân",
    re.IGNORECASE,
)


def _is_simple_message(text: str) -> bool:
    text = text.strip()
    if _SIMPLE_MESSAGE_PATTERN.match(text):
        return True
    if len(text) <= 20 and not _ANALYTICAL_TERMS.search(text):
        return True
    return False


# ---------------------------------------------------------------------------
# Agent 1 — Research Agent  (google/gemma-4-31b-it)
# Tools: calculate metrics, web search, website crawl
# ---------------------------------------------------------------------------

AGENT1_MODEL = os.getenv("AGENT1_MODEL", "google/gemma-4-31b-it")

AGENT1_INSTRUCTIONS = """You are a ZaloPay merchant analytics research agent (Agent 1).
Your job: gather data, compute metrics, and ALWAYS write findings to the persistent log.

## Workflow (follow in order)

1. **Gather data** using the available tools:
   - `calculate_merchant_metrics` — when a transaction file is provided
   - `web_search` — to find news, social posts, competitor activity
   - `crawl_merchant_website` — to detect competing BNPL/payment services on the merchant's site
   - `search_history_files` / `read_history_file` — to load past logs for trend comparison (e.g. TPV this month vs last month from logs)

2. **Compute TPV trends** when historical logs exist:
   - Load past agent1_log_*_metrics_* files to compare MTD_TPV across periods
   - Calculate MoM or period-over-period change from log data
   - Include trend direction and % change in your findings

3. **Write log** — ALWAYS call `write_log` at the end of EVERY task:
   - Include: metrics summary, TPV trend if available, web research findings, diagnosis, recommended actions
   - Set `task_type` to: 'metrics', 'web_research', 'crawl', 'tpv_trend', or 'analysis'
   - Set `merchants` to comma-separated names of merchants analyzed
   - This log becomes the historical knowledge base Agent 2 uses for future queries

4. **Return** a concise structured markdown report to Agent 2. Do not interact with the end user.

## Rules
- Never fabricate data. Only report what tools return.
- Always write the log — even for web research or crawl-only tasks.
- When user asks about historical TPV, search existing logs first before recalculating.
"""


@function_tool
def agent1_calculate_metrics(file_path: str, as_of_date: str = "") -> str:
    """Process an uploaded merchant transaction file: clean data, compute MTD TPV, Prev Month TPV, MoM growth, and save outputs.

    Args:
        file_path: Absolute path to the uploaded CSV or Excel file.
        as_of_date: Optional analysis date (YYYY-MM-DD). Defaults to the latest transaction date.
    """
    return calculate_merchant_metrics(file_path=file_path, as_of_date=as_of_date)


@function_tool
def agent1_web_search(query: str, max_results: int = 4) -> str:
    """Search DuckDuckGo for public information about a merchant, campaign, voucher, or market event.

    Args:
        query: Search query in Vietnamese or English.
        max_results: Maximum number of results to return (default 4).
    """
    try:
        results = _public_web_search(query=query, max_results=max_results)
        return json.dumps(results, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Search error: {exc}"


@function_tool
def agent1_crawl_merchant_website(merchant: str, sof_type: str = "") -> str:
    """Crawl the merchant's official website to detect competing BNPL/installment services or payment methods that may explain a ZaloPay TPV drop.

    Args:
        merchant: Merchant name (e.g. 'Thế Giới Di Động', 'Long Châu').
        sof_type: Source of funds type (e.g. 'BNPL', 'VietQR') to focus the crawl.
    """
    return _crawl_merchant_website(merchant=merchant, sof_type=sof_type) or "No competing services detected."


@function_tool
def agent1_write_log(content: str, merchants: str = "", task_type: str = "analysis") -> str:
    """Save findings and analysis to a persistent markdown log file for future reference.
    ALWAYS call this at the end of every task to build the historical knowledge base.

    Args:
        content: Full markdown content of the findings — metrics, diagnosis, web research results, TPV trends.
        merchants: Comma-separated merchant names involved (e.g. 'Long Chau,The Gioi Di Dong').
        task_type: Task label — one of: 'metrics', 'web_research', 'crawl', 'tpv_trend', 'analysis'.
    """
    merchant_list = [m.strip() for m in merchants.split(",") if m.strip()]
    path = write_agent1_log(content=content, merchants=merchant_list, task_type=task_type)
    return f"Log saved: {path}"


def _build_agent1() -> Agent:
    return Agent(
        name="Research Agent",
        instructions=AGENT1_INSTRUCTIONS,
        model=_make_client(AGENT1_MODEL),
        tools=[agent1_calculate_metrics, agent1_web_search, agent1_crawl_merchant_website, agent1_write_log],
        model_settings=ModelSettings(
            temperature=float(os.getenv("AGENT1_TEMPERATURE", "0.3")),
            max_tokens=int(os.getenv("AGENT1_MAX_TOKENS", "3000")),
        ),
    )


# ---------------------------------------------------------------------------
# Agent 2 — Chat Agent  (minimax/minimax-m2.5)
# Tools: read history directly from output files
# Handoff: to Agent 1 for new research/computation
# ---------------------------------------------------------------------------

AGENT2_MODEL = os.getenv("AGENT2_MODEL", "minimax/minimax-m2.5")

_PDF_FORMAT_INSTRUCTIONS = """
## Quy tắc định dạng khi xuất PDF (export_report_pdf)

Khi user yêu cầu xuất báo cáo PDF, gọi `export_report_pdf` với `analysis_text` tuân thủ:

**CẤU TRÚC BẮT BUỘC:**
```
## Executive Summary
<tổng MTD TPV, MoM growth toàn hàng, số merchant phân tích>

## Top High Growth Segments
<top 5 segment tăng mạnh nhất, kèm % growth>

## Top Underperforming Segments
<top 5 segment giảm, kèm chẩn đoán nguyên nhân theo scenario>

## New Segments
<segment mới xuất hiện tháng này nếu có>

## Recommended Actions
<hành động cụ thể theo từng merchant/channel>

## Web Research Findings
<bằng chứng từ internet nếu có, kèm confidence level>
```

**QUY TẮC FORMAT ĐỂ TRÁNH LỖI PDF:**
- Chỉ dùng: `##` (heading lớn), `###` (heading nhỏ), `- ` (bullet), `| ` (table), `**text**` (bold)
- KHÔNG dùng: emoji trong heading, ký tự đặc biệt Unicode như ₀₁₂→←↑↓✓✗ (gây lỗi font)
- KHÔNG dùng: HTML tags, code blocks (```), subscript/superscript Unicode
- Số liệu: dùng ký tự ASCII (%, +, -, >, <) — KHÔNG dùng ký tự toán học đặc biệt
- Tên merchant tiếng Việt: giữ nguyên dấu (DejaVu font hỗ trợ UTF-8)
- Mỗi section cách nhau bằng dòng trống
- Độ dài tối ưu: 500–2000 từ để PDF không quá dài
"""

AGENT2_INSTRUCTIONS = f"""{ANALYTICS_INSTRUCTIONS}

You are the user-facing ZaloPay merchant analytics assistant.

Workflow:
1. **Câu hỏi lịch sử** (TPV tháng trước, lần phân tích trước, trend theo thời gian...):
   - Dùng search_history_files tìm log `agent1_log_*` liên quan đến merchant/period đó
   - Dùng read_history_file đọc nội dung → trả lời trực tiếp, KHÔNG cần gọi Agent 1
   - Có thể so sánh nhiều log để tính trend TPV theo thời gian

2. **Cần tính toán mới** (file mới upload, web research, root-cause chưa có trong log):
   - Handoff sang Research Agent — Agent 1 sẽ tự ghi log sau khi hoàn thành

3. Tổng hợp findings và trả lời user bằng markdown rõ ràng.

4. **Yêu cầu xuất/tải PDF** (user nói "tóm tắt + tải PDF", "xuất báo cáo", "download PDF", "lưu lại file"...):
   - Soạn nội dung tóm tắt các ý chính theo cấu trúc PDF bên dưới
   - GỌI `export_report_pdf(analysis_text=<nội dung>, title=<tiêu đề>)` — bắt buộc gọi tool này
   - Sau khi tool trả về, báo user "Đã tạo báo cáo PDF, file đang được tải về" (KHÔNG nói path)

5. KHÔNG nhắc đến file path, CSV, JSON, output folder với user.
6. KHÔNG emit tool-call syntax trong câu trả lời cuối.

{_PDF_FORMAT_INSTRUCTIONS}

Data contract that the Research Agent follows:
{DATA_PREPARATION_INSTRUCTIONS}
"""

_NO_TOOL_CALL_INSTRUCTION = (
    "\nCRITICAL: Never output raw tool-call syntax (tool_name:, [TOOL_CALL], function_call, "
    "JSON arguments, or tool names) in your final answer to the user."
)


@function_tool
def search_history_files(query: str = "", limit: int = 6) -> str:
    """Search previously generated analytics reports and agent logs using semantic RAG search.
    Falls back to keyword search if RAG is unavailable.

    Args:
        query: Natural language query (merchant name, channel, period, issue type, etc.).
        limit: Maximum number of results to return (default 6).
    """
    # RAG semantic search
    if _RAG_AVAILABLE and query:
        try:
            chunks = rag.query(query, k=limit)
            if chunks:
                return "\n\n---\n\n".join(chunks)
        except Exception:
            pass
    # Fallback: keyword search trên file system
    return search_output_files(query=query, limit=limit)


@function_tool
def read_history_file(file_path: str, max_characters: int = 12000) -> str:
    """Read the content of a previously generated metrics or report file.

    Args:
        file_path: Path or filename of the output file to read.
        max_characters: Maximum characters to return (default 12000).
    """
    return read_output_file(file_path=file_path, max_characters=max_characters)


@function_tool
def export_report_pdf(analysis_text: str, title: str = "Merchant Analytics Report") -> str:
    """Export the current analysis as a PDF report and return the file path for download.
    Call this when the user asks to export or download a report.

    Args:
        analysis_text: The full analysis text to include in the PDF.
        title: Report title (default: 'Merchant Analytics Report').
    """
    return export_analysis_pdf(analysis_text=analysis_text, title=title)


def _build_agent2(agent1: Agent) -> Agent:
    return Agent(
        name="Analytics Assistant",
        instructions=AGENT2_INSTRUCTIONS + _NO_TOOL_CALL_INSTRUCTION,
        model=_make_client(AGENT2_MODEL),
        tools=[search_history_files, read_history_file, export_report_pdf],
        handoffs=[handoff(agent1)],
        model_settings=ModelSettings(
            temperature=float(os.getenv("AGENT2_TEMPERATURE", "1.0")),
            max_tokens=int(os.getenv("AGENT2_MAX_TOKENS", "2000")),
            top_p=float(os.getenv("AGENT2_TOP_P", "0.95")),
        ),
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _rag_startup_sync() -> None:
    """Index toàn bộ file output cũ vào ChromaDB lúc startup (chạy 1 lần)."""
    if not _RAG_AVAILABLE:
        return
    try:
        n = rag.index_directory(OUTPUT_DIR, pattern="*.md")
        if n:
            import logging
            logging.getLogger(__name__).info(f"RAG startup: indexed {n} existing output files")
    except Exception:
        pass


_rag_startup_sync()  # chạy khi module được import lần đầu


async def run_merchant_workflow_async(user_message: str, uploaded_file_path: Optional[str] = None) -> str:
    """Run the multi-agent workflow: Agent 2 (MiniMax) orchestrates, Agent 1 (Gemma) researches."""
    _greennode_config()  # validate credentials early
    user_message = (user_message or "").strip() or "Xin chào!"

    # Build agents fresh each turn (stateless — history is in saved files)
    agent1 = _build_agent1()
    agent2 = _build_agent2(agent1)

    # Compose the input for Agent 2
    if uploaded_file_path:
        user_input = (
            f"{user_message}\n\n"
            f"[Uploaded file: {uploaded_file_path}] "
            "Please hand off to the Research Agent to process this file and then analyze the results."
        )
    elif _is_simple_message(user_message):
        # Fast path: skip agent overhead for greetings
        user_input = user_message
    else:
        user_input = user_message

    try:
        result = await Runner.run(agent2, input=user_input)
        return result.final_output or "Không có kết quả. Vui lòng thử lại."
    except Exception as exc:
        return (
            f"Đã xảy ra lỗi khi xử lý yêu cầu: {exc}\n\n"
            "Vui lòng kiểm tra kết nối GreenNode và thử lại."
        )


def run_merchant_workflow(user_message: str, uploaded_file_path: Optional[str] = None) -> str:
    """Synchronous wrapper for scripts and simple callers."""
    return asyncio.run(run_merchant_workflow_async(user_message, uploaded_file_path))
