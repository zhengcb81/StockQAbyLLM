"""批量处理策略接口。

该模块定义了批量处理的策略接口。
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, List, TypeVar, Generic, Optional
from dataclasses import dataclass

from src.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class BatchProgress:
    """批量处理进度信息。"""

    current: int
    total: int
    success_count: int
    error_count: int

    @property
    def percentage(self) -> float:
        """获取完成百分比。"""
        if self.total == 0:
            return 0.0
        return (self.current / self.total) * 100

    @property
    def is_complete(self) -> bool:
        """是否已完成。"""
        return self.current >= self.total


class BatchStrategy(ABC, Generic[T, R]):
    """批量处理策略抽象基类。

    所有批量处理策略都应该继承此类并实现 process 方法。
    """

    @abstractmethod
    def process(
        self,
        items: List[T],
        processor: Callable[[T], R],
        on_progress: Optional[Callable[[BatchProgress], None]] = None,
    ) -> List[R]:
        """批量处理项目。

        Args:
            items: 要处理的项目列表
            processor: 处理单个项目的函数
            on_progress: 进度回调函数

        Returns:
            处理结果列表
        """
        pass

    @abstractmethod
    def get_strategy_name(self) -> str:
        """获取策略名称。

        Returns:
            策略名称
        """
        pass


class SerialStrategy(BatchStrategy[T, R]):
    """串行处理策略。

    逐个处理项目，适用于需要按顺序处理的场景。
    """

    def get_strategy_name(self) -> str:
        """获取策略名称。"""
        return "serial"

    def process(
        self,
        items: List[T],
        processor: Callable[[T], R],
        on_progress: Optional[Callable[[BatchProgress], None]] = None,
    ) -> List[R]:
        """串行处理项目。

        Args:
            items: 要处理的项目列表
            processor: 处理单个项目的函数
            on_progress: 进度回调函数

        Returns:
            处理结果列表
        """
        results: List[R] = []
        success_count = 0
        error_count = 0

        for i, item in enumerate(items, 1):
            try:
                result = processor(item)
                results.append(result)
                success_count += 1
            except Exception as e:
                logger.error("处理第 %d 个项目时出错: %s", i, e)
                error_count += 1

            if on_progress:
                progress = BatchProgress(
                    current=i,
                    total=len(items),
                    success_count=success_count,
                    error_count=error_count,
                )
                on_progress(progress)

        logger.info(f"串行处理完成: 总数={len(items)}, 成功={success_count}, 失败={error_count}")
        return results


class ChunkedStrategy(BatchStrategy[T, R]):
    """分块处理策略。

    将项目分成多个块进行处理，适用于大批量数据。
    """

    def __init__(self, chunk_size: int = 10):
        """初始化分块处理策略。

        Args:
            chunk_size: 每块的大小
        """
        self.chunk_size = chunk_size

    def get_strategy_name(self) -> str:
        """获取策略名称。"""
        return "chunked"

    def process(
        self,
        items: List[T],
        processor: Callable[[T], R],
        on_progress: Optional[Callable[[BatchProgress], None]] = None,
    ) -> List[R]:
        """分块处理项目。

        Args:
            items: 要处理的项目列表
            processor: 处理单个项目的函数
            on_progress: 进度回调函数

        Returns:
            处理结果列表
        """
        results: List[R] = []
        success_count = 0
        error_count = 0
        processed = 0

        # 分块处理
        for chunk_start in range(0, len(items), self.chunk_size):
            chunk = items[chunk_start : chunk_start + self.chunk_size]

            for item in chunk:
                processed += 1
                try:
                    result = processor(item)
                    results.append(result)
                    success_count += 1
                except Exception as e:
                    logger.error("处理项目时出错: %s", e)
                    error_count += 1

                if on_progress:
                    progress = BatchProgress(
                        current=processed,
                        total=len(items),
                        success_count=success_count,
                        error_count=error_count,
                    )
                    on_progress(progress)

        logger.info(
            f"分块处理完成: 总数={len(items)}, 块大小={self.chunk_size}, "
            f"成功={success_count}, 失败={error_count}"
        )
        return results


class AsyncStrategy(BatchStrategy[T, R]):
    """异步处理策略。

    使用异步方式并发处理项目。
    """

    def __init__(self, max_concurrency: int = 5):
        """初始化异步处理策略。

        Args:
            max_concurrency: 最大并发数
        """
        self.max_concurrency = max_concurrency

    def get_strategy_name(self) -> str:
        """获取策略名称。"""
        return "async"

    def process(
        self,
        items: List[T],
        processor: Callable[[T], R],
        on_progress: Optional[Callable[[BatchProgress], None]] = None,
    ) -> List[R]:
        """异步处理项目。

        注意：此方法是同步包装器，如果处理器是异步的，
        需要使用 run_async 方法。

        Args:
            items: 要处理的项目列表
            processor: 处理单个项目的函数
            on_progress: 进度回调函数

        Returns:
            处理结果列表
        """
        import asyncio

        # 检查是否在事件循环中
        try:
            loop = asyncio.get_running_loop()
            # 如果已经在事件循环中，创建任务并返回空列表（需要使用 await）
            # 注意：这不是理想的实现，但符合类型系统
            asyncio.create_task(self._process_async(items, processor, on_progress))
            return []
        except RuntimeError:
            # 没有运行的事件循环，创建新的
            return asyncio.run(self._process_async(items, processor, on_progress))

    async def _process_async(
        self,
        items: List[T],
        processor: Callable[[T], R],
        on_progress: Optional[Callable[[BatchProgress], None]] = None,
    ) -> List[R]:
        """异步处理项目的内部实现。"""
        import asyncio

        results: List[R] = []
        success_count = 0
        error_count = 0

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def process_with_semaphore(item: T, index: int) -> Optional[R]:
            nonlocal success_count, error_count
            async with semaphore:
                try:
                    if asyncio.iscoroutinefunction(processor):
                        result = await processor(item)
                    else:
                        result = processor(item)
                    success_count += 1
                    return result  # type: ignore[no-any-return]
                except Exception as e:
                    logger.error("处理第 %s 个项目时出错: %s", index, e)
                    error_count += 1
                    return None
                finally:
                    if on_progress:
                        progress = BatchProgress(
                            current=index,
                            total=len(items),
                            success_count=success_count,
                            error_count=error_count,
                        )
                        on_progress(progress)

        tasks = [process_with_semaphore(item, i + 1) for i, item in enumerate(items)]
        results_or_none = await asyncio.gather(*tasks)
        results = [r for r in results_or_none if r is not None]

        logger.info(
            f"异步处理完成: 总数={len(items)}, 并发数={self.max_concurrency}, "
            f"成功={success_count}, 失败={error_count}"
        )
        return results


class BatchStrategyFactory:
    """批量处理策略工厂。

    用于创建不同的批量处理策略。
    """

    _strategies = {
        "serial": SerialStrategy,
        "chunked": ChunkedStrategy,
        "async": AsyncStrategy,
    }

    @classmethod
    def create(cls, strategy_name: str, **kwargs: Any) -> BatchStrategy[Any, Any]:
        """创建批量处理策略。

        Args:
            strategy_name: 策略名称（serial, chunked, async）
            **kwargs: 传递给策略的参数

        Returns:
            策略实例

        Raises:
            ValueError: 如果策略名称无效
        """
        strategy_name_lower = strategy_name.lower()
        if strategy_name_lower not in cls._strategies:
            raise ValueError(
                f"不支持的策略: {strategy_name}。支持的策略: {list(cls._strategies.keys())}"
            )

        strategy_class = cls._strategies[strategy_name_lower]
        return strategy_class(**kwargs)  # type: ignore[no-any-return]

    @classmethod
    def get_supported_strategies(cls) -> List[str]:
        """获取支持的策略列表。

        Returns:
            支持的策略名称列表
        """
        return list(cls._strategies.keys())
