from fastapi import APIRouter

from app.api.v1.endpoints import catalogue, editions, health, stories

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(stories.router)
api_router.include_router(editions.router)
api_router.include_router(catalogue.router)
