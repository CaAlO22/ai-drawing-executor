# -*- coding: utf-8 -*-
"""OpenAI 兼容 Chat Completions 客户端(纯标准库, 在后台线程调用)。

- base_url 兼容三种填法: 完整 /chat/completions、/v1 根、裸 host。
- api_key 通过 Authorization: Bearer 传递, 绝不打印。
- 异常统一抛 ModelError, 消息中不包含 api_key。
"""
import json
import urllib.error
import urllib.request


class ModelError(RuntimeError):
    """模型请求失败, message 面向用户/模型可读, 不泄露 api_key。"""


def _normalize_chat_url(base_url: str) -> str:
    url = str(base_url).strip().rstrip("/")
    if not url:
        raise ModelError("Model base_url is empty.")
    if url.endswith("/chat/completions"):
        return url
    # 常见: https://host[:port]/v1 -> 拼 chat/completions
    return url + "/chat/completions"


class ModelClient:
    def __init__(self, base_url: str, api_key: str, model_name: str,
                 timeout_seconds: int = 120):
        self.url = _normalize_chat_url(base_url)
        self.api_key = str(api_key or "")
        self.model_name = str(model_name or "")
        self.timeout = max(5, int(timeout_seconds or 120))

    @property
    def ready(self) -> bool:
        return bool(self.url and self.api_key and self.model_name)

    def chat(self, messages: list, tools: list = None,
             temperature: float = 0.7) -> dict:
        """调用模型。返回 {"content": str, "tool_calls": [...], "raw": {...}}。

        tool_calls 元素: {"id": str, "name": str, "arguments": dict}
        """
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise ModelError(f"Model API HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise ModelError(f"Model API connection failed: {e.reason}") from None
        except json.JSONDecodeError:
            raise ModelError("Model API returned invalid JSON.") from None
        except TimeoutError:
            raise ModelError("Model API request timed out.") from None

        try:
            choice = data["choices"][0]
            message = choice.get("message", {}) or {}
        except (KeyError, IndexError, TypeError):
            raise ModelError("Model API response missing choices[0].message.") from None

        content = message.get("content") or ""
        tool_calls = []
        for tc in (message.get("tool_calls") or []):
            try:
                fn = tc.get("function", {}) or {}
                args_raw = fn.get("arguments") or "{}"
                if isinstance(args_raw, str):
                    args = json.loads(args_raw) if args_raw.strip() else {}
                else:
                    args = args_raw
                tool_calls.append({
                    "id": tc.get("id") or f"call_{len(tool_calls)}",
                    "name": fn.get("name") or "",
                    "arguments": args if isinstance(args, dict) else {},
                })
            except (json.JSONDecodeError, AttributeError, TypeError):
                tool_calls.append({
                    "id": tc.get("id") or f"call_{len(tool_calls)}",
                    "name": fn.get("name") or "",
                    "arguments": {"_raw": str(fn.get("arguments"))[:200]},
                    "_parse_error": True,
                })
        return {"content": content, "tool_calls": tool_calls, "raw": data}
