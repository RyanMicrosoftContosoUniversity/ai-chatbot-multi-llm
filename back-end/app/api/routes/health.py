from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health")


@router.get("/live")
async def live():
    return {"status": "alive"}


@router.get("/ready")
async def ready(request: Request):
    return JSONResponse(
        {"status": "ready" if request.app.state.ready else "not_ready"},
        status_code=200 if request.app.state.ready else 503,
    )
