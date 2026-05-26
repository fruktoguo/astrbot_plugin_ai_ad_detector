import asyncio
import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain


DATA_DIR = StarTools.get_data_dir("astrbot_plugin_ai_ad_detector")
DEBUG_DIR = DATA_DIR / "debug"

DEFAULT_AD_PROMPT = """你是群聊广告风控审核员。请根据用户昵称、聊天文本和附带图片判断这条群消息是否为广告、引流、推广或诈骗。

重点识别：
- 兼职、刷单、代付、返利、抽奖、福利、红包群、贷款、博彩、理财、虚拟币、色情、陪玩、灰产、外挂、账号交易。
- 引导添加好友、私聊、扫码、进群、点击链接、复制口令、关注公众号/频道/店铺。
- 图片中的二维码、联系方式、水印、价格表、推广海报、招聘/项目收益截图。
- 昵称本身带有广告、联系方式、推广词，即使正文很短也要判断。

降低误判：
- 正常技术讨论、游戏交流、群友闲聊、对广告现象的吐槽或引用，不应判为广告。
- 只有普通链接或普通图片，缺少推广/引流/交易意图时不要轻易判定。

必须只返回 JSON，不要输出 Markdown：
{
  "is_ad": true 或 false,
  "confidence": 0 到 1 的数字,
  "category": "广告类型，非广告时为空字符串",
  "reason": "一句话说明依据",
  "evidence": ["命中的关键词、昵称特征、图片特征或引流动作"]
}
"""

DEFAULT_CONFIG: dict[str, Any] = {
    "basic": {
        "enabled": True,
        "default_action": "notify",
        "confidence_threshold": 0.72,
        "ignore_admins": True,
        "ignore_self": True,
        "skip_command_messages": True,
        "max_images": 3,
        "message_cooldown_seconds": 3,
        "notify_prefix": "检测到疑似广告",
        "debug_mode": False,
    },
    "llm": {
        "provider_id": "",
        "max_concurrent": 1,
        "retries": 3,
        "retry_backoff_seconds": 3,
    },
    "prompts": {
        "ad_detection_prompt": DEFAULT_AD_PROMPT,
    },
    "monitors": [],
}

