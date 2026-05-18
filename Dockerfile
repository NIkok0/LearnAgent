FROM python:3.12-slim

WORKDIR /app

# Repo layout: build context = copilot-agent/ directory (see compose).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY copilot_agent ./copilot_agent
COPY main.py .
COPY static ./static

ENV PYTHONUNBUFFERED=1
EXPOSE 8090

CMD ["uvicorn", "copilot_agent.server:app", "--host", "0.0.0.0", "--port", "8090"]
