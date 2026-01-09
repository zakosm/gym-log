from app.main import app

# Expose 'app' (ASGI) for Vercel.
# Run locally for testing: python api/index.py
if __name__ == "__main__":
    import uvicorn, os
    uvicorn.run("api.index:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
