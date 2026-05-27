FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY copilot_agent ./copilot_agent
COPY config ./config
COPY scenarios ./scenarios
COPY skills ./skills
COPY main.py .
COPY static ./static

RUN mkdir -p storage artifacts/runtime artifacts/eval

ENV PYTHONUNBUFFERED=1
EXPOSE 8090

CMD ["uvicorn", "copilot_agent.server:app", "--host", "0.0.0.0", "--port", "8090"]
