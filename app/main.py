from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
from typing import Iterator

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.kb import KnowledgeBase, extract_text_from_file
from app.ollama_client import OllamaClient
from app.wechat import (
    build_encrypted_reply,
    build_text_reply,
    decrypt_wechat_message,
    parse_wechat_xml,
    verify_msg_signature,
    verify_signature,
)
from app.wechat_api import get_access_token, get_cached_access_token, send_custom_text_message


app = FastAPI(title="WeiXinAgent", version="2.0.0")
if (Path(__file__).resolve().parent.parent / "web").exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "web")),
        name="static",
    )
kb = KnowledgeBase(
    settings.kb_db_path,
    max_chunk_chars=settings.max_chunk_chars,
    chunk_overlap_chars=settings.chunk_overlap_chars,
    hybrid_dense_weight=settings.hybrid_dense_weight,
    hybrid_bm25_weight=settings.hybrid_bm25_weight,
    hybrid_rrf_k=settings.hybrid_rrf_k,
    retrieval_candidates=settings.retrieval_candidates,
)
ollama = OllamaClient(
    base_url=settings.ollama_base_url,
    chat_model=settings.ollama_chat_model,
    embed_model=settings.ollama_embed_model,
    vision_model=settings.ollama_vision_model,
)

event_log_path = Path("data/events.log")
event_log_path.parent.mkdir(parents=True, exist_ok=True)
kb_miss_log_path = Path("data/kb_miss.log")
session_store_dir = Path(settings.chat_session_store_dir) if settings.chat_session_store_dir else None
if session_store_dir:
    session_store_dir.mkdir(parents=True, exist_ok=True)
web_root = Path(__file__).resolve().parent.parent / "web"
kb_sync_lock = threading.Lock()
kb_sync_stop_event = threading.Event()
kb_sync_thread: threading.Thread | None = None
kb_sync_last_result: dict = {"ok": False, "detail": "not started"}
reply_executor = ThreadPoolExecutor(max_workers=4)
chat_session_lock = threading.Lock()
chat_sessions: dict[str, dict] = {}
chat_session_stop_event = threading.Event()
chat_session_thread: threading.Thread | None = None


def _log_event(kind: str, detail: str = "") -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} [{kind}] {detail}\n"
    with event_log_path.open("a", encoding="utf-8") as f:
        f.write(line)


def _admin_guard(x_admin_token: str | None) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured")
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Unauthorized")


class WebChatRequest(BaseModel):
    message: str
    session_id: str | None = None


def _normalize_session_id(raw: str | None) -> str:
    sid = (raw or "").strip()
    if not sid:
        return ""
    sid = re.sub(r"[^a-zA-Z0-9_-]", "_", sid)
    return sid[:80]


def _ensure_session_id(raw: str | None) -> str:
    sid = _normalize_session_id(raw)
    if sid:
        return sid
    return uuid.uuid4().hex


def _session_file_path(session_id: str) -> Path | None:
    if not session_store_dir or not session_id:
        return None
    return session_store_dir / f"{session_id}.jsonl"


def _session_get_recent_messages(session_id: str) -> list[dict]:
    sid = _normalize_session_id(session_id)
    if not sid:
        return []
    now = time.time()
    ttl_sec = max(60, settings.chat_session_ttl_sec)
    limit = max(1, settings.chat_session_max_turns) * 2
    with chat_session_lock:
        item = chat_sessions.get(sid)
        if not item:
            item = None
        else:
            if float(item.get("updated_at", 0.0)) < now - ttl_sec:
                chat_sessions.pop(sid, None)
                item = None
            else:
                recent = item.get("messages", [])[-limit:]
                return [{"role": str(m.get("role", "")), "content": str(m.get("content", ""))} for m in recent]

    # Warm-up from temporary file when process was restarted but session has not expired.
    path = _session_file_path(sid)
    if path is None or not path.exists():
        return []
    try:
        if path.stat().st_mtime < now - ttl_sec:
            path.unlink(missing_ok=True)
            return []
    except Exception:
        return []

    recovered: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            recent_lines: deque[str] = deque((line.strip() for line in f if line.strip()), maxlen=limit)
        for line in recent_lines:
            row = json.loads(line)
            user_text = str(row.get("user", "")).strip()
            assistant_text = str(row.get("assistant", "")).strip()
            if user_text:
                recovered.append({"role": "user", "content": user_text, "ts": now})
            if assistant_text:
                recovered.append({"role": "assistant", "content": assistant_text, "ts": now})
    except Exception:
        return []

    if not recovered:
        return []
    with chat_session_lock:
        chat_sessions[sid] = {"updated_at": now, "messages": recovered[-limit:]}
    return [{"role": str(m.get("role", "")), "content": str(m.get("content", ""))} for m in recovered[-limit:]]


def _session_append_turn(session_id: str | None, user_text: str, assistant_text: str) -> None:
    sid = _normalize_session_id(session_id)
    if not sid:
        return
    user_text = (user_text or "").strip()
    assistant_text = (assistant_text or "").strip()
    if not user_text or not assistant_text:
        return

    now = time.time()
    payload = {
        "time": datetime.now(timezone.utc).isoformat(),
        "user": user_text,
        "assistant": assistant_text,
    }
    with chat_session_lock:
        item = chat_sessions.setdefault(sid, {"updated_at": now, "messages": []})
        messages = item.setdefault("messages", [])
        messages.append({"role": "user", "content": user_text, "ts": now})
        messages.append({"role": "assistant", "content": assistant_text, "ts": now})
        keep = max(1, settings.chat_session_max_turns) * 2
        if len(messages) > keep:
            del messages[: len(messages) - keep]
        item["updated_at"] = now

    path = _session_file_path(sid)
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _cleanup_expired_sessions() -> None:
    ttl_sec = max(60, settings.chat_session_ttl_sec)
    cutoff = time.time() - ttl_sec
    expired: list[str] = []
    with chat_session_lock:
        for sid, item in list(chat_sessions.items()):
            if float(item.get("updated_at", 0.0)) < cutoff:
                expired.append(sid)
                chat_sessions.pop(sid, None)

    removed_files = 0
    for sid in expired:
        path = _session_file_path(sid)
        if path and path.exists():
            path.unlink(missing_ok=True)
            removed_files += 1

    if session_store_dir and session_store_dir.exists():
        for file_path in session_store_dir.glob("*.jsonl"):
            try:
                if file_path.stat().st_mtime < cutoff:
                    file_path.unlink(missing_ok=True)
                    removed_files += 1
            except Exception:
                continue

    if expired or removed_files:
        _log_event("session_cleanup", f"expired_sessions={len(expired)} removed_files={removed_files}")


