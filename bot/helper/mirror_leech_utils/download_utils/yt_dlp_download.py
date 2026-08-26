from logging import getLogger
from os import path as ospath, listdir
from re import search as re_search
from secrets import token_urlsafe
from yt_dlp import YoutubeDL, DownloadError


from .... import task_dict_lock, task_dict
from ...ext_utils.bot_utils import sync_to_async, async_to_sync
from ...ext_utils.task_manager import check_running_tasks, stop_duplicate_check
from ...mirror_leech_utils.status_utils.queue_status import QueueStatus
from ...telegram_helper.message_utils import send_status_message
from ..status_utils.yt_dlp_status import YtDlpStatus

LOGGER = getLogger(__name__)


class MyLogger:
    def __init__(self, obj, listener):
        self._obj = obj
        self._listener = listener

    def debug(self, msg):
        # Hack to fix changing extension
        if not self._obj.is_playlist:
            # Respect listener's name lock to avoid overriding custom names
            try:
                if getattr(self._listener, 'lock_name', False):
                    return
            except Exception:
                pass
            if match := re_search(
                r".Merger..Merging formats into..(.*?).$", msg
            ) or re_search(r".ExtractAudio..Destination..(.*?)$", msg):
                LOGGER.info(msg)
                newname = match.group(1)
                newname = newname.rsplit("/", 1)[-1]
                self._listener.name = newname

    @staticmethod
    def warning(msg):
        LOGGER.warning(msg)

    @staticmethod
    def error(msg):
        if msg != "ERROR: Cancelling...":
            LOGGER.error(msg)


