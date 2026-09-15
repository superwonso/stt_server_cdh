"""Serve the local test UI with a loopback config, without publishing files."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi.responses import JSONResponse
from starlette.staticfiles import StaticFiles


def attach_local_web(app, port=18766):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid local API port')

    @app.get('/config.json', include_in_schema=False)
    def local_config():
        now=datetime.now(timezone.utc)
        stamp=lambda d:d.isoformat(timespec='milliseconds').replace('+00:00','Z')
        return JSONResponse({'version':1,'state':'online','apiUrl':f'http://127.0.0.1:{port}',
            'publishedAt':stamp(now),'expiresAt':stamp(now+timedelta(hours=24))},
            headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    # StaticFiles rejects traversal and does not list directories. Only the
    # versioned web assets are served; private data and models are elsewhere.
    app.mount('/',StaticFiles(directory=Path(__file__).resolve().parents[1]/'web',html=True))
