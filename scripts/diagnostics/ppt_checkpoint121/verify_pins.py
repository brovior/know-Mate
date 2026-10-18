"""Read-only version/hash gate before any live activation."""
import hashlib,json,os
from pathlib import Path

def verify(root: Path, installed: bool = False):
    pins=json.loads((root/'package-pins.json').read_text())
    manifest=json.loads((root/'controller-manifest.json').read_text())
    for name,digest in manifest['files'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('controller_hash_changed')
    source=Path(pins['source_path'])
    for name,digest in json.loads((root/'source-manifest.json').read_text()).items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('observation_source_changed')
    helper=Path(pins['helper_path'])
    if hashlib.sha256(helper.read_bytes()).hexdigest()!=pins['helper_sha256']:raise RuntimeError('helper_hash_changed')
    if hashlib.sha256((helper.parent/'manifest.json').read_bytes()).hexdigest()!=pins['payload_manifest_sha256']:raise RuntimeError('payload_manifest_changed')
    directory=Path(os.environ['ProgramFiles'])/'AegisDesk/PptReader'/pins['version'] if installed else helper.parent
    for name,digest in pins['installed_files'].items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('payload_hash_changed')
    return True
