from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CATEGORY_LABELS = {
    "phone": "手机",
    "tablet": "平板",
    "laptop": "笔记本",
    "watch": "手表",
}

_CATEGORY_KEYWORDS = {
    "phone": ["手机", "iphone", "苹果手机", "拍照手机"],
    "tablet": ["平板", "ipad", "板子", "手写笔", "学习机"],
    "laptop": ["笔记本", "电脑", "macbook", "mac"],
    "watch": ["手表", "watch", "智能表", "运动表"],
}

_RECOMMEND_INTENT_KEYWORDS = [
    "推荐",
    "买哪个",
    "选哪个",
    "哪款",
    "哪一款",
    "适合",
    "预算",
    "价位",
    "对比",
    "比较",
]

_COMPARE_INTENT_KEYWORDS = [
    "差异",
    "区别",
    "对比",
    "比较",
    "优缺点",
    "怎么选",
    "推荐哪款",
    "推荐哪一个",
    "哪个更好",
    "哪个更适合",
    "这三款",
    "这几款",
    "排序",
    "排个序",
]

_LOW_BUDGET_HINTS = ["预算不多", "预算有限", "预算不高", "便宜点", "便宜一些", "省钱", "划算", "性价比"]
_HIGH_BUDGET_HINTS = ["预算充足", "不差钱", "高预算", "旗舰", "顶配", "最强"]

_FEATURE_KEYWORDS = {
    "camera": ["拍照", "摄影", "录像", "视频拍摄", "vlog", "影像", "长焦", "夜景"],
    "battery": ["续航", "电池", "待机", "充电频率", "一天一充"],
    "performance": ["性能", "游戏", "剪辑", "视频剪辑", "剪视频", "渲染", "本地模型", "开发", "高刷"],
    "portable": ["轻薄", "便携", "轻便", "通勤", "重量"],
    "productivity": ["办公", "文档", "会议", "生产力", "学习", "记笔记"],
    "fitness": ["运动", "跑步", "骑行", "健身", "户外", "潜水"],
    "health": ["健康", "睡眠", "心率", "血氧", "体征"],
    "large_screen": ["大屏", "屏幕大", "观影", "追剧"],
    "small_screen": ["小屏", "单手", "屏幕小", "轻巧"],
    "value": ["性价比", "实惠", "便宜", "划算", "入门"],
}

_RETURN_POLICY_KEYWORDS = ["退货", "退款", "退换", "无理由", "退换货", "退款流程"]
_WARRANTY_KEYWORDS = ["保修", "三包", "质保", "过保", "applecare", "apple care", "applecare+"]
_SERVICE_FLOW_KEYWORDS = ["维修", "送修", "售后", "故障", "返厂", "检修", "服务流程"]

# These intents should break product-recommendation context and return to general FAQ flow.
_CONTEXT_BREAKER_KEYWORDS = [
    "账号",
    "账户",
    "登录",
    "登陆",
    "密码",
    "验证码",
    "注册",
    "解绑",
    "订单",
    "物流",
    "快递",
    "发票",
    "开票",
    "优惠券",
    "支付",
    "投诉",
    "人工客服",
    "转人工",
]

_FOLLOWUP_HINT_KEYWORDS = [
    "继续",
    "请继续",
    "再说",
    "再细",
    "再详细",
    "细致",
    "详细",
    "参数",
    "规格",
    "差异",
    "区别",
    "优缺点",
    "这款",
    "那款",
    "这三款",
    "这几款",
    "哪款",
    "哪个",
    "推荐",
    "怎么选",
    "对比",
    "比较",
    "预算",
    "价格",
    "容量",
    "内存",
    "拍照",
    "续航",
    "性能",
    "屏幕",
    "iphone",
    "ipad",
    "macbook",
    "watch",
]


