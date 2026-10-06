from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from app.campaigns.schemas import CampaignCreate, CampaignFilters, CampaignUpdate
from app.campaigns.store import CampaignStore, UnknownAdSetsError
from app.idempotency import IdempotencyKeyReusedError, IdempotentRequest
from app.models import Campaign, Surface

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def get_campaign_store(request: Request) -> CampaignStore:
    store: CampaignStore = request.app.state.campaign_store
    return store


Store = Annotated[CampaignStore, Depends(get_campaign_store)]


def _not_found(campaign_id: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=f"campaign {campaign_id} not found")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignCreate,
    store: Store,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255, description="Makes retries safe")
    ] = None,
) -> Campaign:
    try:
        return await store.create(body, IdempotentRequest.build("POST /campaigns", idempotency_key, body))
    except (UnknownAdSetsError, IdempotencyKeyReusedError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("")
async def list_campaigns(
    store: Store,
    ids: Annotated[list[str] | None, Query(description="Campaign ids, comma-separated or repeated")] = None,
    surface: Surface | None = None,
    publisher_id: str | None = None,
    active: bool | None = None,
) -> list[Campaign]:
    id_set = frozenset(i.strip() for raw in ids for i in raw.split(",") if i.strip()) if ids else None
    filters = CampaignFilters(ids=id_set, surface=surface, publisher_id=publisher_id, active=active)
    return await store.find(filters)


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: str, store: Store) -> Campaign:
    campaign = await store.get(campaign_id)
    if campaign is None:
        raise _not_found(campaign_id)
    return campaign


@router.patch("/{campaign_id}")
async def update_campaign(campaign_id: str, body: CampaignUpdate, store: Store) -> Campaign:
    try:
        campaign = await store.update(campaign_id, body)
    except UnknownAdSetsError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if campaign is None:
        raise _not_found(campaign_id)
    return campaign


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(campaign_id: str, store: Store) -> Response:
    if not await store.delete(campaign_id):
        raise _not_found(campaign_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
