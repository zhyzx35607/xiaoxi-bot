"""AI provider configuration, failover, status, and vision calls."""

import asyncio
import base64
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger("qqbot")

_AI_SEM = None

_VISION_SEM = None

_PROVIDER_COOLDOWNS = {}

_PROVIDER_STATS = {}

_PROVIDER_NO_TOOLS = set()  # (base_url, model) known to reject the tools parameter

def _get_sigmai_api_key(config):
    return (
        os.getenv("SIGMAI_API_KEY") or
        os.getenv("QQBOT_SIGMAI_API_KEY") or
        config.get("sigmai_api_key") or
        ""
    ).strip()

def _get_sigmai_config(config):
    return {
        "api_key": _get_sigmai_api_key(config),
        "base_url": os.getenv("SIGMAI_BASE_URL") or config.get("sigmai_base_url", "https://www.sigmai.net/v1"),
        "model": os.getenv("SIGMAI_MODEL") or config.get("sigmai_model", "DeepSeek-V4-Flash"),
    }

def _get_agnes_api_key(config):
    return (
        os.getenv("AGNES_API_KEY") or
        os.getenv("QQBOT_AGNES_API_KEY") or
        config.get("agnes_api_key") or
        ""
    ).strip()

def _get_agnes_config(config):
    """Agnes is retained ONLY for image generation (/生图), not for chat."""
    return {
        "api_key": _get_agnes_api_key(config),
        "base_url": os.getenv("AGNES_BASE_URL") or config.get("agnes_base_url", "https://apihub.agnes-ai.com/v1"),
        "model": os.getenv("AGNES_MODEL") or config.get("agnes_model", "agnes-2.0-flash"),
    }

def _get_deepseek_api_key(config):
    return (
        os.getenv("DEEPSEEK_API_KEY") or
        os.getenv("QQBOT_DEEPSEEK_API_KEY") or
        config.get("deepseek_api_key") or
        ""
    ).strip()

def _get_deepseek_config(config):
    return {
        "api_key": _get_deepseek_api_key(config),
        "base_url": config.get("deepseek_base_url", "https://api.deepseek.com"),
        "model": config.get("deepseek_model", "deepseek-chat"),
    }

def _get_vision_api_key(config):
    vision_cfg = config.get("vision_api", {})
    return (
        os.getenv("VISION_API_KEY") or
        os.getenv("QQBOT_VISION_API_KEY") or
        vision_cfg.get("api_key") or
        ""
    ).strip()

def _get_semaphore(name, limit):
    global _AI_SEM, _VISION_SEM
    current = _AI_SEM if name == "ai" else _VISION_SEM
    if current is None or getattr(current, "_qqbot_limit", None) != limit:
        current = asyncio.Semaphore(max(1, int(limit)))
        current._qqbot_limit = max(1, int(limit))
        if name == "ai":
            _AI_SEM = current
        else:
            _VISION_SEM = current
    return current

def is_ai_busy():
    """Check whether the AI semaphore is currently exhausted (all slots taken)."""
    return _AI_SEM is not None and _AI_SEM.locked()

async def _call_deepseek(config, messages, max_tokens=400, temperature=0.7, session=None):
    runtime = config.get("runtime", {})
    async with _get_semaphore("ai", runtime.get("ai_concurrency", 1)):
        return await _call_deepseek_inner(config, messages, max_tokens, temperature, session)

