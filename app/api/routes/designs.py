from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import FileResponse

from app.api.deps import Container, get_container
from app.api.routes.uploads import resolve_upload
from app.api.schemas.design import (
    BudgetRequest,
    BudgetResponse,
    CalculateRequest,
    CreateDesignRequest,
    DesignSummary,
    PlanResponse,
    PreviewRequest,
    RecommendationsResponse,
    RefineRequest,
    RefineResponse,
    SceneNormalizeRequest,
)
from app.budget.optimizer import BudgetResult, calculate_selection
from app.core.errors import ConflictError
from app.integration.mapper import Edit3DOptions, EditPlan
from app.integration.person_a_schemas import JobRead
from app.scene.models import SceneInfo
from app.scene.normalizer import build_scene
from app.services.design_models import Design
from app.services.design_service import Edit3DResult
from app.services.preview_jobs import PreviewJob, PreviewStatus

router = APIRouter(tags=["designs"])
Deps = Annotated[Container, Depends(get_container)]


@router.post("/scene/normalize", response_model=SceneInfo, tags=["scene"])
def normalize_scene(payload: SceneNormalizeRequest) -> SceneInfo:
    """Turn partial scene data into a SceneInfo and report what is missing."""
    return build_scene(payload.raw, payload.room_type)


@router.post("/budget/calculate", response_model=BudgetResult, tags=["budget"])
def calculate_budget(payload: CalculateRequest, deps: Deps) -> BudgetResult:
    """Total a fixed list of products. Unknown prices are listed, never counted as zero."""
    return calculate_selection(payload.product_ids, payload.budget_inr, deps.catalog)


@router.post("/designs", response_model=DesignSummary, status_code=status.HTTP_201_CREATED)
def create_design(payload: CreateDesignRequest, deps: Deps) -> Design:
    """Generate the structured design specification."""
    return deps.designs.create(payload)


@router.post("/designs/full", response_model=Design, status_code=status.HTTP_201_CREATED)
def create_full_design(payload: CreateDesignRequest, deps: Deps) -> Design:
    """Spec, recommendations, budget and plan in one call. Previews are separate."""
    return deps.designs.run_all(payload)


@router.get("/designs/{design_id}", response_model=Design)
def get_design(design_id: str, deps: Deps) -> Design:
    return deps.designs.store.get(design_id)


@router.post("/designs/{design_id}/recommendations", response_model=RecommendationsResponse)
def get_recommendations(design_id: str, deps: Deps) -> RecommendationsResponse:
    design = deps.designs.recommendations(design_id)
    return RecommendationsResponse(design_id=design_id, slots=design.recommendations)


@router.post("/designs/{design_id}/budget", response_model=BudgetResponse)
def optimize(design_id: str, deps: Deps, payload: BudgetRequest | None = None) -> BudgetResponse:
    design = deps.designs.budget(design_id, payload.budget_inr if payload else None)
    return BudgetResponse(design_id=design_id, budget=design.budget)


@router.post("/designs/{design_id}/plan", response_model=PlanResponse)
def transformation_plan(design_id: str, deps: Deps) -> PlanResponse:
    design = deps.designs.plan(design_id)
    return PlanResponse(design_id=design_id, plan=design.plan)


@router.post("/designs/{design_id}/refine", response_model=RefineResponse)
def refine_design(design_id: str, payload: RefineRequest, deps: Deps) -> RefineResponse:
    """Apply a natural-language change and rerun only the affected stages."""
    design, outcome = deps.designs.refine(design_id, payload.message)
    return RefineResponse(design_id=design_id, outcome=outcome, spec=design.spec,
                          budget=design.budget, plan=design.plan)


@router.post("/designs/{design_id}/preview", response_model=PreviewJob,
             status_code=status.HTTP_202_ACCEPTED)
def start_preview(design_id: str, payload: PreviewRequest, deps: Deps) -> PreviewJob:
    """Queue a 2D preview. Poll GET /previews/{job_id}. Jobs run one at a time."""
    image_path = resolve_upload(deps, payload.upload_id)
    return deps.previews.submit(design_id, payload.upload_id, image_path, payload.scope,
                                payload.target_categories)


@router.get("/previews/{job_id}", response_model=PreviewJob)
def get_preview(job_id: str, deps: Deps) -> PreviewJob:
    return deps.previews.get(job_id)


@router.get("/previews/{job_id}/image", response_class=FileResponse)
def get_preview_image(job_id: str, deps: Deps) -> FileResponse:
    job = deps.previews.get(job_id)
    if job.status is not PreviewStatus.COMPLETED:
        raise ConflictError(f"Preview is {job.status.value}.", {"error": job.error})
    return FileResponse(job.preview.image_path, media_type="image/png")


@router.post("/designs/{design_id}/edit-3d/plan", response_model=EditPlan, tags=["person-a"])
def preview_3d_edit(design_id: str, options: Edit3DOptions, deps: Deps) -> EditPlan:
    """Show what would be sent to Person A, and what cannot be. Calls nothing."""
    return deps.designs.preview_edit_plan(design_id, options)


@router.post("/designs/{design_id}/edit-3d", response_model=Edit3DResult,
             status_code=status.HTTP_202_ACCEPTED, tags=["person-a"])
def submit_3d_edit(design_id: str, options: Edit3DOptions, deps: Deps) -> Edit3DResult:
    """Send the design's edit to Person A's POST /api/v1/jobs/edit."""
    return deps.designs.submit_3d_edit(design_id, options)


@router.get("/edit-3d/jobs/{job_id}", response_model=JobRead, tags=["person-a"])
def get_3d_job(job_id: str, deps: Deps) -> JobRead:
    return deps.person_a.get_job(job_id)
