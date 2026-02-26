from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import threading
import time

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

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
from app.wechat_api import get_access_token


app = FastAPI(title="WeiXinAgent", version="1.0.0")
kb = KnowledgeBase(settings.kb_db_path, max_chunk_chars=settings.max_chunk_chars)
ollama = OllamaClient(
    base_url=settings.ollama_base_url,
    chat_model=settings.ollama_chat_model,
    embed_model=settings.ollama_embed_model,
    vision_model=settings.ollama_vision_model,
)

event_log_path = Path("data/events.log")
event_log_path.parent.mkdir(parents=True, exist_ok=True)
kb_sync_lock = threading.Lock()
kb_sync_stop_event = threading.Event()
kb_sync_thread: threading.Thread | None = None
kb_sync_last_result: dict = {"ok": False, "detail": "not started"}


def _log_event(kind: str, detail: str = "") -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} [{kind}] {detail}\n"
    with event_log_path.open("a", encoding="utf-8") as f:
        f.write(line)


def _admin_guard(x_admin_token: str | None) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN is not configured")
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _rag_answer(question: str) -> str:
    preset = _preset_reply(question)
    if preset:
        return preset

    hits = kb.search(question, top_k=settings.top_k, ollama=ollama)
    if not hits or all((h.get("score", 0.0) <= 0.0) for h in hits):
        return "目前知识库没有命中信息，请补充更具体问题，或先把相关FAQ资料放入知识库目录。"

    context = "\n\n".join([f"[{h['source_name']}] {h['chunk_text']}" for h in hits])
    prompt = (
        "你是企业客服助手，请依据知识库回答用户问题。\n"
        "要求：\n"
        "1) 回答简洁准确；\n"
        "2) 如果知识库不足，请明确告知并给出下一步建议；\n"
        "3) 不要编造政策、价格、承诺。\n\n"
        f"知识库片段：\n{context}\n\n"
        f"用户问题：{question}\n\n"
        "请直接给出中文答复："
    )
    try:
        answer = ollama.chat(prompt, timeout_sec=settings.wechat_reply_timeout_sec).strip()
    except Exception:
        return "当前咨询较多，请稍后再试，或提供更具体的问题。"
    answer = re.sub(r"<think>[\s\S]*?</think>", "", answer, flags=re.IGNORECASE).strip()
    return answer or "我暂时无法生成有效回复，请稍后再试。"


def _preset_reply(question: str) -> str | None:
    q = (question or "").strip()
    if not q:
        return None

    greeting_keywords = ["你好", "您好", "哈喽", "嗨", "在吗", "有人吗", "早上好", "下午好", "晚上好"]
    thanks_keywords = ["谢谢", "感谢", "辛苦了", "你真棒", "你很专业", "点赞", "夸奖"]
    complaint_keywords = ["投诉", "太差", "服务不好", "失望", "垃圾", "不满意", "你没用", "问题很多"]
    unclear_keywords = ["没听懂", "没看懂", "不理解", "什么意思", "看不懂", "不会用", "不会操作"]

    if any(k in q for k in greeting_keywords):
        return "你好，我是摩斯象限的AI客服，请问有什么可以帮助您的？"
    if any(k in q for k in thanks_keywords):
        return "不客气，很高兴能帮到您。如果还有其他问题，随时告诉我。"
    if any(k in q for k in complaint_keywords):
        return "非常抱歉给您带来不好的体验。请您告诉我具体问题和相关信息，我会立即协助处理并持续跟进。"
    if any(k in q for k in unclear_keywords):
        return "很抱歉，我没有完全理解您的问题。请您补充一下具体场景或关键细节，我会尽快为您解答。"
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
    if settings.kb_auto_sync_on_start:
        _run_kb_sync("startup")
    _start_interval_sync_if_needed()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    kb_sync_stop_event.set()


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "kb_source_dir": str(Path(settings.kb_source_dir).resolve()),
        "kb_auto_sync_on_start": settings.kb_auto_sync_on_start,
        "kb_sync_interval_sec": settings.kb_sync_interval_sec,
    }


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

    if msg_type != "text":
        reply = "你好，欢迎咨询。当前机器人优先支持文本问答。"
    else:
        reply = _rag_answer(question) if question else "请输入你的问题。"

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
