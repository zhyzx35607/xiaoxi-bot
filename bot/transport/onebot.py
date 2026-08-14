# bot/client.py - OneBot v11 WebSocket Client
import asyncio, json, logging, os, uuid
try:
    import fcntl
except ImportError:  # Windows local development/tests
    fcntl = None
import websockets
import aiohttp, time
from api_registry import REGISTRY

log = logging.getLogger("qqbot")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PID_FILE = os.getenv("QQBOT_PID_FILE") or os.path.join(_ROOT, "bot.pid")
class OneBotClient:
    def __init__(self, config):
        self.config = config
        self.ws_url = config["ws_url"]
        self.token = config["token"]
        self.bot_qq = config["bot_qq"]
        self._ws = None
        self._connected_event = asyncio.Event()
        self._pending = {}
        self._dispatcher = None
        self._running = False
        self._event_tasks = set()
        self._session = None
        runtime = config.get("runtime", {})
        self._pid_fd = None
        self._queue_size = int(runtime.get("ws_queue_size", 50))
        self._max_event_tasks = int(runtime.get("max_event_tasks", 8))
        self._api_timeout = int(runtime.get("api_timeout_seconds", 8))
        self._forward_timeout = int(runtime.get("forward_timeout_seconds", 120))
        self._connect_timeout = float(runtime.get("connect_timeout_seconds", 5))
        self._reconnect_max_delay = float(runtime.get("reconnect_max_delay_seconds", 60))
        self._dispatch_sem = asyncio.Semaphore(self._max_event_tasks)
        self._stop_event = asyncio.Event()
        self._capabilities = {}
        self._last_queue_warning = 0.0
        self._queue_bytes = 0
        self._queue_byte_budget = 8 * 1024 * 1024

    def set_dispatcher(self, dispatcher):
        self._dispatcher = dispatcher

    @property
    def is_connected(self):
        return self._ws is not None

    async def wait_until_connected(self, timeout=None):
        if self.is_connected:
            return True
        try:
            if timeout is None:
                await self._connected_event.wait()
            else:
                await asyncio.wait_for(
                    self._connected_event.wait(), timeout=max(0.1, float(timeout)))
            return self.is_connected
        except asyncio.TimeoutError:
            return False

    def _acquire_pid(self):
        pid = os.getpid()
        fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                with os.fdopen(os.dup(fd), "r", encoding="utf-8") as f:
                    old_pid = f.read().strip() or "unknown"
            except OSError:
                old_pid = "unknown"
            os.close(fd)
            log.warning("Another instance is already running (PID %s). Exiting.", old_pid)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(pid).encode("utf-8"))
        self._pid_fd = fd
        return True

    def _release_pid(self):
        if self._pid_fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._pid_fd, fcntl.LOCK_UN)
            os.close(self._pid_fd)
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except OSError:
            pass
        finally:
            self._pid_fd = None

    async def stop(self):
        self._running = False
        self._stop_event.set()
        self._connected_event.clear()
        if self._ws:
            try:
                await self._ws.close()
            except Exception as error:
                log.debug("WebSocket close failed during shutdown: %s", error)
        await self._cancel_event_tasks()

    async def _cancel_event_tasks(self):
        tasks = [t for t in self._event_tasks if not t.done()]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        except asyncio.TimeoutError:
            log.warning("Timed out waiting for %d event tasks to stop", len(tasks))

    def _discard_oldest_queued_event(self, msg_queue):
        try:
            dropped = msg_queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        if dropped is not None:
            self._queue_bytes = max(0, self._queue_bytes - dropped[1])
        return True

    async def run(self):
        if not self._acquire_pid():
            return

        self._running = True
        self._stop_event.clear()
        self._session = aiohttp.ClientSession()
        try:
            url = self.ws_url
            if self.token:
                sep = "&" if "?" in url else "?"
                url = url + sep + "access_token=" + self.token
            retry_delay = 1

            while self._running:
                try:
                    async with websockets.connect(
                        url,
                        max_size=1 * 1024 * 1024,
                        open_timeout=self._connect_timeout,
                        ping_interval=30,
                        ping_timeout=20,
                        max_queue=16,
                    ) as ws:
                        self._ws = ws
                        self._connected_event.set()
                        retry_delay = 1
                        log.info("Connected to OneBot WS")
                        connected_at = time.monotonic()

                        msg_queue = asyncio.Queue(maxsize=self._queue_size)
                        self._queue_bytes = 0

                        async def ws_reader(queue=msg_queue):
                            try:
                                async for raw in ws:
                                    try:
                                        data = json.loads(raw)
                                    except json.JSONDecodeError:
                                        log.warning("Invalid OneBot JSON frame: chars=%s", len(str(raw)))
                                        continue

                                    # API replies must never wait behind event dispatch. Event
                                    # handlers often await these futures themselves.
                                    if "echo" in data:
                                        echo = data["echo"]
                                        fut = self._pending.pop(echo, None)
                                        if fut is not None and not fut.done():
                                            fut.set_result(data)
                                        continue
                                    if data.get("post_type") == "meta_event":
                                        continue

                                    raw_len = len(raw)
                                    if raw_len > 256 * 1024:
                                        log.warning("Large WS frame: %.0fKB post_type=%s",
                                                    raw_len / 1024, data.get("post_type"))
                                    if self._queue_bytes + raw_len > self._queue_byte_budget:
                                        now = time.monotonic()
                                        if now - self._last_queue_warning >= 10:
                                            log.warning("Dropping event: WS queue byte budget exceeded")
                                            self._last_queue_warning = now
                                        continue
                                    try:
                                        queue.put_nowait((data, raw_len))
                                        self._queue_bytes += raw_len
                                    except asyncio.QueueFull:
                                        now = time.monotonic()
                                        if now - self._last_queue_warning >= 10:
                                            log.warning("Dropping event because WebSocket event queue is full")
                                            self._last_queue_warning = now
                            except Exception as e:
                                log.error("Reader error: %s", e)
                            finally:
                                try:
                                    queue.put_nowait(None)
                                except asyncio.QueueFull:
                                    self._discard_oldest_queued_event(queue)
                                    try:
                                        queue.put_nowait(None)
                                    except asyncio.QueueFull:
                                        log.warning("Message queue full while closing reader")

                        reader_task = asyncio.create_task(ws_reader())
                        probe_task = asyncio.create_task(self._probe_core_capabilities())
                        self._event_tasks.add(probe_task)
                        probe_task.add_done_callback(self._event_tasks.discard)

                        while self._running:
                            item = await msg_queue.get()
                            if item is None:
                                break
                            data, raw_len = item
                            self._queue_bytes -= raw_len
                            try:
                                # Bound dispatch task objects while the reader continues
                                # processing API replies independently.
                                while self._running and len(self._event_tasks) >= self._max_event_tasks * 2:
                                    await asyncio.sleep(0.05)
                                t = asyncio.create_task(self._dispatch_safe(data))
                                self._event_tasks.add(t)
                                t.add_done_callback(self._event_tasks.discard)

                            except Exception as e:
                                log.exception("Message loop error: %s", e)

                        reader_task.cancel()
                        try:
                            await reader_task
                        except asyncio.CancelledError:
                            pass
                        if self._running:
                            session_secs = time.monotonic() - connected_at
                            if session_secs < 5:
                                log.warning(
                                    "WS closed %.1fs after connect (check token/NapCat config); reconnecting",
                                    session_secs)
                            else:
                                log.info("WS connection ended after %.0fs; reconnecting",
                                         session_secs)

                except websockets.ConnectionClosed as e:
                    log.info("Connection closed: %s", e)
                except Exception as e:
                    # Surface connection failures early; silent retries hid a token
                    # mismatch for minutes in the past.
                    if retry_delay <= 2:
                        log.info("Connect error: %s (retry in %ds)", e, retry_delay)
                    else:
                        log.warning("Connect error after repeated retries: %s (retry in %ds)", e, retry_delay)
                finally:
                    self._connected_event.clear()
                    self._ws = None
                    self._queue_bytes = 0
                    for echo, fut in list(self._pending.items()):
                        if not fut.done():
                            fut.set_result({
                                "status": "failed", "retcode": -1,
                                "msg": "disconnected", "message": "disconnected",
                                "error_kind": "disconnected",
                            })
                    self._pending.clear()

                if self._running:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=retry_delay)
                    except asyncio.TimeoutError:
                        pass
                    retry_delay = min(retry_delay * 2, self._reconnect_max_delay)

        finally:
            await self._cancel_event_tasks()
            if self._session:
                try:
                    await asyncio.sleep(0.3)
                    await self._session.close()
                except Exception as error:
                    log.debug("HTTP session close failed during shutdown: %s", error)
            self._session = None
            self._release_pid()

    async def _dispatch_safe(self, data):
        async with self._dispatch_sem:
            try:
                if self._dispatcher:
                    await self._dispatcher.dispatch(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Dispatch error: %s", e)

    async def call(self, action, params=None, timeout=None):
        ws = self._ws
        if not self.is_connected or ws is None:
            return {
                "status": "failed", "retcode": -1, "msg": "not connected",
                "message": "not connected", "action": action,
                "error_kind": "disconnected",
            }
        echo = str(uuid.uuid4())[:8]
        req = {"action": action, "params": params or {}, "echo": echo}
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[echo] = fut
        try:
            await ws.send(json.dumps(req, ensure_ascii=False))
            timeout_seconds = self._api_timeout if timeout is None else max(1, float(timeout))
            result = await asyncio.wait_for(fut, timeout=timeout_seconds)
            normalized = self._normalize_result(action, result)
            self._record_capability(action, normalized)
            return normalized
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            log.warning("API %s -> TIMEOUT", action)
            self._capabilities[action] = "temporary_failed"
            return {"status": "timeout", "msg": "API call timed out", "action": action,
                    "error_kind": "timeout"}
        except Exception as e:
            self._pending.pop(echo, None)
            log.error("API %s error: %s", action, e)
            self._capabilities[action] = "temporary_failed"
            return {"status": "failed", "msg": str(e), "action": action,
                    "error_kind": "exception"}

    @staticmethod
    def _normalize_result(action, result):
        result = result if isinstance(result, dict) else {"data": result}
        status = result.get("status", "failed")
        retcode = result.get("retcode", 0 if status == "ok" else -1)
        return {**result, "action": action, "ok": status == "ok" and retcode == 0,
                "retcode": retcode, "message": result.get("message", result.get("msg", "")),
                "error_kind": None if status == "ok" and retcode == 0 else result.get("error_kind", "api")}

    def _record_capability(self, action, result):
        if result.get("ok"):
            self._capabilities[action] = "supported"
            return
        text = " ".join(str(result.get(k, "")) for k in ("msg", "message", "wording")).lower()
        retcode = result.get("retcode")
        if retcode in (1404, 404) or "not found" in text or "不支持" in text or "不存在" in text:
            self._capabilities[action] = "unsupported"
        elif result.get("error_kind") in ("timeout", "exception"):
            self._capabilities[action] = "temporary_failed"
        else:
            self._capabilities.setdefault(action, "unknown")

    async def group_poke(self, group_id, user_id):
        return await self.call("group_poke", {"group_id": group_id, "user_id": user_id})

    async def _probe_core_capabilities(self):
        """Probe a small read-only capability set after each connection."""
        actions = (
            ("get_status", self.get_status),
            ("get_version_info", self.get_version_info),
            ("get_login_info", self.get_login_info),
            ("can_send_image", self.can_send_image),
            ("can_send_record", self.can_send_record),
            ("get_robot_uin_range", self.get_robot_uin_range),
        )
        results = {}
        for action, handler in actions:
            if not self.is_connected:
                return
            try:
                results[action] = await handler()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.info("NapCat capability probe %s failed: %s", action, error)
        version_data = (results.get("get_version_info") or {}).get("data") or {}
        status_data = (results.get("get_status") or {}).get("data") or {}
        log.info(
            "NapCat probe: version=%s protocol=%s online=%s image=%s record=%s",
            version_data.get("app_version") or "unknown",
            version_data.get("protocol_version") or "unknown",
            status_data.get("online"),
            ((results.get("can_send_image") or {}).get("data") or {}).get("yes"),
            ((results.get("can_send_record") or {}).get("data") or {}).get("yes"),
        )

    async def get_status(self):
        return await self.call("get_status", {})

    async def get_version_info(self):
        return await self.call("get_version_info", {})

    async def get_login_info(self):
        return await self.call("get_login_info", {})

    async def can_send_image(self):
        return await self.call("can_send_image", {})

    async def can_send_record(self):
        return await self.call("can_send_record", {})

    async def api_status(self):
        """Return registry status without probing every API on a low-memory host."""
        connected = self._ws is not None
        return [{"name": spec.name, "category": spec.category, "risk": spec.risk,
                 "ai_allowed": spec.ai_allowed,
                 "status": self._capabilities.get(spec.name, "unknown" if connected else "offline")}
                for spec in REGISTRY.values()]

    async def execute_message_action(self, **kwargs):
        from actions import execute_message_action
        return await execute_message_action(self, **kwargs)

    async def send_group_msg(self, group_id, message):
        if isinstance(message, str):
            log.debug("[SEND] group=%s text_chars=%s", group_id, len(message))
            message = [{"type": "text", "data": {"text": message}}]
        else:
            log.debug("[SEND] group=%s payload_type=%s", group_id, type(message).__name__)
        params = {"group_id": group_id, "message": message}
        if self._message_contains_media(message):
            return await self.call(
                "send_group_msg", params, timeout=max(self._api_timeout, 60))
        return await self.call("send_group_msg", params)

    async def send_private_msg(self, user_id, message):
        if isinstance(message, str):
            message = [{"type": "text", "data": {"text": message}}]
        params = {"user_id": user_id, "message": message}
        if self._message_contains_media(message):
            return await self.call(
                "send_private_msg", params, timeout=max(self._api_timeout, 60))
        return await self.call("send_private_msg", params)

    @staticmethod
    def _message_contains_media(message):
        if not isinstance(message, list):
            return False
        return any(
            isinstance(segment, dict)
            and str(segment.get("type") or "").lower() in {"image", "record", "video", "file"}
            for segment in message
        )

    async def send_msg(self, message_type, user_id=None, group_id=None, message=None):
        if isinstance(message, str):
            message = [{"type": "text", "data": {"text": message}}]
        params = {"message_type": message_type, "message": message or []}
        if user_id:
            params["user_id"] = user_id
        if group_id:
            params["group_id"] = group_id
        return await self.call("send_msg", params)

    async def send_like(self, user_id, times=10):
        return await self.call("send_like", {"user_id": user_id, "times": times})

    async def check_url_safely(self, url):
        return await self.call("check_url_safely", {"url": url})

    async def set_group_kick(self, group_id, user_id, reject_add=False):
        return await self.call("set_group_kick",
                               {"group_id": group_id, "user_id": user_id, "reject_add_request": reject_add})

    async def set_group_ban(self, group_id, user_id, duration=1800):
        return await self.call("set_group_ban", {"group_id": group_id, "user_id": user_id, "duration": duration})

    async def set_group_whole_ban(self, group_id, enable=True):
        return await self.call("set_group_whole_ban", {"group_id": group_id, "enable": bool(enable)})

    async def set_group_admin(self, group_id, user_id, enable=True):
        return await self.call("set_group_admin", {"group_id": group_id, "user_id": user_id, "enable": bool(enable)})

    async def set_group_card(self, group_id, user_id, card=""):
        return await self.call("set_group_card", {"group_id": group_id, "user_id": user_id, "card": str(card)[:60]})

    async def set_group_name(self, group_id, group_name):
        return await self.call("set_group_name", {"group_id": group_id, "group_name": str(group_name)[:120]})

    async def set_group_special_title(self, group_id, user_id, title=""):
        return await self.call("set_group_special_title",
                               {"group_id": group_id, "user_id": user_id, "special_title": title})

    async def get_group_member_info(self, group_id, user_id, no_cache=False):
        params = {"group_id": group_id, "user_id": user_id}
        if no_cache:
            params["no_cache"] = True
        return await self.call("get_group_member_info", params)

    async def get_group_member_list(self, group_id):
        return await self.call("get_group_member_list", {"group_id": group_id})

    async def get_group_info(self, group_id):
        return await self.call("get_group_info", {"group_id": group_id})

    async def get_group_info_ex(self, group_id):
        return await self.call("get_group_info_ex", {"group_id": group_id})

    async def get_group_list(self):
        return await self.call("get_group_list", {})

    async def get_friend_list(self):
        return await self.call("get_friend_list", {})

    async def delete_msg(self, message_id):
        return await self.call("delete_msg", {"message_id": message_id})

    async def mark_msg_as_read(self, message_id):
        return await self.call("mark_msg_as_read", {"message_id": message_id})

    async def mark_group_msg_as_read(self, group_id, message_id=0):
        params = {"group_id": group_id}
        if message_id:
            params["message_id"] = message_id
        return await self.call("mark_group_msg_as_read", params)

    async def mark_all_as_read(self):
        return await self.call("_mark_all_as_read", {})

    async def set_msg_emoji_like(self, message_id, emoji_id):
        return await self.call("set_msg_emoji_like", {
            "message_id": message_id,
            "emoji_id": str(emoji_id),
        })

    async def get_image(self, file):
        try:
            result = await self.call("get_image", {"file": file})
            if result.get("status") == "ok":
                data = result.get("data", {})
                return data.get("file") or data.get("url")
        except Exception as error:
            log.debug("OneBot get_image failed: %s", error)
        return None

    async def set_group_leave(self, group_id, is_dismiss=False):
        return await self.call("set_group_leave", {
            "group_id": group_id,
            "is_dismiss": is_dismiss,
        })

    async def send_group_sign(self, group_id):
        """群打卡"""
        return await self.call("send_group_sign", {"group_id": group_id})

    async def get_group_honor_info(self, group_id, honor_type="all"):
        return await self.call("get_group_honor_info", {
            "group_id": group_id,
            "type": honor_type,
        })

    async def get_group_at_all_remain(self, group_id):
        return await self.call("get_group_at_all_remain", {"group_id": group_id})

    async def get_group_shut_list(self, group_id):
        return await self.call("get_group_shut_list", {"group_id": group_id})

    async def get_essence_msg_list(self, group_id):
        return await self.call("get_essence_msg_list", {"group_id": group_id})

    async def set_essence_msg(self, message_id):
        return await self.call("set_essence_msg", {"message_id": message_id})

    async def delete_essence_msg(self, message_id):
        return await self.call("delete_essence_msg", {"message_id": message_id})

    async def send_group_notice(self, group_id, content, image=""):
        params = {"group_id": group_id, "content": content}
        if image:
            params["image"] = image
        return await self.call("_send_group_notice", params)

    async def get_group_notice(self, group_id):
        return await self.call("_get_group_notice", {"group_id": group_id})

    async def del_group_notice(self, group_id, notice_id):
        return await self.call("_del_group_notice", {
            "group_id": group_id,
            "notice_id": notice_id,
        })

    async def get_record(self, file, out_format="mp3"):
        return await self.call("get_record", {"file": file, "out_format": out_format})

    async def get_file(self, file_id):
        return await self.call("get_file", {"file_id": file_id})

    async def ocr_image(self, image):
        return await self.call("ocr_image", {"image": image})

    async def ocr_image_enhanced(self, image):
        return await self.call(".ocr_image", {"image": image})

    async def get_forward_msg(self, message_id):
        return await self.call("get_forward_msg", {"message_id": message_id})

    async def send_group_forward_msg(self, group_id, messages):
        return await self.call("send_group_forward_msg", {
            "group_id": group_id,
            "messages": messages,
        }, timeout=max(self._api_timeout, self._forward_timeout))

    async def send_private_forward_msg(self, user_id, messages):
        return await self.call("send_private_forward_msg", {
            "user_id": user_id,
            "messages": messages,
        }, timeout=max(self._api_timeout, 60))

    async def forward_friend_single_msg(self, user_id, message_id):
        return await self.call("forward_friend_single_msg", {"user_id": user_id, "message_id": message_id})

    async def send_forward_msg(self, message_type=None, user_id=None, group_id=None, messages=None):
        params = {"messages": messages or []}
        if message_type:
            params["message_type"] = message_type
        if user_id:
            params["user_id"] = user_id
        if group_id:
            params["group_id"] = group_id
        return await self.call("send_forward_msg", params)

    async def upload_group_file(self, group_id, file, name, folder=""):
        return await self.call("upload_group_file", {
            "group_id": group_id,
            "file": file,
            "name": name,
            "folder": folder,
        })

    async def delete_group_file(self, group_id, file_id, busid):
        return await self.call("delete_group_file", {
            "group_id": group_id,
            "file_id": file_id,
            "busid": busid,
        })

    async def create_group_file_folder(self, group_id, name, parent_id="/"):
        return await self.call("create_group_file_folder", {
            "group_id": group_id,
            "name": name,
            "parent_id": parent_id,
        })

    async def delete_group_folder(self, group_id, folder_id):
        return await self.call("delete_group_folder", {
            "group_id": group_id,
            "folder_id": folder_id,
        })

    async def get_group_file_system_info(self, group_id):
        return await self.call("get_group_file_system_info", {"group_id": group_id})

    async def get_group_root_files(self, group_id):
        return await self.call("get_group_root_files", {"group_id": group_id})

    async def get_group_files_by_folder(self, group_id, folder_id):
        return await self.call("get_group_files_by_folder", {
            "group_id": group_id,
            "folder_id": folder_id,
        })

    async def get_group_file_url(self, group_id, file_id, busid):
        return await self.call("get_group_file_url", {
            "group_id": group_id,
            "file_id": file_id,
            "busid": busid,
        })

    async def move_group_file(self, group_id, file_id, current_parent_directory, target_parent_directory):
        return await self.call("move_group_file", {
            "group_id": group_id,
            "file_id": file_id,
            "current_parent_directory": current_parent_directory,
            "target_parent_directory": target_parent_directory,
        })

    async def trans_group_file(self, group_id, file_id, current_parent_directory, target_group_id, target_directory):
        return await self.call("trans_group_file", {
            "group_id": group_id,
            "file_id": file_id,
            "current_parent_directory": current_parent_directory,
            "target_group_id": target_group_id,
            "target_directory": target_directory,
        })

    async def rename_group_file(self, group_id, file_id, current_parent_directory, new_name):
        return await self.call("rename_group_file", {
            "group_id": group_id,
            "file_id": file_id,
            "current_parent_directory": current_parent_directory,
            "new_name": new_name,
        })

    async def upload_private_file(self, user_id, file, name):
        return await self.call("upload_private_file", {
            "user_id": user_id,
            "file": file,
            "name": name,
        })

    async def get_private_file_url(self, user_id, file_id):
        return await self.call("get_private_file_url", {
            "user_id": user_id,
            "file_id": file_id,
        })

    async def download_file(self, url, thread_count=2, headers=None):
        return await self.call("download_file", {
            "url": url,
            "thread_count": thread_count,
            "headers": headers or [],
        })

    async def set_group_add_request(self, flag, sub_type, approve=True, reason=""):
        return await self.call("set_group_add_request", {
            "flag": flag,
            "sub_type": sub_type,
            "approve": approve,
            "reason": reason,
        })

    async def set_friend_add_request(self, flag, approve=True, remark=""):
        return await self.call("set_friend_add_request", {
            "flag": flag,
            "approve": approve,
            "remark": remark,
        })

    async def get_ai_characters(self, group_id, chat_type=1):
        return await self.call("get_ai_characters", {
            "group_id": group_id,
            "chat_type": chat_type,
        })

    async def get_ai_record(self, group_id, character, text):
        return await self.call("get_ai_record", {
            "group_id": group_id,
            "character": character,
            "text": text,
        })

    async def send_group_ai_record(self, group_id, character, text):
        return await self.call("send_group_ai_record", {
            "group_id": group_id,
            "character": character,
            "text": text,
        })
    async def send_group_msg_reply(self, group_id, message, reply_to_msg_id):
        """Send a group message that replies to a specific message."""
        if isinstance(message, str):
            reply_seg = {"type": "reply", "data": {"id": str(reply_to_msg_id)}}
            text_seg = {"type": "text", "data": {"text": message}}
            full_message = [reply_seg, text_seg]
        else:
            reply_seg = {"type": "reply", "data": {"id": str(reply_to_msg_id)}}
            full_message = [reply_seg] + message
        return await self.call("send_group_msg", {"group_id": group_id, "message": full_message})

    async def send_group_msg_with_at(self, group_id, text, at_qqs):
        """Send a group message with @mentions."""
        segments = []
        for qq in at_qqs:
            segments.append({"type": "at", "data": {"qq": str(qq)}})
        segments.append({"type": "text", "data": {"text": " " + text}})
        return await self.call("send_group_msg", {"group_id": group_id, "message": segments})

    async def get_msg(self, message_id):
        """Get a specific message by ID."""
        return await self.call("get_msg", {"message_id": message_id})

    async def get_group_member_list_cached(self, group_id):
        """Get group member list with caching."""
        cache_key = f"_member_cache_{group_id}"
        if not hasattr(self, "_member_cache"):
            self._member_cache = {}
        cached = self._member_cache.get(cache_key)
        if cached and time.time() - cached.get("ts", 0) < 300:
            return cached["data"]
        result = await self.call("get_group_member_list", {"group_id": group_id})
        if result.get("status") == "ok":
            self._member_cache[cache_key] = {"data": result, "ts": time.time()}
        return result

    async def _get_message_history(self, action, target_key, target_id, count,
                                   message_seq, reverse_order, disable_get_url,
                                   parse_mult_msg, quick_reply):
        if message_seq is None and time.time() < getattr(
                self, "_history_api_unavailable_until", 0):
            return {
                "status": "failed", "retcode": 1200,
                "message": "当前 NapCat 版本不支持无游标历史查询",
                "data": {"messages": []},
            }
        params = {
            target_key: target_id,
            "count": max(1, min(int(count), 20)),
            "reverse_order": bool(reverse_order), "reverseOrder": bool(reverse_order),
            "disable_get_url": bool(disable_get_url),
            "parse_mult_msg": bool(parse_mult_msg), "quick_reply": bool(quick_reply),
        }
        if message_seq is not None:
            params["message_seq"] = int(message_seq)
        result = await self.call(action, params)
        error_text = str(result.get("message") or result.get("msg") or result.get("wording") or "")
        if (message_seq is None and result.get("status") != "ok"
                and "undefined" in error_text.lower() and "不存在" in error_text):
            self._history_api_unavailable_until = time.time() + 600
        return result

    async def get_group_msg_history(self, group_id, count=20, message_seq=None,
                                    reverse_order=False, disable_get_url=True,
                                    parse_mult_msg=False, quick_reply=False):
        return await self._get_message_history(
            "get_group_msg_history", "group_id", group_id, count, message_seq,
            reverse_order, disable_get_url, parse_mult_msg, quick_reply)

    async def get_friend_msg_history(self, user_id, message_seq=None, count=20,
                                     reverse_order=False, disable_get_url=True,
                                     parse_mult_msg=False, quick_reply=False):
        return await self._get_message_history(
            "get_friend_msg_history", "user_id", user_id, count, message_seq,
            reverse_order, disable_get_url, parse_mult_msg, quick_reply)

    async def get_recent_contact(self, count=10):
        return await self.call("get_recent_contact", {"count": max(1, min(int(count), 30))})

    async def get_friends_with_category(self):
        return await self.call("get_friends_with_category", {})

    async def get_robot_uin_range(self):
        return await self.call("get_robot_uin_range", {})

    async def mark_private_msg_as_read(self, user_id):
        return await self.call("mark_private_msg_as_read", {"user_id": user_id})

    async def set_group_sign(self, group_id):
        return await self.call("set_group_sign", {"group_id": group_id})

    async def send_poke(self, user_id, group_id=None):
        params = {"user_id": user_id}
        if group_id:
            params["group_id"] = group_id
        return await self.call("send_poke", params)

    async def set_online_status(self, status, ext_status=0, battery_status=0):
        return await self.call("set_online_status", {
            "status": status, "ext_status": ext_status, "battery_status": battery_status,
        })

    async def set_qq_avatar(self, file):
        return await self.call("set_qq_avatar", {"file": file})

    async def set_self_longnick(self, long_nick):
        return await self.call("set_self_longnick", {"longNick": str(long_nick)[:120]})

    async def fetch_custom_face(self, count=48):
        return await self.call("fetch_custom_face", {"count": max(1, min(int(count), 48))})

    async def create_collection(self, raw_data, brief):
        return await self.call("create_collection", {
            "rawData": str(raw_data), "brief": str(brief),
        })

    async def get_collection_list(self, category="0", count="20"):
        return await self.call("get_collection_list", {
            "category": str(category), "count": str(count),
        })

    async def ark_share_group(self, group_id):
        return await self.call("ArkShareGroup", {"group_id": group_id})

    async def ark_share_peer(self, user_id=None, group_id=None, phone_number=""):
        params = {}
        if user_id: params["user_id"] = user_id
        if group_id: params["group_id"] = group_id
        if phone_number: params["phoneNumber"] = phone_number
        return await self.call("ArkSharePeer", params)

    async def set_group_portrait(self, group_id, file, cache=1):
        return await self.call("set_group_portrait", {"group_id": group_id, "file": file, "cache": cache})

    async def get_group_system_msg(self):
        return await self.call("get_group_system_msg", {})

    async def friend_poke(self, user_id):
        return await self.call("friend_poke", {"user_id": user_id})

    async def translate_en2zh(self, text):
        """Native NapCat English-to-Chinese translation (free, no API cost)."""
        words = text if isinstance(text, list) else [str(text)]
        return await self.call("translate_en2zh", {"words": words})

    async def get_stranger_info(self, user_id, no_cache=False):
        return await self.call("get_stranger_info", {"user_id": user_id, "no_cache": no_cache})

    async def get_profile_like(self):
        return await self.call("get_profile_like", {})

    async def forward_group_single_msg(self, group_id, message_id):
        return await self.call("forward_group_single_msg", {
            "group_id": group_id, "message_id": str(message_id)
        })


    async def fetch_emoji_like(self, message_id, emoji_id, emoji_type="1", count=20, cookie=""):
        return await self.call("fetch_emoji_like", {"message_id": message_id, "emojiId": str(emoji_id), "emojiType": str(emoji_type), "count": int(count), "cookie": str(cookie)})

    async def get_emoji_likes(self, message_id, emoji_id, group_id=None, emoji_type="1", count=20):
        params = {"message_id": str(message_id), "emoji_id": str(emoji_id), "emoji_type": str(emoji_type), "count": int(count)}
        if group_id:
            params["group_id"] = str(group_id)
        return await self.call("get_emoji_likes", params)

    async def send_flash_msg(self, fileset_id, group_id=None, user_id=None):
        params = {"fileset_id": str(fileset_id)}
        if group_id:
            params["group_id"] = str(group_id)
        if user_id:
            params["user_id"] = str(user_id)
        return await self.call("send_flash_msg", params)

    async def _group_todo(self, action, group_id, message_id=None, message_seq=None):
        params = {"group_id": str(group_id)}
        if message_id is not None:
            params["message_id"] = str(message_id)
        if message_seq is not None:
            params["message_seq"] = str(message_seq)
        return await self.call(action, params)

    async def set_group_todo(self, group_id, message_id=None, message_seq=None):
        return await self._group_todo("set_group_todo", group_id, message_id, message_seq)

    async def cancel_group_todo(self, group_id, message_id=None, message_seq=None):
        return await self._group_todo("cancel_group_todo", group_id, message_id, message_seq)

    async def complete_group_todo(self, group_id, message_id=None, message_seq=None):
        return await self._group_todo("complete_group_todo", group_id, message_id, message_seq)

    async def get_group_album_list(self, group_id, attach_info=""):
        return await self.call("get_qun_album_list", {"group_id": str(group_id), "attach_info": str(attach_info)})

    async def get_group_album_media_list(self, group_id, album_id, attach_info=""):
        return await self.call("get_group_album_media_list", {"group_id": str(group_id), "album_id": str(album_id), "attach_info": str(attach_info)})

    async def upload_group_album_image(self, group_id, album_id, album_name, file):
        return await self.call("upload_image_to_qun_album", {"group_id": str(group_id), "album_id": str(album_id), "album_name": str(album_name), "file": str(file)}, timeout=max(self._api_timeout, 120))

    async def delete_group_album_media(self, group_id, album_id, lloc):
        return await self.call("del_group_album_media", {"group_id": str(group_id), "album_id": str(album_id), "lloc": str(lloc)})

    async def comment_group_album_media(self, group_id, album_id, lloc, content):
        return await self.call("do_group_album_comment", {"group_id": str(group_id), "album_id": str(album_id), "lloc": str(lloc), "content": str(content)})

    async def set_group_album_media_like(self, group_id, album_id, batch_id, lloc="", cancel=False):
        action = "cancel_group_album_media_like" if cancel else "set_group_album_media_like"
        return await self.call(action, {"group_id": str(group_id), "album_id": str(album_id), "batch_id": str(batch_id), "lloc": str(lloc)})

    async def get_group_detail_info(self, group_id):
        return await self.call("get_group_detail_info", {"group_id": str(group_id)})

    async def get_group_signed_list(self, group_id):
        return await self.call("get_group_signed_list", {"group_id": str(group_id)})

    async def get_online_file_msg(self, user_id):
        return await self.call("get_online_file_msg", {"user_id": str(user_id)})

    async def send_online_file(self, user_id, file_path, file_name=""):
        return await self.call("send_online_file", {"user_id": str(user_id), "file_path": str(file_path), "file_name": str(file_name)}, timeout=max(self._api_timeout, 120))

    async def send_online_folder(self, user_id, folder_path, folder_name=""):
        return await self.call("send_online_folder", {"user_id": str(user_id), "folder_path": str(folder_path), "folder_name": str(folder_name)}, timeout=max(self._api_timeout, 120))

    async def cancel_online_file(self, user_id, msg_id):
        return await self.call("cancel_online_file", {"user_id": str(user_id), "msg_id": str(msg_id)})

    async def receive_online_file(self, user_id, msg_id, element_id, approve=True):
        action = "receive_online_file" if approve else "refuse_online_file"
        return await self.call(action, {"user_id": str(user_id), "msg_id": str(msg_id), "element_id": str(element_id)})

    async def set_friend_remark(self, user_id, remark):
        return await self.call("set_friend_remark", {"user_id": str(user_id), "remark": str(remark)})

    async def delete_friend(self, user_id, block=False, both=False):
        return await self.call("delete_friend", {"user_id": str(user_id), "temp_block": bool(block), "temp_both_del": bool(both)})

    async def get_unidirectional_friend_list(self):
        return await self.call("get_unidirectional_friend_list", {})

    async def set_qq_profile(self, nickname, personal_note="", sex=0):
        return await self.call("set_qq_profile", {"nickname": str(nickname), "personal_note": str(personal_note), "sex": int(sex)})

    async def set_diy_online_status(self, face_id, face_type, wording):
        return await self.call("set_diy_online_status", {"face_id": int(face_id), "face_type": int(face_type), "wording": str(wording)})

    async def add_custom_face(self, file):
        return await self.call("add_custom_face", {"file": str(file)}, timeout=max(self._api_timeout, 60))

    async def delete_custom_face(self, res_id="", emoji_id="", md5=""):
        params = {}
        if res_id:
            params["res_id"] = str(res_id)
        if emoji_id:
            params["id"] = str(emoji_id)
        if md5:
            params["md5"] = str(md5)
        return await self.call("delete_custom_face", params)

    async def set_custom_face_desc(self, emoji_id, res_id, md5, desc):
        return await self.call("set_custom_face_desc", {"emoji_id": str(emoji_id), "res_id": str(res_id), "md5": str(md5), "desc": str(desc)})

    async def fetch_custom_face_detail(self, count=20):
        return await self.call("fetch_custom_face_detail", {"count": int(count)})

    async def set_group_remark(self, group_id, remark):
        return await self.call("set_group_remark", {"group_id": str(group_id), "remark": str(remark)})

    async def set_group_add_option(self, group_id, add_type, question="", answer=""):
        return await self.call("set_group_add_option", {"group_id": str(group_id), "add_type": int(add_type), "group_question": str(question), "group_answer": str(answer)})

    async def set_group_robot_add_option(self, group_id, enabled, examine=True):
        return await self.call("set_group_robot_add_option", {"group_id": str(group_id), "robot_member_switch": int(bool(enabled)), "robot_member_examine": int(bool(examine))})

    async def set_group_kick_members(self, group_id, user_ids, reject_add=False):
        return await self.call("set_group_kick_members", {"group_id": str(group_id), "user_id": [str(uid) for uid in list(user_ids)[:20]], "reject_add_request": bool(reject_add)})

    async def get_group_ignored_notifies(self):
        return await self.call("get_group_ignored_notifies", {})

    async def get_group_ignore_add_request(self):
        return await self.call("get_group_ignore_add_request", {})

    async def get_doubt_friends_add_request(self, count=20):
        return await self.call("get_doubt_friends_add_request", {"count": int(count)})

    async def set_doubt_friends_add_request(self, flag, approve=True):
        return await self.call("set_doubt_friends_add_request", {"flag": str(flag), "approve": bool(approve)})

    async def fetch_ptt_text(self, message_id):
        return await self.call("fetch_ptt_text", {"message_id": message_id})

    @property
    def session(self):
        return self._session
