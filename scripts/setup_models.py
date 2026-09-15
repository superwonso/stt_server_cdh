"""Fetch the unchanged, pinned Qwen snapshots for native and ROCm runners."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = (
    ('Qwen/Qwen3-ASR-1.7B', '7278e1e70fe206f11671096ffdd38061171dd6e5'),
    ('Qwen/Qwen3-ForcedAligner-0.6B', 'c7cbfc2048c462b0d63a45797104fc9db3ad62b7'),
)

def main():
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    os.environ['HF_HUB_DISABLE_XET']='1'
    os.environ['HF_HUB_DOWNLOAD_TIMEOUT']='600'
    os.environ['HF_HOME']=str(ROOT/'.models'/'hf-cache')
    from huggingface_hub import snapshot_download
    for repo,revision in SNAPSHOTS:
        destination=ROOT/'.models'/repo.split('/')[1]
        snapshot_download(repo_id=repo,revision=revision,local_dir=destination,token=False,max_workers=4)
        print(f'Verified download: {repo} @ {revision}',flush=True)

if __name__=='__main__':
    main()
