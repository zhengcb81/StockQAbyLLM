#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海康威视问答演示 - 使用真实的网络搜索。

这个演示程序展示了如何扩展 StockQAbyLLM 系统以使用真实的网络搜索。
"""

import sys
from pathlib import Path

# 添加 src 到 Python 路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.core.qa_engine import QAEngine
from src.config.config_manager import ConfigManager
from src.services.answer_generator import AnswerGenerator
from src.core.models import Question, Answer
from src.interfaces.search_provider import SearchProvider
from src.core.exceptions import ProcessingError
from src.utils.logger import get_logger

# 尝试导入 web 搜索库
try:
    import requests
    from bs4 import BeautifulSoup
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False
    print("警告: 未安装 requests 或 beautifulsoup4，将使用占位符实现")
    print("安装命令: pip install requests beautifulsoup4")


class RealWebSearchProvider(SearchProvider):
    """真实的网络搜索提供者（使用百度搜索）。"""

    def __init__(self):
        """初始化网络搜索提供者。"""
        self.search_url = "https://www.baidu.com/s"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        self.logger = get_logger(__name__)

    def search(self, query: str):
        """执行网络搜索。

        Args:
            query: 搜索查询

        Returns:
            搜索结果列表
        """
        if not HAS_DEPS:
            # 回退到占位符实现
            return [{
                "title": f"关于 '{query}' 的搜索结果",
                "url": "https://www.baidu.com",
                "snippet": f"这是关于 '{query}' 的模拟搜索结果。",
                "source": "mock_search"
            }]

        self.logger.info(f"正在搜索: {query}")

        try:
            # 构建搜索参数
            params = {'wd': query, 'rn': 5}

            # 发送请求
            response = requests.get(
                self.search_url,
                params=params,
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()

            # 解析 HTML
            soup = BeautifulSoup(response.text, 'html.parser')

            # 提取搜索结果
            results = []
            divs = soup.find_all('div', class_='result')

            for i, div in enumerate(divs[:3], 1):  # 只取前 3 个结果
                title_tag = div.find('h3')
                snippet_tag = div.find('div', class_='c-abstract')

                if title_tag:
                    title = title_tag.get_text(strip=True)
                    snippet = snippet_tag.get_text(strip=True) if snippet_tag else "无摘要"

                    results.append({
                        "title": title,
                        "url": "https://www.baidu.com",  # 实际 URL 需要进一步解析
                        "snippet": snippet[:200],  # 限制摘要长度
                        "source": "baidu_web_search"
                    })

            if not results:
                self.logger.warning(f"未找到搜索结果: {query}")
                return [{
                    "title": f"未找到 '{query}' 的相关结果",
                    "url": "",
                    "snippet": f"抱歉，未找到关于 '{query}' 的相关信息。",
                    "source": "no_results"
                }]

            self.logger.info(f"搜索完成，找到 {len(results)} 个结果")
            return results

        except requests.RequestException as e:
            self.logger.error(f"网络请求失败: {e}")
            raise ProcessingError(
                message=f"网络搜索失败: {str(e)}",
                question=query
            )
        except Exception as e:
            self.logger.error(f"搜索失败: {e}")
            raise ProcessingError(
                message=f"搜索执行失败: {str(e)}",
                question=query
            )

    def get_provider_name(self) -> str:
        """获取提供者名称。"""
        return "real_web_search"


def main():
    """主函数。"""
    logger = get_logger(__name__)
    logger.info("=" * 60)
    logger.info("海康威视问答演示 - 使用真实网络搜索")
    logger.info("=" * 60)

    try:
        # 加载问题
        config_manager = ConfigManager("hikvision_questions.txt")
        questions = config_manager.load_questions()

        # 初始化组件
        search_provider = RealWebSearchProvider()
        answer_generator = AnswerGenerator()
        qa_engine = QAEngine(search_provider, answer_generator)

        # 处理问题
        logger.info(f"\n开始处理 {len(questions)} 个关于海康威视的问题...\n")

        batch_result = qa_engine.process_questions(questions)

        # 显示结果
        print("\n" + "=" * 60)
        print("问答结果")
        print("=" * 60 + "\n")

        for i, result in enumerate(batch_result.results, 1):
            print(f"[{i}/{len(questions)}] 问题：{result.question.text}")
            print(f"    答案：{result.answer.text}")
            print(f"    来源：{result.answer.source}\n")

        # 输出 JSON
        qa_engine.output_results(batch_result, "outputs/hikvision_real_results.json")

        # 显示统计
        stats = qa_engine.get_statistics(batch_result)
        logger.info(f"\n处理统计: {stats}")

        logger.info("\n" + "=" * 60)
        logger.info("演示完成！")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"演示失败: {e}", exc_info=True)
        return 1

    return 0


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
