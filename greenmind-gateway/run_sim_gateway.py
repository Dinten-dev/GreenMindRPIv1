import asyncio
from src.runtime.gateway_app import run_gateway

async def main():
    await run_gateway(credentials={}, port=8000)

if __name__ == "__main__":
    asyncio.run(main())
