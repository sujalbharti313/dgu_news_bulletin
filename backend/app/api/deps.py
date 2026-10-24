from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import Database
from app.services.bulletin import BulletinService
from app.services.catalogue import CatalogueService


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_session(db: Annotated[Database, Depends(get_database)]) -> Iterator[Session]:
    yield from db.read_session()


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
SessionDep = Annotated[Session, Depends(get_session)]


def get_bulletin(session: SessionDep, settings: SettingsDep) -> BulletinService:
    return BulletinService(session, include_demo=settings.expose_demo_content)


def get_catalogue(session: SessionDep, settings: SettingsDep) -> CatalogueService:
    return CatalogueService(session, include_demo=settings.expose_demo_content)


BulletinDep = Annotated[BulletinService, Depends(get_bulletin)]
CatalogueDep = Annotated[CatalogueService, Depends(get_catalogue)]