def _active_session_count() -> int:
    with chat_session_lock:
        return len(chat_sessions)


def _start_session_cleanup_if_needed() -> None:
    global chat_session_thread
    if chat_session_thread and chat_session_thread.is_alive():
        return

    interval = max(30, settings.chat_session_cleanup_sec)

    def _loop() -> None:
        _log_event("session_cleanup_loop", f"started interval={interval}s ttl={settings.chat_session_ttl_sec}s")
        while not chat_session_stop_event.wait(interval):
            _cleanup_expired_sessions()
        _log_event("session_cleanup_loop", "stopped")

    chat_session_thread = threading.Thread(target=_loop, daemon=True)
    chat_session_thread.start()


def _render_session_history(messages: list[dict]) -> str:
    if not messages:
        return ""
    rows: list[str] = []
    for msg in messages[-8:]:
        role = "用户" if msg.get("role") == "user" else "客服"
        content = (msg.get("content", "") or "").strip()
        if not content:
            continue
        rows.append(f"{role}: {content[:280]}")
    return "\n".join(rows)


def _resolve_web_chat_url(request: Request | None = None) -> str:
    if settings.web_chat_url:
        return settings.web_chat_url
    if request is not None:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if host:
            return f"{proto}://{host}/chat"
    return "/chat"


def _build_web_rag_prompt(question: str, hits: list[dict], history_text: str = "") -> str:
    context = "\n\n".join([f"[{h['source_name']}] {(h['chunk_text'] or '')[:320]}" for h in hits[:4]])
    history_block = f"同一会话近期记录：\n{history_text}\n\n" if history_text else ""
    return (
        "你是企业客服助手，请基于提供的知识库片段回答用户。\n"
        "要求：\n"
        "1) 只用中文回答；\n"
        "2) 先给结论，再给可执行步骤；\n"
        "3) 不要编造政策、价格、时效承诺；\n"
        "4) 语气专业但自然。\n\n"
        f"{history_block}"
        f"知识库片段：\n{context}\n\n"
        f"用户问题：{question}\n\n"
        "请直接给出客服回复："
    )


def _strip_think_blocks(text: str) -> str:
    # Remove complete think blocks first.
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    # While streaming, model output may contain an opening <think> without a closing tag yet.
    # In that case, hide everything after <think> until the closing tag arrives in later chunks.
    lower = cleaned.lower()
    open_idx = lower.find("<think>")
    if open_idx >= 0:
        cleaned = cleaned[:open_idx]
    cleaned = cleaned.replace("</think>", "")
    return cleaned


def _query_keywords(text: str) -> list[str]:
    stopwords = {
        "你好",
        "您好",
        "请问",
        "请",
        "帮我",
        "帮忙",
        "咨询",
        "问题",
        "怎么",
        "如何",
        "什么",
        "一下",
        "这个",
        "那个",
        "我们",
        "你们",
        "可以",
    }
    keywords: list[str] = []
    for token in KnowledgeBase._tokenize_text(text or ""):
        t = (token or "").strip().lower()
        if len(t) < 2 or t in stopwords:
            continue
        keywords.append(t)
    return keywords[:12]


def _hits_match_query(question: str, hits: list[dict]) -> bool:
    if not hits:
        return False
    tokens = _query_keywords(question)
    if not tokens:
        return True
    corpus = "\n".join((h.get("chunk_text", "") or "")[:500] for h in hits[:3]).lower()
    return any(t in corpus for t in tokens)


def _record_kb_miss(question: str, reason: str, hits: list[dict] | None = None) -> None:
    top_hits = []
    for h in (hits or [])[:3]:
        top_hits.append(
            {
                "source_name": h.get("source_name", ""),
                "score": round(float(h.get("score", 0.0) or 0.0), 6),
            }
        )
    payload = {
        "time": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "question": (question or "").strip(),
        "top_hits": top_hits,
    }
    try:
        with kb_miss_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


