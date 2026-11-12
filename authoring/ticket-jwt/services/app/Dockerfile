FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
RUN ln -s /answers/flag /flag
ENV PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
