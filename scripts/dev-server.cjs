// Serve only the allowlisted console files and API through one ASGI origin.
const { spawn } = require('node:child_process');
const { existsSync } = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.PYTHON_BIN || (existsSync(localPython) ? localPython : process.platform === 'win32' ? 'python' : 'python3');
const child = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', process.env.PORT || '4173', '--reload'], { cwd: root, stdio: 'inherit', windowsHide: true });
child.on('error', error => { console.error(`Cannot start API/console: ${error.message}`); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code ?? 1; });
process.on('SIGINT', () => child.kill('SIGINT'));
process.on('SIGTERM', () => child.kill('SIGTERM'));
