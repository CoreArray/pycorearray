"""Local HTTP server for test_cloud.py, run in a child process

The cloud reads hold the GIL, so the server cannot be a thread of the test
process. Usage: ``python cloud_server.py <dir>`` serves the files in <dir>,
and ``python cloud_server.py --silent`` accepts connections but never answers;
the port is printed on the first line of stdout.

Routes of the file server:
  /data/<f>       proper HTTP Range support (206 + Content-Range)
  /norange/<f>    ignores Range: always 200 with the whole file
  /badrange/<f>   206 whose Content-Range start is always 0
  /flaky/<f>      503 on two out of every three requests, else /data
  /auth/<f>       requires "Authorization: Bearer secret-token"
  /login/<f>      302 to /signin/page, like a download behind a login
  /signin/<f>     200 with an HTML page larger than the size probe
  /html/<f>       200 with a short HTML page, no redirect
"""

import http.server
import os
import re
import socket
import sys


class Handler(http.server.BaseHTTPRequestHandler):
	counter = 0
	protocol_version = 'HTTP/1.1'

	def log_message(self, *args):
		pass

	def reply(self, status, body=b'', headers=None):
		headers = dict(headers or {})
		headers.setdefault('Content-Type', 'application/octet-stream')
		headers['Content-Length'] = str(len(body))
		self.send_response(status)
		for k, v in headers.items():
			self.send_header(k, v)
		self.end_headers()
		if self.command != 'HEAD':
			self.wfile.write(body)

	def serve(self, fname, mode):
		path = os.path.join(self.server.dir, os.path.basename(fname))
		if not os.path.exists(path):
			return self.reply(404, b'<Error>NoSuchKey</Error>')
		size = os.path.getsize(path)
		rng = self.headers.get('Range')
		m = re.match(r'^bytes=([0-9]+)-([0-9]*)$', rng or '')
		with open(path, 'rb') as fh:
			if mode == 'norange' or m is None:
				return self.reply(200, fh.read(), {'Accept-Ranges': 'bytes'})
			start = int(m.group(1))
			end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
			if start >= size:
				return self.reply(416, b'',
					{'Content-Range': 'bytes */%d' % size})
			fh.seek(start)
			body = fh.read(end - start + 1)
		s = 0 if mode == 'badrange' else start
		self.reply(206, body, {'Accept-Ranges': 'bytes',
			'Content-Range': 'bytes %d-%d/%d' % (s, s + end - start, size)})

	def html(self, n):
		page = ('<!DOCTYPE html><html><body>' +
			'<p>Sign in to download this file.</p>' * n + '</body></html>')
		self.reply(200, page.encode(),
			{'Content-Type': 'text/html; charset=utf-8'})

	def do_GET(self):
		Handler.counter += 1
		if self.path == '/__ping':
			return self.reply(200, b'ok')
		parts = self.path.lstrip('/').split('/')
		if len(parts) != 2:
			return self.reply(404, b'not found')
		mode, fname = parts
		if mode in ('data', 'norange', 'badrange'):
			self.serve(fname, mode)
		elif mode == 'auth':
			if self.headers.get('Authorization') == 'Bearer secret-token':
				self.serve(fname, 'data')
			else:
				self.reply(401, b'{"error":"unauthorized"}')
		elif mode == 'login':
			self.reply(302, b'', {'Location': '/signin/page'})
		elif mode == 'signin':
			self.html(200)
		elif mode == 'html':
			self.html(1)
		elif mode == 'flaky':
			if Handler.counter % 3 != 0:
				self.reply(503, b'<Error><Code>SlowDown</Code></Error>')
			else:
				self.serve(fname, 'data')
		else:
			self.reply(404, b'not found')

	do_HEAD = do_GET


def main():
	if sys.argv[1] == '--silent':
		s = socket.socket()
		s.bind(('127.0.0.1', 0))
		s.listen(16)
		print(s.getsockname()[1], flush=True)
		conns = []
		while True:
			conns.append(s.accept()[0])   # keep it open, send nothing
	srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
	srv.dir = sys.argv[1]
	print(srv.server_address[1], flush=True)
	srv.serve_forever()


if __name__ == '__main__':
	main()