DIRECT_FAQ_ITEMS: list[dict[str, object]] = [
    {
        "id": "product_specs",
        "title": "商品规格咨询",
        "patterns": ["商品规格", "规格", "型号", "尺码", "参数", "怎么选", "选型", "兼容"],
        "reply": (
            "商品规格建议按“使用场景、关键参数、安装兼容、预算”四项来选。"
            "请提供商品名称或编号、使用场景和预算区间，我可以给你 1-2 个可选型号并说明差异。"
        ),
    },
    {
        "id": "product_compatibility",
        "title": "兼容与适配",
        "patterns": ["兼容", "适配", "能不能用", "支持机型", "支持型号", "接口匹配"],
        "reply": (
            "兼容判断请以商品参数页与设备型号为准。"
            "请提供设备品牌、型号和接口信息，我会帮你核对是否适配并给替代方案。"
        ),
    },
    {
        "id": "stock_availability",
        "title": "库存与现货",
        "patterns": ["有货", "现货", "库存", "补货", "缺货", "什么时候到货"],
        "reply": (
            "库存会随下单与仓库调拨实时变化，请以商品页展示为准。"
            "如果当前缺货，可告诉我商品名或编号，我可以帮你登记到货提醒。"
        ),
    },
    {
        "id": "price_policy",
        "title": "价格与活动",
        "patterns": ["多少钱", "价格", "优惠", "活动价", "能便宜", "最低价"],
        "reply": (
            "价格和优惠以商品页实时展示及结算页为准。"
            "你可以提供商品链接或编号，我帮你确认当前可用活动、券后价和是否支持叠加。"
        ),
    },
    {
        "id": "order_status",
        "title": "订单状态查询",
        "patterns": ["订单状态", "订单查询", "查订单", "未发货", "处理中", "已完成"],
        "reply": (
            "订单进度可在“我的订单”查看最新节点。"
            "若状态长时间未更新，请提供订单号和下单时间，我帮你进一步核查。"
        ),
    },
    {
        "id": "shipping_time",
        "title": "发货时效",
        "patterns": ["多久发货", "发货时间", "什么时候发货", "几天到", "时效"],
        "reply": (
            "发货时效通常以商品页和订单页承诺为准。"
            "节假日、天气和仓配压力可能影响送达时间，如有延迟我可协助催单。"
        ),
    },
    {
        "id": "logistics_tracking",
        "title": "物流追踪",
        "patterns": ["物流", "快递", "运单", "查件", "包裹", "签收"],
        "reply": (
            "物流信息以承运商轨迹为准，通常会有揽收、运输、中转、派送等节点。"
            "若 24 小时无更新或显示异常，请提供订单号/运单号，我帮你排查。"
        ),
    },
    {
        "id": "address_modify",
        "title": "修改收货地址",
        "patterns": ["改地址", "修改地址", "收货地址", "填错地址", "地址变更"],
        "reply": (
            "订单未发货时一般可尝试修改地址；已发货订单通常需联系物流协商改派。"
            "请尽快提供订单号和新地址信息，我帮你判断可操作路径。"
        ),
    },
    {
        "id": "cancel_order",
        "title": "取消订单",
        "patterns": ["取消订单", "不想要了", "撤销订单", "申请取消"],
        "reply": (
            "未发货订单通常可直接申请取消；已发货订单一般需签收后走退货流程。"
            "你可以先在订单页提交取消申请，我也可以协助你核对当前状态。"
        ),
    },
    {
        "id": "invoice",
        "title": "发票问题",
        "patterns": ["发票", "开票", "电子发票", "纸质发票", "抬头", "税号"],
        "reply": (
            "发票类型、抬头和税号请以开票页面填写信息为准。"
            "如需补开发票，请提供订单号和开票信息，我帮你确认是否在可申请时效内。"
        ),
    },
    {
        "id": "coupon",
        "title": "优惠券问题",
        "patterns": ["优惠券", "券", "红包", "满减", "不能用券", "券失效"],
        "reply": (
            "优惠券通常受有效期、适用商品、门槛和叠加规则限制。"
            "请提供券名称和商品信息，我帮你核对不可用原因并给替代方案。"
        ),
    },
    {
        "id": "payment_issue",
        "title": "支付异常",
        "patterns": ["支付失败", "支付异常", "扣款", "重复扣款", "无法付款", "支付报错"],
        "reply": (
            "支付异常可先检查网络、支付方式限额与风控提示，再重试一次。"
            "若已扣款但订单未更新，请提供订单号、支付时间和截图，我会协助核账处理。"
        ),
    },
    {
        "id": "returns",
        "title": "退换货政策",
        "patterns": ["退换货", "退货", "换货", "七天无理由", "售后政策"],
        "reply": (
            "退换货一般遵循《消费者权益保护法》及平台规则处理。"
            "常见路径是：提交申请 -> 客服审核 -> 按指引寄回/取件 -> 验货后退款或换货。"
            "请先准备订单号、问题说明和必要图片，具体时效以订单页与平台规则为准。"
        ),
    },
    {
        "id": "refund_timeline",
        "title": "退款时效",
        "patterns": ["退款", "多久到账", "何时到账", "退款进度", "退款慢"],
        "reply": (
            "退款到账时间通常受支付渠道和银行处理时效影响。"
            "一般在审核通过后按原路退回，具体到账时间请以订单退款进度页为准。"
        ),
    },
    {
        "id": "account_security",
        "title": "账号安全与冻结",
        "patterns": ["账号冻结", "账户冻结", "风控", "被盗", "异常登录", "解封"],
        "reply": (
            "账号安全场景建议先修改密码并开启二次验证。"
            "若账号被冻结，请提供账号ID、报错信息和时间，我会协助提交身份核验与解封。"
        ),
    },
    {
        "id": "account_login",
        "title": "账号与登录",
        "patterns": ["登录", "登陆", "账号", "账户", "验证码", "密码", "收不到短信", "无法登录"],
        "reply": (
            "账号登录问题可先按顺序排查：确认账号密码/验证码正确、检查网络与短信通道、使用“忘记密码”重置。"
            "若提示风控或冻结，请提供账号ID、报错截图和发生时间，我会转人工做身份核验与解封处理。"
        ),
    },
    {
        "id": "warranty",
        "title": "质保与维修",
        "patterns": ["质保", "保修", "维修", "保修期", "三包"],
        "reply": (
            "质保与维修通常以商品详情页、订单信息和三包政策为准。"
            "请提供订单号、故障现象和照片/视频，我帮你确认是否在保修范围内。"
        ),
    },
    {
        "id": "after_sales",
        "title": "售后处理建议",
        "patterns": ["售后", "质量问题", "投诉处理", "处理建议"],
        "reply": (
            "售后处理建议：先收集订单号、问题现象、发生时间、图片/视频证据。"
            "再明确诉求（退款/换货/维修）并保留物流与沟通记录；若协商超时可申请平台介入。"
        ),
    },
    {
        "id": "complaint_escalation",
        "title": "投诉与升级处理",
        "patterns": ["投诉", "升级处理", "不满意", "负责人", "平台介入"],
        "reply": (
            "非常抱歉给你带来不便。你可以先提供订单号、问题经过和诉求，我会优先升级处理。"
            "如平台支持介入，也可同步提交工单以便加速闭环。"
        ),
    },
    {
        "id": "human_support",
        "title": "人工客服",
        "patterns": ["人工", "转人工", "真人客服", "电话客服", "联系人工"],
        "reply": (
            "可以为你转人工继续处理。"
            "请先留下订单号、问题摘要和可联系时间段，我会优先安排客服跟进。"
        ),
    },
]


