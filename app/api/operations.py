"""Operator-only diagnostics, kept out of the personal workbench and schema."""
from fastapi import APIRouter,Depends,HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from ..database import get_db
from ..services.operations import operational_snapshot,job_diagnostic
from .deps import require_internal_execution

router=APIRouter(prefix='/internal/operations',dependencies=[Depends(require_internal_execution)],include_in_schema=False)


@router.get('')
def snapshot(db:Session=Depends(get_db)):
    return JSONResponse(jsonable_encoder(operational_snapshot(db)),headers={'Cache-Control':'no-store'})


@router.get('/jobs/{job_id}')
def diagnostic(job_id:str,db:Session=Depends(get_db)):
    value=job_diagnostic(db,job_id)
    if value is None:raise HTTPException(404,detail={'code':'diagnostic_job_missing'})
    return JSONResponse(jsonable_encoder(value),headers={'Cache-Control':'no-store'})
