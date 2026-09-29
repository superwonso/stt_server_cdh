"""Isolated real reminder API and web preview; temporary synthetic database only."""
import argparse, json, socket, sys, tempfile, threading, time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
import uvicorn
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles
from server.app import create_app
from server.settings import Settings
from server.security import PASSWORD_HASHER

class FakeTranscriber:
    def status(self):return {'model_state':'ready','model':'synthetic','device':'cpu'}

arguments=argparse.ArgumentParser(description=__doc__)
arguments.add_argument('--state-file',type=Path,required=True)
args=arguments.parse_args()
sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();port=sock.getsockname()[1]
with tempfile.TemporaryDirectory(prefix='reminder-browser-') as temporary:
    settings=Settings(data_dir=Path(temporary)/'data',model_cache_dir=Path(temporary)/'models',site_origins=(f'http://127.0.0.1:{port}',))
    app=create_app(settings,FakeTranscriber())
    app.state.review_service.today=lambda:'2026-09-29'
    with app.state.database.connect() as connection:
        connection.execute('UPDATE users SET password_hash=?',(PASSWORD_HASHER.hash('synthetic-test-password'),))
    app.mount('/app',StaticFiles(directory=str(root/'web'),html=True),name='fixture-web')
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    @app.post('/_fixture/stop')
    def stop(request:Request):
        if request.headers.get('x-fixture')!='synthetic-preview-only':raise HTTPException(403)
        server.should_exit=True
        return {'stopping':True}
    timer=threading.Timer(2400,lambda:setattr(server,'should_exit',True));timer.daemon=True;timer.start()
    args.state_file.write_text(json.dumps({'port':port}),encoding='utf-8')
    server.run(sockets=[sock]);timer.cancel();sock.close()