BUSINESS_HINT_KEYWORDS = {
    "订单",
    "商品",
    "库存",
    "发货",
    "物流",
    "快递",
    "退款",
    "退货",
    "换货",
    "售后",
    "质保",
    "保修",
    "账号",
    "账户",
    "登录",
    "登陆",
    "冻结",
    "密码",
    "验证码",
    "支付",
    "发票",
    "开票",
    "优惠券",
    "优惠",
    "活动",
    "地址",
    "取消订单",
}


def _direct_faq_reply(question: str) -> str | None:
    q = (question or "").strip().lower()
    if not q:
        return None
    for item in DIRECT_FAQ_ITEMS:
        patterns = item.get("patterns", [])
        if not isinstance(patterns, list):
            continue
        if any(str(p).lower() in q for p in patterns):
            return str(item.get("reply", "")).strip() or None
    return None


def _is_business_question(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return False
    if any(k in q for k in BUSINESS_HINT_KEYWORDS):
        return True
    return _direct_faq_reply(q) is not None


def _build_general_fallback_prompt(question: str, history_text: str = "") -> str:
    history_block = f"同一会话近期记录：\n{history_text}\n\n" if history_text else ""
    return (
        "你是中文客服助手。请直接回答用户问题，不要输出推理过程。\n"
        "要求：\n"
        "1) 只用中文；\n"
        "2) 先给结论，再给 1-3 条可执行建议；\n"
        "3) 不确定时明确说明，并建议以官方信息为准；\n"
        "4) 输出控制在 120 字以内。\n\n"
        f"{history_block}"
        f"用户问题：{question}\n\n"
        "请直接输出最终回复："
    )


def _general_model_stream(question: str, timeout_sec: float = 60, history_text: str = "") -> Iterator[str]:
    prompt = _build_general_fallback_prompt(question, history_text=history_text)
    raw = ""
    sent_len = 0
    emitted = False
    try:
        for delta in ollama.chat_stream(prompt, timeout_sec=timeout_sec):
            if not delta:
                continue
            raw += delta
            cleaned = _strip_think_blocks(raw)
            if len(cleaned) <= sent_len:
                continue
            piece = cleaned[sent_len:]
            sent_len = len(cleaned)
            if not piece:
                continue
            if not emitted:
                piece = piece.lstrip()
                if not piece:
                    continue
            emitted = True
            yield piece
    except Exception:
        return


def _general_model_answer(question: str, timeout_sec: float = 60, history_text: str = "") -> str:
    prompt = _build_general_fallback_prompt(question, history_text=history_text)
    try:
        answer = ollama.chat(prompt, timeout_sec=timeout_sec).strip()
    except Exception:
        return ""
    answer = _strip_think_blocks(answer).strip()
    return answer


def _web_rag_stream(question: str, session_id: str | None = None) -> Iterator[str]:
    q = (question or "").strip()
    if not q:
        yield "请先输入你的问题，我会尽快帮你处理。"
        return

    sid = _normalize_session_id(session_id)
    history_text = _render_session_history(_session_get_recent_messages(sid))

    preset = _preset_reply(q)
    if preset:
        yield preset
        return

    direct_reply = _direct_faq_reply(q)
    if direct_reply:
        yield direct_reply
        return

    hits = kb.search(q, top_k=max(settings.top_k, 4), ollama=ollama)
    if not hits or all((h.get("score", 0.0) <= 0.0) for h in hits):
        _record_kb_miss(q, "no_hits", hits)
        _log_event("kb_miss", f"reason=no_hits q={q[:120]}")
        if settings.general_fallback_enabled and not _is_business_question(q):
            emitted = False
            for piece in _general_model_stream(q, timeout_sec=75, history_text=history_text):
                emitted = True
                yield piece
            if emitted:
                return
        yield "目前知识库没有命中信息。你可以补充商品名、订单号或具体场景，我再继续帮你。"
        return
    if not _hits_match_query(q, hits):
        _record_kb_miss(q, "low_relevance", hits)
        _log_event("kb_miss", f"reason=low_relevance q={q[:120]}")
        if settings.general_fallback_enabled and not _is_business_question(q):
            emitted = False
            for piece in _general_model_stream(q, timeout_sec=75, history_text=history_text):
                emitted = True
                yield piece
            if emitted:
                return
        yield "目前知识库里还没有足够匹配的信息。请补充商品名、订单号、时间或问题现象，我会马上继续处理。"
        return

    prompt = _build_web_rag_prompt(q, hits, history_text=history_text)
    emitted = False
    raw = ""
    sent_len = 0
    try:
        for delta in ollama.chat_stream(prompt, timeout_sec=180):
            if not delta:
                continue
            raw += delta
            cleaned = _strip_think_blocks(raw)
            if len(cleaned) <= sent_len:
                continue
            piece = cleaned[sent_len:]
            sent_len = len(cleaned)
            if not piece:
                continue
            if not emitted:
                piece = piece.lstrip()
                if not piece:
                    continue
            emitted = True
            yield piece
    except Exception:
        pass

    if not emitted:
        yield "当前咨询较多，请稍后再试，或换一种更具体的问法。"


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _rag_answer(question: str) -> str:
    preset = _preset_reply(question)
    if preset:
        return preset

    direct_reply = _direct_faq_reply(question)
    if direct_reply:
        return direct_reply

    hits = kb.search(question, top_k=settings.top_k, ollama=ollama)
    if not hits or all((h.get("score", 0.0) <= 0.0) for h in hits):
        _record_kb_miss(question, "no_hits", hits)
        _log_event("kb_miss", f"reason=no_hits q={(question or '')[:120]}")
        if settings.general_fallback_enabled and not _is_business_question(question):
            answer = _general_model_answer(question, timeout_sec=60)
            if answer:
                return answer
        return (
            "\u76ee\u524d\u77e5\u8bc6\u5e93\u6ca1\u6709\u547d\u4e2d\u4fe1\u606f\uff0c"
            "\u8bf7\u8865\u5145\u66f4\u5177\u4f53\u7684\u95ee\u9898\uff0c"
            "\u6216\u5148\u628a\u76f8\u5173 FAQ \u8d44\u6599\u653e\u5165\u77e5\u8bc6\u5e93\u76ee\u5f55\u3002"
        )
    if not _hits_match_query(question, hits):
        _record_kb_miss(question, "low_relevance", hits)
        _log_event("kb_miss", f"reason=low_relevance q={(question or '')[:120]}")
        if settings.general_fallback_enabled and not _is_business_question(question):
            answer = _general_model_answer(question, timeout_sec=60)
            if answer:
                return answer
        return "目前知识库匹配度不够。请补充商品名、订单号、时间或问题现象，我会继续帮您处理。"

    # Keep context short so qwen3:14b can reply within WeChat timeout window.
    selected_hits = hits[:2]
    context = "\n\n".join(
        [f"[{h['source_name']}] {(h['chunk_text'] or '')[:180]}" for h in selected_hits]
    )
    prompt = (
        "\u4f60\u662f\u4f01\u4e1a\u5ba2\u670d\u52a9\u624b\uff0c\u8bf7\u4f9d\u636e\u77e5\u8bc6\u5e93\u56de\u7b54\u7528\u6237\u95ee\u9898\u3002\n"
        "\u8981\u6c42\uff1a\n"
        "1) \u56de\u7b54\u7b80\u6d01\u51c6\u786e\uff1b\n"
        "2) \u5982\u4fe1\u606f\u4e0d\u8db3\uff0c\u660e\u786e\u544a\u77e5\u5e76\u7ed9\u51fa\u4e0b\u4e00\u6b65\u5efa\u8bae\uff1b\n"
        "3) \u4e0d\u8981\u7f16\u9020\u653f\u7b56\u3001\u4ef7\u683c\u6216\u627f\u8bfa\uff1b\n"
        "4) \u8f93\u51fa 2-4 \u53e5\u4e2d\u6587\u5ba2\u670d\u56de\u590d\uff0c\u603b\u957f\u5ea6\u4e0d\u8d85\u8fc7 80 \u5b57\u3002\n\n"
        f"\u77e5\u8bc6\u5e93\u7247\u6bb5\uff1a\n{context}\n\n"
        f"\u7528\u6237\u95ee\u9898\uff1a{question}\n\n"
        "\u8bf7\u76f4\u63a5\u8f93\u51fa\u4e2d\u6587\u5ba2\u670d\u56de\u590d\uff1a"
    )
    try:
        answer = ollama.chat(prompt, timeout_sec=settings.wechat_reply_timeout_sec).strip()
    except Exception:
        return "\u5f53\u524d\u54a8\u8be2\u8f83\u591a\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\uff0c\u6216\u63d0\u4f9b\u66f4\u5177\u4f53\u7684\u95ee\u9898\u3002"
    answer = re.sub(r"<think>[\s\S]*?</think>", "", answer, flags=re.IGNORECASE).strip()
    return answer or "\u6211\u6682\u65f6\u65e0\u6cd5\u751f\u6210\u6709\u6548\u56de\u590d\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002"


def _rag_answer_stream_segments(question: str):
    preset = _preset_reply(question)
    if preset:
        yield preset
        return

    direct_reply = _direct_faq_reply(question)
    if direct_reply:
        yield direct_reply
        return

    hits = kb.search(question, top_k=settings.top_k, ollama=ollama)
    if not hits or all((h.get("score", 0.0) <= 0.0) for h in hits):
        _record_kb_miss(question, "no_hits", hits)
        _log_event("kb_miss", f"reason=no_hits q={(question or '')[:120]}")
        if settings.general_fallback_enabled and not _is_business_question(question):
            emitted = False
            for piece in _general_model_stream(question, timeout_sec=60):
                emitted = True
                yield piece
            if emitted:
                return
        yield (
            "\u76ee\u524d\u77e5\u8bc6\u5e93\u6ca1\u6709\u547d\u4e2d\u4fe1\u606f\uff0c"
            "\u8bf7\u8865\u5145\u66f4\u5177\u4f53\u7684\u95ee\u9898\uff0c"
            "\u6216\u5148\u628a\u76f8\u5173 FAQ \u8d44\u6599\u653e\u5165\u77e5\u8bc6\u5e93\u76ee\u5f55\u3002"
        )
        return
    if not _hits_match_query(question, hits):
        _record_kb_miss(question, "low_relevance", hits)
        _log_event("kb_miss", f"reason=low_relevance q={(question or '')[:120]}")
        if settings.general_fallback_enabled and not _is_business_question(question):
            emitted = False
            for piece in _general_model_stream(question, timeout_sec=60):
                emitted = True
                yield piece
            if emitted:
                return
        yield "目前知识库匹配度不够。请补充商品名、订单号、时间或问题现象，我会继续帮您处理。"
        return

    selected_hits = hits[:2]
    context = "\n\n".join([f"[{h['source_name']}] {(h['chunk_text'] or '')[:180]}" for h in selected_hits])
    prompt = (
        "\u4f60\u662f\u4f01\u4e1a\u5ba2\u670d\u52a9\u624b\uff0c\u8bf7\u4f9d\u636e\u77e5\u8bc6\u5e93\u56de\u7b54\u7528\u6237\u95ee\u9898\u3002\n"
        "\u8981\u6c42\uff1a\n"
        "1) \u56de\u7b54\u7b80\u6d01\u51c6\u786e\uff1b\n"
        "2) \u5982\u4fe1\u606f\u4e0d\u8db3\uff0c\u660e\u786e\u544a\u77e5\u5e76\u7ed9\u51fa\u4e0b\u4e00\u6b65\u5efa\u8bae\uff1b\n"
        "3) \u4e0d\u8981\u7f16\u9020\u653f\u7b56\u3001\u4ef7\u683c\u6216\u627f\u8bfa\uff1b\n"
        "4) \u8f93\u51fa 2-5 \u53e5\u4e2d\u6587\u5ba2\u670d\u56de\u590d\u3002\n\n"
        f"\u77e5\u8bc6\u5e93\u7247\u6bb5\uff1a\n{context}\n\n"
        f"\u7528\u6237\u95ee\u9898\uff1a{question}\n\n"
        "\u8bf7\u76f4\u63a5\u8f93\u51fa\u4e2d\u6587\u5ba2\u670d\u56de\u590d\uff1a"
    )

    buffer = ""
    sent_any = False
    try:
        for delta in ollama.chat_stream(prompt, timeout_sec=max(20, settings.wechat_reply_timeout_sec * 8)):
            if not delta:
                continue
            delta = re.sub(r"<think>[\s\S]*?</think>", "", delta, flags=re.IGNORECASE)
            if not delta:
                continue
            buffer += delta
            while len(buffer) >= 90:
                cut = max(buffer.rfind(p, 0, 90) for p in ("\u3002", "\uff01", "\uff1f", "\n", ".", "!", "?"))
                if cut < 20:
                    cut = 90
                chunk = buffer[:cut].strip()
                buffer = buffer[cut:].lstrip()
                if chunk:
                    sent_any = True
                    yield chunk
        tail = buffer.strip()
        if tail:
            sent_any = True
            yield tail
    except Exception:
        pass

    if not sent_any:
        yield "\u5f53\u524d\u54a8\u8be2\u8f83\u591a\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\uff0c\u6216\u63d0\u4f9b\u66f4\u5177\u4f53\u7684\u95ee\u9898\u3002"


def _send_async_wechat_segments(openid: str, question: str) -> None:
    if not settings.wechat_app_id or not settings.wechat_app_secret:
        _log_event("wechat_async_skip", "missing app credentials")
        return

    try:
        token = get_cached_access_token(settings.wechat_app_id, settings.wechat_app_secret)
    except Exception as exc:
        _log_event("wechat_async_token_fail", str(exc))
        return

    for segment in _rag_answer_stream_segments(question):
        content = (segment or "").strip()
        if not content:
            continue
        if len(content) > 300:
            content = content[:300]
        try:
            ret = send_custom_text_message(
                app_id=settings.wechat_app_id,
                app_secret=settings.wechat_app_secret,
                openid=openid,
                content=content,
                access_token=token,
            )
            if not ret.get("ok"):
                _log_event("wechat_async_send_fail", f"openid={openid} detail={ret.get('detail')}")
                break
            token = str(ret.get("access_token") or token)
            _log_event("wechat_async_send", f"openid={openid} chars={len(content)}")
        except Exception as exc:
            _log_event("wechat_async_send_fail", f"openid={openid} exc={exc}")
            break


def _preset_reply(question: str) -> str | None:
    q = (question or "").strip()
    if not q:
        return None
    compact_q = re.sub(r"[\s，。,.!！？?、~～:：;；\-\(\)（）\[\]【】]+", "", q)

    greeting_keywords = [
        "\u4f60\u597d",
        "\u60a8\u597d",
        "\u54c8\u55bd",
        "\u55e8",
        "\u5728\u5417",
        "\u6709\u4eba\u5417",
        "\u65e9\u4e0a\u597d",
        "\u4e0b\u5348\u597d",
        "\u665a\u4e0a\u597d",
    ]
    thanks_keywords = [
        "\u8c22\u8c22",
        "\u611f\u8c22",
        "\u8f9b\u82e6\u4e86",
        "\u4f60\u771f\u68d2",
        "\u4f60\u5f88\u4e13\u4e1a",
        "\u70b9\u8d5e",
        "\u5938\u5956",
    ]
    complaint_keywords = [
        "\u6295\u8bc9",
        "\u592a\u5dee",
        "\u670d\u52a1\u4e0d\u597d",
        "\u5931\u671b",
        "\u5783\u573e",
        "\u4e0d\u6ee1\u610f",
        "\u6ca1\u7528",
        "\u95ee\u9898\u5f88\u591a",
    ]
    unclear_keywords = [
        "\u6ca1\u542c\u61c2",
        "\u6ca1\u770b\u61c2",
        "\u4e0d\u7406\u89e3",
        "\u4ec0\u4e48\u610f\u601d",
        "\u770b\u4e0d\u61c2",
        "\u4e0d\u4f1a\u7528",
        "\u4e0d\u4f1a\u64cd\u4f5c",
    ]

    if any(k in q for k in greeting_keywords) and len(compact_q) <= 8:
        return "\u4f60\u597d\uff0c\u6211\u662f AI \u5ba2\u670d\u52a9\u624b\uff0c\u8bf7\u95ee\u6709\u4ec0\u4e48\u53ef\u4ee5\u5e2e\u60a8\uff1f"
    if any(k in q for k in thanks_keywords) and len(compact_q) <= 12:
        return "\u4e0d\u5ba2\u6c14\uff0c\u5f88\u9ad8\u5174\u80fd\u5e2e\u5230\u60a8\u3002\u5982\u679c\u8fd8\u6709\u5176\u4ed6\u95ee\u9898\uff0c\u968f\u65f6\u544a\u8bc9\u6211\u3002"
    if any(k in q for k in complaint_keywords):
        return (
            "\u975e\u5e38\u62b1\u6b49\u7ed9\u60a8\u5e26\u6765\u4e0d\u597d\u7684\u4f53\u9a8c\u3002"
            "\u8bf7\u544a\u8bc9\u6211\u5177\u4f53\u95ee\u9898\u548c\u76f8\u5173\u4fe1\u606f\uff0c"
            "\u6211\u4f1a\u7acb\u5373\u534f\u52a9\u5904\u7406\u5e76\u6301\u7eed\u8ddf\u8fdb\u3002"
        )
    if any(k in q for k in unclear_keywords):
        return (
            "\u62b1\u6b49\uff0c\u6211\u8fd8\u6ca1\u6709\u5b8c\u5168\u7406\u89e3\u60a8\u7684\u95ee\u9898\u3002"
            "\u8bf7\u8865\u5145\u5177\u4f53\u573a\u666f\u6216\u5173\u952e\u7ec6\u8282\uff0c"
            "\u6211\u4f1a\u5c3d\u5feb\u4e3a\u60a8\u89e3\u7b54\u3002"
        )
    return None


def _run_kb_sync(trigger: str) -> dict:
    global kb_sync_last_result
    source_dir = Path(settings.kb_source_dir)
    now = datetime.now(timezone.utc).isoformat()

    if not kb_sync_lock.acquire(blocking=False):
        result = {
            "ok": False,
            "trigger": trigger,
            "time": now,
            "detail": "sync already running",
        }
        kb_sync_last_result = result
        return result

    try:
        result = kb.sync_directory(source_dir=source_dir, ollama=ollama)
        result["trigger"] = trigger
        result["time"] = now
        kb_sync_last_result = result
        _log_event("kb_sync", f"trigger={trigger} result={result}")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "trigger": trigger,
            "time": now,
            "detail": f"sync failed: {exc}",
        }
        kb_sync_last_result = result
        _log_event("kb_sync_fail", str(result))
        return result
    finally:
        kb_sync_lock.release()


