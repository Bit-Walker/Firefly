"""Build a repository-layout archive from explicit public source locations."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib

workspace = Path(__file__).resolve().parents[2]
fleet = workspace / 'fleet'
files = {workspace / name for name in ('README.md', '.gitignore', '.dockerignore', 'pyproject.toml')}


def public(path):
    relative = path.relative_to(workspace)
    forbidden = {'.state', '.local', '.venv', '__pycache__', 'build', 'dist', 'backup'}
    return (path.is_file() and not path.is_symlink() and not forbidden.intersection(relative.parts)
            and not any(p.endswith('.egg-info') for p in relative.parts)
            and '.local.' not in path.name and path.name not in ('config.h', '.env')
            and path.suffix not in ('.pyc', '.key', '.pem', '.sqlite3'))


for folder, patterns in {
    'docs': ('**/*.md',),
    'contracts': ('**/*.md', '**/*.json', '**/*.py', 'requirements-dev.txt'),
    'apps/control-plane': ('pyproject.toml', 'README.md', 'src/**/*.py'),
    'apps/cli': ('pyproject.toml', 'README.md', 'src/**/*.py'),
    'apps/web': ('*.html', '*.css', '*.js'),
    'packages/sdk-python': ('pyproject.toml', 'README.md', 'src/**/*.py'),
    'packages/analysis': ('pyproject.toml', 'README.md', 'src/**/*.py'),
    'hardware': ('**/*.json', '**/*.md'),
    'profiles/examples': ('*.json', '*.md'),
    'tests': ('**/*.py',),
    'tools': ('bootstrap.ps1', 'edbg.ps1'),
    'fleet': ('server.py', 'debug_api.py', 'wave_api.py', 'i2c_api.py', '_bootstrap.py', 'requirements.txt', 'README.md', 'DEPLOY.md', '.gitignore', '.dockerignore'),
    'fleet/tools': ('*.py', '*.ps1'),
    'fleet/tests': ('*.py', '*.cjs', '*.c'),
    'fleet/deploy': ('Dockerfile', 'Caddyfile', 'compose.yaml', '.env.example'),
    'fleet/firmware': ('CMakeLists.txt', 'sdkconfig.defaults', 'partitions.csv'),
    'fleet/firmware/main': ('*.c', '*.h', 'CMakeLists.txt'),
}.items():
    for pattern in patterns:
        files.update((workspace / folder).glob(pattern))

for version in ('4.0.0',):
    for pattern in ('*.bin', '*.elf', '*.map'):
        files.update((fleet / 'artifacts' / version).glob(pattern))

selected = sorted(p for p in files if public(p))
output = fleet / 'artifacts/fleet-source-and-deploy.zip'
output.parent.mkdir(parents=True, exist_ok=True)
with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
    for path in selected:
        archive.write(path, path.relative_to(workspace).as_posix())
with ZipFile(output) as archive:
    assert archive.testzip() is None
    names = set(archive.namelist())
    for required in ('apps/control-plane/src/edbg_server/app.py', 'apps/web/index.html',
                     'packages/sdk-python/src/edbg_sdk/client.py', 'hardware/boards/esp32s3-gpio-v1.json',
                     'fleet/server.py', '.dockerignore', 'docs/README.md'):
        assert required in names, required

digest = hashlib.sha256(output.read_bytes()).hexdigest()
output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n', encoding='ascii')
print('Package:', output)
print('Files:', len(selected), 'Size:', output.stat().st_size)
print('Verified: repository layout preserved; private state and local credentials excluded.')
