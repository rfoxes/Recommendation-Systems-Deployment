import logging
from collections.abc import Awaitable, Callable
from itertools import product

from pymongo import ReturnDocument
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.errors import DuplicateKeyError

from app.adsets.schemas import AdSetCreate, AdSetWithVariants
from app.campaigns.store import CampaignStore
from app.db import AD_SETS, AD_VARIANTS, CAMPAIGNS, Database, run_in_transaction, to_document
from app.idempotency import IdempotencyStore, IdempotentRequest
from app.models import AdSet, AdVariant, Campaign, new_id, utcnow

logger = logging.getLogger(__name__)


class UnknownCampaignError(ValueError):
    def __init__(self, campaign_id: str) -> None:
        super().__init__(f"campaign {campaign_id} not found")


class AdSetStore:
    def __init__(
        self,
        db: Database,
        campaigns: CampaignStore,
        idempotency: IdempotencyStore,
        on_created: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._campaigns = campaigns
        self._idempotency = idempotency
        self._on_created = on_created  # e.g. start pre-generating ad copy for the new variants

    async def create(self, data: AdSetCreate, request: IdempotentRequest | None = None) -> AdSetWithVariants:
        """Create an ad set, its variants (every asset combination), and link it to its campaign.

        All three writes (plus the idempotency record) happen in one transaction.
        """
        if saved := await self._idempotency.saved_response(request):
            return AdSetWithVariants.model_validate(saved)

        ad_set = AdSet(ad_set_id=new_id("adset"), **data.model_dump())
        variants = [
            AdVariant(
                variant_id=new_id("var"),
                ad_set_id=ad_set.ad_set_id,
                campaign_id=ad_set.campaign_id,
                character_name=character,
                video_url=video,
                cta=cta,
                ai_prompt=prompt,
                created_at=ad_set.created_at,
                updated_at=ad_set.created_at,
            )
            for character, video, cta, prompt in product(
                data.character_names, data.video_urls, data.ctas, data.ai_prompts
            )
        ]
        result = AdSetWithVariants(**ad_set.model_dump(), variants=variants)

        async def write(session: AsyncClientSession) -> Campaign:
            await self._idempotency.save(request, result.model_dump(mode="json"), session)
            await self._db[AD_SETS].insert_one(to_document(ad_set, "ad_set_id"), session=session)
            await self._db[AD_VARIANTS].insert_many([to_document(v, "variant_id") for v in variants], session=session)
            doc = await self._db[CAMPAIGNS].find_one_and_update(
                {"_id": ad_set.campaign_id},
                {
                    "$addToSet": {"native_ad_set_ids": ad_set.ad_set_id},
                    "$set": {"updated_at": utcnow()},
                    "$inc": {"version": 1},
                },
                projection={"_id": 0},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if doc is None:  # raising aborts the transaction, undoing the inserts above
                raise UnknownCampaignError(ad_set.campaign_id)
            return Campaign.model_validate(doc)

        try:
            campaign = await run_in_transaction(self._db, write)
        except DuplicateKeyError:
            # A concurrent request with the same Idempotency-Key committed first: return its result.
            if saved := await self._idempotency.saved_response(request):
                return AdSetWithVariants.model_validate(saved)
            raise
        # Updating the cached campaign also bumps catalog:version, so serving picks up the new variants.
        await self._campaigns.put_in_cache(campaign)
        if self._on_created is not None:
            try:
                await self._on_created(ad_set.ad_set_id)
            except Exception:  # never fail a committed create; the copy backfill catches up later
                logger.warning("Post-create hook failed for %s", ad_set.ad_set_id, exc_info=True)
        return result
