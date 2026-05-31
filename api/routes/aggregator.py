from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/summary")
async def get_summary(request: Request):
    aggregator = request.app.state.aggregator
    return await aggregator.get_dashboard_summary()
