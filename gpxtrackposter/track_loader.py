# Copyright 2016-2017 Florian Pigorsch & Contributors. All rights reserved.
#
# Use of this source code is governed by a MIT-style
# license that can be found in the LICENSE file.

import hashlib
import logging
import os
import shutil
from typing import Dict, Generator, List
import concurrent.futures
from . import track
from . import year_range
from .exceptions import ParameterError, TrackLoadError

log = logging.getLogger(__name__)


def load_gpx_file(file_name: str) -> track.Track:
    log.info("Loading track {}...".format(os.path.basename(file_name)))
    t = track.Track()
    t.load_gpx(file_name)
    return t


def load_cached_track_file(cache_file_name: str, file_name: str) -> track.Track:
    try:
        t = track.Track()
        t.load_cache(cache_file_name)
        t.file_names = [os.path.basename(file_name)]
        log.info('Loaded track {} from cache file {}'.format(file_name, cache_file_name))
        return t
    except Exception as e:
        raise TrackLoadError('Failed to load track from cache.') from e


class TrackLoader:
    def __init__(self):
        self.min_length = 1000
        self.special_file_names = []
        self.year_range = year_range.YearRange()
        self.cache_dir = None
        self._cache_file_names = {}

    def clear_cache(self):
        if os.path.isdir(self.cache_dir):
            log.info("Removing cache dir: {}".format(self.cache_dir))
            try:
                shutil.rmtree(self.cache_dir)
            except OSError as e:
                log.info("Failed: {}".format(e))

    def load_tracks(self, base_dir: str) -> List[track.Track]:
        file_names = [x for x in self._list_gpx_files(base_dir)]
        log.info("GPX files: {}".format(len(file_names)))

        tracks = []

        # load track from cache
        cached_tracks = []
        if self.cache_dir:
            log.info("Trying to load {} track(s) from cache...".format(len(file_names)))
            cached_tracks = self._load_tracks_from_cache(file_names)
            log.info("Loaded tracks from cache:  {}".format(len(cached_tracks)))
            tracks = list(cached_tracks.values())

        # load remaining gpx files
        remaining_file_names = [f for f in file_names if f not in cached_tracks]
        if remaining_file_names:
            log.info(
                "Trying to load {} track(s) from GPX files; this may take a while...".format(len(remaining_file_names)))
            loaded_tracks = self._load_tracks(remaining_file_names)
            tracks.extend(loaded_tracks.values())
            log.info("Conventionally loaded tracks: {}".format(len(loaded_tracks)))
            self._store_tracks_to_cache(loaded_tracks)

        tracks = self._filter_tracks(tracks)

        # merge tracks that took place within one hour
        tracks = self._merge_tracks(tracks)
        # filter out tracks with length < min_length
        return [t for t in tracks if t.length >= self.min_length]

    def _filter_tracks(self, tracks: List[track.Track]) -> List[track.Track]:
        filtered_tracks = []
        for t in tracks:
            file_name = t.file_names[0]
            if t.length == 0:
                log.info("{}: skipping empty track".format(file_name))
            elif not t.start_time:
                log.info("{}: skipping track without start time".format(file_name))
            elif not self.year_range.contains(t.start_time):
                log.info("{}: skipping track with wrong year {}".format(file_name, t.start_time.year))
            else:
                t.special = (file_name in self.special_file_names)
                filtered_tracks.append(t)
        return filtered_tracks

    @staticmethod
    def _merge_tracks(tracks: List[track.Track]) -> List[track.Track]:
        log.info("Merging tracks...")
        tracks = sorted(tracks, key=lambda t1: t1.start_time)
        merged_tracks = []
        last_end_time = None
        for t in tracks:
            if last_end_time is None:
                merged_tracks.append(t)
            else:
                dt = (t.start_time - last_end_time).total_seconds()
                if 0 < dt < 3600:
                    merged_tracks[-1].append(t)
                else:
                    merged_tracks.append(t)
            last_end_time = t.end_time
        log.info("Merged {} track(s)".format(len(tracks) - len(merged_tracks)))
        return merged_tracks

    @staticmethod
    def _load_tracks(file_names: List[str]) -> List[track.Track]:
        tracks = {}
        with concurrent.futures.ProcessPoolExecutor() as executor:
            future_to_file_name = {
                executor.submit(load_gpx_file, file_name): file_name for file_name in file_names
            }
        for future in concurrent.futures.as_completed(future_to_file_name):
            file_name = future_to_file_name[future]
            try:
                t = future.result()
            except TrackLoadError as e:
                log.info("Error while loading {}: {}".format(file_name, e))
            else:
                tracks[file_name] = t

        return tracks

    def _load_tracks_from_cache(self, file_names: List[str]) -> List[track.Track]:
        tracks = {}
        with concurrent.futures.ProcessPoolExecutor() as executor:
            future_to_file_name = {
                executor.submit(load_cached_track_file, self._get_cache_file_name(file_name), file_name):
                    file_name for file_name in file_names
            }
        for future in concurrent.futures.as_completed(future_to_file_name):
            file_name = future_to_file_name[future]
            try:
                t = future.result()
            except Exception:
                # silently ignore failed cache load attempts
                pass
            else:
                tracks[file_name] = t
        return tracks

    def _store_tracks_to_cache(self, tracks: Dict[str, track.Track]):
        if (not tracks) or (not self.cache_dir):
            return

        log.info('Storing {} track(s) to cache...'.format(len(tracks)))
        for (file_name, t) in tracks.items():
            try:
                t.store_cache(self._get_cache_file_name(file_name))
            except Exception as e:
                log.warning('Failed to store track {} to cache: {}'.format(file_name, e))
            else:
                log.info('Stored track {} to cache'.format(file_name))

    @staticmethod
    def _list_gpx_files(base_dir: str) -> Generator[str, None, None]:
        base_dir = os.path.abspath(base_dir)
        if not os.path.isdir(base_dir):
            raise ParameterError("Not a directory: {}".format(base_dir))
        for name in os.listdir(base_dir):
            path_name = os.path.join(base_dir, name)
            if name.endswith(".gpx") and os.path.isfile(path_name):
                yield path_name

    def _get_cache_file_name(self, file_name: str) -> str:
        assert self.cache_dir

        if file_name in self._cache_file_names:
            return self._cache_file_names[file_name]

        try:
            checksum = hashlib.sha256(open(file_name, 'rb').read()).hexdigest()
        except PermissionError as e:
            raise TrackLoadError('Failed to compute checksum (bad permissions).') from e
        except Exception as e:
            raise TrackLoadError('Failed to compute checksum.') from e

        cache_file_name = os.path.join(self.cache_dir, checksum + '.json')
        self._cache_file_names[file_name] = cache_file_name
        return cache_file_name
