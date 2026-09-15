"""Offline Qwen/SDPA smoke benchmark; hypotheses stay in the selected local folder."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import unicodedata

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def normalized(text):
    return ''.join(c.lower() for c in unicodedata.normalize('NFKC',text) if c.isalnum())

def distance(a,b):
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        new=[i]
        for j,y in enumerate(b,1):
            new.append(min(row[j]+1,new[-1]+1,row[j-1]+(x!=y)))
        row=new
    return row[-1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--asr-only',action='store_true')
    args=parser.parse_args()
    if not 1<=args.repeats<=100: parser.error('repeats must be 1..100')
    args.output.mkdir(parents=True,exist_ok=True)
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false',HF_HOME=str(ROOT/'.models'/'hf-cache'),NUMBA_CACHE_DIR=str(args.output/'numba'))
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    import torch
    from server.settings import Settings
    from server.transcriber import LocalTranscriber
    torch.set_num_threads(8)
    manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
    model=LocalTranscriber(Settings(data_dir=args.output/'unused-data',model_cache_dir=ROOT/'.models'))
    torch.cuda.reset_peak_memory_stats()
    started=time.perf_counter()
    if args.asr_only:
        from qwen_asr import Qwen3ASRModel
        model=Qwen3ASRModel.from_pretrained(str(ROOT/'.models'/'Qwen3-ASR-1.7B'),dtype=torch.bfloat16,
            device_map='cuda:0',attn_implementation='sdpa',max_inference_batch_size=1,max_new_tokens=256)
    else:
        model._load()
    torch.cuda.synchronize()
    report={'engine':'qwen3-asr-transformers-sdpa','torch':torch.__version__,'cuda':torch.version.cuda,
            'model_revision':'7278e1e70fe206f11671096ffdd38061171dd6e5','aligner_revision':'c7cbfc2048c462b0d63a45797104fc9db3ad62b7',
            'includes_aligner':not args.asr_only,'load_seconds':time.perf_counter()-started,'load_peak_allocated_bytes':torch.cuda.max_memory_allocated(),'repeats':args.repeats,'runs':[]}
    print(json.dumps({k:v for k,v in report.items() if k!='runs'}),flush=True)
    hypotheses=[]
    for repetition in range(args.repeats):
        for sample in manifest:
            path=Path(sample['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=sample['sha256']:
                raise ValueError('Sample checksum mismatch')
            audio,rate=sf.read(path,dtype='float32',always_2d=False)
            if audio.ndim==2: audio=audio.mean(axis=1)
            if rate!=16000:
                divisor=math.gcd(rate,16000)
                audio=resample_poly(audio,16000//divisor,rate//divisor).astype(np.float32)
            torch.cuda.reset_peak_memory_stats();t=time.perf_counter()
            if args.asr_only:
                results=model.transcribe(audio=(audio,16000),language={'ko':'Korean','en':'English'}[sample['language']],return_time_stamps=False)
                segments=[{'text':item.text} for item in results]
            else:
                segments=model.transcribe(audio,sample['language'],final_chunk=True)
            torch.cuda.synchronize();elapsed=time.perf_counter()-t
            hypothesis=' '.join(segment['text'] for segment in segments)
            reference=Path(sample['reference_path']).read_text(encoding='utf-8')
            ref,hyp=normalized(reference),normalized(hypothesis)
            run={'id':sample['id'],'language':sample['language'],'repeat':repetition,'audio_seconds':len(audio)/16000,
                 'seconds':elapsed,'rtf':elapsed/(len(audio)/16000),'cer':distance(ref,hyp)/max(1,len(ref)),
                 'segments':len(segments),'peak_allocated_bytes':torch.cuda.max_memory_allocated(),'peak_reserved_bytes':torch.cuda.max_memory_reserved()}
            report['runs'].append(run);hypotheses.append({'id':sample['id'],'repeat':repetition,'text':hypothesis})
            (args.output/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            (args.output/'hypotheses.json').write_text(json.dumps(hypotheses,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(run),flush=True)
    report['scope']='Short licensed samples and bounded repeats; not a long-duration stability validation.'
    (args.output/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
