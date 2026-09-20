FROM python:3.12-slim
WORKDIR /app
COPY meme_sniper_runtime.tar.gz /tmp/meme_sniper_runtime.tar.gz
RUN tar -xzf /tmp/meme_sniper_runtime.tar.gz -C /app && rm /tmp/meme_sniper_runtime.tar.gz
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python","-m","app.main"]
