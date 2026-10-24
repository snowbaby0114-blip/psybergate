from fastapi import APIRouter

from app.api.v1 import dispenses, medicines

router = APIRouter(prefix="/api/v1")
router.include_router(medicines.router)
router.include_router(dispenses.router)
