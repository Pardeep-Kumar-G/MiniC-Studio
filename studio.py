#!/usr/bin/env python3
"""MiniC Studio GUI. Run: python3 studio.py [port]   (opens in your web browser)"""
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import minic

HERE = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def reply(self, body, ctype='application/json', status=200):
        data = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/':
            self.reply((HERE / 'studio.html').read_text(encoding='utf-8'), 'text/html')
        elif self.path == '/examples':
            self.reply(json.dumps({p.name: p.read_text(encoding='utf-8') for p in sorted((HERE / 'examples').glob('*.mc'))}))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != '/compile':
            return self.send_error(404)
        size = int(self.headers.get('Content-Length') or 0)
        if size > 1_000_000:
            return self.send_error(413, 'program too large')
        try:
            source = json.loads(self.rfile.read(size))['source']
            self.reply(json.dumps(minic.compile_source(source)))
        except Exception as e:  # report compiler bugs to the page instead of dropping the connection
            self.reply(json.dumps({'internal': f'{type(e).__name__}: {e}'}), status=500)

    def log_message(self, *args):
        pass


def main():
    port = int(os.environ.get('PORT') or (sys.argv[1] if len(sys.argv) > 1 else 8000))
    host = '0.0.0.0' if 'PORT' in os.environ else '127.0.0.1'  # public only on a host (Render, Heroku)
    server = ThreadingHTTPServer((host, port), Handler)
    url = f'http://127.0.0.1:{port}/'
    print(f'MiniC Studio running at {url}  (Ctrl+C to stop)')
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
