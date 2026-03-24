from fastapi import FastAPI
import asyncio
import logging
import os
from src.api.router import router
from src.repository.database import engine
from src.repository.models import Base
from src.services.uploader import process_upload_queue
from src.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Ensure queue directory exists
db_path = settings.sqlite_db_url.replace("sqlite:///", "")
if db_path.startswith("/"):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="GreenMind Pi-Gateway")
app.include_router(router, prefix="/api/v1")

@app.on_event("startup")
async def startup_event():
    # Start the async upload worker loop
    asyncio.create_task(process_upload_queue())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)
