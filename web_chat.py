import uvicorn

from helperme.channels.web import create_web_app


app = create_web_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, timeout_graceful_shutdown=1)
