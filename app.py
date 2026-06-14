from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import gradio as gr
import gradio_client.utils as gradio_client_utils

from merchant_agent_workflow import OUTPUT_DIR, export_analysis_pdf, run_merchant_workflow_async

_LOGO_PATH = Path(__file__).parent / "static" / "zalopay_logo.svg"
_BG_B64_PATH = Path(__file__).parent / "static" / "bg_blurred.b64"


def _bg_js() -> Optional[str]:
    """Return JS to inject background image on page load."""
    if _BG_B64_PATH.exists():
        b64 = _BG_B64_PATH.read_text().strip()
        return f"""
() => {{
    const style = document.createElement('style');
    style.textContent = `
        html, body {{
            background-image: url("data:image/jpeg;base64,{b64}") !important;
            background-size: cover !important;
            background-position: center !important;
            background-attachment: fixed !important;
            background-repeat: no-repeat !important;
        }}
        .gradio-container, .gradio-container > .main,
        .gradio-container .contain, .app,
        .gap, .flex {{
            background: transparent !important;
            background-color: transparent !important;
        }}
    `;
    document.head.appendChild(style);
}}
"""
    return None


def _logo_html() -> str:
    """Return header HTML with ZaloPay logo + title."""
    if _LOGO_PATH.exists():
        data = _LOGO_PATH.read_bytes()
        b64 = base64.b64encode(data).decode()
        return (
            '<div style="display:flex;align-items:center;gap:6px;padding:12px 0 4px 0;">'
            f'<img src="data:image/svg+xml;base64,{b64}" style="height:90px;overflow:visible;display:block;" alt="ZaloPay"/>'
            '<div style="border-left:2px solid #ddd;padding-left:8px;">'
            '<div style="font-size:22px;font-weight:700;color:#1A4FBA;line-height:1.2;">Merchant Analytics</div>'
            '<div style="font-size:14px;color:#888;margin-top:2px;">AI-powered payment performance assistant</div>'
            '</div>'
            '</div>'
        )
    return "<h1>Merchant Analytics Assistant</h1>"


def _patch_gradio_boolean_schema() -> None:
    original = gradio_client_utils._json_schema_to_python_type

    def patched(schema: Any, defs: Any) -> str:
        if isinstance(schema, bool):
            return "Any" if schema else "None"
        return original(schema, defs)

    gradio_client_utils._json_schema_to_python_type = patched


_patch_gradio_boolean_schema()


def _configured_server_port() -> Optional[int]:
    configured = os.getenv("GRADIO_SERVER_PORT")
    if configured:
        return int(configured)
    return None


def _uploaded_path(uploaded_file: Any) -> Optional[str]:
    if uploaded_file is None:
        return None
    if isinstance(uploaded_file, str):
        return uploaded_file
    return getattr(uploaded_file, "name", None)