def _start_interval_sync_if_needed() -> None:
    global kb_sync_thread
    if settings.kb_sync_interval_sec <= 0:
        return
    if kb_sync_thread and kb_sync_thread.is_alive():
        return

    def _loop() -> None:
        _log_event("kb_sync_loop", f"started interval={settings.kb_sync_interval_sec}s")
        while not kb_sync_stop_event.wait(settings.kb_sync_interval_sec):
            _run_kb_sync("interval")
        _log_event("kb_sync_loop", "stopped")

    kb_sync_thread = threading.Thread(target=_loop, daemon=True)
    kb_sync_thread.start()


@app.on_event("startup")
def _on_startup() -> None:
    chat_session_stop_event.clear()
    if settings.kb_auto_sync_on_start:
        _run_kb_sync("startup")
    _start_interval_sync_if_needed()
    _cleanup_expired_sessions()
    _start_session_cleanup_if_needed()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    kb_sync_stop_event.set()
    chat_session_stop_event.set()
    reply_executor.shutdown(wait=False)


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "kb_source_dir": str(Path(settings.kb_source_dir).resolve()),
        "kb_miss_log_path": str(kb_miss_log_path.resolve()),
        "kb_auto_sync_on_start": settings.kb_auto_sync_on_start,
        "kb_sync_interval_sec": settings.kb_sync_interval_sec,
        "wechat_async_stream_reply": settings.wechat_async_stream_reply,
        "general_fallback_enabled": settings.general_fallback_enabled,
        "chat_session_ttl_sec": settings.chat_session_ttl_sec,
        "chat_session_max_turns": settings.chat_session_max_turns,
        "chat_session_cleanup_sec": settings.chat_session_cleanup_sec,
        "chat_session_store_dir": str(session_store_dir.resolve()) if session_store_dir else "",
        "active_chat_sessions": _active_session_count(),
        "web_chat_url": settings.web_chat_url,
        "web_chat_title": settings.web_chat_title,
        "ollama_chat_model": settings.ollama_chat_model,
        "ollama_embed_model": settings.ollama_embed_model,
        "chunk_overlap_chars": settings.chunk_overlap_chars,
        "retrieval_candidates": settings.retrieval_candidates,
        "hybrid_dense_weight": settings.hybrid_dense_weight,
        "hybrid_bm25_weight": settings.hybrid_bm25_weight,
    }


