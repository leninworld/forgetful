"""Inert identity check of real FastEmbed cache files, no model imports."""
import hashlib
import json
from pathlib import Path
import sys

pins = json.loads((Path(__file__).parent / 'model-pins.json').read_text())
root = Path(sys.argv[1])
proof = []
for model in pins['models']:
    repo = root / ('models--' + model['repo_id'].replace('/', '--'))
    assert (repo / 'refs/main').read_text().strip() == model['revision'], repo
    snapshot = repo / 'snapshots' / model['revision']
    for item in model['required_files']:
        file = snapshot / item['rfilename']
        data = file.read_bytes()
        assert len(data) == item['size'], str(file)
        if 'lfs' in item:
            actual = hashlib.sha256(data).hexdigest()
            assert actual == item['lfs']['sha256'], str(file)
        else:
            actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            assert actual == item['blobId'], str(file)
        proof.append({'repo': model['repo_id'], 'file': item['rfilename'], 'hash': actual})
print(json.dumps({'status': 'EXACT_MODEL_FILES_PASS', 'files': proof}), flush=True)