class YoutubeDLHelper:
    def __init__(self, listener):
        self._last_downloaded = 0
        self._progress = 0
        self._downloaded_bytes = 0
        self._download_speed = 0
        self._eta = "-"
        self._listener = listener
        self._gid = ""
        self._ext = ""
        self.is_playlist = False
        self.keep_thumb = False
        self.opts = {
            "progress_hooks": [self._on_download_progress],
            "logger": MyLogger(self, self._listener),
            "usenetrc": True,
            "cookiefile": "cookies.txt",
            "allow_multiple_video_streams": True,
            "allow_multiple_audio_streams": True,
            "noprogress": True,
            "allow_playlist_files": True,
            "overwrites": True,
            "writethumbnail": True,
            "trim_file_name": 220,
            "fragment_retries": 10,
            "retries": 10,
            "retry_sleep_functions": {
                "http": lambda n: 3,
                "fragment": lambda n: 3,
                "file_access": lambda n: 3,
                "extractor": lambda n: 3,
            },
        }

    @property
    def download_speed(self):
        return self._download_speed

    @property
    def downloaded_bytes(self):
        return self._downloaded_bytes

    @property
    def size(self):
        return self._listener.size

    @property
    def progress(self):
        return self._progress

    @property
    def eta(self):
        return self._eta

    def _on_download_progress(self, d):
        if self._listener.is_cancelled:
            raise ValueError("Cancelling...")
        if d["status"] == "finished":
            if self.is_playlist:
                self._last_downloaded = 0
        elif d["status"] == "downloading":
            self._download_speed = d["speed"] or 0
            if self.is_playlist:
                downloadedBytes = d["downloaded_bytes"] or 0
                chunk_size = downloadedBytes - self._last_downloaded
                self._last_downloaded = downloadedBytes
                self._downloaded_bytes += chunk_size
            else:
                if d.get("total_bytes"):
                    self._listener.size = d["total_bytes"] or 0
                elif d.get("total_bytes_estimate"):
                    self._listener.size = d["total_bytes_estimate"] or 0
                self._downloaded_bytes = d["downloaded_bytes"] or 0
                self._eta = d.get("eta", "-") or "-"
            try:
                self._progress = (self._downloaded_bytes / self._listener.size) * 100
            except:
                pass

    async def _on_download_start(self, from_queue=False):
        async with task_dict_lock:
            task_dict[self._listener.mid] = YtDlpStatus(self._listener, self, self._gid)
        if not from_queue:
            await self._listener.on_download_start()
            if self._listener.multi <= 1 and not self._listener.is_rss:
                await send_status_message(self._listener.message)

    def _on_download_error(self, error):
        self._listener.is_cancelled = True
        async_to_sync(self._listener.on_download_error, error)

    def _extract_meta_data(self):
        if self._listener.link.startswith(("rtmp", "mms", "rstp", "rtmps")):
            self.opts["external_downloader"] = "ffmpeg"

        # 与实际下载阶段保持一致：如果指定的 format 在此阶段不可用，回退到 'best' 再试一次
        tried_format_fallback = False
        while True:
            with YoutubeDL(self.opts) as ydl:
                try:
                    result = ydl.extract_info(self._listener.link, download=False)
                    if result is None:
                        raise ValueError("Info result is None")
                    break
                except DownloadError as e:
                    msg = str(e)
                    if (
                        not tried_format_fallback
                        and (
                            "Requested format is not available" in msg
                            or "format is not available" in msg
                        )
                    ):
                        tried_format_fallback = True
                        LOGGER.warning(
                            "YT-DLP format not available during metadata extraction, falling back to 'best'"
                        )
                        self.opts["format"] = "best"
                        continue
                    return self._on_download_error(msg)
                except Exception as e:
                    return self._on_download_error(str(e))
            if "entries" in result:
                for entry in result["entries"]:
                    if not entry:
                        continue
                    elif "filesize_approx" in entry:
                        self._listener.size += entry.get("filesize_approx", 0) or 0
                    elif "filesize" in entry:
                        self._listener.size += entry.get("filesize", 0) or 0
                    if not self._listener.name:
                        outtmpl_ = "%(series,playlist_title,channel)s%(season_number& |)s%(season_number&S|)s%(season_number|)02d.%(ext)s"
                        self._listener.name, ext = ospath.splitext(
                            ydl.prepare_filename(entry, outtmpl=outtmpl_)
                        )
                        if not self._ext:
                            self._ext = ext
            else:
                outtmpl_ = "%(title,fulltitle,alt_title)s%(season_number& |)s%(season_number&S|)s%(season_number|)02d%(episode_number&E|)s%(episode_number|)02d%(height& |)s%(height|)s%(height&p|)s%(fps|)s%(fps&fps|)s%(tbr& |)s%(tbr|)d.%(ext)s"
                realName = ydl.prepare_filename(result, outtmpl=outtmpl_)
                ext = ospath.splitext(realName)[-1]
                # 如果已提前设置了自定义文件名，则仅在它没有扩展名时追加提取到的扩展名，避免出现 .mp3.mp3
                if self._listener.name:
                    base, cur_ext = ospath.splitext(self._listener.name)
                    self._listener.name = (
                        self._listener.name if cur_ext else f"{self._listener.name}{ext}"
                    )
                else:
                    self._listener.name = realName
                if not self._ext:
                    self._ext = ext

    def _download(self, path):
        try:
            # 通用兜底：如果指定的 format 不可用，则自动回退到 'best' 再重试一次
            tried_format_fallback = False
            while True:
                with YoutubeDL(self.opts) as ydl:
                    try:
                        ydl.download([self._listener.link])
                        break
                    except DownloadError as e:
                        msg = str(e)
                        # 只针对格式不可用的错误做一次兜底重试
                        if (
                            not tried_format_fallback
                            and (
                                "Requested format is not available" in msg
                                or "format is not available" in msg
                            )
                        ):
                            tried_format_fallback = True
                            LOGGER.warning(
                                "YT-DLP format not available, falling back to 'best' format for download"
                            )
                            # 回退到最稳妥的 'best'，交由 yt-dlp 自行选择合适流
                            self.opts["format"] = "best"
                            continue

                        if not self._listener.is_cancelled:
                            self._on_download_error(msg)
                        return
            if self.is_playlist and (
                not ospath.exists(path) or len(listdir(path)) == 0
            ):
                self._on_download_error(
                    "No video available to download from this playlist. Check logs for more details"
                )
                return
            if self._listener.is_cancelled:
                return
            async_to_sync(self._listener.on_download_complete)
        except:
            pass
        return

    async def add_download(self, path, qual, playlist, options):
        if playlist:
            self.opts["ignoreerrors"] = True
            self.is_playlist = True

        self._gid = token_urlsafe(10)

        await self._on_download_start()

        self.opts["postprocessors"] = [
            {
                "add_chapters": True,
                "add_infojson": "if_exists",
                "add_metadata": True,
                "key": "FFmpegMetadata",
            }
        ]

        if qual.startswith("ba/b-"):
            audio_info = qual.split("-")
            qual = audio_info[0]
            audio_format = audio_info[1]
            rate = audio_info[2]
            self.opts["postprocessors"].append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": rate,
                }
            )
            if audio_format == "vorbis":
                self._ext = ".ogg"
            elif audio_format == "alac":
                self._ext = ".m4a"
            else:
                self._ext = f".{audio_format}"

        if not self._listener.is_leech or self._listener.thumbnail_layout:
            self.opts["writethumbnail"] = False

        if options:
            self._set_options(options)

        self.opts["format"] = qual

        await sync_to_async(self._extract_meta_data)
        if self._listener.is_cancelled:
            return

        base_name, ext = ospath.splitext(self._listener.name) if self._listener.name else ("", "")
        start_path = path if self.keep_thumb else f"{path}/yt-dlp-thumb"

        if not self._listener.name:
            # Let yt-dlp resolve the title when metadata did not provide a name.
            generic_name_tmpl = "%(title,fulltitle,alt_title)s"
            self.opts["outtmpl"] = {
                "default": f"{path}/{generic_name_tmpl}.%(ext)s",
                "thumbnail": f"{start_path}/{generic_name_tmpl}.%(ext)s",
            }
        elif self.is_playlist:
            self.opts["outtmpl"] = {
                "default": f"{path}/{self._listener.name}/%(playlist_index)03d - %(title,fulltitle,alt_title)s%(season_number& |)s%(season_number&S|)s%(season_number|)02d%(episode_number&E|)s%(episode_number|)02d%(height& |)s%(height|)s%(height&p|)s%(fps|)s%(fps&fps|)s%(tbr& |)s%(tbr|)d.%(ext)s",
                "thumbnail": f"{start_path}/%(playlist_index)03d - %(title,fulltitle,alt_title)s%(season_number& |)s%(season_number&S|)s%(season_number|)02d%(episode_number&E|)s%(episode_number|)02d%(height& |)s%(height|)s%(height&p|)s%(fps|)s%(fps&fps|)s%(tbr& |)s%(tbr|)d.%(ext)s",
            }
        elif "download_ranges" in options:
            self.opts["outtmpl"] = {
                "default": f"{path}/{base_name}/%(section_number|)s%(section_number&.|)s%(section_title|)s%(section_title&-|)s%(title,fulltitle,alt_title)s %(section_start)s to %(section_end)s.%(ext)s",
                "thumbnail": f"{start_path}/%(section_number|)s%(section_number&.|)s%(section_title|)s%(section_title&-|)s%(title,fulltitle,alt_title)s %(section_start)s to %(section_end)s.%(ext)s",
            }
        elif any(
            key in options
            for key in [
                "writedescription",
                "writeinfojson",
                "writeannotations",
                "writedesktoplink",
                "writewebloclink",
                "writelink",
                "writeurllink",
                "writesubtitles",
                "write_all_thumbnails",
                "writeautomaticsub",
            ]
        ):
            self.opts["outtmpl"] = {
                "default": f"{path}/{base_name}/{self._listener.name}",
                "thumbnail": f"{start_path}/{base_name}.%(ext)s",
            }
        else:
            trim_name = self._listener.name if self.is_playlist else base_name
            if len(trim_name.encode()) > 200:
                self._listener.name = (
                    self._listener.name[:200]
                    if self.is_playlist
                    else f"{base_name[:200]}{ext}"
                )
                base_name = ospath.splitext(self._listener.name)[0]

            self.opts["outtmpl"] = {
                "default": f"{path}/{self._listener.name}",
                "thumbnail": f"{start_path}/{base_name}.%(ext)s",
            }

        if qual.startswith("ba/b"):
            self._listener.name = f"{base_name}{self._ext}"

        if self.opts["writethumbnail"]:
            self.opts["postprocessors"].append(
                {
                    "format": "jpg",
                    "key": "FFmpegThumbnailsConvertor",
                    "when": "before_dl",
                }
            )
        if self._ext in [
            ".mp3",
            ".mkv",
            ".mka",
            ".ogg",
            ".opus",
            ".flac",
            ".m4a",
            ".mp4",
            ".mov",
            ".m4v",
        ]:
            # 对于音频格式（特别是 ba/b- 提取的），不使用 already_have_thumbnail
            # 让 yt-dlp 自己下载并嵌入缩略图，避免文件名不匹配问题
            if qual.startswith("ba/b"):
                self.opts["postprocessors"].append(
                    {
                        "already_have_thumbnail": False,
                        "key": "EmbedThumbnail",
                    }
                )
            else:
                self.opts["postprocessors"].append(
                    {
                        "already_have_thumbnail": bool(
                            self._listener.is_leech and not self._listener.thumbnail_layout
                        ),
                        "key": "EmbedThumbnail",
                    }
                )
        elif not self._listener.is_leech:
            self.opts["writethumbnail"] = False

        msg, button = await stop_duplicate_check(self._listener)
        if msg:
            await self._listener.on_download_error(msg, button)
            return

        add_to_queue, event = await check_running_tasks(self._listener)
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Download: {self._listener.name}")
            async with task_dict_lock:
                task_dict[self._listener.mid] = QueueStatus(
                    self._listener, self._gid, "dl"
                )
            await event.wait()
            if self._listener.is_cancelled:
                return
            LOGGER.info(f"Start Queued Download from YT_DLP: {self._listener.name}")
            await self._on_download_start(True)

        if not add_to_queue:
            LOGGER.info(f"Download with YT_DLP: {self._listener.name}")

        await sync_to_async(self._download, path)

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self._listener.name}")
        await self._listener.on_download_error("Stopped by User!")

    def _set_options(self, options):
        for key, value in options.items():
            if key == "postprocessors":
                if isinstance(value, list):
                    self.opts[key].extend(tuple(value))
                elif isinstance(value, dict):
                    self.opts[key].append(value)
            elif key == "download_ranges":
                if isinstance(value, list):
                    self.opts[key] = lambda info, ytdl: value
            else:
                if key == "writethumbnail" and value is True:
                    self.keep_thumb = True
                self.opts[key] = value
