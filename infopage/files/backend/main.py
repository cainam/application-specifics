#main.py 

from fastapi import FastAPI, Request
from core.config import settings
from apis.general_pages.route_homepage import general_pages_router

import logging
import re

logger = logging.getLogger(__name__)
repeated_slashes = re.compile(r'//+')

def include_router(app):
	app.include_router(general_pages_router)


def start_application():
	app = FastAPI(title=settings.PROJECT_NAME,version=settings.PROJECT_VERSION)
	include_router(app)
	return app 


app = start_application()

@app.middleware('http')
async def some_middleware(request: Request, call_next):
    #logger.info("request.scope: "+str(request.scope))
    #logger.info("request.url: "+str(request.url))
    #logger.info("request._url: "+str(request._url))
    request.scope["path"] = repeated_slashes.sub('/', request.scope["path"])
    return await call_next(request)


