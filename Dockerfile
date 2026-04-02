FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

RUN groupadd -r agent && useradd -r -g agent agent
USER agent

ENTRYPOINT ["python", "-m", "src"]