async def _call_deepseek_inner(config, messages, max_tokens=400, temperature=0.7, session=None, tools=None):
    # Try SigmaI first (if configured), then fall back to official DeepSeek.
    # With tools given (OpenAI function calling), returns the raw message dict;
    # otherwise returns the content string as before.
    sigmai_cfg = _get_sigmai_config(config)
    deepseek_cfg = _get_deepseek_config(config)
    async def _call_api(cfg, model_label, use_session, timeout_seconds):
        if not cfg["api_key"]:
            return None
        provider_key = (cfg["base_url"], cfg["model"])
        stats = _PROVIDER_STATS.setdefault(model_label, {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "last_attempt": 0,
            "last_success": 0,
            "last_failure": 0,
            "last_latency_seconds": None,
            "last_error": "",
        })
        if _PROVIDER_COOLDOWNS.get(provider_key, 0) > time.monotonic():
            return None
        stats["attempts"] += 1
        stats["last_attempt"] = time.time()
        started_at = time.monotonic()
        def _record_result(success, error=""):
            stats["last_latency_seconds"] = round(time.monotonic() - started_at, 3)
            if success:
                stats["successes"] += 1
                stats["last_success"] = time.time()
                stats["last_error"] = ""
            else:
                stats["failures"] += 1
                stats["last_failure"] = time.time()
                stats["last_error"] = str(error or "unknown")[:120]
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": temperature,
            "top_p": 0.9,
            "presence_penalty": 0.3,
            "frequency_penalty": 0.3,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        url = f"{cfg['base_url']}/chat/completions"
        async def _do_post(sess):
            request_timeout = max(5, min(30, int(timeout_seconds)))
            async with sess.post(url, headers=headers, json=payload,
                                timeout=aiohttp.ClientTimeout(total=request_timeout)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    message = data["choices"][0].get("message") or {}
                    if tools:
                        _PROVIDER_COOLDOWNS.pop(provider_key, None)
                        _record_result(True)
                        return message
                    content_text = (message.get("content") or "").strip()
                    _PROVIDER_COOLDOWNS.pop(provider_key, None)
                    if not content_text:
                        _record_result(False, "empty_content")
                        log.warning("%s returned empty content. finish_reason=%s",
                                   model_label, data["choices"][0].get("finish_reason", "?"))
                    else:
                        _record_result(True)
                    return content_text
                else:
                    body = await resp.text()
                    if tools and resp.status in (400, 404, 422) and "tool" in body.lower():
                        # Provider/model does not support function calling:
                        # remember it and let the caller degrade to plain calls.
                        _PROVIDER_NO_TOOLS.add(provider_key)
                        _record_result(False, "tools_unsupported")
                        log.warning("%s rejected tools (HTTP %d), degrading to plain calls",
                                    model_label, resp.status)
                        return None
                    _record_result(False, "HTTP {}".format(resp.status))
                    log.warning("%s API returned %d: %s", model_label, resp.status, body[:200])
                    cooldown = 3600 if resp.status in (400, 401, 403, 404) else 60
                    _PROVIDER_COOLDOWNS[provider_key] = time.monotonic() + cooldown
                    return None  # Signal caller to try fallback
        try:
            if use_session:
                return await _do_post(use_session)
            async with aiohttp.ClientSession() as s:
                return await _do_post(s)
        except asyncio.TimeoutError:
            _record_result(False, "timeout")
            log.warning("%s API timeout", model_label)
            _PROVIDER_COOLDOWNS[provider_key] = time.monotonic() + 60
        except Exception as e:
            _record_result(False, type(e).__name__)
            log.error("%s API error: %s", model_label, e)
            _PROVIDER_COOLDOWNS[provider_key] = time.monotonic() + 30
        return None
    runtime = config.get("runtime", {})
    sigmai_timeout = runtime.get("sigmai_timeout_seconds",
                                 runtime.get("agnes_timeout_seconds",
                                             runtime.get("ai_timeout_seconds", 15)))
    deepseek_timeout = runtime.get("deepseek_timeout_seconds", 20)
    if sigmai_cfg["api_key"]:
        # SigmaI is the normal provider.  DeepSeek is deliberately not
        # hedged: wait for the bounded SigmaI request to finish before using
        # the paid/secondary provider.
        result = await _call_api(sigmai_cfg, "SigmaI", session, sigmai_timeout)
        if result:
            return result
        log.info("SigmaI failed or returned empty; falling back to DeepSeek")
    if deepseek_cfg["api_key"]:
        return await _call_api(deepseek_cfg, "DeepSeek", session, deepseek_timeout)
    log.warning("No AI model API key configured (SigmaI or DeepSeek)")
    return None

def _providers_support_tools(config):
    """True unless every configured chat provider is known to reject tools."""
    configured = [c for c in (_get_sigmai_config(config), _get_deepseek_config(config))
                  if c["api_key"]]
    if not configured:
        return False
    return any((c["base_url"], c["model"]) not in _PROVIDER_NO_TOOLS
               for c in configured)

def get_ai_provider_status(config):
    """Return safe, in-memory provider health data without exposing secrets."""
    providers = (
        ("SigmaI", _get_sigmai_config(config)),
        ("DeepSeek", _get_deepseek_config(config)),
    )
    now = time.monotonic()
    result = []
    for label, cfg in providers:
        stats = dict(_PROVIDER_STATS.get(label, {}))
        provider_key = (cfg["base_url"], cfg["model"])
        stats.update({
            "name": label,
            "model": cfg["model"],
            "configured": bool(cfg["api_key"]),
            "cooldown_seconds": max(
                0, int(_PROVIDER_COOLDOWNS.get(provider_key, 0) - now)),
        })
        result.append(stats)
    return result

def format_ai_provider_status(config):
    def _time_text(timestamp):
        if not timestamp:
            return "暂无"
        return time.strftime("%m-%d %H:%M:%S", time.localtime(timestamp))
    lines = ["AI 供应商状态（本次启动以来）"]
    for item in get_ai_provider_status(config):
        name = item["name"]
        if not item["configured"]:
            lines.append("{}：未配置".format(name))
            continue
        cooldown = item.get("cooldown_seconds", 0)
        state = "冷却中 {}秒".format(cooldown) if cooldown else "可用"
        latency = item.get("last_latency_seconds")
        latency_text = "暂无" if latency is None else "{:.2f}秒".format(latency)
        lines.append(
            "{}（{}）：{}\n"
            "  成功 {}/失败 {}，最近耗时 {}\n"
            "  最近成功 {}，最近失败 {}{}".format(
                name, item["model"], state,
                item.get("successes", 0), item.get("failures", 0), latency_text,
                _time_text(item.get("last_success")),
                _time_text(item.get("last_failure")),
                "（{}）".format(item.get("last_error")) if item.get("last_error") else "",
            )
        )
    lines.append("SigmaI 失败或超时后会串行降级到 DeepSeek（先等 SigmaI 结束，不会并行兜底）。")
    return "\n".join(lines)

async def _call_vision_api(config, image_url, session=None):
    runtime = config.get("runtime", {})
    async with _get_semaphore("vision", runtime.get("vision_concurrency", 1)):
        return await _call_vision_api_inner(config, image_url, session)

async def _call_vision_api_inner(config, image_url, session=None):
    """Describe an image via the configured vision API (Aliyun qwen-vl)."""
    vision_cfg = config.get("vision_api", {})
    prompt = "请详细描述这张图片或表情包的内容和含义。如果是表情包/梗图请说明图中的人物、表情、文字和整体含义；如果是照片请描述场景和主体。一句话概括（10-30字）"
    async def _call_openai_compat(cfg, label):
        if not cfg.get("api_key"):
            return None
        headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
        payload = {
            "model": cfg["model"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }],
            "max_tokens": 100,
            "temperature": 0.3,
        }
        url = f"{cfg['base_url']}/chat/completions"
        async def _do(sess):
            async with sess.post(url, headers=headers, json=payload,
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                body = await resp.text()
                log.warning("%s vision returned %d: %s", label, resp.status, body[:200])
                return None
        try:
            if session:
                return await _do(session)
            async with aiohttp.ClientSession() as s:
                return await _do(s)
        except Exception as e:
            log.warning("%s vision failed: %s", label, e)
            return None
    # Configured vision API (Aliyun DashScope)
    api_key = _get_vision_api_key(config)
    if api_key and vision_cfg:
        ds_cfg = {
            "api_key": api_key,
            "model": vision_cfg.get("model", "qwen-vl-plus"),
            "base_url": vision_cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        }
        result = await _call_openai_compat(ds_cfg, "Fallback")
        if result:
            log.info("Vision request completed through fallback provider")
            return result
    return None

async def generate_image(dispatcher, prompt, session=None):
    """Generate an image using Agnes API (OpenAI-compatible /v1/images/generations)."""
    config = dispatcher.config
    agnes_cfg = _get_agnes_config(config)
    if not agnes_cfg["api_key"]:
        return None, "Agnes API key not configured"
    headers = {
        "Authorization": f"Bearer {agnes_cfg['api_key']}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "agnes-image-2.1-flash",
        "prompt": prompt,
        "size": "1024x1024",
        "n": 1,
    }
    url = f"{agnes_cfg['base_url']}/images/generations"
    timeout = aiohttp.ClientTimeout(total=60)
    async def _do(sess):
        async with sess.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                # OpenAI-compatible: data["data"][0]["url"]
                if data.get("data"):
                    return data["data"][0].get("url"), None
            else:
                body = await resp.text()
                log.warning("Image gen API returned %d: %s", resp.status, body[:200])
                return None, f"生图失败 (HTTP {resp.status})"
        return None, "生图失败，请稍后重试"
    try:
        if session:
            return await _do(session)
        async with aiohttp.ClientSession() as s:
            return await _do(s)
    except asyncio.TimeoutError:
        log.warning("Image generation timeout")
        return None, "生图超时了，再试一次吧"
    except Exception as e:
        log.error("Image generation error: %s", e)
        return None, f"生图出错: {str(e)[:80]}"
