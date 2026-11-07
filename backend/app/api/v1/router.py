from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin_ingestion,
    admin_intelligence,
    admin_processing,
    catalogue,
    editions,
    health,
    stories,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(stories.router)
api_router.include_router(editions.router)
api_router.include_router(catalogue.router)
api_router.include_router(admin_ingestion.router)
api_router.include_router(admin_processing.router)
api_router.include_router(admin_intelligence.router)
