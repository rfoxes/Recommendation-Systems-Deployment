import secrets
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response, status

from app.config import get_settings
from app.features.store import FeatureStore
from app.models import utcnow
from app.request_context import client_ip
from app.serving.serve_writer import ServeWriter
from app.serving.service import AdServer, LoadNativeRequest, LoadNativeResponse, ServeResult, UnknownSessionError

router = APIRouter(tags=["serving"])


def get_ad_server(request: Request) -> AdServer:
    server: AdServer = request.app.state.ad_server
    return server


def get_serve_writer(request: Request) -> ServeWriter:
    writer: ServeWriter = request.app.state.serve_writer
    return writer


def get_feature_store(request: Request) -> FeatureStore:
    store: FeatureStore = request.app.state.feature_store
    return store


async def _record(server: AdServer, writer: ServeWriter, result: ServeResult) -> None:
    await server.after_response(result)
    writer.record(result.serve)


@router.post(
    "/load/native",
    response_model=LoadNativeResponse,
    responses={204: {"description": "No eligible campaign for this user and slot (no fill)"},
               404: {"description": "Unknown session"}},
)
async def load_native(
    body: LoadNativeRequest,
    request: Request,
    background: BackgroundTasks,
    server: Annotated[AdServer, Depends(get_ad_server)],
    writer: Annotated[ServeWriter, Depends(get_serve_writer)],
) -> Any:
    """Serve a native sponsored-character ad for a feed slot."""
    try:
        result = await server.serve(body, client_ip(request), request.headers.get("user-agent"))
    except UnknownSessionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"session {exc} not found") from exc
    if result is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    background.add_task(_record, server, writer, result)  # after the response is sent
    return result.response


@router.post("/impressions/{impression_id}/click", status_code=status.HTTP_204_NO_CONTENT,
             responses={401: {"description": "Missing or wrong API key"}, 404: {"description": "Unknown impression"}})
async def record_click(
    impression_id: str,
    features: Annotated[FeatureStore, Depends(get_feature_store)],
    writer: Annotated[ServeWriter, Depends(get_serve_writer)],
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """Called by the ad's CTA. Counted once per impression; repeats are accepted and ignored."""
    expected = f"Bearer {get_settings().click_api_key.get_secret_value()}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
    first = await features.record_click(impression_id)
    if first is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown impression, or older than 24 hours")
    if first:
        writer.record_click(impression_id, utcnow())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
