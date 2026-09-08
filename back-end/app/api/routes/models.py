from fastapi import APIRouter, Request

from app.api.deps import AuthenticatedUser

router = APIRouter()


@router.get("/models")
async def models(request: Request, user: AuthenticatedUser):
    return {
        "models": await request.app.state.services.gateway.models(
            user.oid, request.state.request_id
        ),
        "historyEnabled": request.app.state.services.history is not None,
    }
