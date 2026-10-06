from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

INDEX = Path(__file__).with_name("index.html")

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Landing page: links to the live demo, API docs and readiness, plus live status."""
    return INDEX.read_text()
