"""Raw audio kept as WAV for replay, with age and size cleanup."""

import logging
import os
import re
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

_LOGGER = logging.getLogger(__name__)

# A tiny clip still costs a disk block, so the size cap counts at least this per file.
MIN_FILE_COST = 4096
MAX_RUNLOG_BYTES = 20 * 1024 * 1024  # per device per day; past it, stdout only
# A day folder is only removed once it has been idle this long, so a save that
# straddles midnight cannot lose its folder to a purge.
FOLDER_IDLE_S = 3600

# Disk work gets its own threads. On the default executor a hung share would park
# threads that name lookups for Whisper also need.
IO_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dcv-audio")

# Only files written by this proxy are ever counted or deleted, so pointing
# DCV_AUDIO_DIR at a folder that holds other things cannot cost anyone their files.
_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WAV_RE = re.compile(r"[0-9]{6}_[A-Za-z0-9_.-]{1,64}_[0-9a-f]{8}\.wav")
_RUNLOG_RE = re.compile(r"runs_[A-Za-z0-9_.-]{1,64}\.jsonl")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # not available on Windows


def _day_folders(audio_dir: Path) -> Iterator[Path]:
    try:
        with os.scandir(audio_dir) as entries:
            for entry in entries:
                if _DAY_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                    yield Path(entry.path)
    except OSError:
        return


def _files_in(folder: Path, pattern: "re.Pattern") -> Iterator[tuple]:
    """(path, stat) for regular files in folder whose names match; symlinks are skipped."""
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not pattern.fullmatch(entry.name):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        yield Path(entry.path), entry.stat(follow_symlinks=False)
                except OSError:
                    continue
    except OSError:
        return


def _clips(audio_dir: Path) -> Iterator[tuple]:
    for folder in _day_folders(audio_dir):
        yield from _files_in(folder, _WAV_RE)


def _make_day_folder(audio_dir: Path, day: str) -> Path:
    folder = audio_dir / day
    if folder.is_symlink():
        raise OSError("day folder is a symlink")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_wav(
    audio_dir: Path, device: str, run_id: str, pcm: bytes, rate: int, width: int, channels: int
) -> Optional[Path]:
    """Write pcm as WAV. Returns the path, or None if the write failed (never raises)."""
    try:
        now = datetime.now()
        folder = _make_day_folder(audio_dir, now.strftime("%Y-%m-%d"))
        path = folder / f"{now.strftime('%H%M%S')}_{device}_{run_id}.wav"
        # Exclusive create: a planted symlink or an existing file is an error, never followed.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | _NOFOLLOW
        with os.fdopen(os.open(path, flags, 0o640), "wb") as raw:
            with wave.open(raw, "wb") as wav:
                wav.setframerate(rate)
                wav.setsampwidth(width)
                wav.setnchannels(channels)
                wav.writeframes(pcm)
        return path  # created 0640 (and the process umask is 027): not world-readable
    except Exception as err:  # noqa: BLE001 - audio logging must never break a run
        _LOGGER.error("could not save audio: %s", type(err).__name__)
        return None


def append_run_log(audio_dir: Path, device: str, line: str, folder: Optional[Path] = None) -> bool:
    """Append one run's JSON line to <day folder>/runs_<device>.jsonl, next to the audio.

    One file per device so two profiles sharing a folder never interleave. `folder`
    is the clip's own day folder when there is a clip (a run that straddles midnight
    still lands beside its audio). The line goes out in a single write so a failure
    cannot leave a fragment that the next line gets glued onto. Returns False on
    failure (never raises): the same line is always on stdout as well.
    """
    try:
        folder = folder or _make_day_folder(audio_dir, datetime.now().strftime("%Y-%m-%d"))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"runs_{device}.jsonl"
        data = (line + "\n").encode("utf-8")
        try:
            if path.stat().st_size + len(data) > MAX_RUNLOG_BYTES:
                return False  # size limit: a flood must not fill the disk
        except FileNotFoundError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0) | _NOFOLLOW
        fd = os.open(path, flags, 0o640)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("short write")
        finally:
            os.close(fd)
        return True
    except Exception as err:  # noqa: BLE001 - run logging must never break a run
        _LOGGER.error("could not write run log: %s", type(err).__name__)
        return False


def dir_size(audio_dir: Path) -> int:
    """Total bytes of this proxy's .wav files under audio_dir."""
    return sum(max(st.st_size, MIN_FILE_COST) for _, st in _clips(audio_dir))


def check_writable(audio_dir: Path) -> bool:
    """Probe at startup so a permissions problem is loud, not a silent null in the logs."""
    try:
        audio_dir.mkdir(parents=True, exist_ok=True)
        probe = audio_dir / ".write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError as err:
        _LOGGER.error("AUDIO DIR NOT WRITABLE (%s): raw audio will NOT be saved", err)
        return False


def purge_old(audio_dir: Path, retention_days: int, max_bytes: int = 0) -> int:
    """Delete this proxy's .wav files older than retention_days, then oldest-first until
    under max_bytes, then empty day folders. 0 disables that limit. Returns files removed.
    Files and folders that do not match the proxy's own naming are never touched."""
    removed = 0
    try:
        if not audio_dir.is_dir():
            return 0
        files = [(st.st_mtime, max(st.st_size, MIN_FILE_COST), path) for path, st in _clips(audio_dir)]
        files.sort()
        cutoff = time.time() - retention_days * 86400
        total = sum(size for _, size, _ in files)
        for mtime, size, wav in files:
            too_old = retention_days > 0 and mtime < cutoff
            too_big = max_bytes > 0 and total > max_bytes
            if not (too_old or too_big):
                break  # sorted oldest first: nothing later qualifies either
            try:
                wav.unlink()
                removed += 1
                total -= size
            except FileNotFoundError:
                total -= size  # another purge already removed it: don't delete more for it
            except OSError:
                pass
        if retention_days > 0:
            for folder in _day_folders(audio_dir):
                for log, st in _files_in(folder, _RUNLOG_RE):
                    try:
                        if st.st_mtime < cutoff:
                            log.unlink()
                    except OSError:
                        pass
        today = datetime.now().strftime("%Y-%m-%d")
        for folder in _day_folders(audio_dir):
            try:
                # Never today's folder, and never one touched recently: a save may be
                # about to create a file in it (for example just after midnight).
                if (
                    folder.name != today
                    and time.time() - folder.stat().st_mtime > FOLDER_IDLE_S
                    and not any(folder.iterdir())
                ):
                    folder.rmdir()
            except OSError:
                pass
    except OSError as err:
        _LOGGER.error("audio purge failed: %s", err)
    return removed
