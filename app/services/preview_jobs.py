"""Background runner for 2D previews. One worker, because the GPU fits one job at a time.
Job records live in memory: a restart forgets running jobs, not finished designs."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from app.core.errors import NotFoundError, RoomSageError
from app.llm.rules import off_palette_negative
from app.redesign.base import EditScope, Renderer, RenderRequest
from app.services.design_models import PreviewRecord
from app.services.design_service import DesignService

logger = logging.getLogger(__name__)


class PreviewStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PreviewJob(BaseModel):
    job_id: str
    design_id: str
    status: PreviewStatus = PreviewStatus.QUEUED
    error: str | None = None
    preview: PreviewRecord | None = None


class PreviewJobs:
    def __init__(self, service: DesignService, renderer: Renderer, output_dir: Path, mode: str):
        self._service = service
        self._renderer = renderer
        self._output_dir = Path(output_dir)
        self._mode = mode
        self._jobs: dict[str, PreviewJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview")

    def submit(
        self, design_id: str, upload_id: str, image_path: Path, scope: EditScope,
        target_categories: list,
    ) -> PreviewJob:
        self._service.store.get(design_id)
        job = PreviewJob(job_id=uuid4().hex, design_id=design_id)
        with self._lock:
            self._jobs[job.job_id] = job
        self._executor.submit(self._run, job, upload_id, image_path, scope, target_categories)
        return job

    def get(self, job_id: str) -> PreviewJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"Preview job {job_id} does not exist.")
        return job

    def _run(self, job, upload_id, image_path, scope, target_categories) -> None:
        job.status = PreviewStatus.RUNNING
        try:
            design = self._service.store.get(job.design_id)
            prompt = self._service.preview_prompt(design)
            result = self._renderer.render(RenderRequest(
                image_path=image_path,
                output_path=self._output_dir / f"{job.job_id}.png",
                prompt=prompt,
                negative_prompt=off_palette_negative(
                    design.spec.negative_prompt, design.spec.color_palette, design.spec.accent_colors),
                scope=scope,
                target_categories=target_categories,
                keep_categories=self._service.kept_categories(design),
                palette=design.spec.color_palette,
            ))
            record = PreviewRecord(
                job_id=job.job_id, mode=self._mode, image_path=str(result.image_path),
                source_upload_id=upload_id, prompt=prompt, is_mock=result.is_mock,
                limitations=result.limitations,
            )
            # Re-read: a refinement may have landed while the image was rendering.
            latest = self._service.store.get(job.design_id)
            record.stale = self._service.preview_prompt(latest) != prompt
            latest.preview = record
            self._service.store.save(latest)
            job.preview = record
            job.status = PreviewStatus.COMPLETED
        except RoomSageError as exc:
            job.status, job.error = PreviewStatus.FAILED, exc.message
        except Exception as exc:
            logger.exception("Preview job %s failed", job.job_id)
            job.status, job.error = PreviewStatus.FAILED, f"{type(exc).__name__}: {exc}"
