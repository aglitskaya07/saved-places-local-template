"""Resume all media in the last complete local snapshot, without Instagram reads."""
import argparse
import logging
from pathlib import Path

from saved_places.prepare import preparation_lock
from saved_places.probe import DATA_ROOT
from saved_places.update import selected_collections, update_many


def main():
    parser = argparse.ArgumentParser(description='Продолжить подготовку всех материалов из снимка')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--snapshot', type=Path, default=DATA_ROOT/'batches'/'latest.json')
    parser.add_argument('--retry-errors', action='store_true')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    with preparation_lock():
        selections = selected_collections(config_path=args.config)
        raise SystemExit(update_many(selections, retry_errors=args.retry_errors,
                                    all_items=True, cached=args.snapshot, workers=args.workers))


if __name__ == '__main__':
    main()
