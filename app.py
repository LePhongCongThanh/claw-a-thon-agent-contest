from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import gradio as gr
import gradio_client.utils as gradio_client_utils

from merchant_agent_workflow import (
    OUTPUT_DIR,
    export_analysis_pdf,
    run_merchant_workflow_async,
    run_merchant_workflow_stream,
    synthesize_conversation_report,
)

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
    """Return header HTML with Zalopay logo + title."""
    if _LOGO_PATH.exists():
        data = _LOGO_PATH.read_bytes()
        b64 = base64.b64encode(data).decode()
        return (
            '<div style="display:flex;align-items:center;gap:6px;padding:12px 0 4px 0;">'
            f'<img src="data:image/svg+xml;base64,{b64}" style="height:90px;overflow:visible;display:block;" alt="Zalopay"/>'
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


# Chuyển LaTeX/math artifacts → Unicode để chat hiển thị sạch (chat không render KaTeX).
_LATEX_REPLACEMENTS = {
    r"\uparrow": "▲", r"\Uparrow": "▲", r"\nearrow": "↗",
    r"\downarrow": "▼", r"\Downarrow": "▼", r"\searrow": "↘",
    r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←",
    r"\times": "×", r"\div": "÷", r"\pm": "±",
    r"\approx": "≈", r"\geq": "≥", r"\leq": "≤", r"\neq": "≠",
    r"\%": "%", r"\$": "$", r"\#": "#", r"\&": "&",
}


def _clean_latex_artifacts(text: str) -> str:
    """Loại bỏ cú pháp LaTeX/math mà chat không render được.
    Chỉ gỡ cặp $...$ khi bên trong là LaTeX (có dấu \\) để KHÔNG đụng tiền tệ như $5000."""
    if not text or "\\" not in text:
        return text
    # Gỡ $...$ / $$...$$ chỉ khi nội dung chứa lệnh LaTeX (có backslash)
    text = re.sub(r"\${1,2}\s*([^$]*?\\[^$]*?)\s*\${1,2}", r"\1", text, flags=re.DOTALL)
    # Gỡ \(...\) và \[...\]
    text = re.sub(r"\\\(\s*(.*?)\s*\\\)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\\\[\s*(.*?)\s*\\\]", r"\1", text, flags=re.DOTALL)
    # Đổi các lệnh LaTeX phổ biến sang Unicode
    for token, repl in _LATEX_REPLACEMENTS.items():
        text = text.replace(token, repl)
    return text


def _pdf_download_link(pdf_path: str, label: str = "📄 Tải báo cáo PDF") -> str:
    """HTML link tải PDF hiển thị trong chat (Gradio serve qua /file=). Click → tải về."""
    abspath = str(Path(pdf_path).resolve())
    fname = Path(pdf_path).name
    return (
        f'\n\n<a class="pdf-dl" href="/file={abspath}" download="{fname}" '
        f'target="_blank" rel="noopener">{label}</a>'
    )


def _parse_multimodal(payload: Any) -> tuple:
    """Parse gr.MultimodalTextbox value: {'text': str, 'files': [paths]}."""
    if isinstance(payload, dict):
        text = (payload.get("text") or "").strip()
        files = payload.get("files") or []
        file_path = _uploaded_path(files[0]) if files else None
        return text, file_path
    # Fallback nếu là string thuần
    return (str(payload or "").strip(), None)


# Bong bóng "đang phân tích" — TEXT THUẦN (chắc chắn render). Dấu chấm được cycle thật trong bot_respond.
_TYPING_LABEL = "Đang phân tích"
_TYPING_HTML = f"{_TYPING_LABEL}..."  # giá trị khởi tạo ở bước submit_user


def submit_user(payload: Any, history: Optional[list]) -> tuple:
    """Bước 1: hiện tin nhắn user NGAY + bong bóng 3 chấm chờ. Trả về pending cho bước 2."""
    history = history or []
    clean_message, file_path = _parse_multimodal(payload)
    empty_input = {"text": "", "files": []}

    if not clean_message and not file_path:
        return history, payload, None  # không có gì để gửi

    user_display = clean_message or "Phân tích file dữ liệu merchant vừa upload."
    if file_path:
        user_display += f"\n\nFile: {Path(file_path).name}"

    # Hiện ngay user message + bong bóng bot 3 chấm động (sẽ thay bằng câu trả lời ở bước 2)
    history = history + [(user_display, _TYPING_HTML)]
    pending = {"message": user_display, "file_path": file_path, "ts": time.time()}
    return history, empty_input, pending


async def bot_respond(history: Optional[list], pending: Any):
    """Bước 2 (STREAMING): chữ hiện dần khi agent đang viết, rồi xử lý PDF ở cuối.

    Là async generator → Gradio cập nhật chatbot liên tục theo từng yield.
    """
    history = history or []
    if not pending or not history:
        yield history, gr.update(visible=False, value=None)
        return

    # Lịch sử các lượt TRƯỚC (bỏ lượt cuối đang chứa typing indicator) để giữ ngữ cảnh follow-up
    prior_history = history[:-1] if history else []
    user_turn = history[-1][0]
    no_change = gr.update()  # giữ nguyên download_file trong lúc streaming

    # Tiêu thụ stream qua queue để có thể cycle dấu "..." (text thuần, animation thật) trong lúc CHỜ
    queue: asyncio.Queue = asyncio.Queue()

    async def _consume():
        try:
            async for item in run_merchant_workflow_stream(
                pending["message"], pending.get("file_path"), history=prior_history
            ):
                await queue.put(item)
        except Exception as exc:  # noqa: BLE001
            await queue.put((f"Đã xảy ra lỗi: {exc}", True))
        finally:
            await queue.put(None)  # sentinel kết thúc

    consumer = asyncio.create_task(_consume())
    final_answer = ""
    last_text = ""        # text thật mới nhất đã stream
    got_text = False
    dots = 0
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.4)
            except asyncio.TimeoutError:
                # TẠM DỪNG (đang gọi tool/tra cứu) → cycle dấu 3 chấm cho biết còn work
                dots = (dots % 3) + 1
                if got_text:
                    # đã có text (vd "Để tôi tìm kiếm...") → chèn chỉ báo tra cứu phía sau
                    history[-1] = (user_turn, f"{last_text}\n\n⏳ Đang tra cứu{'.' * dots}")
                else:
                    history[-1] = (user_turn, f"{_TYPING_LABEL}{'.' * dots}")
                yield history, no_change
                continue
            if item is None:
                break
            partial, is_final = item
            if is_final:
                final_answer = _clean_latex_artifacts(partial)
                history[-1] = (user_turn, final_answer)
            else:
                got_text = bool(partial)
                last_text = partial or last_text
                history[-1] = (user_turn, partial or f"{_TYPING_LABEL}...")
                yield history, no_change
    finally:
        if not consumer.done():
            consumer.cancel()

    # Xử lý PDF khi đã có câu trả lời cuối cùng
    pdf_path = _extract_pdf_path_from_answer(final_answer, pending.get("ts", 0))
    if pdf_path:
        link = _pdf_download_link(pdf_path)
        history[-1] = (user_turn, final_answer + link)
        download_update = gr.update(visible=True, value=pdf_path)
    else:
        download_update = gr.update(visible=False, value=None)

    yield history, download_update


