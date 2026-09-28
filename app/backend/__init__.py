"""Fool4School 后端任务服务。

后端只负责接收、校验、排队和记录 F4S_JOB；耗时的 PDF 处理由 worker 执行。
"""

from .protocol import F4SJob, JobValidationError, parse_job
from .store import JobStore

__all__ = ["F4SJob", "JobStore", "JobValidationError", "parse_job"]