@app.get("/")
def web_home() -> Response:
    if web_root.exists() and (web_root / "index.html").exists():
        return FileResponse(web_root / "index.html")
    return PlainTextResponse("Web chat is not ready.", status_code=404)


@app.get("/chat")
def web_chat_page() -> Response:
    if web_root.exists() and (web_root / "index.html").exists():
        return FileResponse(web_root / "index.html")
    return PlainTextResponse("Web chat is not ready.", status_code=404)


@app.get("/api/faq/list")
def api_faq_list() -> dict:
    items = [
        {
            "id": str(item.get("id", "")),
            "title": str(item.get("title", "")),
            "reply": str(item.get("reply", "")),
        }
        for item in DIRECT_FAQ_ITEMS
    ]
    return {"ok": True, "items": items}


@app.post("/api/chat")
def api_chat(req: WebChatRequest) -> dict:
    q = (req.message or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="message is required")
    sid = _ensure_session_id(req.session_id)
    chunks = list(_web_rag_stream(q, session_id=sid))
    answer = "".join(chunks).strip()
    if answer:
        _session_append_turn(sid, q, answer)
    return {"ok": True, "answer": answer, "session_id": sid}


@app.post("/api/chat/stream")
def api_chat_stream(req: WebChatRequest) -> StreamingResponse:
    q = (req.message or "").strip()
    sid = _ensure_session_id(req.session_id)

    def _iter():
        if not q:
            yield _sse("error", {"message": "message is required"})
            yield _sse("done", {"answer": ""})
            return
        yield _sse("meta", {"title": settings.web_chat_title, "session_id": sid})
        full = ""
        for chunk in _web_rag_stream(q, session_id=sid):
            full += chunk
            yield _sse("chunk", {"text": chunk})
        answer = full.strip()
        if answer:
            _session_append_turn(sid, q, answer)
        yield _sse("done", {"answer": answer, "session_id": sid})

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_iter(), media_type="text/event-stream", headers=headers)