def _normalize_text(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def _format_price(value: int | float | None) -> str:
    if value is None:
        return "价格以官网配置页为准"
    return f"RMB {int(value):,} 起"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _to_cny(number_text: str, unit_text: str) -> int | None:
    try:
        number = float(number_text)
    except Exception:
        return None
    unit = (unit_text or "").strip().lower()
    if unit in {"w", "万"}:
        number *= 10000
    elif unit in {"k", "千"}:
        number *= 1000
    elif unit in {"", "元"}:
        pass
    else:
        return None
    value = int(number)
    if value < 300:
        return None
    if value > 300000:
        return None
    return value


def _parse_budget_range(question: str) -> tuple[int | None, int | None]:
    q = (question or "").lower()
    range_pattern = re.compile(
        r"(\d+(?:\.\d+)?)\s*(万|w|千|k|元)?\s*(?:-|~|～|到|至)\s*(\d+(?:\.\d+)?)\s*(万|w|千|k|元)?"
    )
    m = range_pattern.search(q)
    if m:
        left = _to_cny(m.group(1), m.group(2) or "")
        right = _to_cny(m.group(3), m.group(4) or m.group(2) or "")
        if left and right:
            return (min(left, right), max(left, right))

    values: list[int] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(万|w|千|k|元)?", q):
        value = _to_cny(m.group(1), m.group(2) or "")
        if value is None:
            continue
        values.append(value)
    if not values:
        return (None, None)

    max_words = ["以内", "以下", "不超过", "最多", "封顶", "预算"]
    min_words = ["以上", "不少于", "至少", "起步", "不低于"]

    if any(w in q for w in max_words):
        return (None, max(values))
    if any(w in q for w in min_words):
        return (min(values), None)

    # Approximate budget expression, e.g. "4k左右/上下/大概".
    approx_words = ["左右", "上下", "约", "大概", "差不多"]
    if len(values) == 1 and any(w in q for w in approx_words):
        val = values[0]
        return (int(val * 0.85), int(val * 1.15))

    # Single "预算X" without qualifier: default as upper bound.
    if len(values) == 1 and "预算" in q:
        return (None, values[0])

    return (None, None)


def _parse_storage_gb(question: str) -> int | None:
    q = (question or "").lower()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(tb|t|gb|g)", q):
        value = float(m.group(1))
        unit = m.group(2)
        if unit in {"tb", "t"}:
            return int(value * 1024)
        return int(value)

    if any(k in q for k in ["内存", "容量", "存储"]):
        for m in re.finditer(r"\b(64|128|256|512|1024|2048)\b", q):
            return int(m.group(1))
    return None


def _parse_screen_requirement(question: str) -> tuple[float | None, str | None]:
    q = (question or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(寸|英寸|inch|in)", q)
    if m:
        try:
            return (float(m.group(1)), "target")
        except Exception:
            return (None, None)

    if any(k in q for k in ["小屏", "单手", "轻巧"]):
        return (None, "small")
    if any(k in q for k in ["大屏", "屏幕大", "观影", "追剧"]):
        return (None, "large")
    return (None, None)


def _detect_feature_tags(question: str) -> list[str]:
    q = (question or "").lower()
    tags: list[str] = []
    for tag, keywords in _FEATURE_KEYWORDS.items():
        if any(k in q for k in keywords):
            tags.append(tag)
    return tags


def _detect_category(question: str) -> str | None:
    q = (question or "").lower()
    scored: dict[str, int] = {}
    for category, keywords in _CATEGORY_KEYWORDS.items():
        hits = sum(1 for k in keywords if k in q)
        if hits > 0:
            scored[category] = hits
    if not scored:
        return None
    return max(scored.items(), key=lambda kv: kv[1])[0]


@dataclass
class _CatalogState:
    loaded: bool
    mtime: float
    products: list[dict[str, Any]]
    aliases: list[tuple[str, int]]
    meta: dict[str, Any]
    after_sales: dict[str, Any]


class ProductCatalog:
    def __init__(self, catalog_path: str, enabled: bool = True) -> None:
        self.catalog_path = Path(catalog_path).expanduser().resolve()
        self.enabled = enabled
        self._lock = threading.Lock()
        self._state = _CatalogState(False, -1.0, [], [], {}, {})

    def stats(self) -> dict[str, Any]:
        self._load_if_needed()
        return {
            "enabled": self.enabled,
            "loaded": self._state.loaded,
            "path": str(self.catalog_path),
            "product_count": len(self._state.products),
            "verified_on": str(self._state.meta.get("verified_on", "")),
        }

    def is_product_question(self, question: str) -> bool:
        self._load_if_needed()
        q = (question or "").lower().strip()
        if not q:
            return False
        if any(k in q for k in _RECOMMEND_INTENT_KEYWORDS + _COMPARE_INTENT_KEYWORDS):
            return True
        if _detect_category(q):
            return True
        if any(k in q for k in _RETURN_POLICY_KEYWORDS + _WARRANTY_KEYWORDS + _SERVICE_FLOW_KEYWORDS):
            return True
        return self._match_product_index(q) is not None

    def answer(self, question: str, recent_messages: list[dict] | None = None) -> str | None:
        resolved = self.resolve(question=question, recent_messages=recent_messages)
        if not resolved:
            return None
        return str(resolved.get("reply", "")).strip() or None

    def resolve(self, question: str, recent_messages: list[dict] | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        q = (question or "").strip()
        if not q:
            return None
        self._load_if_needed()
        if not self._state.loaded:
            return None

        policy_reply = self._policy_reply(q)
        if policy_reply:
            return {
                "mode": "policy",
                "question": q,
                "reply": policy_reply,
                "profile": {},
                "candidates": [],
                "category": None,
                "needs_llm": False,
            }

        # If user switches to account/login/order topics, stop inheriting product context.
        if self._is_context_breaker_question(q):
            return None

        profile = self._merge_profile(q, recent_messages)
        recent_models = self._models_from_recent_messages(recent_messages, limit=6)
        question_model_indices = self._match_model_indices(q, limit=5)
        if not profile.get("category"):
            history_category = self._infer_category_from_models(recent_models)
            if history_category:
                profile["category"] = history_category
                profile["category_from_history"] = True

        comparison_intent = self._looks_like_comparison(q) or len(question_model_indices) >= 2
        if comparison_intent:
            compare_candidates = self._prepare_compare_candidates(q, recent_models, profile)
            if len(compare_candidates) >= 2:
                ranked = self._rank_products(compare_candidates, profile=profile, allow_over_budget=True)
                compare_rows = [self._candidate_clean(c) for c in ranked[:3]]
                return {
                    "mode": "compare",
                    "question": q,
                    "reply": self._build_comparison_reply(compare_rows, profile),
                    "profile": profile,
                    "candidates": compare_rows,
                    "category": profile.get("category"),
                    "needs_llm": True,
                }

        product_idx = question_model_indices[0] if question_model_indices else None
        if product_idx is not None:
            product = self._state.products[product_idx]
            return {
                "mode": "detail",
                "question": q,
                "reply": self._build_product_detail_reply(product),
                "profile": {},
                "candidates": [self._candidate_from_product(product, score=0.0, reasons=["命中具体机型"])],
                "category": str(product.get("category", "")),
                "needs_llm": False,
            }

        if self._looks_like_recommendation_with_profile(q, profile):
            category = str(profile.get("category") or "").strip()
            base_products = self._state.products
            if category:
                base_products = [p for p in base_products if str(p.get("category", "")) == category]
            else:
                allow_categories = self._infer_candidate_categories(q)
                if allow_categories:
                    base_products = [
                        p for p in base_products if str(p.get("category", "")) in allow_categories
                    ]
            if not base_products:
                return {
                    "mode": "recommend",
                    "question": q,
                    "reply": "我还没找到对应品类的数据，请告诉我你想看手机、平板、笔记本还是手表。",
                    "profile": profile,
                    "candidates": [],
                    "category": category or None,
                    "needs_llm": False,
                }

            ranked = self._rank_products(base_products, profile=profile, allow_over_budget=False)
            if not ranked and category and profile.get("max_budget") is None:
                ranked = self._rank_products(base_products, profile=profile, allow_over_budget=True)
            if not ranked:
                low = min(
                    (
                        int(p.get("starting_price_cny"))
                        for p in base_products
                        if isinstance(p.get("starting_price_cny"), (int, float))
                    ),
                    default=None,
                )
                if low is not None:
                    return {
                        "mode": "recommend",
                        "question": q,
                        "reply": (
                            f"按当前条件没有完全匹配机型。你可以把预算提高到约 RMB {low:,} 起，"
                            "或放宽容量/屏幕要求，我可以继续给你细化推荐。"
                        ),
                        "profile": profile,
                        "candidates": [],
                        "category": category or None,
                        "needs_llm": False,
                    }
                return {
                    "mode": "recommend",
                    "question": q,
                    "reply": "按当前条件没有匹配机型。你可以补充预算、用途和容量需求，我继续帮你筛选。",
                    "profile": profile,
                    "candidates": [],
                    "category": category or None,
                    "needs_llm": False,
                }

            top = [self._candidate_clean(c) for c in ranked[:3]]
            return {
                "mode": "recommend",
                "question": q,
                "reply": self._build_rich_recommendation_reply(top, profile),
                "profile": profile,
                "candidates": top,
                "category": profile.get("category"),
                "needs_llm": True,
            }

        return None

    def _load_if_needed(self) -> None:
        with self._lock:
            if not self.enabled:
                self._state = _CatalogState(False, -1.0, [], [], {}, {})
                return
            if not self.catalog_path.exists():
                self._state = _CatalogState(False, -1.0, [], [], {}, {})
                return

            try:
                mtime = self.catalog_path.stat().st_mtime
            except Exception:
                self._state = _CatalogState(False, -1.0, [], [], {}, {})
                return

            if self._state.loaded and self._state.mtime == mtime:
                return

            try:
                payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            except Exception:
                self._state = _CatalogState(False, -1.0, [], [], {}, {})
                return

            products = payload.get("products", [])
            if not isinstance(products, list):
                products = []

            aliases: list[tuple[str, int]] = []
            for idx, product in enumerate(products):
                if not isinstance(product, dict):
                    continue
                model = str(product.get("model", "")).strip()
                if model:
                    aliases.append((_normalize_text(model), idx))
                for raw_alias in product.get("aliases", []) if isinstance(product.get("aliases", []), list) else []:
                    alias = _normalize_text(str(raw_alias))
                    if alias:
                        aliases.append((alias, idx))

            self._state = _CatalogState(
                loaded=True,
                mtime=mtime,
                products=[p for p in products if isinstance(p, dict)],
                aliases=aliases,
                meta=payload.get("meta", {}) if isinstance(payload.get("meta", {}), dict) else {},
                after_sales=payload.get("after_sales", {}) if isinstance(payload.get("after_sales", {}), dict) else {},
            )

    def _collect_user_texts(self, question: str, recent_messages: list[dict] | None) -> list[str]:
        texts: list[str] = [question]
        if not recent_messages:
            return texts
        for msg in reversed(recent_messages):
            if str(msg.get("role", "")) != "user":
                continue
            content = str(msg.get("content", "")).strip()
            if not content or content == question:
                continue
            texts.append(content)
            if len(texts) >= 5:
                break
        return texts

    def _merge_profile(self, question: str, recent_messages: list[dict] | None) -> dict[str, Any]:
        texts = self._collect_user_texts(question, recent_messages)
        category: str | None = None
        category_from_history = False
        min_budget: int | None = None
        max_budget: int | None = None
        required_storage: int | None = None
        screen_target: float | None = None
        screen_mode: str | None = None
        tags: list[str] = []
        budget_style = "neutral"

        for idx, text in enumerate(texts):
            c = _detect_category(text)
            if c and category is None:
                category = c
                if idx > 0:
                    category_from_history = True

            low, high = _parse_budget_range(text)
            if idx == 0:
                if low is not None:
                    min_budget = low
                if high is not None:
                    max_budget = high
            else:
                if min_budget is None and low is not None:
                    min_budget = low
                if max_budget is None and high is not None:
                    max_budget = high

            storage = _parse_storage_gb(text)
            if storage is not None and required_storage is None:
                required_storage = storage

            target, mode = _parse_screen_requirement(text)
            if screen_target is None and target is not None:
                screen_target = target
            if screen_mode is None and mode is not None:
                screen_mode = mode

            for tag in _detect_feature_tags(text):
                if tag not in tags:
                    tags.append(tag)

            low_text = text.lower()
            if any(w in low_text for w in _LOW_BUDGET_HINTS):
                budget_style = "low"
            if any(w in low_text for w in _HIGH_BUDGET_HINTS):
                budget_style = "high"

        if "value" in tags and budget_style == "neutral":
            budget_style = "low"

        return {
            "category": category,
            "category_from_history": category_from_history,
            "min_budget": min_budget,
            "max_budget": max_budget,
            "required_storage": required_storage,
            "screen_target": screen_target,
            "screen_mode": screen_mode,
            "tags": tags,
            "budget_style": budget_style,
        }

    def _match_model_indices(self, text: str, limit: int = 6) -> list[int]:
        normalized = _normalize_text(text)
        if not normalized:
            return []
        rows: list[tuple[int, int, int]] = []
        for idx, product in enumerate(self._state.products):
            keys: list[str] = []
            model = _normalize_text(str(product.get("model", "")))
            if model:
                keys.append(model)
            aliases = product.get("aliases", [])
            if isinstance(aliases, list):
                for item in aliases:
                    alias = _normalize_text(str(item))
                    if alias:
                        keys.append(alias)
            best_pos: int | None = None
            best_len = 0
            for key in keys:
                if len(key) < 3:
                    continue
                pos = normalized.find(key)
                if pos < 0:
                    continue
                if best_pos is None or pos < best_pos or (pos == best_pos and len(key) > best_len):
                    best_pos = pos
                    best_len = len(key)
            if best_pos is not None:
                rows.append((best_pos, -best_len, idx))
        rows.sort(key=lambda x: (x[0], x[1], x[2]))
        result: list[int] = []
        seen: set[int] = set()
        for _, _, idx in rows:
            if idx in seen:
                continue
            seen.add(idx)
            result.append(idx)
            if len(result) >= max(1, limit):
                break
        return result

    def _models_from_recent_messages(self, recent_messages: list[dict] | None, limit: int = 6) -> list[dict[str, Any]]:
        if not recent_messages:
            return []
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for msg in reversed(recent_messages):
            text = str(msg.get("content", "")).strip()
            if not text:
                continue
            for idx in self._match_model_indices(text, limit=6):
                product = self._state.products[idx]
                row = self._candidate_from_product(product, score=0.0, reasons=["会话历史提及"])
                pid = str(row.get("id", ""))
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                rows.append(row)
                if len(rows) >= max(1, limit):
                    return rows
        return rows

    def _infer_category_from_models(self, candidates: list[dict[str, Any]]) -> str | None:
        counter: dict[str, int] = {}
        for c in candidates:
            cat = str(c.get("category", "")).strip()
            if not cat:
                continue
            counter[cat] = counter.get(cat, 0) + 1
        if not counter:
            return None
        return max(counter.items(), key=lambda kv: kv[1])[0]

    def _infer_candidate_categories(self, question: str) -> set[str]:
        q = (question or "").lower()
        if any(k in q for k in ["手表", "watch", "运动", "健康", "睡眠", "心率"]):
            return {"watch"}
        if any(k in q for k in ["平板", "ipad"]):
            return {"tablet"}
        if any(k in q for k in ["手机", "iphone", "拍照", "通话"]):
            return {"phone"}
        if any(k in q for k in ["电脑", "笔记本", "macbook", "开发", "办公", "剪辑", "剪视频", "生产力"]):
            return {"laptop", "tablet"}
        # Generic entertainment/consumption scenarios: exclude watch by default.
        if any(k in q for k in ["看剧", "观影", "电影", "影音", "娱乐", "游戏", "屏幕", "便携", "出差"]):
            return {"phone", "tablet", "laptop"}
        # Default: device purchase usually means phone/tablet first.
        return {"phone", "tablet", "laptop"}

    def _is_context_breaker_question(self, question: str) -> bool:
        q = (question or "").lower().strip()
        if not q:
            return False
        return any(k in q for k in _CONTEXT_BREAKER_KEYWORDS)

    def _has_followup_signal(self, question: str) -> bool:
        q = (question or "").lower().strip()
        if not q:
            return False
        return any(k in q for k in _FOLLOWUP_HINT_KEYWORDS)

    def _match_product_index(self, question: str) -> int | None:
        matched = self._match_model_indices(question, limit=1)
        return matched[0] if matched else None

    def _looks_like_recommendation(self, question: str) -> bool:
        q = (question or "").lower()
        if any(k in q for k in _RECOMMEND_INTENT_KEYWORDS):
            return True
        if _detect_category(q) and any(k in q for k in ["多少钱", "价格", "价位", "预算", "适合"]):
            return True
        min_budget, max_budget = _parse_budget_range(q)
        if min_budget is not None or max_budget is not None:
            return True
        if _parse_storage_gb(q) is not None:
            return True
        return False

    def _policy_reply(self, question: str) -> str | None:
        q = (question or "").lower()
        after_sales = self._state.after_sales
        if not after_sales:
            return None

        if any(k in q for k in _RETURN_POLICY_KEYWORDS):
            items = after_sales.get("return_refund", [])
            if not isinstance(items, list) or not items:
                return None
            return (
                "苹果售后-退货退款要点：\n"
                + "\n".join(f"{i + 1}. {str(item)}" for i, item in enumerate(items[:3]))
                + "\n说明：最终以 Apple 官方条款与订单页面为准。"
            )

        if any(k in q for k in _WARRANTY_KEYWORDS):
            items = after_sales.get("warranty", [])
            if not isinstance(items, list) or not items:
                return None
            return (
                "苹果售后-保修要点：\n"
                + "\n".join(f"{i + 1}. {str(item)}" for i, item in enumerate(items[:3]))
                + "\n说明：不同产品和地区可能存在细则差异。"
            )

        if any(k in q for k in _SERVICE_FLOW_KEYWORDS):
            items = after_sales.get("service_flow", [])
            if not isinstance(items, list) or not items:
                return None
            return (
                "苹果售后-维修流程建议：\n"
                + "\n".join(f"{i + 1}. {str(item)}" for i, item in enumerate(items[:3]))
                + "\n你可以把设备型号和故障现象发我，我帮你走下一步。"
            )

        return None

    def _build_product_detail_reply(self, product: dict[str, Any]) -> str:
        model = str(product.get("model", "该机型")).strip() or "该机型"
        category = _CATEGORY_LABELS.get(str(product.get("category", "")), "设备")
        price = product.get("starting_price_cny")
        price_note = str(product.get("price_note", "")).strip()
        screen = product.get("screen_size_inch")
        chip = str(product.get("chip", "") or "以官方技术规格页为准").strip()
        storage = product.get("storage_options_gb", [])
        battery_hours = product.get("battery_video_playback_hours")
        use_cases = product.get("use_cases", [])
        buy_url = str(product.get("buy_url", "")).strip()
        specs_url = str(product.get("specs_url", "")).strip()
        verified_on = str(self._state.meta.get("verified_on", "")).strip()

        parts: list[str] = [f"{model}（{category}）"]
        parts.append(f"- 参考价格：{_format_price(price)}")
        if price_note:
            parts.append(f"- 价格说明：{price_note}")
        if isinstance(screen, (int, float)):
            parts.append(f"- 屏幕尺寸：{screen} 英寸")
        if chip:
            parts.append(f"- 芯片：{chip}")
        if isinstance(storage, list) and storage:
            storage_text = "/".join(f"{int(v)}GB" for v in storage if isinstance(v, (int, float)))
            if storage_text:
                parts.append(f"- 容量可选：{storage_text}")
        if isinstance(battery_hours, (int, float)):
            parts.append(f"- 官方视频播放最长：约 {int(battery_hours)} 小时")
        if isinstance(use_cases, list) and use_cases:
            parts.append(f"- 适用场景：{'; '.join(str(x) for x in use_cases[:3])}")
        if buy_url:
            parts.append(f"- 购买页：{buy_url}")
        if specs_url:
            parts.append(f"- 规格页：{specs_url}")
        if verified_on:
            parts.append(f"- 数据校验日期：{verified_on}")
        return "\n".join(parts)

    def _build_recommendation_reply(self, question: str) -> str:
        category = _detect_category(question)
        min_budget, max_budget = _parse_budget_range(question)
        required_storage = _parse_storage_gb(question)
        screen_target, screen_mode = _parse_screen_requirement(question)
        want_tags = _detect_feature_tags(question)

        products = self._state.products
        if category:
            products = [p for p in products if str(p.get("category", "")) == category]

        if not products:
            return "我还没找到对应品类的数据，请告诉我你想看手机、平板、笔记本还是手表。"

        scored: list[tuple[float, dict[str, Any], list[str]]] = []
        for product in products:
            price = product.get("starting_price_cny")
            if not isinstance(price, (int, float)):
                continue
            score = 0.0
            reasons: list[str] = []

            if max_budget is not None:
                if price <= max_budget:
                    score += 4.0
                    reasons.append("预算内")
                else:
                    overflow_ratio = (price - max_budget) / max(max_budget, 1)
                    if overflow_ratio > 0.2:
                        continue
                    score += 1.0
                    reasons.append("略超预算")

            if min_budget is not None:
                if price >= min_budget:
                    score += 1.0
                else:
                    score -= 0.5

            tags = product.get("tags", [])
            if isinstance(tags, list):
                hit_tags = [t for t in want_tags if t in tags]
                if hit_tags:
                    score += float(len(hit_tags)) * 1.25
                    reasons.append("满足核心需求")

            storage_options = product.get("storage_options_gb", [])
            if required_storage is not None and isinstance(storage_options, list) and storage_options:
                if any(int(v) >= required_storage for v in storage_options if isinstance(v, (int, float))):
                    score += 1.0
                    reasons.append("容量可满足")
                else:
                    continue

            screen_size = product.get("screen_size_inch")
            if isinstance(screen_size, (int, float)):
                if screen_target is not None:
                    diff = abs(float(screen_size) - screen_target)
                    score += max(0.0, 1.5 - diff)
                elif screen_mode == "small":
                    if screen_size <= 11.0:
                        score += 0.8
                elif screen_mode == "large":
                    if screen_size >= 6.7:
                        score += 0.8

            if not reasons:
                reasons.append("参数均衡")

            scored.append((score, product, reasons))

        if not scored:
            low = min(
                (
                    int(p.get("starting_price_cny"))
                    for p in products
                    if isinstance(p.get("starting_price_cny"), (int, float))
                ),
                default=None,
            )
            if low is not None:
                return (
                    f"按当前条件没有完全匹配机型。你可以把预算提高到约 RMB {low:,} 起，"
                    "或放宽容量/屏幕要求，我可以继续给你细化推荐。"
                )
            return "按当前条件没有匹配机型。你可以补充预算、用途和容量需求，我继续帮你筛选。"

        scored.sort(
            key=lambda row: (
                row[0],
                -int(row[1].get("starting_price_cny", 0)),
            ),
            reverse=True,
        )
        top = scored[:3]

        condition_bits: list[str] = []
        if category:
            condition_bits.append(f"品类={_CATEGORY_LABELS.get(category, category)}")
        if min_budget is not None or max_budget is not None:
            if min_budget is not None and max_budget is not None:
                condition_bits.append(f"预算={min_budget:,}-{max_budget:,}")
            elif max_budget is not None:
                condition_bits.append(f"预算<={max_budget:,}")
            elif min_budget is not None:
                condition_bits.append(f"预算>={min_budget:,}")
        if required_storage is not None:
            condition_bits.append(f"容量>={required_storage}GB")
        if screen_target is not None:
            condition_bits.append(f"屏幕约{screen_target}英寸")
        if screen_mode == "small":
            condition_bits.append("偏好小屏")
        elif screen_mode == "large":
            condition_bits.append("偏好大屏")

        header = "根据你的条件，推荐以下机型："
        if condition_bits:
            header = f"根据你的条件（{'；'.join(condition_bits)}），推荐以下机型："

        lines: list[str] = [header]
        for idx, (_, product, reasons) in enumerate(top, start=1):
            model = str(product.get("model", ""))
            price = _format_price(product.get("starting_price_cny"))
            chip = str(product.get("chip", "") or "芯片参数见规格页")
            screen = product.get("screen_size_inch")
            storage = product.get("storage_options_gb", [])
            spec_bits: list[str] = []
            if isinstance(screen, (int, float)):
                spec_bits.append(f"{screen}英寸")
            if chip:
                spec_bits.append(chip)
            if isinstance(storage, list) and storage:
                compact = "/".join(f"{int(v)}G" for v in storage if isinstance(v, (int, float)))
                if compact:
                    spec_bits.append(compact)
            lines.append(f"{idx}. {model}（{price}）")
            if spec_bits:
                lines.append(f"   规格：{'，'.join(spec_bits)}")
            lines.append(f"   推荐理由：{', '.join(reasons[:2])}")

        verified_on = str(self._state.meta.get("verified_on", "")).strip()
        if verified_on:
            lines.append(f"价格校验日期：{verified_on}；实际成交价请以下单页为准。")
        else:
            lines.append("价格为官网起售价参考，实际成交价请以下单页为准。")
        return "\n".join(lines)

    def _looks_like_comparison(self, question: str) -> bool:
        q = (question or "").lower()
        if any(k in q for k in _COMPARE_INTENT_KEYWORDS):
            return True
        if ("哪款" in q or "哪个" in q) and ("更" in q or "推荐" in q):
            return True
        return False

    def _looks_like_recommendation_with_profile(self, question: str, profile: dict[str, Any]) -> bool:
        q = (question or "").lower()
        if self._is_context_breaker_question(q):
            return False
        if any(k in q for k in _RECOMMEND_INTENT_KEYWORDS):
            return True
        if _detect_category(q) and any(k in q for k in ["多少钱", "价格", "价位", "预算", "适合"]):
            return True
        if profile.get("min_budget") is not None or profile.get("max_budget") is not None:
            return True
        if profile.get("required_storage") is not None:
            return True
        if profile.get("tags"):
            return True
        if profile.get("category_from_history"):
            return self._has_followup_signal(q)
        return False

    def _prepare_compare_candidates(
        self,
        question: str,
        recent_models: list[dict[str, Any]],
        profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for idx in self._match_model_indices(question, limit=5):
            product = self._state.products[idx]
            row = self._candidate_from_product(product, score=0.0, reasons=["当前问题提及"])
            pid = str(row.get("id", ""))
            if pid and pid not in seen:
                seen.add(pid)
                rows.append(row)
        for row in recent_models:
            pid = str(row.get("id", ""))
            if not pid or pid in seen:
                continue
            seen.add(pid)
            rows.append(row)
            if len(rows) >= 4:
                break

        category = str(profile.get("category") or "").strip()
        if category:
            rows = [r for r in rows if str(r.get("category", "")) == category]
        return rows[:4]

    def _candidate_from_product(
        self,
        product: dict[str, Any],
        score: float,
        reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        price = _as_int(product.get("starting_price_cny"))
        storage_options = [
            int(v)
            for v in (
                product.get("storage_options_gb", [])
                if isinstance(product.get("storage_options_gb", []), list)
                else []
            )
            if isinstance(v, (int, float))
        ]
        screen_size = product.get("screen_size_inch")
        screen_value = float(screen_size) if isinstance(screen_size, (int, float)) else None
        return {
            "id": str(product.get("id", "")),
            "model": str(product.get("model", "")),
            "category": str(product.get("category", "")),
            "price": price,
            "price_text": _format_price(price),
            "price_note": str(product.get("price_note", "")).strip(),
            "chip": str(product.get("chip", "")).strip(),
            "screen_size": screen_value,
            "storage_options": storage_options,
            "use_cases": [
                str(x)
                for x in (
                    product.get("use_cases", [])
                    if isinstance(product.get("use_cases", []), list)
                    else []
                )
            ],
            "tags": [str(x) for x in (product.get("tags", []) if isinstance(product.get("tags", []), list) else [])],
            "reasons": reasons or [],
            "score": round(float(score), 4),
            "specs_url": str(product.get("specs_url", "")).strip(),
            "buy_url": str(product.get("buy_url", "")).strip(),
            "raw_product": product,
        }

    def _candidate_clean(self, candidate: dict[str, Any]) -> dict[str, Any]:
        row = dict(candidate)
        row.pop("raw_product", None)
        return row

    def _product_tradeoffs(self, candidate: dict[str, Any], profile: dict[str, Any]) -> tuple[list[str], list[str]]:
        tags = {str(t) for t in (candidate.get("tags") or [])}
        price = _as_int(candidate.get("price"))
        storage_options = [int(v) for v in (candidate.get("storage_options") or []) if isinstance(v, int)]
        storage_max = max(storage_options, default=None)

        pros: list[str] = []
        cons: list[str] = []

        if "camera" in tags:
            pros.append("影像能力更强")
        if "performance" in tags:
            pros.append("性能余量更大，游戏更稳")
        if "value" in tags:
            pros.append("价格门槛相对低")
        if "battery" in tags:
            pros.append("续航表现更友好")
        if "portable" in tags:
            pros.append("机身更轻便")
        if "large_screen" in tags:
            pros.append("大屏观影体验更好")
        if "small_screen" in tags:
            pros.append("单手操作更友好")

        required_storage = _as_int(profile.get("required_storage"))
        if required_storage is not None and storage_max is not None and storage_max >= required_storage:
            pros.append(f"可选容量覆盖 {required_storage}GB 需求")

        if "premium" in tags or (price is not None and price >= 8000):
            cons.append("价格压力相对更大")
        if "large_screen" in tags:
            cons.append("机身更大，便携性一般")
        if storage_max is not None and storage_max <= 512:
            cons.append("超大容量可选空间有限")
        if "small_screen" in tags and str(profile.get("screen_mode") or "") == "large":
            cons.append("偏好大屏观影时体验会一般")

        if not pros:
            pros.append("参数较均衡")
        if not cons:
            cons.append("高配版本价格会继续上升")
        return pros[:2], cons[:2]

    def _rank_products(
        self,
        products_or_candidates: list[dict[str, Any]],
        profile: dict[str, Any],
        allow_over_budget: bool,
    ) -> list[dict[str, Any]]:
        if not products_or_candidates:
            return []

        base_rows: list[dict[str, Any]] = []
        for row in products_or_candidates:
            if "raw_product" in row:
                base_rows.append(row)
            else:
                base_rows.append(self._candidate_from_product(row, score=0.0, reasons=[]))

        min_budget = _as_int(profile.get("min_budget"))
        max_budget = _as_int(profile.get("max_budget"))
        required_storage = _as_int(profile.get("required_storage"))
        screen_target = profile.get("screen_target")
        screen_mode = str(profile.get("screen_mode") or "")
        wanted_tags = [str(t) for t in (profile.get("tags") or [])]
        budget_style = str(profile.get("budget_style") or "neutral")

        prices = [int(r.get("price")) for r in base_rows if isinstance(r.get("price"), int)]
        price_min = min(prices) if prices else None
        price_max = max(prices) if prices else None
        soft_budget_cap = None
        if max_budget is None and budget_style == "low" and price_min is not None:
            # When user only says "预算不多/预算有限", keep recommendations near entry-level prices.
            soft_budget_cap = int(price_min * 1.8)

        ranked: list[dict[str, Any]] = []
        for row in base_rows:
            price = _as_int(row.get("price"))
            if price is None:
                continue
            score = 0.0
            reasons: list[str] = []

            if max_budget is not None:
                if price <= max_budget:
                    score += 4.5
                    reasons.append("预算内")
                else:
                    overflow = (price - max_budget) / max(max_budget, 1)
                    overflow_limit = 0.35 if allow_over_budget else 0.2
                    if overflow > overflow_limit:
                        continue
                    score += max(0.2, 1.2 - overflow)
                    reasons.append("略超预算")
            elif soft_budget_cap is not None:
                if price <= soft_budget_cap:
                    score += 1.6
                else:
                    overflow = (price - soft_budget_cap) / max(soft_budget_cap, 1)
                    soft_overflow_limit = 0.15 if allow_over_budget else 0.08
                    if overflow > soft_overflow_limit:
                        continue
                    score += max(0.1, 0.6 - overflow)
                    reasons.append("预算偏高")

            if min_budget is not None:
                if price >= min_budget:
                    score += 1.0
                else:
                    score -= 0.8

            if budget_style == "low" and price_min is not None and price_max is not None and price_max > price_min:
                norm = (price - price_min) / (price_max - price_min)
                score += max(0.0, 1.4 - norm * 1.4)
            elif budget_style == "high" and price_min is not None and price_max is not None and price_max > price_min:
                norm = (price - price_min) / (price_max - price_min)
                score += norm * 0.8

            tag_set = {str(t) for t in (row.get("tags") or [])}
            hit_tags = [t for t in wanted_tags if t in tag_set]
            if hit_tags:
                score += len(hit_tags) * 1.3
                reasons.append("满足核心需求")

            storage_options = [int(v) for v in (row.get("storage_options") or []) if isinstance(v, int)]
            if required_storage is not None and storage_options:
                if any(v >= required_storage for v in storage_options):
                    score += 1.0
                    reasons.append("容量可满足")
                else:
                    continue

            screen_size = row.get("screen_size")
            if isinstance(screen_size, (int, float)):
                if isinstance(screen_target, (int, float)):
                    diff = abs(float(screen_size) - float(screen_target))
                    score += max(0.0, 1.2 - diff)
                elif screen_mode == "small" and screen_size <= 6.3:
                    score += 0.8
                elif screen_mode == "large" and screen_size >= 6.7:
                    score += 0.8

            if not reasons:
                reasons.append("参数均衡")

            out = dict(row)
            out["score"] = round(score, 4)
            out["reasons"] = reasons[:2]
            pros, cons = self._product_tradeoffs(out, profile)
            out["pros"] = pros
            out["cons"] = cons
            ranked.append(out)

        ranked.sort(
            key=lambda r: (r.get("score", 0.0), -(r.get("price") or 0)),
            reverse=True,
        )
        return ranked

    def _choose_primary(self, candidates: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any] | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        budget_style = str(profile.get("budget_style") or "neutral")
        if budget_style == "low":
            return min(candidates, key=lambda x: x.get("price") or 10**9)
        return max(candidates, key=lambda x: x.get("score", 0.0))

    def _user_need_summary(self, profile: dict[str, Any]) -> str:
        parts: list[str] = []
        category = str(profile.get("category") or "").strip()
        if category:
            parts.append(_CATEGORY_LABELS.get(category, category))
        min_budget = _as_int(profile.get("min_budget"))
        max_budget = _as_int(profile.get("max_budget"))
        if min_budget is not None and max_budget is not None:
            parts.append(f"预算 {min_budget:,}-{max_budget:,}")
        elif max_budget is not None:
            parts.append(f"预算不超过 {max_budget:,}")
        elif min_budget is not None:
            parts.append(f"预算不低于 {min_budget:,}")
        elif str(profile.get("budget_style") or "") == "low":
            parts.append("预算偏紧")

        required_storage = _as_int(profile.get("required_storage"))
        if required_storage is not None:
            parts.append(f"容量至少 {required_storage}GB")

        tags = [str(t) for t in (profile.get("tags") or [])]
        if tags:
            tag_labels = {
                "camera": "拍照",
                "battery": "续航",
                "performance": "性能/游戏",
                "portable": "轻薄便携",
                "productivity": "办公学习",
                "large_screen": "大屏",
                "small_screen": "小屏",
                "value": "性价比",
            }
            mapped = [tag_labels.get(t, t) for t in tags if t in tag_labels]
            if mapped:
                parts.append("关注 " + "、".join(mapped[:3]))
        return "；".join(parts)

    def _build_rich_recommendation_reply(self, candidates: list[dict[str, Any]], profile: dict[str, Any]) -> str:
        primary = self._choose_primary(candidates, profile)
        backup = candidates[1] if len(candidates) > 1 else None
        need_summary = self._user_need_summary(profile)
        verified_on = str(self._state.meta.get("verified_on", "")).strip()

        lines: list[str] = []
        if need_summary:
            lines.append(f"结合你的需求（{need_summary}），我给你更实用的结论：")
        else:
            lines.append("我按你的需求做了筛选，给你更实用的结论：")
        if profile.get("category_from_history"):
            lines.append("我沿用了你上文同品类的需求继续筛选。")

        if primary:
            lines.append(f"首推：{primary.get('model')}（{primary.get('price_text')}）")
            lines.append(f"优点：{'；'.join(primary.get('pros', [])[:2])}")
            lines.append(f"注意点：{'；'.join(primary.get('cons', [])[:2])}")

        if backup:
            lines.append(f"备选：{backup.get('model')}（{backup.get('price_text')}）")
            use_case = "; ".join(backup.get("use_cases", [])[:2]) or "预算或屏幕偏好不同时更合适"
            lines.append(f"更适合：{use_case}")

        if len(candidates) > 2:
            third = candidates[2]
            lines.append(
                f"第三选择：{third.get('model')}（{third.get('price_text')}），优势是 {'；'.join(third.get('pros', [])[:1])}"
            )

        if verified_on:
            lines.append(f"价格校验日期：{verified_on}；实际成交价请以下单页为准。")
        else:
            lines.append("价格为官网起售价参考，实际成交价请以下单页为准。")
        return "\n".join(lines)

    def _build_comparison_reply(self, candidates: list[dict[str, Any]], profile: dict[str, Any]) -> str:
        primary = self._choose_primary(candidates, profile)
        need_summary = self._user_need_summary(profile)
        verified_on = str(self._state.meta.get("verified_on", "")).strip()

        lines: list[str] = []
        if need_summary:
            lines.append(f"你这几款我按你的需求（{need_summary}）做个直观对比：")
        else:
            lines.append("你这几款我做个直观对比：")

        for idx, row in enumerate(candidates[:3], start=1):
            lines.append(f"{idx}. {row.get('model')}（{row.get('price_text')}）")
            lines.append(f"   优点：{'；'.join(row.get('pros', [])[:2])}")
            lines.append(f"   不足：{'；'.join(row.get('cons', [])[:2])}")

        if primary:
            lines.append(f"如果按你当前诉求，我更推荐：{primary.get('model')}。")
            lines.append("理由：在你关注的点上综合得分更高，且后续升级空间更稳。")

        if verified_on:
            lines.append(f"价格校验日期：{verified_on}；实际成交价请以下单页为准。")
        return "\n".join(lines)
