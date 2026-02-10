#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用的LLM提供者。

该提供者使用LLM API来分析公司并返回评分和描述。
完全通用，不硬编码任何公司特定的信息。
支持多个LLM提供商和自动切换。
"""

import json
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple, cast

import requests
from src.interfaces.search_provider import SearchProvider
from src.config.llm_config import LLMConfig
from src.config.settings import (
    DEFAULT_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_SCORE,
    DEFAULT_RETRY_DELAY,
    MAX_TOKENS,
    TEMPERATURE,
    SCORE_MIN,
    SCORE_MAX,
    DISPLAY_QUERY_TRUNCATE,
    DISPLAY_TITLE_TRUNCATE,
)
from src.utils.logger import get_logger
from src.utils.http_client import http_client_manager

logger = get_logger(__name__)


def mask_api_key(api_key: str) -> str:
    """脱敏API密钥，只显示前4位和后4位。

    Args:
        api_key: 原始API密钥

    Returns:
        脱敏后的API密钥，如: sk-***abcd
    """
    if not api_key:
        return "***"
    if len(api_key) <= 8:
        return "***"
    return f"{api_key[:4]}...{api_key[-4:]}"


class LLMProvider(SearchProvider):
    """使用LLM API的提供者。

    通过通用提示词让LLM分析任意公司并生成评分（1-10）和详细描述。
    支持多个LLM提供商和自动故障切换。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        company_name: Optional[str] = None,
        provider_name: Optional[str] = None,
        config_file: str = "llm_apis.json",
    ):
        """初始化LLM提供者。

        Args:
            api_key: LLM API密钥（可选，优先从配置文件读取）
            model: 模型名称（可选，从配置文件读取）
            company_name: 要分析的公司名称（可选，用于提示词）
            provider_name: 指定使用的提供商名称（可选）
            config_file: LLM配置文件路径
        """
        self.config_manager = LLMConfig(config_file)
        self.company_name = company_name
        self.provider_name = provider_name or self.config_manager.get_default_provider()
        self.api_key = api_key
        self.model = model

        # 如果没有提供 api_key 或 model，从配置文件读取
        if not self.api_key or not self.model:
            provider_config = self.config_manager.get_provider_config(self.provider_name)
            if provider_config:
                # 优先从环境变量读取 API 密钥
                if not self.api_key:
                    # 尝试从环境变量获取（支持多种命名方式）
                    env_key_names = [
                        f"{self.provider_name.upper()}_API_KEY",
                        f"{self.provider_name.upper()}_KEY",
                        "API_KEY",
                        "LLM_API_KEY",
                    ]
                    for env_key in env_key_names:
                        env_api_key = os.getenv(env_key)
                        if env_api_key:
                            self.api_key = env_api_key
                            logger.info(f"从环境变量 {env_key} 读取 API 密钥")
                            break

                    # 如果环境变量没有，再从配置文件读取
                    if not self.api_key:
                        self.api_key = provider_config.get("api_key", "")
                        if self.api_key:
                            logger.warning(f"从配置文件读取 API 密钥（建议使用环境变量）")

                if not self.model:
                    self.model = provider_config.get("model", "")
                self.base_url = provider_config.get("base_url", "")
                self.timeout = provider_config.get("timeout", DEFAULT_TIMEOUT)
                self.max_retries = provider_config.get("max_retries", DEFAULT_MAX_RETRIES)

        masked_key = mask_api_key(self.api_key) if self.api_key else "N/A"
        logger.info(
            f"LLMProvider initialized (provider: {self.provider_name}, "
            f"model: {self.model}, api_key: {masked_key})"
        )

    def search(self, query: str) -> List[Dict[str, Any]]:  # type: ignore[override]
        """使用LLM API分析问题。

        Args:
            query: 问题文本

        Returns:
            包含评分和描述的搜索结果
        """
        logger.info(f"LLM处理问题: {query[:DISPLAY_QUERY_TRUNCATE]}...")

        # 构建通用提示词
        prompt = self._build_prompt(query)

        # 调用LLM API
        score, description = self._call_llm_api(prompt)

        result = {
            "title": f"关于 '{query[:DISPLAY_TITLE_TRUNCATE]}...' 的评估",
            "url": "",
            "snippet": description,
            "score": score,
            "source": "llm_api",
        }

        logger.info(f"LLM答案生成完成 (评分: {score}/10)")
        return [result]

    def _build_prompt(self, question: str) -> str:
        """构建通用的LLM提示词。

        完全通用，不包含任何特定公司的硬编码信息。

        Args:
            question: 用户的问题

        Returns:
            完整的提示词
        """
        # 如果指定了公司名称，在提示词中提及
        company_context = f"关于公司：{self.company_name}\n" if self.company_name else ""

        prompt = f"""你是一位专业的投资分析师。请对以下问题进行深入分析并给出评分。

{company_context}**问题**：
{question}

**要求**：
1. 基于该公司的实际情况进行分析
2. 给出一个1-10分的评分（1=最差，10=最好）
3. 提供详细的评分理由和分析，包括：
   - 评估维度的具体表现
   - 支撑评分的数据或事实
   - 相关风险因素
   - 与行业同行的对比（如适用）
4. **重要**：请严格按以下JSON格式返回：
{{
  "score": <评分数字，1-10之间的整数>,
  "description": "<详细分析描述，包含评估维度、具体分析、数据支撑、风险因素等>"
}}

请返回JSON格式的回答："""
        return prompt

    def get_provider_name(self) -> str:
        """获取提供者名称。"""
        return self.provider_name or "llm_api"

    # ========== API 调用辅助函数 ==========

    def _send_api_request(self, prompt: str) -> str:
        """发送 API 请求并返回响应内容。

        Args:
            prompt: 提示词

        Returns:
            LLM 响应内容

        Raises:
            requests.RequestException: API 请求失败
        """
        # 使用共享的 HTTP Session（连接池）
        session = http_client_manager.get_sync_session()

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一位专业的投资分析师，擅长分析公司的投资价值。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
        }

        base_url = getattr(self, "base_url", "https://api.deepseek.com/v1/chat/completions")
        timeout = getattr(self, "timeout", DEFAULT_TIMEOUT)

        logger.debug(f"发送请求到 {base_url} (超时: {timeout}s, 模型: {self.model})")

        response = session.post(base_url, headers=headers, json=data, timeout=timeout)

        response.raise_for_status()
        result = response.json()

        # 提取LLM返回的内容
        content = cast(str, result["choices"][0]["message"]["content"])
        logger.info(f"{self.provider_name} API返回内容长度: {len(content)} 字符")
        return content

    def _try_parse_json_direct(self, content: str) -> Optional[Tuple[int, str]]:
        """尝试直接解析 JSON。

        Args:
            content: LLM 响应内容

        Returns:
            (评分, 描述) 元组，如果解析失败则返回 None
        """
        try:
            llm_response = json.loads(content)

            # 验证JSON格式
            if "score" not in llm_response or "description" not in llm_response:
                logger.warning("LLM返回的JSON格式不符合要求")
                return None

            score = int(llm_response.get("score", DEFAULT_SCORE))
            description = llm_response.get("description", content)
            # 确保评分在1-10范围
            score = max(SCORE_MIN, min(SCORE_MAX, score))
            logger.info(f"成功解析LLM响应（直接JSON），评分: {score}/10")
            return (score, description)

        except json.JSONDecodeError:
            return None

    def _try_extract_json_with_regex(self, content: str) -> Optional[Tuple[int, str]]:
        """使用正则表达式从文本中提取 JSON。

        Args:
            content: LLM 响应内容

        Returns:
            (评分, 描述) 元组，如果解析失败则返回 None
        """
        # 查找JSON代码块（支持多行和嵌套）
        json_patterns = [
            r'\{[^{}]*"score"[^{}]*"description"[^{}]*\}',  # 简单模式
            (
                r'\{(?:[^{}]|\{[^{}]*\})*"score"(?:[^{}]|\{[^{}]*\})*'
                r'"description"(?:[^{}]|\{[^{}]*\})*\}'  # 支持一层嵌套
            ),
            r"```json\s*(\{.*?\})\s*```",  # Markdown 代码块
            r'(\{[^{}]*"score".*?"description"[^{}]*\})',  # 更宽松的匹配
        ]

        for pattern in json_patterns:
            json_match = re.search(pattern, content, re.DOTALL)
            if json_match:
                try:
                    json_str = json_match.group(1) if "```json" in pattern else json_match.group(0)
                    llm_response = json.loads(json_str)

                    if "score" in llm_response and "description" in llm_response:
                        score = int(llm_response.get("score", DEFAULT_SCORE))
                        description = llm_response.get("description", content)
                        score = max(SCORE_MIN, min(SCORE_MAX, score))
                        logger.info(f"成功解析LLM响应（正则提取），评分: {score}/10")
                        return (score, description)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.warning(f"使用模式解析失败: {e}")
                    continue

        return None

    def _parse_llm_response(
        self, content: str, retry: int, max_retries: int
    ) -> Optional[Tuple[int, str]]:
        """解析 LLM 响应。

        Args:
            content: LLM 响应内容
            retry: 当前重试次数
            max_retries: 最大重试次数

        Returns:
            (评分, 描述) 元组，如果解析失败则返回 None
        """
        # 尝试直接解析JSON
        result = self._try_parse_json_direct(content)
        if result:
            return result

        # 如果直接解析失败，尝试从文本中提取JSON
        logger.warning("无法直接解析JSON，尝试从文本中提取")
        result = self._try_extract_json_with_regex(content)
        if result:
            return result

        # 如果所有解析方法都失败
        logger.error(f"无法从LLM响应中解析JSON，将在 {retry + 1} 秒后重试...")
        return None

    def _call_llm_api(self, prompt: str, max_retries: Optional[int] = None) -> Tuple[int, str]:
        """调用LLM API，带重试机制。

        Args:
            prompt: 提示词
            max_retries: 最大重试次数（默认从配置读取）

        Returns:
            (评分, 描述) 的元组

        Raises:
            Exception: 所有重试都失败后抛出异常
        """
        if not self.api_key:
            # 未配置API密钥，返回占位符
            logger.warning("LLM API未配置，返回占位符结果")
            return (DEFAULT_SCORE, "LLM API未配置。请配置 API密钥。")

        # 使用配置的重试次数，如果没有指定则使用实例的max_retries
        if max_retries is None:
            max_retries = getattr(self, "max_retries", DEFAULT_MAX_RETRIES)

        for retry in range(max_retries):
            try:
                logger.info(
                    f"正在调用 {self.provider_name} API... (尝试 {retry + 1}/{max_retries})"
                )

                # 发送 API 请求
                content = self._send_api_request(prompt)

                # 解析响应
                result = self._parse_llm_response(content, retry, max_retries)
                if result:
                    return result

                # 解析失败，等待重试
                if retry < max_retries - 1:
                    time.sleep(retry + 1)
                    continue
                else:
                    # 最后一次重试也失败，返回默认评分
                    logger.error(f"经过 {max_retries} 次尝试后仍无法解析JSON，返回默认评分")
                    return (DEFAULT_SCORE, content)

            except requests.RequestException as e:
                # 网络错误或超时，进行重试
                if retry < max_retries - 1:
                    wait_time = (retry + 1) * DEFAULT_RETRY_DELAY  # 指数退避：2秒、4秒、6秒...
                    logger.warning(
                        f"API请求失败（第{retry + 1}次尝试）: {e}，{wait_time}秒后重试..."
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    # 所有重试都失败，抛出异常
                    logger.error(f"{self.provider_name} API请求失败，已重试{max_retries}次: {e}")
                    raise Exception(f"API请求失败，已重试{max_retries}次: {str(e)}")

            except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError) as e:
                # JSON解析或其他数据处理错误，进行重试
                if retry < max_retries - 1:
                    logger.warning(
                        f"数据处理错误（第{retry + 1}次尝试）: {e}，{retry + 1}秒后重试..."
                    )
                    time.sleep(retry + 1)
                    continue
                else:
                    # 所有重试都失败，返回默认评分
                    logger.error(f"经过 {max_retries} 次尝试后仍无法处理响应: {e}")
                    return (DEFAULT_SCORE, f"数据处理失败: {str(e)}")

        # 理论上不会到这里，但为了完整性
        raise Exception("API调用失败：未知错误")
