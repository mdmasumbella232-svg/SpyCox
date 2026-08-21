FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY prediction_bot.py .

CMD ["python3", "prediction_bot.py"]
