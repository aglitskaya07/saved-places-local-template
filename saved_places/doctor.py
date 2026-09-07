"""Read-only dependency check; never opens or prints credentials."""
import shutil
import sys
from pathlib import Path
from saved_places.probe import DATA_ROOT

def main():
    print('Python:',sys.version.split()[0])
    for name in ('ffmpeg','ffprobe','swiftc'):
        print(name+':', 'готово' if shutil.which(name) else 'нужно установить')
    models=[Path('/opt/homebrew/share/whisper-cpp/ggml-small.bin'),Path.home()/'.cache/whisper-cpp/ggml-small.bin']
    speech=bool(shutil.which('whisper') or (shutil.which('whisper-cli') and any(p.is_file() for p in models)))
    print('Распознавание речи:', 'готово' if speech else 'нужны Whisper и модель; без них речь будет неполной')
    print('Локальные данные:',DATA_ROOT)
    print('HTML можно собрать без входа в Instagram.')
if __name__=='__main__':main()
