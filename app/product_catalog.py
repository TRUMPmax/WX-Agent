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

_FEATURE_KEYWORDS = {
    "camera": ["拍照", "摄影", "录像", "视频", "vlog", "影像", "长焦", "夜景"],
    "battery": ["续航", "电池", "待机", "充电频率", "一天一充"],
    "performance": ["性能", "游戏", "剪辑", "渲染", "本地模型", "开发", "高刷"],
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


def _normalize_text(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def _format_price(value: int | float | None) -> str:
    if value is None:
        return "价格以官网配置页为准"
    return f"RMB {int(value):,} 起"


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
        q = (question or "").lower().strip()
        if not q:
            return False
        if any(k in q for k in _RECOMMEND_INTENT_KEYWORDS):
            return True
        if _detect_category(q):
            return True
        if any(k in q for k in _RETURN_POLICY_KEYWORDS + _WARRANTY_KEYWORDS + _SERVICE_FLOW_KEYWORDS):
            return True
        return self._match_product_index(q) is not None

    def answer(self, question: str) -> str | None:
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
            return policy_reply

        product_idx = self._match_product_index(q)
        if product_idx is not None:
            return self._build_product_detail_reply(self._state.products[product_idx])

        if self._looks_like_recommendation(q):
            return self._build_recommendation_reply(q)

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

    def _match_product_index(self, question: str) -> int | None:
        normalized_q = _normalize_text(question)
        if not normalized_q:
            return None

        best: tuple[int, int] | None = None
        for alias, idx in self._state.aliases:
            if len(alias) < 3:
                continue
            if alias not in normalized_q:
                continue
            alias_len = len(alias)
            if best is None or alias_len > best[0]:
                best = (alias_len, idx)
        if best is None:
            return None
        return best[1]

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