def _build_conversation_transcript(history: Optional[list]) -> str:
    """Ghép cả hội thoại thành transcript để LLM tổng hợp (bỏ tin nhắn rỗng/typing)."""
    parts = []
    for user_msg, bot_msg in (history or []):
        if user_msg and str(user_msg).strip():
            parts.append(f"User: {str(user_msg).strip()}")
        bot_s = str(bot_msg or "").strip()
        if bot_s and not bot_s.startswith(_TYPING_LABEL):  # bỏ message "Đang phân tích..."
            parts.append(f"Assistant: {bot_s}")
    return "\n\n".join(parts)


async def export_pdf(history: Optional[list]) -> Any:
    """Tổng hợp Ý CHÍNH của CẢ cuộc hội thoại thành báo cáo rồi xuất PDF."""
    if not history:
        return gr.update(visible=False, value=None)

    transcript = _build_conversation_transcript(history)
    if not transcript.strip():
        return gr.update(visible=False, value=None)

    try:
        # LLM tổng hợp toàn hội thoại thành báo cáo mạch lạc (không chỉ reply cuối)
        report_md = await synthesize_conversation_report(transcript)
        report_md = _clean_latex_artifacts(report_md)
        pdf_path = export_analysis_pdf(report_md, title="Zalopay Merchant Analytics Report")
        return gr.update(visible=True, value=pdf_path)
    except Exception:
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