VALID_ACTIONS = {"log", "notify", "recall", "recall_and_notify"}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def write_json(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def safe_int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def safe_float(value: Any, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def strip_code_fence(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_ai_json(text: str) -> dict[str, Any]:
    cleaned = strip_code_fence(text)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        data = json.loads(cleaned[start : end + 1])
        if isinstance(data, dict):
            return data
    raise ValueError(f"AI 未返回有效 JSON: {text[:500]}")


def message_id_from_event(event: AstrMessageEvent) -> str:
    msg_obj = getattr(event, "message_obj", None)
    return str(getattr(msg_obj, "message_id", "") or "")


def compact_text(text: str, limit: int = 1600) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 12)].rstrip() + "...(已截断)"


@register(
    "astrbot_plugin_ai_ad_detector",
    "YuoHira",
    "调用 AI 识别群聊文本、图片和昵称中的广告/引流内容，并按群配置处理动作。",
    "0.1.0",
    "https://github.com/fruktoguo/astrbot_plugin_ai_ad_detector",
)
class AIAdDetectorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.plugin_config = config
        self.config = self._load_config(config)
        max_concurrent = safe_int(self._llm_config().get("max_concurrent"), 1, 1)
        self.llm_semaphore = asyncio.Semaphore(max_concurrent)
        self.last_seen: dict[str, int] = {}
        self.bot_id = ""

    async def initialize(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("AI 广告识别插件已初始化")

    async def terminate(self):
        return

    def _load_config(self, plugin_config: AstrBotConfig | None) -> dict[str, Any]:
        raw = dict(plugin_config) if plugin_config is not None else {}
        config = deep_merge(DEFAULT_CONFIG, raw)
        if "provider_id" in config:
            config["llm"]["provider_id"] = config.pop("provider_id")
        if "groups" in config and not config.get("monitors"):
            config["monitors"] = config.pop("groups")
        for monitor in self._monitors(config):
            monitor.setdefault("__template_key", "monitor")
            if str(monitor.get("action") or "") not in VALID_ACTIONS:
                monitor["action"] = ""
        return config

    def _basic_config(self) -> dict[str, Any]:
        return self.config.get("basic", {})

    def _llm_config(self) -> dict[str, Any]:
        return self.config.get("llm", {})

    def _prompts_config(self) -> dict[str, Any]:
        return self.config.get("prompts", {})

    def _monitors(self, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        cfg = config or self.config
        monitors = cfg.get("monitors", [])
        return monitors if isinstance(monitors, list) else []

    def _monitor_for_group(self, group_id: str) -> dict[str, Any] | None:
        for monitor in self._monitors():
            if not monitor.get("enabled", True):
                continue
            if str(monitor.get("group_id") or "").strip() == str(group_id):
                return monitor
        return None

    def _debug_enabled(self) -> bool:
        return bool(self._basic_config().get("debug_mode", False))

    def _save_debug_payload(self, name: str, payload: Any) -> None:
        if not self._debug_enabled():
            return
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)[:90]
        path = DEBUG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}.json"
        try:
            write_json(path, payload)
        except Exception as exc:
            logger.warning(f"写入 AI 广告识别调试数据失败: {exc}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def detect_group_ad(self, event: AstrMessageEvent):
        if not self._basic_config().get("enabled", True):
            return
        self.bot_id = str(event.get_self_id() or self.bot_id)
        group_id = str(event.get_group_id() or "")
        monitor = self._monitor_for_group(group_id)
        if not monitor:
            return
        if self._should_skip(event, monitor):
            return

        payload = self._build_payload(event)
        image_urls = await self._collect_images(event, monitor)
        if not payload["text"] and not image_urls and not payload["sender_name"]:
            return

        dedupe_key = f"{group_id}:{message_id_from_event(event) or payload['sender_id']}:{payload['text'][:80]}"
        now = int(datetime.now().timestamp())
        cooldown = safe_int(self._basic_config().get("message_cooldown_seconds"), 3, 0)
        if cooldown > 0 and now - self.last_seen.get(dedupe_key, 0) < cooldown:
            return
        self.last_seen[dedupe_key] = now

        try:
            prompt = self._build_prompt(payload, image_urls)
            self._save_debug_payload(f"{group_id}_{payload['sender_id']}_prompt", {"prompt": prompt, "image_urls": image_urls})
            response_text = await self._call_llm(prompt, image_urls, monitor)
            self._save_debug_payload(f"{group_id}_{payload['sender_id']}_response", {"response": response_text})
            result = self._normalize_result(parse_ai_json(response_text))
        except Exception as exc:
            logger.warning(f"AI 广告识别失败 group={group_id}: {exc}")
            return

        threshold = self._confidence_threshold(monitor)
        if result["is_ad"] and result["confidence"] >= threshold:
            await self._handle_detection(event, monitor, payload, result)

    def _should_skip(self, event: AstrMessageEvent, monitor: dict[str, Any]) -> bool:
        sender_id = str(event.get_sender_id() or "")
        if bool(monitor.get("skip_command_messages", self._basic_config().get("skip_command_messages", True))):
            text = str(event.get_message_str() or "").lstrip()
            if text.startswith("/"):
                return True
        ignore_self = bool(monitor.get("ignore_self", self._basic_config().get("ignore_self", True)))
        if ignore_self and self.bot_id and sender_id == self.bot_id:
            return True
        ignore_admins = bool(monitor.get("ignore_admins", self._basic_config().get("ignore_admins", True)))
        if ignore_admins and bool(event.is_admin()):
            return True
        return False

    def _confidence_threshold(self, monitor: dict[str, Any]) -> float:
        raw = monitor.get("confidence_threshold")
        if raw in (None, "", 0, 0.0, "0"):
            raw = self._basic_config().get("confidence_threshold")
        return safe_float(raw, 0.72, 0.0, 1.0)

    def _build_payload(self, event: AstrMessageEvent) -> dict[str, Any]:
        sender_id = str(event.get_sender_id() or "")
        return {
            "group_id": str(event.get_group_id() or ""),
            "sender_id": sender_id,
            "sender_name": event.get_sender_name() or sender_id,
            "message_id": message_id_from_event(event),
            "text": compact_text(event.get_message_str() or ""),
            "message_outline": compact_text(event.get_message_outline() or "", 800),
        }

    async def _collect_images(self, event: AstrMessageEvent, monitor: dict[str, Any]) -> list[str]:
        max_images = safe_int(monitor.get("max_images", self._basic_config().get("max_images")), 3, 0)
        if max_images <= 0:
            return []
        image_urls: list[str] = []
        for comp in event.get_messages():
            if not isinstance(comp, Image):
                continue
            try:
                image_urls.append(await comp.convert_to_file_path())
            except Exception as exc:
                logger.warning(f"转换待识别图片失败: {exc}")
            if len(image_urls) >= max_images:
                break
        return image_urls

    def _build_prompt(self, payload: dict[str, Any], image_urls: list[str]) -> str:
        prompt = str(self._prompts_config().get("ad_detection_prompt") or DEFAULT_AD_PROMPT).strip()
        input_json = json.dumps(
            {
                "sender": {
                    "id": payload["sender_id"],
                    "nickname": payload["sender_name"],
                },
                "message": {
                    "text": payload["text"],
                    "outline": payload["message_outline"],
                    "image_count": len(image_urls),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        if "${payload_json}" in prompt:
            return prompt.replace("${payload_json}", input_json)
        return f"{prompt}\n\n待审核消息 JSON：\n{input_json}"

    async def _call_llm(self, prompt: str, image_urls: list[str], monitor: dict[str, Any]) -> str:
        llm_cfg = self._llm_config()
        retries = safe_int(llm_cfg.get("retries"), 3, 1)
        backoff = safe_int(llm_cfg.get("retry_backoff_seconds"), 3, 0)
        last_exc: Exception | None = None
        provider_id = self._resolve_provider_id(monitor)
        async with self.llm_semaphore:
            for attempt in range(1, retries + 1):
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        image_urls=image_urls,
                        system_prompt="你是严格的群聊广告识别模型，只返回 JSON。",
                    )
                    return resp.completion_text or ""
                except Exception as exc:
                    last_exc = exc
                    logger.warning(f"AI 广告识别 LLM 调用失败 {attempt}/{retries}: {exc}")
                    if attempt < retries and backoff > 0:
                        await asyncio.sleep(backoff * attempt)
        raise RuntimeError(f"AI 广告识别 LLM 调用失败: {last_exc}")

    def _resolve_provider_id(self, monitor: dict[str, Any]) -> str:
        provider_id = str(monitor.get("provider_id") or self._llm_config().get("provider_id") or "").strip()
        if provider_id:
            return provider_id
        provider = self.context.get_using_provider()
        if not provider:
            providers = self.context.get_all_providers()
            provider = providers[0] if providers else None
        if not provider:
            raise RuntimeError("没有可用的 AstrBot 模型提供商")
        return provider.meta().id

    def _normalize_result(self, result: dict[str, Any]) -> dict[str, Any]:
        is_ad_raw = result.get("is_ad", False)
        if isinstance(is_ad_raw, str):
            is_ad = is_ad_raw.strip().lower() in {"true", "1", "yes", "是", "广告"}
        else:
            is_ad = bool(is_ad_raw)
        evidence = result.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        return {
            "is_ad": is_ad,
            "confidence": safe_float(result.get("confidence"), 0.0, 0.0, 1.0),
            "category": str(result.get("category") or ""),
            "reason": str(result.get("reason") or ""),
            "evidence": [str(x) for x in evidence if str(x).strip()][:5],
        }

    async def _handle_detection(
        self,
        event: AstrMessageEvent,
        monitor: dict[str, Any],
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        action = str(monitor.get("action") or self._basic_config().get("default_action") or "notify")
        if action not in VALID_ACTIONS:
            action = "notify"

        recalled = False
        if action in {"recall", "recall_and_notify"}:
            recalled = await self._recall_message(event, payload["message_id"])

        if action in {"notify", "recall_and_notify"}:
            text = self._build_notify_text(payload, result, recalled)
            await self._send_group_text(event, monitor, text)

        logger.info(
            "AI 广告识别命中 group=%s sender=%s confidence=%.2f category=%s action=%s recalled=%s reason=%s",
            payload["group_id"],
            payload["sender_id"],
            result["confidence"],
            result["category"],
            action,
            recalled,
            result["reason"],
        )

    def _build_notify_text(self, payload: dict[str, Any], result: dict[str, Any], recalled: bool) -> str:
        prefix = str(self._basic_config().get("notify_prefix") or "检测到疑似广告")
        evidence = "、".join(result["evidence"]) if result["evidence"] else "无"
        status = "已尝试撤回" if recalled else "未撤回"
        lines = [
            f"{prefix}（置信度 {result['confidence']:.2f}，{status}）",
            f"成员：{payload['sender_name']}（{payload['sender_id']}）",
            f"类型：{result['category'] or '未分类'}",
            f"理由：{result['reason'] or '无'}",
            f"依据：{evidence}",
        ]
        if payload["text"]:
            lines.append(f"文本：{compact_text(payload['text'], 180)}")
        return "\n".join(lines)

    async def _send_group_text(self, event: AstrMessageEvent, monitor: dict[str, Any], text: str) -> None:
        platform_id = str(monitor.get("platform_id") or "").strip()
        group_id = str(event.get_group_id() or "")
        if await self._send_onebot_group_message(group_id, text, platform_id):
            return
        session = event.unified_msg_origin
        await self.context.send_message(session, MessageChain().message(text))

    async def _recall_message(self, event: AstrMessageEvent, message_id: str) -> bool:
        if not message_id:
            logger.warning("广告命中但缺少 message_id，无法撤回")
            return False
        platform_manager = getattr(self.context, "platform_manager", None)
        get_insts = getattr(platform_manager, "get_insts", None)
        if not callable(get_insts):
            return False
        try:
            platforms = get_insts()
        except Exception as exc:
            logger.warning(f"获取平台实例失败，无法撤回广告消息: {exc}")
            return False
        for platform in platforms if isinstance(platforms, (list, tuple)) else []:
            if self._platform_id(platform) != event.platform_meta.id:
                continue
            client = self._platform_client(platform)
            call_action = getattr(client, "call_action", None)
            if not callable(call_action):
                continue
            try:
                await call_action("delete_msg", message_id=int(message_id))
                return True
            except Exception as exc:
                logger.warning(f"撤回广告消息失败 message_id={message_id}: {exc}")
        return False

    async def _send_onebot_group_message(self, group_id: str, text: str, platform_id: str = "") -> bool:
        platform_manager = getattr(self.context, "platform_manager", None)
        get_insts = getattr(platform_manager, "get_insts", None)
        if not callable(get_insts):
            return False
        try:
            platforms = get_insts()
        except Exception as exc:
            logger.warning(f"获取平台实例失败，无法发送广告识别通知: {exc}")
            return False
        if not isinstance(platforms, (list, tuple)):
            return False
        for platform in platforms:
            if platform_id and self._platform_id(platform) != platform_id:
                continue
            client = self._platform_client(platform)
            call_action = getattr(client, "call_action", None)
            if not callable(call_action):
                continue
            try:
                await call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=[{"type": "text", "data": {"text": text}}],
                )
                return True
            except Exception as exc:
                logger.warning(f"发送广告识别通知失败 group={group_id}: {exc}")
        return False

    def _platform_id(self, platform: object) -> str:
        meta = getattr(platform, "meta", None)
        if callable(meta):
            try:
                metadata = meta()
                return str(getattr(metadata, "id", "") or getattr(metadata, "name", "") or "")
            except Exception:
                return ""
        return str(getattr(platform, "id", "") or "")

    def _platform_client(self, platform: object) -> object:
        for name in ("client", "bot", "adapter"):
            client = getattr(platform, name, None)
            if client is not None:
                return client
        return platform

    @filter.command("广告检测状态")
    async def ad_detector_status(self, event: AstrMessageEvent):
        monitors = [m for m in self._monitors() if m.get("enabled", True)]
        group_id = str(event.get_group_id() or "")
        matched = self._monitor_for_group(group_id) if group_id else None
        lines = [
            "AI 广告识别插件状态",
            f"总开关：{'开启' if self._basic_config().get('enabled', True) else '关闭'}",
            f"启用群规则：{len(monitors)} 条",
            f"当前群：{'已监控' if matched else '未监控'}",
            f"默认动作：{self._basic_config().get('default_action', 'notify')}",
            f"默认阈值：{safe_float(self._basic_config().get('confidence_threshold'), 0.72):.2f}",
        ]
        yield event.plain_result("\n".join(lines))