@app.get("/wechat/access-token")
def wechat_access_token(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> Response:
    _admin_guard(x_admin_token)
    try:
        data = get_access_token(settings.wechat_app_id, settings.wechat_app_secret)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"wechat api request failed: {exc}") from exc

    if "access_token" in data:
        token = data["access_token"]
        masked = f"{token[:8]}...{token[-6:]}" if len(token) > 16 else "***"
        return JSONResponse({"ok": True, "access_token_masked": masked, "expires_in": data.get("expires_in")})
    return JSONResponse({"ok": False, "wechat_response": data}, status_code=400)


@app.post("/kb/upload")
async def kb_upload(
    file: UploadFile = File(...),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict:
    _admin_guard(x_admin_token)
    suffix = Path(file.filename or "upload.bin").suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
        content = await file.read()
        tmp.write(content)

    try:
        text = extract_text_from_file(tmp_path, ollama=ollama)
        if not text.strip():
            raise HTTPException(status_code=400, detail="No text extracted from file")
        chunks = kb.add_document(source_name=file.filename or "uploaded_file", text=text, ollama=ollama)
        return {"ok": True, "source": file.filename, "chunks": chunks}
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@app.post("/kb/sync")
def kb_sync(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict:
    _admin_guard(x_admin_token)
    return _run_kb_sync("manual_api")


@app.get("/kb/sync/status")
def kb_sync_status(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict:
    _admin_guard(x_admin_token)
    return kb_sync_last_result


@app.get("/kb/sources")
def kb_sources(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    _admin_guard(x_admin_token)
    return {"ok": True, "sources": kb.list_sources(limit=limit)}


@app.get("/kb/miss/recent")
def kb_miss_recent(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    _admin_guard(x_admin_token)
    if not kb_miss_log_path.exists():
        return {"ok": True, "items": []}

    tail: deque[str] = deque(maxlen=limit)
    with kb_miss_log_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line:
                tail.append(line)

    items = []
    for line in reversed(tail):
        try:
            items.append(json.loads(line))
        except Exception:
            items.append({"raw": line})
    return {"ok": True, "items": items}


@app.get("/kb/miss/top")
def kb_miss_top(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    limit: int = Query(20, ge=1, le=200),
) -> dict:
    _admin_guard(x_admin_token)
    if not kb_miss_log_path.exists():
        return {"ok": True, "items": []}

    counter: dict[str, int] = {}
    latest: dict[str, dict] = {}
    with kb_miss_log_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            question = str(item.get("question", "")).strip()
            if not question:
                continue
            counter[question] = counter.get(question, 0) + 1
            latest[question] = item

    ranked = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    items = []
    for question, cnt in ranked:
        meta = latest.get(question, {})
        items.append(
            {
                "question": question,
                "count": cnt,
                "last_time": meta.get("time", ""),
                "last_reason": meta.get("reason", ""),
            }
        )
    return {"ok": True, "items": items}


@app.get("/kb/query")
def kb_query(q: str = Query(..., min_length=1)) -> dict:
    hits = kb.search(q, top_k=settings.top_k, ollama=ollama)
    answer = _rag_answer(q)
    return {"query": q, "hits": hits, "answer": answer}


@app.get("/wechat")
def wechat_verify(
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
    signature: str | None = Query(default=None),
    msg_signature: str | None = Query(default=None),
) -> Response:
    # Plaintext mode
    if signature and verify_signature(settings.wechat_token, signature, timestamp, nonce):
        return PlainTextResponse(echostr)

    # Encrypted mode
    if msg_signature and settings.wechat_encoding_aes_key:
        if not verify_msg_signature(settings.wechat_token, msg_signature, timestamp, nonce, echostr):
            return PlainTextResponse("invalid signature", status_code=401)
        try:
            plain_echostr = decrypt_wechat_message(
                echostr,
                settings.wechat_encoding_aes_key,
                settings.wechat_app_id,
            )
        except Exception:
            return PlainTextResponse("decrypt failed", status_code=400)
        return PlainTextResponse(plain_echostr)

    return PlainTextResponse("invalid signature", status_code=401)


@app.post("/wechat")
async def wechat_callback(
    request: Request,
    timestamp: str = Query(...),
    nonce: str = Query(...),
    signature: str | None = Query(default=None),
    msg_signature: str | None = Query(default=None),
) -> Response:
    t0 = time.perf_counter()
    body = (await request.body()).decode("utf-8", errors="ignore")
    encrypted_mode = False

    try:
        xml_data = parse_wechat_xml(body)
    except Exception:
        _log_event("wechat_parse_fail", body[:300])
        return PlainTextResponse("invalid xml", status_code=400)

    if signature and verify_signature(settings.wechat_token, signature, timestamp, nonce):
        msg = xml_data
    elif msg_signature and "Encrypt" in xml_data:
        encrypted_mode = True
        encrypted = xml_data.get("Encrypt", "")
        if not settings.wechat_encoding_aes_key:
            _log_event("wechat_encrypt_missing_key", "")
            return PlainTextResponse("missing aes key", status_code=500)
        if not verify_msg_signature(settings.wechat_token, msg_signature, timestamp, nonce, encrypted):
            _log_event("wechat_invalid_msg_signature", "")
            return PlainTextResponse("invalid signature", status_code=401)
        try:
            plain_xml = decrypt_wechat_message(encrypted, settings.wechat_encoding_aes_key, settings.wechat_app_id)
            msg = parse_wechat_xml(plain_xml)
        except Exception as exc:
            _log_event("wechat_decrypt_fail", str(exc))
            return PlainTextResponse("decrypt failed", status_code=400)
    else:
        _log_event("wechat_invalid_signature", f"query={dict(request.query_params)}")
        return PlainTextResponse("invalid signature", status_code=401)

    msg_type = msg.get("MsgType", "")
    from_user = msg.get("FromUserName", "")
    to_user = msg.get("ToUserName", "")
    question = msg.get("Content", "").strip()
    web_url = _resolve_web_chat_url(request)
    if msg_type == "text" and question:
        reply = (
            "您好，已收到您的咨询。为了给您更完整、连续的回复，请进入网页客服继续沟通：\n"
            f"{web_url}\n"
            "进入后可直接提问，我会实时回复。"
        )
    else:
        reply = (
            "您好，欢迎咨询。为了给您更完整、连续的回复，请进入网页客服：\n"
            f"{web_url}\n"
            "进入后可直接提问，我会实时回复。"
        )

    plain_reply = build_text_reply(to_user=from_user, from_user=to_user, content=reply)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    _log_event(
        "wechat_reply",
        f"mode={'encrypted' if encrypted_mode else 'plain'} type={msg_type} from={from_user} elapsed_ms={elapsed_ms}",
    )

    if not encrypted_mode:
        return Response(content=plain_reply, media_type="text/xml")

    try:
        encrypted_reply = build_encrypted_reply(
            plain_reply,
            token=settings.wechat_token,
            encoding_aes_key=settings.wechat_encoding_aes_key,
            app_id=settings.wechat_app_id,
            timestamp=timestamp,
            nonce=nonce,
        )
    except Exception as exc:
        _log_event("wechat_encrypt_reply_fail", str(exc))
        return PlainTextResponse("encrypt reply failed", status_code=500)
    return Response(content=encrypted_reply, media_type="text/xml")
