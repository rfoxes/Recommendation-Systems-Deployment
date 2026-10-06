import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app.adsets.router import router as adsets_router
from app.adsets.store import AdSetStore
from app.cache import create_redis
from app.campaigns.cache import CampaignCache
from app.campaigns.router import router as campaigns_router
from app.campaigns.store import CacheRefreshConflictError, CampaignStore
from app.catalog import ActiveCatalog
from app.config import get_settings
from app.copywriting.generator import CopyGenerator
from app.copywriting.prompt import DialoguePrompt
from app.copywriting.writers import create_copy_writer
from app.db import create_mongo_client, ensure_indexes
from app.demo.router import router as demo_router
from app.features.store import FeatureStore
from app.health import router as health_router
from app.idempotency import IdempotencyStore
from app.ranking.ctr_model import CTRModel
from app.ranking.model_files import DEFAULT_MODEL_DIR, MODEL_RELEASE, ensure_model_files
from app.ratelimit import RateLimiter
from app.serving.geo import GeoIP
from app.serving.render import AdTemplate
from app.serving.router import router as serving_router
from app.serving.serve_writer import ServeWriter
from app.serving.service import AdServer
from app.sessions.router import router as sessions_router
from app.sessions.store import SessionStore
from app.temporal.activities import CacheActivities, CopyActivities
from app.temporal.runner import TemporalRunner
from app.web.router import router as web_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # The CTR model must score exactly like the trained V1, or the app refuses to start.
    ctr_model = CTRModel(ensure_model_files(settings.ctr_model_dir or DEFAULT_MODEL_DIR))
    drift = ctr_model.verify_golden_sample()
    app.state.ctr_model_drift = drift
    logger.info("CTR model %s loaded (golden sample max diff %.1e)", MODEL_RELEASE, drift)

    mongo = create_mongo_client(settings)
    redis = create_redis(settings)
    db = mongo[settings.mongo_db]
    await ensure_indexes(db)

    cache = CampaignCache(redis)
    idempotency = IdempotencyStore(db)
    store = CampaignStore(db, cache, idempotency)
    try:
        await store.refresh_cache()  # warm the cache on startup (also covers a cold start)
    except (RedisError, CacheRefreshConflictError):
        logger.warning("Startup cache warm failed; reads will fall back to MongoDB", exc_info=True)
    writer = create_copy_writer(settings)
    generator = CopyGenerator(writer, DialoguePrompt.load())
    if writer is None:
        logger.info("No %s API key set: ads will use fallback copy", settings.llm_provider)
    else:
        logger.info("Ad copy by %s (%s)", writer.provider, writer.model)

    temporal = None
    if settings.temporal_enabled:
        copy_activities = CopyActivities(db, redis, generator, settings.copy_pool_size)
        temporal = TemporalRunner(
            settings,
            activities=[CacheActivities(store).refresh_campaign_cache, copy_activities.find_variants_needing_copy],
            llm_activities=[copy_activities.generate_variant_copy],
        )
        temporal.start()

    app.state.campaign_store = store
    app.state.campaign_cache = cache
    app.state.db = db
    app.state.redis = redis
    app.state.temporal = temporal
    app.state.ad_set_store = AdSetStore(
        db, store, idempotency, on_created=temporal.start_copy_generation if temporal else None
    )
    app.state.catalog = ActiveCatalog(db, store, cache)
    app.state.copy_generator = generator
    app.state.session_store = SessionStore(
        redis,
        inactivity_seconds=settings.session_inactivity_seconds,
        record_ttl_seconds=settings.session_record_ttl_seconds,
    )
    app.state.rate_limiter = RateLimiter(redis)
    app.state.ctr_model = ctr_model

    geoip = GeoIP()
    app.state.feature_store = FeatureStore(redis)
    app.state.serve_writer = ServeWriter(
        db, batch_size=settings.serve_batch_size, flush_seconds=settings.serve_flush_seconds
    )
    app.state.serve_writer.start()
    app.state.ad_server = AdServer(
        settings,
        sessions=app.state.session_store,
        catalog=app.state.catalog,
        features=app.state.feature_store,
        model=ctr_model,
        copy=generator,
        template=AdTemplate(),
        geoip=geoip,
    )
    try:
        yield
    finally:
        await app.state.serve_writer.stop()  # flush queued serve records before closing MongoDB
        if temporal:
            await temporal.stop()
        geoip.close()
        await redis.aclose()
        await mongo.close()


app = FastAPI(title="Simula Native Ads API", lifespan=lifespan)
# Ads render inside other apps' pages and call the click endpoint from there.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)
app.include_router(campaigns_router)
app.include_router(adsets_router)
app.include_router(sessions_router)
app.include_router(serving_router)
app.include_router(health_router)
app.include_router(demo_router)
app.include_router(web_router)


def _json_safe(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422, except values like Infinity are echoed as strings instead of crashing."""
    return JSONResponse(status_code=422, content={"detail": _json_safe(jsonable_encoder(exc.errors()))})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