def _extract_pdf_path_from_answer(answer: str, since_ts: float) -> Optional[str]:
    """Return a PDF path ONLY if a report was generated during this turn.

    Args:
        answer: Agent final text (may contain explicit path).
        since_ts: Unix timestamp marking the start of this turn — chỉ nhận PDF tạo sau mốc này.
    """
    # 1. Path xuất hiện rõ ràng trong câu trả lời
    match = re.search(r"(/[^\s\"']+merchant_analytics_report_[^\s\"']+\.pdf)", answer)
    if match:
        p = Path(match.group(1))
        if p.exists():
            return str(p)
    # 2. PDF mới được tạo trong turn này (mtime > thời điểm bắt đầu turn)
    pdfs = sorted(
        OUTPUT_DIR.glob("merchant_analytics_report_*.pdf"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if pdfs and pdfs[0].stat().st_mtime >= since_ts:
        return str(pdfs[0])
    # Không có PDF mới → KHÔNG download
    return None


def _parse_multimodal(payload: Any) -> tuple:
    """Parse gr.MultimodalTextbox value: {'text': str, 'files': [paths]}."""
    if isinstance(payload, dict):
        text = (payload.get("text") or "").strip()
        files = payload.get("files") or []
        file_path = _uploaded_path(files[0]) if files else None
        return text, file_path
    # Fallback nếu là string thuần
    return (str(payload or "").strip(), None)


async def chat_turn(payload: Any, history: Optional[list]) -> tuple:
    history = history or []
    clean_message, file_path = _parse_multimodal(payload)

    empty_input = {"text": "", "files": []}
    if not clean_message and not file_path:
        return history, empty_input, gr.update(visible=False, value=None)

    user_display = clean_message or "Phân tích file dữ liệu merchant vừa upload."
    if file_path:
        user_display += f"\n\nFile: {Path(file_path).name}"

    # Mốc thời gian bắt đầu turn — để chỉ nhận PDF được tạo TRONG turn này
    turn_start_ts = time.time()

    try:
        answer = await run_merchant_workflow_async(user_display, file_path)
    except Exception as exc:
        answer = (
            f"Đã xảy ra lỗi: {exc}\n\n"
            "Vui lòng upload file CSV/XLSX với các cột: Date, Merchant, SOF_Type, Acq_Type, TPV."
        )

    history.append((user_display, answer))

    # Check if agent exported a PDF during this turn
    pdf_path = _extract_pdf_path_from_answer(answer, turn_start_ts)
    if pdf_path:
        download_update = gr.update(visible=True, value=pdf_path)
    else:
        download_update = gr.update(visible=False, value=None)

    return history, empty_input, download_update


async def export_pdf(history: Optional[list]) -> Any:
    """Export the latest assistant response as PDF when user clicks the export button."""
    if not history:
        return gr.update(visible=False, value=None)

    # Collect all assistant messages into one report text
    report_lines = ["# ZaloPay Merchant Analytics Report\n"]
    for _, assistant_msg in history:
        if assistant_msg:
            report_lines.append(assistant_msg)
            report_lines.append("\n---\n")

    report_text = "\n".join(report_lines)
    try:
        pdf_path = export_analysis_pdf(report_text, title="ZaloPay Merchant Analytics Report")
        return gr.update(visible=True, value=pdf_path)
    except Exception as exc:
        return gr.update(visible=False, value=None)


def _build_css() -> str:
    bg_css = ""
    if _BG_B64_PATH.exists():
        b64 = _BG_B64_PATH.read_text().strip()
        bg_css = f"""
html::before {{
    content: '';
    position: fixed;
    inset: 0;
    width: 100vw;
    height: 100vh;
    background-image: url("data:image/jpeg;base64,{b64}");
    background-size: cover;
    background-position: center;
    z-index: -9999;
    pointer-events: none;
}}
html, body {{
    background: transparent !important;
    background-color: transparent !important;
}}
.gradio-container, .gradio-container > .main, .gradio-container > .main > .wrap {{
    background: transparent !important;
    background-color: transparent !important;
}}
"""
    return bg_css + """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

* { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important; }

/* Primary button — ZaloPay blue */
button.primary, .gr-button-primary {
    background: #1A4FBA !important;
    border-color: #1A4FBA !important;
    color: white !important;
}
button.primary:hover, .gr-button-primary:hover {
    background: #1541A0 !important;
    border-color: #1541A0 !important;
}

/* Secondary buttons — outlined blue */
button.secondary, .gr-button-secondary {
    background: white !important;
    border: 1.5px solid #1A4FBA !important;
    color: #1A4FBA !important;
}
button.secondary:hover, .gr-button-secondary:hover {
    background: #EEF3FF !important;
}

/* Export PDF button — ZaloPay green */
#export-btn {
    background: #06C755 !important;
    border-color: #06C755 !important;
    color: white !important;
}
#export-btn:hover {
    background: #05A847 !important;
    border-color: #05A847 !important;
}


/* Textbox focus */
textarea:focus, input:focus {
    border-color: #1A4FBA !important;
    box-shadow: 0 0 0 2px rgba(26,79,186,0.15) !important;
}

/* Tab / label color */
.label-wrap span { color: #1A4FBA !important; font-weight: 600 !important; }

/* Hide all loading indicators */
.eta-bar, .progress-bar, .generating,
.progress-text, .meta-text, .meta-text-center,
span.border, .loader, .pending, .loading,
.chatbot .pending, .chatbot .loading,
.chatbot .loader, [data-testid="bot"].pending,
.status-tracker, .wrap.hide { display: none !important; }

/* Claude-style input bar */
#chat-input {
    border-radius: 24px !important;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08) !important;
    border: 1.5px solid #e0e0e0 !important;
    background: white !important;
    padding: 4px 6px !important;
}
#chat-input:focus-within {
    border-color: #1A4FBA !important;
    box-shadow: 0 0 0 2px rgba(26,79,186,0.12) !important;
}
#chat-input textarea {
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
    font-size: 15px !important;
}
/* Submit button ZaloPay blue */
#chat-input button.submit-button, #chat-input .submit-button {
    background: #1A4FBA !important;
    border-radius: 50% !important;
}
/* Round the chatbot too */
#main-chatbot {
    border-radius: 16px !important;
}
"""


_AUTO_SCROLL_JS = """
() => {
    // ── Auto scroll chatbot ──────────────────────────────────────────
    const scrollToBottom = () => {
        const root = document.querySelector('#main-chatbot') || document.querySelector('.chatbot');
        if (!root) return;
        // Tìm phần tử scroll thực sự (có overflow-y) bên trong chatbot
        let best = null, bestH = 0;
        root.querySelectorAll('*').forEach(el => {
            const style = getComputedStyle(el);
            const scrollable = (style.overflowY === 'auto' || style.overflowY === 'scroll');
            if (scrollable && el.scrollHeight > bestH) {
                best = el; bestH = el.scrollHeight;
            }
        });
        const targets = best ? [best] : [];
        // Fallback: bất kỳ div nào overflow
        root.querySelectorAll('div').forEach(el => {
            if (el.scrollHeight > el.clientHeight + 10) targets.push(el);
        });
        targets.forEach(el => { el.scrollTop = el.scrollHeight; });
        // Cũng scroll cả trang xuống cuối phòng trường hợp chatbot không có inner scroll
        window.scrollTo(0, document.body.scrollHeight);
    };

    const attachScroll = () => {
        const target = document.querySelector('#main-chatbot') || document.querySelector('.chatbot');
        if (!target) { setTimeout(attachScroll, 500); return; }
        const obs = new MutationObserver(() => {
            // chờ 1 frame để DOM render xong rồi mới scroll
            requestAnimationFrame(() => {
                scrollToBottom();
                setTimeout(scrollToBottom, 60);
            });
        });
        obs.observe(target, { childList: true, subtree: true, characterData: true });
    };

    if (document.readyState === 'complete') {
        attachScroll();
    } else {
        window.addEventListener('load', attachScroll);
    }
}
"""


# Trigger browser download chỉ khi có PDF mới (dedupe theo href để tránh down lại file cũ)
_AUTO_DOWNLOAD_JS = """
() => {
    setTimeout(() => {
        const link = document.querySelector('a[href*="/file="][href*=".pdf"]');
        if (!link) return;
        if (window.__lastPdfHref === link.href) return;  // đã down rồi, bỏ qua
        window.__lastPdfHref = link.href;
        const a = document.createElement('a');
        a.href = link.href;
        a.download = link.href.split('/').pop() || 'report.pdf';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }, 800);
}
"""


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Merchant Analytics Assistant", theme=gr.themes.Soft(), css=_build_css(), js=_AUTO_SCROLL_JS) as demo:
        gr.HTML(_logo_html())
        gr.Markdown(
            "Upload dữ liệu giao dịch (CSV/XLSX), sau đó đặt câu hỏi để phân tích hiệu suất merchant — "
            "TPV, MoM growth, chẩn đoán nguyên nhân drop theo kênh, và đề xuất hành động PnL."
        )

        chatbot = gr.Chatbot(height=520, show_label=False, elem_id="main-chatbot")

        # Claude-style: input gộp chung (text + upload + send) trong 1 thanh
        message = gr.MultimodalTextbox(
            placeholder="Hỏi về MTD TPV, MoM growth, kênh underperforming, hoặc nguyên nhân drop...",
            file_count="single",
            file_types=[".csv", ".xlsx", ".xls"],
            show_label=False,
            elem_id="chat-input",
            submit_btn=True,
        )

        with gr.Row():
            export_btn = gr.Button("📄 Xuất PDF", scale=2, elem_id="export-btn")
            clear = gr.Button("Xóa chat", scale=1)

        download_file = gr.File(
            label="Tải báo cáo PDF",
            visible=False,
            interactive=False,
        )

        message.submit(
            chat_turn,
            inputs=[message, chatbot],
            outputs=[chatbot, message, download_file],
        ).then(
            fn=None, inputs=None, outputs=None, js=_AUTO_DOWNLOAD_JS,
        )
        export_btn.click(
            export_pdf,
            inputs=[chatbot],
            outputs=[download_file],
        ).then(
            fn=None, inputs=None, outputs=None, js=_AUTO_DOWNLOAD_JS,
        )
        clear.click(
            lambda: ([], {"text": "", "files": []}, gr.update(visible=False, value=None)),
            outputs=[chatbot, message, download_file],
        )

    return demo


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    build_app().launch(
        # 0.0.0.0 trong Docker để truy cập từ ngoài container; mặc định 127.0.0.1 khi chạy local
        server_name=os.getenv("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=_configured_server_port() or 7860,
        show_api=False,
        show_error=True,
    )
