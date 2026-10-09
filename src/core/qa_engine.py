"""Q&A 引擎核心编排器。

该模块是系统的核心，负责协调问题处理的整个流程。
"""

from contextlib import nullcontext
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.config.settings import (
    DISPLAY_QUESTION_TRUNCATE,
    DISPLAY_TITLE_TRUNCATE,
)
from src.core.exceptions import ProcessingError, ValidationError
from src.core.models import Answer, QABatchResult, QAResult, Question
from src.interfaces.progress_reporter import ProgressReporter
from src.interfaces.search_provider import SearchProvider
from src.services.answer_generator import AnswerGenerator
from src.utils.console_reporter import ConsoleReporter
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _checkpoint_created_at(provenance: Dict[str, Any]) -> Optional[datetime]:
    """Original execution time from checkpoint provenance (r1 P1-2: hydrated
    answers must never forge answer.created_at with the hydration wall
    clock). None keeps the dataclass default when provenance lacks a
    parseable timestamp."""
    raw = provenance.get("response_completed_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _provenance_metadata(provenance: Dict[str, Any]) -> Dict[str, Any]:
    """Answer-level metadata reconstructed from checkpoint provenance so the
    output envelope keeps the ORIGINAL execution receipt for hydrated answers
    (r1 P1-2: provider / search_status / answered_at must survive
    hydration instead of collapsing to null / not_attempted)."""
    actual_provider = provenance.get("actual_provider")
    return {
        "actual_model": provenance.get("actual_model"),
        "model_requested": provenance.get("model_requested"),
        "request_id": provenance.get("request_id"),
        "response_id": provenance.get("response_id"),
        "search_status": provenance.get("search_status"),
        "source_urls": provenance.get("source_urls") or [],
        "execution": {
            "provider": actual_provider,
            "response_status": provenance.get("response_status"),
            "http_status_code": provenance.get("http_status_code"),
            "completed_at": provenance.get("response_completed_at"),
            "attempt_id": provenance.get("provider_attempt_id"),
            "prompt_sha256": provenance.get("provider_prompt_sha256")
            or provenance.get("work_prompt_sha256"),
            "search_receipt_id": provenance.get("search_receipt_id"),
        },
        "attempts": [
            {
                "provider": actual_provider,
                "response_id": provenance.get("response_id"),
                "http_status_code": provenance.get("http_status_code"),
                "requested_model": provenance.get("model_requested"),
            }
        ],
    }


class QAEngine:
    """Q&A 处理引擎。

    负责协调整个问题-答案处理流程：
    1. 接收问题列表
    2. 使用搜索服务获取信息
    3. 使用答案生成器生成答案
    4. 返回结果
    """

    def __init__(
        self,
        search_provider: SearchProvider,
        answer_generator: Optional[AnswerGenerator] = None,
        progress_reporter: Optional[ProgressReporter] = None,
        work_item_lifecycle: Optional[Any] = None,
    ):
        """初始化 Q&A 引擎。

        Args:
            search_provider: 搜索提供者实例
            answer_generator: 答案生成器实例（可选）
            progress_reporter: 进度报告器实例（可选）
            work_item_lifecycle: 可选的逐题 work-item 生命周期钩子（Q06）。
                契约：before_question(question)→handle（claimed/reason）、
                after_question(handle, result)、after_question_failed(handle, error)。
                缺省 None 时行为与本钩子引入前逐字节一致（Q06 选项 1）。
        """
        self.search_provider = search_provider
        self.answer_generator = answer_generator or AnswerGenerator()
        self.progress_reporter = progress_reporter or ConsoleReporter()
        self.work_item_lifecycle = work_item_lifecycle
        logger.info("QAEngine 初始化完成（搜索提供者: %s）", search_provider.get_provider_name())

    def process_question(self, question_text: Any) -> QAResult:
        """处理单个问题。

        Args:
            question_text: 问题文本

        Returns:
            问答结果对象

        Raises:
            ValidationError: 问题验证失败
            ProcessingError: 问题处理失败
        """
        display_text = question_text.text if isinstance(question_text, Question) else question_text
        logger.debug("处理单个问题: %s...", display_text[:DISPLAY_QUESTION_TRUNCATE])

        # 验证并创建问题对象
        try:
            question = (
                question_text
                if isinstance(question_text, Question)
                else Question(text=question_text)
            )
        except ValueError as e:
            raise ValidationError(message=f"问题验证失败: {str(e)}", field="question") from e

        # 执行搜索
        try:
            search_question = getattr(self.search_provider, "search_question", None)
            if callable(search_question):
                search_results = search_question(question)
            else:
                search_results = self.search_provider.search(question.text)
            logger.debug("搜索完成，返回 %d 个结果", len(search_results))
        except ProcessingError:
            raise
        except Exception as e:
            logger.error("搜索失败（%s）", type(e).__name__)
            raise ProcessingError(
                message=f"搜索执行失败（{type(e).__name__}）", question=question.text
            ) from e

        # 生成答案
        try:
            answer = self.answer_generator.generate_answer(question, search_results)
        except ProcessingError:
            raise
        except Exception as e:
            logger.error("答案生成失败（%s）", type(e).__name__)
            raise ProcessingError(
                message=f"答案生成失败（{type(e).__name__}）", question=question.text
            ) from e

        # 创建结果对象
        result = QAResult(question=question, answer=answer)
        logger.debug("问题处理完成: %s...", question.text[:DISPLAY_TITLE_TRUNCATE])

        return result

    def process_questions(self, question_texts: List[Any]) -> QABatchResult:
        """批量处理问题。

        Args:
            question_texts: 问题文本列表

        Returns:
            批次结果对象

        Raises:
            ValidationError: 问题列表验证失败
        """
        if not question_texts:
            raise ValidationError(message="问题列表不能为空")

        batch_result = QABatchResult(total_questions=len(question_texts))
        logger.info("开始批量处理 %d 个问题", len(question_texts))

        # 使用进度报告器
        self.progress_reporter.start_batch(len(question_texts))
        refused_count = 0

        for i, question_text in enumerate(question_texts, 1):
            display_text = (
                question_text.text if isinstance(question_text, Question) else question_text
            )
            # 更新进度
            msg = f"正在处理: {display_text[:DISPLAY_QUESTION_TRUNCATE]}..."
            self.progress_reporter.update_progress(i, len(question_texts), msg)

            # Q06: work-item 生命周期（可选；claim 拒绝→不派发，记 error 结果）
            lifecycle_handle: Optional[dict] = None
            if self.work_item_lifecycle is not None:
                try:
                    hook_question = (
                        question_text
                        if isinstance(question_text, Question)
                        else Question(text=question_text)
                    )
                except ValueError:
                    hook_question = None
                if hook_question is not None:
                    # Q07: hydrate a stored answer checkpoint BEFORE any claim
                    # (JOB-03/PAR-04) — saved questions are never re-dispatched;
                    # hooks without hydrate_question keep the Q06 behavior.
                    hydrate = getattr(self.work_item_lifecycle, "hydrate_question", None)
                    checkpoint = hydrate(hook_question) if callable(hydrate) else None
                    if checkpoint is not None:
                        payload = checkpoint.get("payload") or {}
                        stored = payload.get("answer") or {}
                        provenance = payload.get("provenance") or {}
                        original_time = _checkpoint_created_at(provenance)
                        answer_metadata = _provenance_metadata(provenance)
                        external_execution = checkpoint.get("external_execution_metadata")
                        if isinstance(external_execution, dict):
                            answer_metadata["execution"] = external_execution
                            answer_metadata["attempts"] = [
                                external_execution["work_transport"]["final_receipt"]
                            ]
                        created_kwargs = (
                            {"created_at": original_time} if original_time is not None else {}
                        )
                        hydrated_result = QAResult(
                            question=hook_question,
                            answer=Answer(
                                text=str(stored.get("description") or ""),
                                score=stored.get("score"),
                                status=str(stored.get("status") or "scored"),
                                source="answer_checkpoint",
                                metadata=answer_metadata,
                                **created_kwargs,
                            ),
                            metadata={
                                "answer_checkpoint": {
                                    "hydrated": True,
                                    "provenance": payload.get("provenance"),
                                    "attempt_id": checkpoint.get("attempt_id"),
                                    "work_item_id": checkpoint.get("work_item_id"),
                                    "checkpointed_at": checkpoint.get("checkpointed_at"),
                                }
                            },
                        )
                        batch_result.add_result(hydrated_result)
                        logger.info("[%d/%d] Q07 水合已存检查点", i, len(question_texts))
                        hyd_msg = (
                            f"[HYD] 复用检查点: {display_text[:DISPLAY_QUESTION_TRUNCATE]}...  "
                        )
                        self.progress_reporter.update_progress(i, len(question_texts), hyd_msg)
                        continue
                    lifecycle_handle = self.work_item_lifecycle.before_question(hook_question)
                if lifecycle_handle is None or not lifecycle_handle.get("claimed"):
                    reason = (lifecycle_handle or {}).get("reason") or "lifecycle_refused"
                    logger.warning(
                        "[%d/%d] work claim 拒绝（%s），不派发", i, len(question_texts), reason
                    )
                    refuse_question = hook_question or (
                        question_text
                        if isinstance(question_text, Question)
                        else Question(text=question_text)
                    )
                    refuse_answer = Answer(
                        text=f"work claim 拒绝（{reason}），本题未派发",
                        score=None,
                        status="error",
                        source="work_store",
                    )
                    refuse_result = QAResult(
                        question=refuse_question,
                        answer=refuse_answer,
                        metadata={
                            "work_claim_refused": True,
                            "work_claim_reason": reason,
                            **(
                                {"work_item_id": lifecycle_handle["work_item_id"]}
                                if lifecycle_handle and lifecycle_handle.get("work_item_id")
                                else {}
                            ),
                        },
                    )
                    refused_count += 1
                    batch_result.add_result(refuse_result)
                    fail_msg = f"[SKIP] 未派发: {display_text[:DISPLAY_QUESTION_TRUNCATE]}...  "
                    self.progress_reporter.update_progress(i, len(question_texts), fail_msg)
                    continue

            try:
                context_factory = getattr(self.work_item_lifecycle, "dispatch_context", None)
                context = (
                    context_factory(lifecycle_handle)
                    if callable(context_factory) and lifecycle_handle is not None
                    else nullcontext()
                )
                with context:
                    result = self.process_question(question_text)
                if self.work_item_lifecycle is not None and lifecycle_handle is not None:
                    self.work_item_lifecycle.after_question(lifecycle_handle, result)
                batch_result.add_result(result)
                logger.info("[%d/%d] 处理成功", i, len(question_texts))

                # 更新进度（成功）
                done_msg = f"[OK] 完成: {display_text[:DISPLAY_QUESTION_TRUNCATE]}...  "
                self.progress_reporter.update_progress(i, len(question_texts), done_msg)

            except (ValidationError, ProcessingError) as e:
                logger.error("[%d/%d] 处理失败: %s", i, len(question_texts), e)
                if self.work_item_lifecycle is not None and lifecycle_handle is not None:
                    self.work_item_lifecycle.after_question_failed(lifecycle_handle, str(e))

                # 更新进度（失败）
                fail_msg = f"[FAIL] 失败: {display_text[:DISPLAY_QUESTION_TRUNCATE]}...  "
                self.progress_reporter.update_progress(i, len(question_texts), fail_msg)

                # 添加错误结果，继续处理下一个问题
                try:
                    error_question = (
                        question_text
                        if isinstance(question_text, Question)
                        else Question(text=question_text)
                    )
                    error_answer = Answer(
                        text=f"处理失败: {str(e)}", score=None, status="error", source="error"
                    )
                    error_result = QAResult(
                        question=error_question, answer=error_answer, metadata={"error": str(e)}
                    )
                    batch_result.add_result(error_result)
                except (ConnectionError, TimeoutError):
                    # 如果连错误结果都无法创建，跳过此问题
                    logger.error("[%d/%d] 无法创建错误结果，跳过", i, len(question_texts))

        # P2-2 (r1): claim refusals are not dispatched work — report them
        # separately instead of inflating the "成功 N/N" summary.
        success = batch_result.processed_count - refused_count
        summary = f"批量处理完成，成功: {success}/{len(question_texts)}"
        if refused_count:
            summary += f"（work拒绝未派发 {refused_count}）"
        self.progress_reporter.finish_batch(summary)
        return batch_result

    def output_results(
        self,
        batch_result: QABatchResult,
        output_file: Optional[str] = None,
        quick_scan_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """输出结果。

        Args:
            batch_result: 批次结果对象
            output_file: 输出文件路径（可选，默认输出到控制台）
        """
        import json

        result_dict = (
            batch_result.to_quick_scan_dict(**quick_scan_context)
            if quick_scan_context is not None
            else batch_result.to_dict()
        )
        json_output = json.dumps(result_dict, ensure_ascii=False, indent=4)

        if output_file:
            from pathlib import Path

            output_path = Path(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(json_output)
            logger.info("[OK] 结果已保存到: %s", output_file)
        else:
            print(json_output)
            logger.info("[OK] 结果已输出到控制台")

    def get_statistics(self, batch_result: QABatchResult) -> Dict[str, Any]:
        """获取统计信息。

        Args:
            batch_result: 批次结果对象

        Returns:
            包含统计信息的字典
        """
        error_count = sum(
            1
            for r in batch_result.results
            if r.metadata.get("error")
            or r.answer.status == "error"
            or (
                isinstance(r.answer.metadata.get("format_repair"), dict)
                and r.answer.metadata["format_repair"].get("status")
                in {"failed", "unverified", "not_enabled"}
            )
        )

        return {
            "total_questions": batch_result.total_questions,
            "processed_count": batch_result.processed_count,
            "success_count": batch_result.processed_count - error_count,
            "error_count": error_count,
            "is_complete": batch_result.is_complete(),
            "created_at": batch_result.created_at.isoformat(),
        }
