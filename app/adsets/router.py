from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.adsets.schemas import AdSetCreate, AdSetWithVariants
from app.adsets.store import AdSetStore, UnknownCampaignError
from app.idempotency import IdempotencyKeyReusedError, IdempotentRequest

router = APIRouter(prefix="/adsets", tags=["adsets"])

IdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=255, description="Makes retries safe"),
]


def get_ad_set_store(request: Request) -> AdSetStore:
    store: AdSetStore = request.app.state.ad_set_store
    return store


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ad_set(
    body: AdSetCreate,
    store: Annotated[AdSetStore, Depends(get_ad_set_store)],
    idempotency_key: IdempotencyKey = None,
) -> AdSetWithVariants:
    try:
        return await store.create(body, IdempotentRequest.build("POST /adsets", idempotency_key, body))
    except (UnknownCampaignError, IdempotencyKeyReusedError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
