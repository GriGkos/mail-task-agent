from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.config import get_settings
from app.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    yield


app = FastAPI(title="Mail Task Agent", version="0.1.0", lifespan=lifespan)
app.include_router(router)
