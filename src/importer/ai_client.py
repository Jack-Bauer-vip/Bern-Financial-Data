"""AI 客户端 — 调用本地 ollama（deepseek-r1）做智能识别兜底

经验证：
- /api/generate + format=json 对 deepseek-r1 输出不稳定（thinking 干扰）
- /api/chat 端点稳定，返回 markdown 围栏包裹的 JSON
- 本地 14B 模型响应约 5-20 秒 → 调用方必须在后台线程使用

设计：所有方法可降级——AI 不可用 / 超时 / JSON 解析失败时返回 None，
上层退回规则匹配结果。
"""

import json
import re
from typing import Any, Optional

import httpx

from src.utils.config import ConfigManager


def strip_think(text: str) -> str:
    """剥离 deepseek-r1 的 <think>...</think> 思维链前缀"""
    if not text:
        return text
    m = re.match(r"^\s*<think>[\s\S]*?</think>\s*", text, re.I)
    return text[m.end():] if m else text


def extract_json(text: str) -> Optional[dict]:
    """从模型输出中提取 JSON 对象（防御式解析，失败返回 None）

    处理：<think> 前缀 → ```json 围栏 → 平衡括号扫描 → 尾随逗号容错。
    """
    if not text:
        return None
    text = strip_think(text)
    # 去掉 ```json ... ``` 围栏
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        # 直接找第一个 {...}（平衡括号）
        m = re.search(r"\{", text)
        if m:
            start = m.start()
            depth = 0
            end = len(text)
            for i in range(start, len(text)):
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            text = text[start:end]
    try:
        return json.loads(text)
    except Exception:
        # 容错：去掉尾随逗号等常见问题后重试
        cleaned = re.sub(r",\s*}", "}", text)
        try:
            return json.loads(cleaned)
        except Exception:
            return None


class AiClient:
    """AI 识别客户端（默认本地 ollama，可配置 deepseek API）"""

    def __init__(self, config: ConfigManager | None = None):
        self.config = config or ConfigManager()
        self.provider = self.config.get("ai.provider", "ollama")
        self.timeout = float(self.config.get("ai.ollama_timeout", 120))

    # ------------------------------------------------------------------
    # 探测
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """探测 AI 服务是否在线（快速，短超时）"""
        if self.provider == "ollama":
            url = self.config.get("ai.ollama_url", "http://localhost:11434")
            try:
                r = httpx.get(f"{url}/api/tags", timeout=3)
                return r.status_code == 200
            except Exception:
                return False
        # deepseek API
        key = self.config.get_env("DEEPSEEK_API_KEY")
        return bool(key)

    # ------------------------------------------------------------------
    # 目标表识别
    # ------------------------------------------------------------------

    def identify_table(
        self,
        columns: list[str],
        sample_values: dict,
        tables_desc: str,
    ) -> Optional[dict]:
        """判断文件应导入哪张表

        Returns:
            {"table": str, "confidence": float, "reason": str} 或 None（失败）
        """
        file_desc = json.dumps(sample_values, ensure_ascii=False)
        prompt = (
            f"把待导入文件匹配到数据库表。\n\n"
            f"表清单:\n{tables_desc}\n\n"
            f"待导入文件的列和样本值: {file_desc}\n\n"
            f"请判断应导入哪张表，只回复JSON: "
            '{"table":"表名","confidence":0到1数字,"reason":"简短中文原因"}'
        )
        content = self._chat(prompt)
        if not content:
            return None
        data = extract_json(content)
        if not data or not data.get("table"):
            return None
        return data

    def map_columns(
        self, file_columns: list[str], table_columns: list[str]
    ) -> Optional[dict]:
        """让 AI 给出文件列 → 表列的映射 JSON"""
        prompt = (
            f"把以下文件列名映射到数据库表的列名。\n"
            f"文件列: {json.dumps(file_columns, ensure_ascii=False)}\n"
            f"表列: {json.dumps(table_columns, ensure_ascii=False)}\n\n"
            f"只回复JSON，格式: "
            '{"文件列名":"表列名", ...}，映射不上的列不要出现在结果里'
        )
        content = self._chat(prompt)
        if not content:
            return None
        data = extract_json(content)
        if not isinstance(data, dict):
            return None
        return data

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _chat(self, prompt: str) -> Optional[str]:
        """调用聊天端点，返回 assistant 文本（失败返回 None）"""
        try:
            if self.provider == "ollama":
                url = self.config.get("ai.ollama_url", "http://localhost:11434")
                model = self.config.get("ai.ollama_model", "deepseek-r1:14b")
                r = httpx.post(
                    f"{url}/api/chat",
                    json={
                        "model": model,
                        "stream": False,
                        "think": False,   # 抑制思维链，稳定 JSON 输出
                        "messages": [
                            {"role": "system",
                             "content": "你是金融数据导入助手，根据列名和样本值判断应导入哪张数据库表。"},
                            {"role": "user", "content": prompt},
                        ],
                    },
                    timeout=self.timeout,
                )
                if r.status_code != 200:
                    return None
                return r.json().get("message", {}).get("content")
            # deepseek API（OpenAI 兼容）
            key = self.config.get_env("DEEPSEEK_API_KEY")
            if not key:
                return None
            base = self.config.get("ai.deepseek_base_url",
                                   "https://api.deepseek.com")
            model = self.config.get("ai.deepseek_model", "deepseek-chat")
            r = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system",
                         "content": "你是金融数据导入助手，根据列名和样本值判断应导入哪张数据库表。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            if r.status_code != 200:
                return None
            return r.json()["choices"][0]["message"]["content"]
        except Exception:
            return None
