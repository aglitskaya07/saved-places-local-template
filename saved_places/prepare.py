"""Local media preparation. Interpretation is performed by Codex, not by FFmpeg."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import os
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import io
import requests
from PIL import Image

from saved_places.probe import (
    DATA_ROOT, SESSION_ROOT, AccessStopped, connect, read_sample, safe_media,
    downloaded_video, atomic_json, read_json, validate_cdn_url,
)
from saved_places.store import init_db,import_sources,database,now

PACKETS=DATA_ROOT/'packets'


@contextmanager
def preparation_lock():
    """Keep two manual launches from changing the same packets and run state."""
    DATA_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(DATA_ROOT/'prepare.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Подготовка уже запущена. Дождитесь её завершения.') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def recognize_frames(directory: Path, frame_paths: list[Path]) -> list:
    executable=DATA_ROOT/'bin'/'ocr'
    if not executable.exists():
        executable.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        command(['swiftc',str(Path(__file__).with_name('ocr.swift')),'-o',str(executable)])
    ocr=json.loads(command([str(executable),*[str(p) for p in frame_paths]],timeout=300))
    atomic_json(directory/'ocr.json',{'frames':ocr})
    return ocr


def prepare_images(media: dict, directory: Path) -> list[Path]:
    frames=directory/'frames'
    if frames.exists(): shutil.rmtree(frames)
    frames.mkdir(exist_ok=True,mode=0o700)
    for index,item in enumerate((media.get('carousel_media') or [media])[:20],1):
        candidates=item.get('image_versions2',{}).get('candidates',[])
        if not candidates: continue
        url=candidates[0]['url']
        validate_cdn_url(url)
        with requests.get(url,stream=True,timeout=(10,20),allow_redirects=False) as response:
            if response.status_code!=200: raise RuntimeError('Изображение недоступно')
            content=bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content)>20*1024*1024: raise RuntimeError('Изображение превышает 20 МБ')
        with Image.open(io.BytesIO(content)) as image:
            image.thumbnail((1080,1920))
            image.convert('RGB').save(frames/f'{index:04}.jpg')
    return sorted(frames.glob('*.jpg'))


def command(args: list[str], timeout: int=120) -> str:
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{Path(args[0]).name}: не удалось подготовить материал')
    return result.stdout


def transcribe_audio(wav: Path, directory: Path) -> tuple[dict, str]:
    """Prefer the installed Metal backend; keep a portable CPU fallback."""
    models = [Path('/opt/homebrew/share/whisper-cpp/ggml-small.bin'),
              Path.home()/'.cache/whisper-cpp/ggml-small.bin']
    model = next((p for p in models if p.is_file()), None)
    if shutil.which('whisper-cli') and model:
        output = directory/'transcript'
        command(['whisper-cli','-m',str(model),'-f',str(wav),'-l','auto',
                 '-oj','-of',str(output),'-t','4','-bs','1','-bo','1'],timeout=180)
        result = read_json(output.with_suffix('.json'))
        segments = [{'text':s['text'],'start':s['offsets']['from']/1000,
                     'end':s['offsets']['to']/1000} for s in result.get('transcription',[])]
        return {'text':' '.join(s['text'].strip() for s in segments), 'segments':segments}, 'whisper.cpp small (Metal)'
    command(['whisper',str(wav),'--model','tiny','--device','cpu','--fp16','False',
             '--output_format','json','--output_dir',str(directory),'--verbose','False',
             '--condition_on_previous_text','False'],timeout=180)
    return read_json(directory/'audio.json'), 'Whisper tiny (CPU)'


def prepare_media(media:dict) -> dict:
    post=safe_media(media)
    directory=PACKETS/post['id']
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in ('speech.json', 'ocr.json'):
        (directory/name).unlink(missing_ok=True)
    packet={**post,'coverage':{'caption':bool(post['caption']),'frames':False,'speech':False},'errors':[]}
    versions=media.get('video_versions') or []
    if not versions:
        frame_paths=prepare_images(media,directory)
        packet['frame_count']=len(frame_paths)
        packet['coverage']['frames']=bool(frame_paths)
        packet['format']='image_or_carousel'
        items=media.get('carousel_media') or [media]
        if len(frame_paths) != len(items):
            packet['errors'].append('Часть изображений недоступна или превышен предел 20 слайдов.')
        if any(item.get('media_type') == 2 for item in items):
            packet['errors'].append('Видео недоступно для разбора: использована только обложка.')
        if frame_paths:
            ocr=recognize_frames(directory,frame_paths)
            packet['ocr_file']=str(directory/'ocr.json')
            if any('error' in frame for frame in ocr):
                packet['errors'].append('Часть кадров не удалось прочитать.')
    else:
        with downloaded_video(versions[0]['url']) as video:
            info=json.loads(command(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(video)]))
            duration=float(info['format'].get('duration',0))
            packet['duration_seconds']=duration
            # Up to 180 frames per reel. Timestamp spacing is explicit in the packet.
            interval=max(1,duration/180)
            packet['frame_interval_seconds']=interval
            frames=directory/'frames'
            if frames.exists(): shutil.rmtree(frames)
            frames.mkdir(mode=0o700)
            command(['ffmpeg','-v','error','-nostdin','-i',str(video),'-vf',f'fps=1/{interval},scale=720:-2',
                     '-frames:v','180',str(frames/'%04d.jpg')])
            frame_paths=sorted(frames.glob('*.jpg'))
            packet['frame_count']=len(frame_paths)
            packet['coverage']['frames']=bool(frame_paths)
            if frame_paths:
                ocr=recognize_frames(directory,frame_paths)
                packet['ocr_file']=str(directory/'ocr.json')
                if any('error' in frame for frame in ocr): packet['errors'].append('Часть кадров не удалось прочитать.')
            else:
                packet['errors'].append('Не удалось извлечь кадры видео.')
            if any(s.get('codec_type')=='audio' for s in info.get('streams',[])):
                packet['audio_present']=True
                with tempfile.TemporaryDirectory(prefix='saved-places-audio-') as tmp:
                    wav=Path(tmp)/'audio.wav'
                    command(['ffmpeg','-v','error','-nostdin','-i',str(video),'-vn','-ar','16000','-ac','1',str(wav)])
                    try:
                        transcript, speech_model=transcribe_audio(wav,Path(tmp))
                        atomic_json(directory/'speech.json',transcript)
                        packet['coverage']['speech']=True
                        packet['speech_model']=speech_model+'; проверить имена по кадрам и описанию'
                    except (RuntimeError,subprocess.TimeoutExpired):
                        packet['errors'].append('Расшифровка речи не завершена.')
            else:
                packet['audio_present']=False
    packet['incomplete']=bool(packet['errors'])
    atomic_json(directory/'packet.json',packet)
    return packet


def main():
    with preparation_lock():
        _main()


def _main():
    parser=argparse.ArgumentParser(description='Подготовить сохранения локально для разбора в Codex')
    parser.add_argument('--collection',required=True)
    parser.add_argument('--city',required=True)
    parser.add_argument('--limit',type=int,choices=range(1,16),default=10)
    parser.add_argument('--retry',action='store_true')
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    init_db()
    collections=read_json(DATA_ROOT/'collections.json').get('collections',[])
    collection=next((c for c in collections if c['id']==args.collection),None)
    if not collection: raise SystemExit('Коллекция не выбрана из подтверждённого списка.')
    sessions=[p for p in SESSION_ROOT.glob('*.json') if re.fullmatch(r'[a-f0-9]{24}',p.stem)]
    if len(sessions)!=1: raise SystemExit('Нужна одна явно подключённая Instagram-сессия.')
    with database() as db:
        run=db.execute("INSERT INTO runs(started_at,status) VALUES(?,'running')",(now(),)).lastrowid
    processed=errors=found=0
    status='interrupted'
    try:
        client,_=connect(sessions[0],'')
        media,complete=read_sample(client,args.collection,args.limit)
        import_sources(collection,args.city,[safe_media(m) for m in media],complete)
        found=len(media)
        for m in media:
            mid=str(m['pk'])
            with database() as db:
                row=db.execute('SELECT status FROM sources WHERE id=?',(mid,)).fetchone()
            if row['status']=='done' or (row['status']=='prepared' and not args.retry): continue
            print(f'Готовлю {mid}…',flush=True)
            try:
                packet=prepare_media(m)
                with database() as db:
                    db.execute("UPDATE sources SET status='prepared',error=?,coverage=? WHERE id=?",
                               ('; '.join(packet['errors']),json.dumps(packet['coverage'],ensure_ascii=False),mid))
                processed+=1
                print(f"Готово: {packet.get('frame_count',0)} кадров, речь: {packet['coverage']['speech']}",flush=True)
            except AccessStopped: raise
            except Exception as exc:
                errors+=1
                with database() as db:
                    db.execute("UPDATE sources SET status='error',error=? WHERE id=?",(type(exc).__name__,mid))
                print('Не удалось подготовить публикацию:',type(exc).__name__,flush=True)
            with database() as db:
                db.execute('UPDATE runs SET found=?,processed=?,errors=? WHERE id=?',(found,processed,errors,run))
        status='finished' if not errors else 'partial'
    except KeyboardInterrupt:
        status='interrupted'
        print('Подготовка прервана. Готовые публикации сохранены.',flush=True)
        raise
    except Exception as exc:
        status='blocked'
        print('Подготовка остановлена:',type(exc).__name__,flush=True)
        raise SystemExit(1) from None
    finally:
        with database() as db:
            db.execute('UPDATE runs SET finished_at=?,status=?,found=?,processed=?,errors=?,message=? WHERE id=?',
                       (now(),status,found,processed,errors,'Материалы готовятся для разбора в Codex.',run))


if __name__=='__main__': main()
