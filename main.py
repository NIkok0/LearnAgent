"""Run: uvicorn copilot_agent.server:app --host 0.0.0.0 --port 8090"""

import uvicorn

from copilot_agent.settings import settings

if __name__ == "__main__":
    uvicorn.run(
        "copilot_agent.server:app",
        host=settings.copilot_host,
        port=settings.copilot_port,
        reload=False,
    )
