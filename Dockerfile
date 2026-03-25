# Step 1: Base image
FROM python:3.11-slim

# Step 2: Workdir
WORKDIR /app

# Step 3: Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 4: Copy project
COPY . .

# Step 5: Render uses $PORT
ENV PORT=10000

# Step 6: Run with gunicorn
# IMPORTANT: Render will provide PORT automatically; keep this format
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4"]