/* Primary button — Zalopay blue */
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

/* Export PDF button — Zalopay green */
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
/* Submit button Zalopay blue */
#chat-input button.submit-button, #chat-input .submit-button {
    background: #1A4FBA !important;
    border-radius: 50% !important;
}
/* Round the chatbot too */
#main-chatbot {
    border-radius: 16px !important;
}

/* Link tải PDF trong chat — nút xanh Zalopay */
a.pdf-dl {
    display: inline-block;
    margin-top: 8px;
    padding: 8px 16px;
    background: #06C755 !important;
    color: #fff !important;
    border-radius: 20px;
    font-weight: 600;
    text-decoration: none !important;
}
a.pdf-dl:hover { background: #05A847 !important; }
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
            "Upload dữ liệu giao dịch (CSV, XLSX) hoặc tài liệu (PDF, Word, PowerPoint, TXT), "
            "sau đó đặt câu hỏi để phân tích hiệu suất merchant — TPV, MoM growth, "
            "chẩn đoán nguyên nhân drop theo kênh, và đề xuất hành động PnL."
        )

        chatbot = gr.Chatbot(height=520, show_label=False, elem_id="main-chatbot", sanitize_html=False)

        # Claude-style: input gộp chung (text + upload + send) trong 1 thanh
        message = gr.MultimodalTextbox(
            placeholder="Hỏi về MTD TPV, MoM growth, kênh underperforming, hoặc nguyên nhân drop...",
            file_count="single",
            file_types=[
                ".csv", ".tsv", ".xlsx", ".xls", ".json",   # tabular
                ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".txt", ".md",  # document
            ],
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

        # State giữ request đang chờ giữa bước 1 (hiện user msg) và bước 2 (agent trả lời)
        pending_state = gr.State(None)

        # Bước 1: hiện tin nhắn user ngay + xóa input → Bước 2: agent trả lời → auto-download nếu có PDF
        message.submit(
            submit_user,
            inputs=[message, chatbot],
            outputs=[chatbot, message, pending_state],
        ).then(
            bot_respond,
            inputs=[chatbot, pending_state],
            outputs=[chatbot, download_file],
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
            lambda: ([], {"text": "", "files": []}, None, gr.update(visible=False, value=None)),
            outputs=[chatbot, message, pending_state, download_file],
        )

    return demo


def create_asgi_app():
    """Mount Gradio vào FastAPI + thêm /health (AgentBase yêu cầu GET /health → 200)."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    fastapi_app = FastAPI(title="Zalopay Merchant Analytics")

    @fastapi_app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    demo = build_app()
    demo.queue()  # bật queue cho streaming generator (bot_respond)
    # Mount Gradio tại "/"; route /health khai báo trước nên không bị che
    return gr.mount_gradio_app(
        fastapi_app,
        demo,
        path="/",
        allowed_paths=[str(OUTPUT_DIR.resolve())],  # serve PDF qua link /file= trong chat
    )


if __name__ == "__main__":
    import uvicorn

    # AgentBase yêu cầu container listen 8080; local có thể override bằng GRADIO_SERVER_PORT
    host = os.getenv("GRADIO_SERVER_NAME", "0.0.0.0")
    port = _configured_server_port() or 8080
    uvicorn.run(create_asgi_app(), host=host, port=port)
